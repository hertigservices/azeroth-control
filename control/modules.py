# -*- coding: utf-8 -*-
"""Three states per module, per realm: on, paused, out.

The launcher could already edit a module's settings but never its existence, so
"try the realm without AutoBalance for a week" meant hand-editing a config, or
moving a directory out of src\\modules - which is exactly the operation that trips
the configure race in TROUBLESHOOTING.md ("CMake Error at modules/CMakeLists.txt").
This module is the ledger that makes the question answerable from the panel.

  on      built in, Enable = 1                     free
  paused  built in, Enable = 0                     see APPLY - a command, or a restart
  out     MODULE_<DIR>=disabled at configure time  rebuild + install

FOUR THINGS THAT MAKE THIS HONEST, each learned the hard way:

  1. THE CMAKE VARIABLE IS GENERATED, NEVER TYPED. AzerothCore names it
     'MODULE_' + TOUPPER(directory), so the HYPHENS SURVIVE:

         MODULE_MOD-DUNGEON-CLEAR=disabled     <- keeps the module out of the build
         MODULE_MOD_DUNGEON_CLEAR=disabled     <- an unused cache entry; a no-op

     RealmMaster's .env uses the underscore spelling. Copied here it would appear
     to work and do nothing, so nothing in this file lets a human write one.

  2. "DOES A RELOAD APPLY IT?" IS A QUESTION ABOUT THE KEY, NOT THE MODULE.
     Whether a module implements OnAfterConfigLoad is the WRONG test, and it is
     wrong in both directions - see APPLY below, which answers it per module from
     the site where the key is actually read.

  3. "PAUSED" IS A PAUSE, NOT AN UNINSTALL. Re-reading the config does not undo
     work a module has already done: IndividualProgression does not un-gate a
     character it already gated, AutoBalance does not un-scale creatures already
     spawned, and the bots playerbots created stay logged in. James's call
     (2026-09-08): offer 'paused' for those anyway, behind a confirm that names
     what does NOT get undone. See PAUSE_CAVEAT - the caller must not apply a
     caveated pause without `confirm`.

  4. "OUT" NEVER DELETES ANYTHING. It records an intent in this realm's ledger.
     rebuild.ps1 reads it, passes the -D flags, and reconfigures when they differ
     from the cache. Source, git repo and the realm/custom branch are untouched,
     so 'out' is reversible by flipping the same switch back.

NOT OFFERED: dynamic (DLL) modules. CMake accepts the value and the failure is
silent - nothing in this tree loads the DLLs (no ScriptReloadMgr, no boost::dll
scan of server\\scripts), and mod-playerbots is force-linked static anyway. The
honest menu is static or disabled. Do not add a third.

Scope: the ledger and the config write. Nothing here starts, stops or restarts a
world - sending the apply command to a running realm is the caller's job, and
restarting one is James's.
"""

import json
import os
import re
import time

C = None
R = None

LEDGER_SCHEMA = 1

# Compilers that mean "a build is in flight". Toggling under one of these is how
# you get a half-configured tree: module-sync.ps1 refuses to merge for the same
# reason, and this refuses for the same reason.
BUILD_IMAGES = ('cl', 'MSBuild', 'link')

# Prettier than mod-ah-bot -> "Ah Bot". Anything absent falls through to title
# case, so a module added tomorrow renders sensibly without an edit here.
LABELS = {
    'mod-ah-bot': 'AH Bot',
    'mod-aoe-loot': 'AoE Loot',
    'mod-dk-talents': 'DK Talents',
    'mod-solo-lfg': 'Solo LFG',
    'mod-ale': 'ALE (Lua engine)',
    'mod-ollama-chat': 'Ollama Chat',
    'mod-individual-progression': 'Individual Progression',
    'mod-no-bot-achievements': 'No Bot Achievements',
}

# HOW A PAUSE REACHES A RUNNING WORLD, verified per module 2026-09-08 by reading
# the site where the master switch is used. This table exists because the obvious
# test - "does the module implement OnAfterConfigLoad?" - is wrong BOTH ways:
#
#   mod-junk-to-gold has no OnAfterConfigLoad and IS live on a reload, because it
#     reads the key inside OnPlayerLootItem and ConfigMgr::Reload() has already
#     refreshed the config map by the time that hook next fires;
#   mod-dungeon-clear HAS OnAfterConfigLoad and still needs a restart, because its
#     master switch is latched on the first world tick - the module says so itself
#     (DcModule::WarnIfConfDiffersFromLatch).
#
# `.reload config` runs World::LoadConfigSettings(true), which fires BOTH
# OnBeforeConfigLoad and OnAfterConfigLoad (World.cpp:185 and :301) - so a key
# read from either hook, or read live inside a gameplay hook, is covered.
#
# Value: (command to send a running world, evidence). None = only a restart will
# do. A module absent from this table defaults to None: understating what a
# toggle can do is recoverable, claiming a live apply that never happened is not.
APPLY = {
    'mod-aoe-loot':
        ('.reload config', 'aoe_loot.cpp:291 - read inline in CanPacketReceive'),
    'mod-autobalance':
        ('.reload config', 'ABWorldScript.cpp:102 in SetInitialWorldSettings, '
                           'called from OnBeforeConfigLoad'),
    'mod-individual-progression':
        ('.reload config', 'IndividualProgression.cpp:1067 in LoadConfig, called '
                           'from OnBeforeConfigLoad:1151'),
    'mod-junk-to-gold':
        ('.reload config', 'mod_junk_to_gold.cpp:33 - read inline in OnPlayerLootItem'),
    'mod-no-bot-achievements':
        ('.reload config', 'no_bot_achievements.cpp:55 - read inline in '
                           'OnPlayerBeforeCriteriaProgress'),
    'mod-raid-roster':
        ('.reload config', 'RaidRosterConfig.cpp:9 in RaidRosterLoadConfig, called '
                           'from RaidRosterLoader.cpp:10 OnAfterConfigLoad'),
    'mod-shared-quest-loot':
        ('.reload config', 'SharedQuestLoot.cpp:74 - read in OnAfterConfigLoad'),
    'mod-solo-lfg':
        ('.reload config', 'Lfg_Solo.cpp:33 - read inline in '
                           'OnPlayerRewardKillRewarder'),
    'mod-spelldraft':
        ('.reload config', 'SpellDraft.cpp:225 - read inline in '
                           'OnPlayerAfterUpdateMaxPower'),

    # Its own command, not the generic one: HandleOllamaReloadCommand calls
    # sConfigMgr->Reload() AND LoadOllamaChatConfig(), which is the only thing
    # that re-reads OllamaChat.* - a plain .reload config refreshes the map and
    # the module keeps using the values it cached at OnStartup.
    'mod-ollama-chat':
        ('.ollama reload', 'mod-ollama-chat_config.cpp:774 - LoadOllamaChatConfig '
                           'runs from OnStartup only; the module ships '
                           '`.ollama reload` (Console::Yes) to re-read it'),

    # Restart-only, and each for a different reason worth keeping:
    'mod-playerbots':
        (None, 'PlayerbotAIConfig.cpp:83 - AiPlayerbot.Enabled is read in '
               'Initialize(), which runs once at startup'),
    'mod-dungeon-clear':
        (None, 'DcModuleEnable.cpp - latched by LatchFromConf on the first world '
               'tick; OnAfterConfigLoad only warns that conf and latch disagree'),
    'mod-ale':
        (None, 'ALEConfig.cpp:20 BuildConfigCache - an engine-init override with '
               'no config-hook caller found; treated as restart-only'),
}

# Keys the derivation provably cannot find, because the module composes the string
# at runtime instead of writing it as a literal. Only add a row here with the
# evidence, never to save a lookup.
KEY_OVERRIDE = {
    # DcSettings::GetBool(guid, "Enable") builds "DungeonClear." + "Enable", so
    # the full key never appears as a literal anywhere in the module.
    'mod-dungeon-clear': ('DungeonClear.Enable', 'mod_dungeon_clear.conf'),
}

# Modules where Enable = 0 changes behaviour from now on but leaves the world in
# the state the module put it in. The text is shown in a confirm before the write,
# not in a tooltip after it.
PAUSE_CAVEAT = {
    'mod-playerbots':
        'Pausing playerbots stops NEW bots being added and stops the AI acting. '
        'Bots already logged in stay in the world until the realm restarts, and '
        'every bot character, its gear and its guild membership remain in the '
        'playerbots database. This is a quiet switch, not an uninstall.',
    'mod-individual-progression':
        'Pausing Individual Progression lifts the gates from now on. It does NOT '
        'un-gate a character it has already gated, does not roll back a completed '
        'progression phase, and does not restore vanilla scaling to mobs already '
        'spawned. Characters mid-progression can immediately reach content the '
        'phase was holding back.',
    'mod-autobalance':
        'Pausing AutoBalance stops NEW creatures being scaled. Creatures already '
        'spawned in a running instance keep the scaling they were given until that '
        'instance resets.',
}


def bind(control_module, realms_module):
    """Same contract as realms.bind() and status.bind() - the caller owns them."""
    global C, R
    C = control_module
    R = realms_module


# ------------------------------------------------------------------ trees

def src_dir(realm):
    """This realm's AzerothCore source tree, or None.

    Declared as paths.src in profiles.json. A realm that declares none runs
    binaries this hub does not build - the Ascension archive world is a different
    worldserver.exe with no source here - and gets Tier 1 (Enable keys) only,
    rather than being offered a rebuild that would build somebody else's realm.
    """
    return R.hub((realm.get('paths') or {}).get('src'))


def build_dir(realm):
    return R.hub((realm.get('paths') or {}).get('build'))


def cmake_var(dirname):
    """MODULE_MOD-AH-BOT. Hyphens deliberately preserved - see rule 1 up top."""
    return 'MODULE_' + dirname.upper()


def label_for(dirname):
    if dirname in LABELS:
        return LABELS[dirname]
    return dirname[4:].replace('-', ' ').title() if dirname.startswith('mod-') \
        else dirname


# ------------------------------------------------------- discovery (static)

# A master switch, as opposed to one of the dozen per-feature Enables a module may
# also carry. AutoBalance.Enable.Global is the real global gate (its source reads
# AutoBalance.enable only as a backwards-compatible fallback), so .Global is
# allowed as a suffix and nothing else is: AutoBalance.Enable.5M is a feature flag,
# and turning it off would look like a pause that did almost nothing.
_ENABLE = re.compile(
    r'^\s*([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*?\.Enabled?(?:\.Global)?)\s*=\s*(\S+)', re.M)


def _conf_dists(mdir):
    out = []
    for root, dirs, files in os.walk(mdir):
        dirs[:] = [d for d in dirs if d != '.git']
        out += [os.path.join(root, f) for f in files if f.endswith('.conf.dist')]
    return sorted(out)


def _read(p):
    try:
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except Exception:
        return ''


def _source_blob(mdir):
    """Every C++ file under the module, concatenated. Used to prove a key is read."""
    parts = []
    for root, dirs, files in os.walk(os.path.join(mdir, 'src')):
        dirs[:] = [d for d in dirs if d != '.git']
        for f in files:
            if os.path.splitext(f)[1].lower() in ('.cpp', '.h', '.hpp'):
                parts.append(_read(os.path.join(root, f)))
    return '\n'.join(parts)


def _derive_key(mdir, dists, blob):
    """(conf basename, key, on, off) or (None, None, None, None).

    The enable key is only accepted if the module's own C++ actually reads it. A
    key that exists in the .conf.dist and is read by nothing is a documentation
    leftover, and a toggle wired to one would report success and change nothing -
    which is worse than saying the module has no switch.
    """
    for d in dists:
        txt = _read(d)
        cands = [(k, v) for k, v in _ENABLE.findall(txt) if ('"%s"' % k) in blob]
        if not cands:
            continue
        # .Global outranks a bare .Enable when a module ships both.
        cands.sort(key=lambda kv: (0 if kv[0].endswith('.Global') else 1, len(kv[0])))
        key, val = cands[0]
        # mod-ale writes true/false, everyone else 1/0. Match the file's own style
        # rather than normalising: a module is free to parse only what it ships.
        on, off = ('true', 'false') if val.lower() in ('true', 'false') else ('1', '0')
        return os.path.basename(d)[:-len('.dist')], key, on, off
    return None, None, None, None


def _discover_one(mdir, dirname):
    """Static facts about one module, read off disk plus the two verified tables."""
    dists = _conf_dists(mdir)
    blob = _source_blob(mdir)
    conf, key, on, off = _derive_key(mdir, dists, blob)

    if dirname in KEY_OVERRIDE:
        key, conf = KEY_OVERRIDE[dirname]
        on, off = on or '1', off or '0'

    cmd, why = APPLY.get(dirname, (None, 'not verified - assumed restart-only'))
    return {
        'dir': dirname,
        'label': label_for(dirname),
        'cmakeVar': cmake_var(dirname),
        'conf': conf,
        'enableKey': key,
        'enableOn': on,
        'enableOff': off,
        'applyCmd': cmd,                 # None = restart-only
        'applyWhy': why,
        'build': 'static',               # intent; preserved across a re-sync
    }


def discover(realm):
    src = src_dir(realm)
    if not src:
        return []
    mods = os.path.join(src, 'modules')
    if not os.path.isdir(mods):
        return []
    out = []
    for d in sorted(os.listdir(mods)):
        p = os.path.join(mods, d)
        if d.startswith('mod-') and os.path.isdir(p):
            out.append(_discover_one(p, d))
    return out


# ------------------------------------------------------------------ ledger

def ledger_path(slug):
    return os.path.join(C.ROOT, 'realms', slug, 'modules.json')


def load_ledger(slug):
    try:
        with open(ledger_path(slug), 'r', encoding='utf-8') as f:
            doc = json.load(f)
        if isinstance(doc, dict) and doc.get('schema') == LEDGER_SCHEMA:
            return doc
    except Exception:
        pass
    return None


def save_ledger(slug, doc):
    p = ledger_path(slug)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write('\n')
    os.replace(tmp, p)          # a half-written ledger would strand the rebuild


def sync(realm, force=False):
    """Rebuild the static half of the ledger from disk, keeping the intent half.

    Called on every read. The facts (conf file, enable key, how it applies) are
    cheap to re-derive and go stale the moment a module is updated from upstream,
    so they are never trusted from the file - only `build`, the one field a human
    sets, survives.
    """
    slug = realm.get('slug')
    old = load_ledger(slug) or {}
    intent = dict((m.get('dir'), m.get('build'))
                  for m in old.get('modules', []) if m.get('dir'))
    mods = discover(realm)
    for m in mods:
        if intent.get(m['dir']) in ('static', 'disabled'):
            m['build'] = intent[m['dir']]
    doc = {
        'schema': LEDGER_SCHEMA,
        '_contract': ('Per-realm module ledger. `build` is INTENT for the next '
                      'configure (static|disabled) and is the only field a human '
                      'sets; everything else is re-derived from the source tree on '
                      'every read. tools/rebuild.ps1 reads this file. See '
                      'control/modules.py for why the CMake variable keeps its '
                      'hyphens, and for how `applyCmd` was established.'),
        'realm': slug,
        'generated': time.strftime('%Y-%m-%d %H:%M:%S'),
        'srcDir': src_dir(realm),
        'buildDir': build_dir(realm),
        'modules': mods,
    }
    if force or old.get('modules') != mods:
        try:
            save_ledger(slug, doc)
        except Exception:
            pass                # a read-only hub still gets a working view
    return doc


# -------------------------------------------------------------- live state

def cache_values(bdir):
    """{MODULE_MOD-X: value} from a build tree's CMakeCache.txt, or {} if none.

    {} means "not configured yet", which is not the same as "everything is in" -
    callers report it as unknown rather than inventing a state.
    """
    if not bdir:
        return {}
    txt = _read(os.path.join(bdir, 'CMakeCache.txt'))
    return dict(re.findall(r'^(MODULE_MOD-[A-Z0-9-]+):STRING=(\S*)$', txt, re.M))


def build_running():
    """(True, 'cl.exe x3') while a compile is in flight, else (False, '')."""
    try:
        found = C.procs_many(list(BUILD_IMAGES))
    except Exception:
        return False, ''
    busy = ['%s.exe x%d' % (n, len(v)) for n, v in found.items() if v]
    return bool(busy), ', '.join(busy)


def _truthy(v):
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def view(realm, modconf, world_ready):
    """Everything the panel renders for one realm. Read fresh, never cached.

    A module is reported against THIS realm's own config directory and THIS
    realm's own build tree. The vanilla and SpellDraft trees share module names
    (both run playerbots and junk-to-gold) and share nothing else, so a view
    assembled from the hub's own paths would show one realm's state on the
    other's page - the exact bug panel.py exists to have fixed.
    """
    doc = sync(realm)
    cache = cache_values(doc.get('buildDir'))
    configured = bool(cache)
    busy, busy_what = build_running()
    out = []
    for m in doc['modules']:
        cur = cache.get(m['cmakeVar'])
        # 'default' resolves to whatever -DMODULES= passed, and every configure
        # here passes static. Unknown is treated as built-in rather than out: a
        # module the cache has never heard of is one configure away from being
        # compiled in, and reporting it as 'out' would invite a pointless rebuild.
        built = None if cur is None else (cur != 'disabled')
        conf_path = os.path.join(modconf, m['conf']) if m['conf'] else None
        has_conf = bool(conf_path and os.path.isfile(conf_path))
        val = C.conf_get(conf_path, m['enableKey']) if (has_conf and m['enableKey']) else None
        enabled = None if val is None else _truthy(val)

        if built is False:
            state = 'out'
        elif enabled is False:
            state = 'paused'
        else:
            state = 'on'

        pending = None
        if configured and cur is not None:
            want_dis = m['build'] == 'disabled'
            if want_dis and cur != 'disabled':
                pending = 'out'          # queued to leave the build
            elif not want_dis and cur == 'disabled':
                pending = 'in'           # queued to come back

        out.append({
            'dir': m['dir'], 'label': m['label'], 'cmakeVar': m['cmakeVar'],
            'conf': m['conf'], 'confExists': has_conf,
            'enableKey': m['enableKey'], 'enableValue': val,
            'state': state, 'built': built, 'enabled': enabled,
            'buildIntent': m['build'], 'buildActual': cur, 'pending': pending,
            'canPause': bool(m['enableKey'] and has_conf),
            'caveat': PAUSE_CAVEAT.get(m['dir']),
            # What a toggle will COST, said before the click rather than after.
            'applyCmd': m['applyCmd'],
            'applyWhy': m['applyWhy'],
            'applyPause': (m['applyCmd'] if (m['applyCmd'] and world_ready)
                           else ('world restart' if not m['applyCmd']
                                 else m['applyCmd'])),
        })
    return {
        'modules': out,
        'srcDir': doc.get('srcDir'), 'buildDir': doc.get('buildDir'),
        'ledger': ledger_path(realm.get('slug')),
        'buildControl': bool(doc.get('buildDir')),
        'configured': configured,
        'worldReady': world_ready,
        'buildBusy': busy, 'buildBusyWhat': busy_what,
        'pendingCount': len([m for m in out if m['pending']]),
    }


# ------------------------------------------------------------------ writes

def find(realm, dirname):
    return next((m for m in sync(realm)['modules'] if m['dir'] == dirname), None)


def set_enabled(realm, modconf, dirname, on):
    """Tier 1: write the module's own Enable key. (ok, [log lines])."""
    m = find(realm, dirname)
    if not m:
        return False, ['unknown module: %s' % dirname]
    if not m['enableKey']:
        return False, ['%s has no master Enable key - it can only be built out.'
                       % m['label']]
    p = os.path.join(modconf, m['conf'])
    if not os.path.isfile(p):
        return False, ["%s is not in this realm's module config directory." % m['conf']]
    val = m['enableOn'] if on else m['enableOff']
    if not C.conf_set(p, m['enableKey'], val):
        # conf_set only rewrites a line that is already there. A commented-out key
        # is left alone on purpose: appending one would change which value the
        # module reads without anyone seeing the file change.
        return False, ['%s is not present (or is commented out) in %s - edit the '
                       'file by hand.' % (m['enableKey'], m['conf'])]
    return True, ['OK  %s = %s  (%s)' % (m['enableKey'], val, m['conf'])]


def set_build(realm, dirname, disabled):
    """Tier 2: record the intent. Applying it is the next rebuild's job."""
    slug = realm.get('slug')
    doc = sync(realm)
    if not doc.get('buildDir'):
        return False, ['%s does not declare a build tree in profiles.json, so this '
                       'hub does not compile its worldserver.'
                       % realm.get('name', slug)]
    busy, what = build_running()
    if busy:
        return False, ['A build is running (%s). Toggling now would leave the tree '
                       'half-configured - wait for it to finish.' % what]
    hit = False
    for m in doc['modules']:
        if m['dir'] == dirname:
            m['build'] = 'disabled' if disabled else 'static'
            hit = True
    if not hit:
        return False, ['unknown module: %s' % dirname]
    save_ledger(slug, doc)
    return True, ['OK  %s = %s (queued)' % (cmake_var(dirname),
                                            'disabled' if disabled else 'static')]


def flags(slug):
    """The -D arguments for this realm's next configure. Used by the CLI and by
    tools/rebuild.ps1, which reads the same ledger directly."""
    doc = load_ledger(slug) or {}
    return ['-D%s=%s' % (m['cmakeVar'], m['build'])
            for m in doc.get('modules', []) if m.get('build') == 'disabled']
