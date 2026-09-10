"""Launcher backend for Azeroth Control.

A thin /v1 REST surface over the primitives already in control.py, modelled on
the local-daemon pattern game launchers use.  It lives in its own module so
control.py only gains a short delegation block.

Two ideas from that pattern, adapted to the stdlib:

  * progress as a subscribable stream.  Python's stdlib http.server has no
    websocket, so /v1/patch/{slug}/state/stream is Server-Sent Events over the
    same connection model.  Same push semantics, no dependency.
  * modules/add|remove|replace.  'replace' is the piece the old panel lacked:
    enable and disable an addon without deleting it, by sidelining the folder to
    Interface/AddOns.disabled and moving it back on demand.

Everything here is read-mostly.  The only writes are: the addon folders under a
profile's own AddOns directory, the "<Folder>: enabled" line a load-on-demand
companion module needs in that client's WTF AddOns.txt (see _autotick),
realms/profiles.json's activeRealm field, and whatever control.py's start/stop
already did.
"""

import glob
import json
import os
import re
import shutil
import threading
import time
import uuid

import panel as P       # realm-scoped versions of the old control panel's tabs
import modules as M
import status as S
import realms as R      # registry IO + per-profile start/stop; shared with the CLI

# control.py runs as __main__, so `import control` here would build a SECOND
# module object with its own globals - two ADDONS_DIRs, two job tables, two of
# everything.  control.py calls bind() with itself instead.
C = None
ROOT = REALMS_DIR = PROFILES_FILE = CHANGELOG_DIR = None

# The launcher owns this side of the split; ui.html keeps serving the old panel.
LAUNCHER_PAGE = 'launcher.html'


def bind(control_module):
    global C, ROOT, REALMS_DIR, PROFILES_FILE, CHANGELOG_DIR
    C = control_module
    ROOT = C.ROOT
    REALMS_DIR = os.path.join(ROOT, 'realms')
    PROFILES_FILE = os.path.join(REALMS_DIR, 'profiles.json')
    CHANGELOG_DIR = os.path.join(REALMS_DIR, 'changelog')
    R.bind(control_module)
    P.bind(control_module)
    S.bind(control_module, R)
    M.bind(control_module, R)
    _bind_parked_remove(control_module)


def _bind_parked_remove(C_mod):
    """Teach control.addon_remove() to sweep the parked copy as well.

    Wrapped rather than reimplemented so the ledger write, the path check and
    the return contract all stay in control.py where they belong."""
    if getattr(C_mod.addon_remove, '_parked_aware', False):
        return
    inner = C_mod.addon_remove

    def addon_remove(name, dest=None):
        folders = (C_mod._ledger().get(name) or {}).get('folders', [])
        ok, log = inner(name, dest)
        park = (dest or C_mod.ADDONS_DIR) + '.disabled'
        swept = []
        for f in folders:
            p = os.path.join(park, f)
            # Never delete outside the parked tree, whatever the ledger says.
            if os.path.abspath(p).startswith(os.path.abspath(park)) and os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
                swept.append(f)
        if swept:
            log.append('also removed from AddOns.disabled: %s' % ', '.join(swept))
        return ok, log

    addon_remove._parked_aware = True
    C_mod.addon_remove = addon_remove


# ---------------------------------------------------------------- profiles

def _hub(rel):
    """Resolve a profiles.json path, which may be hub-relative or absolute."""
    if not rel:
        return None
    return rel if os.path.isabs(rel) else os.path.normpath(os.path.join(ROOT, rel))


# The registry has two writers - this panel and tools/switch-realm.py - so both
# go through realms.py's single atomic writer rather than each rolling its own.
def profiles_load():
    return R.load()


def profiles_save(doc):
    R.save(doc)


def _find(doc, slug):
    return R.find(doc, slug)


def _installed(r):
    """On-disk truth beats the registry's own optimism."""
    p = r.get('paths', {})
    client = _hub(p.get('client'))
    server = _hub(p.get('server'))
    if r.get('kind') == 'client':
        root = _hub(p.get('root') or p.get('client'))
        return bool(root and os.path.isdir(root))
    return bool((client and os.path.isdir(client)) or (server and os.path.isdir(server)))


def _launch_target(r):
    t = r.get('launch', {}).get('target')
    return _hub(t) if t else None


# ---------------------------------------------------------------- products

def products():
    """What you can click, and whether the thing behind it is up.

    Both halves of that used to be answered from `activeRealm`: a realm was "up" when
    a process called worldserver.exe existed AND the registry said this realm was the
    active one. That is two guesses stacked - it showed a crashed realm as running
    while the registry still pointed at it, and a hand-started one as stopped. The
    snapshot answers it by image path instead, per realm, so `serverUp` here is an
    observation.
    """
    doc = profiles_load()
    snap = S.snapshot(include_population=False)
    by_slug = dict((r['slug'], r) for r in snap['realms'])
    active = doc.get('activeRealm')
    run = by_slug.get(snap['runningRealm'] or '')
    world = P._proc(run['processes']['worldserver']) if run else {'up': False}
    auth = P._proc(run['processes']['authserver']) if run else {'up': False}
    ready = bool(run['ready']) if run else False
    # Some profiles have no authserver.exe AT ALL - Ascension serves auth from a
    # helper script on its own port (3799), so `auth['up']` is False while login
    # works perfectly. status.py already computes this; forwarding it is what
    # stops the UI drawing a red light for a healthy realm. Helper liveness comes
    # from the snapshot's port scan, which keys each helper by its profile name.
    uses_auth = bool(run.get('usesAuthserver', True)) if run else True
    helper_rows = []
    if run:
        rprof = _find(doc, run['slug'])
        rports = run.get('ports') or {}
        for h in (R.helpers(rprof) if rprof else []):
            hname = h.get('name') or 'helper'
            helper_rows.append({
                'name': hname,
                'label': h.get('label') or hname,
                'port': h.get('port'),
                'up': bool((rports.get(hname) or {}).get('open')),
            })

    out = []
    for r in sorted(doc.get('realms', []), key=lambda x: x.get('order', 99)):
        inst = _installed(r)
        tgt = _launch_target(r)
        shares = r.get('sharesRealm')
        managed = r.get('controls', {}).get('serverManaged', False)
        # A realm is up when ITS OWN world is up. A client-kind product is up when the
        # realm it shares is - named explicitly, so a client bound to SpellDraft no
        # longer lights up because vanilla happens to be running.
        me = by_slug.get(r['slug']) if managed else by_slug.get(shares or '')
        up = bool(me and me['running'])
        out.append({
            'slug': r['slug'],
            'name': r.get('name', r['slug']),
            'tagline': r.get('tagline', ''),
            'kind': r.get('kind', 'realm'),
            'accent': r.get('accent', '#c9aa71'),
            'status': r.get('status', 'planned'),
            'enabled': bool(r.get('enabled')),
            'installed': inst,
            'active': active == r['slug'],
            'builder': r.get('builder', {}),
            'serverManaged': managed,
            'sharesRealm': shares,
            'playable': bool(inst and tgt and os.path.exists(tgt)),
            'launchTarget': tgt,
            'serverUp': up,
            'ready': bool(me['ready']) if me else False,
            'blockedBy': me['blockedBy'] if me else None,
            'notes': r.get('notes', ''),
            'requires': r.get('requires', []),
        })
    return {
        'hub': ROOT,
        'activeRealm': active,
        # What the machine shows, beside what the registry claims. When they differ,
        # `conflicts` says so in a sentence rather than one of them winning silently.
        'runningRealm': snap['runningRealm'],
        'conflicts': snap['conflicts'],
        'schema': doc.get('schema', 0),
        'error': doc.get('error'),
        'worldserver': world,
        'authserver': auth,
        # False => this profile has no authserver.exe and `authserver` above is
        # meaningless for it; render `helpers` instead of an auth light.
        'usesAuthserver': uses_auth,
        'helpers': helper_rows,
        'ready': ready,
        'products': out,
    }


def product(slug):
    doc = profiles_load()
    r = _find(doc, slug)
    if not r:
        return None
    d = next((p for p in products()['products'] if p['slug'] == slug), {})
    paths = {k: _hub(v) for k, v in (r.get('paths') or {}).items()}
    d['paths'] = paths
    d['pathsExist'] = {k: bool(v and os.path.exists(v)) for k, v in paths.items()}
    d['db'] = r.get('db', {})
    d['launch'] = r.get('launch', {})
    return d


def product_settings(slug):
    """Small, display-shaped settings summary for the Play view's cards.

    The editable knobs live in panel.settings(); this stays the flat key/value
    digest the hero cards read.  It used to mirror control.SETTABLE only when the
    profile's config directory WAS vanilla's, because those paths are bound to
    vanilla at import time - so every other realm showed "no live configs" even
    with a full worldserver.conf of its own.  Now each value is read from the
    profile's own files, and a key whose file the realm does not have is simply
    absent rather than reported as vanilla's value.
    """
    doc = profiles_load()
    r = _find(doc, slug)
    if not r:
        return None
    conf_dir = _hub(r.get('paths', {}).get('configs'))
    out = {'slug': slug, 'configDir': conf_dir,
           'configDirExists': bool(conf_dir and os.path.isdir(conf_dir)),
           'settings': {}, 'phase': None, 'builder': r.get('builder', {})}
    if not out['configDirExists']:
        return out
    st = P.settings(slug) or {}
    out['phase'] = st.get('phase')
    out['phases'] = st.get('phases') or []
    out['settable'] = [i['key'] for i in st.get('items', [])]
    mod = st.get('modConfigDir') or os.path.join(conf_dir, 'modules')

    def g(fn, key):
        base = conf_dir if fn == 'worldserver.conf' else mod
        p = os.path.join(base, fn)
        return C.conf_get(p, key) if os.path.isfile(p) else None

    pairs = (
        ('PlayerSaveInterval',   'worldserver.conf',           'PlayerSaveInterval'),
        ('MapUpdate.Threads',    'worldserver.conf',           'MapUpdate.Threads'),
        ('MinRandomBots',        'playerbots.conf',            'AiPlayerbot.MinRandomBots'),
        ('MaxRandomBots',        'playerbots.conf',            'AiPlayerbot.MaxRandomBots'),
        ('RandomBotMaxLevel',    'playerbots.conf',            'AiPlayerbot.RandomBotMaxLevel'),
        ('EnableGuildTasks',     'playerbots.conf',            'AiPlayerbot.EnableGuildTasks'),
        ('BotAccountsMaxLevel',  'individualProgression.conf',
         'IndividualProgression.BotAccountsMaxLevel'),
        ('OllamaModel',          'mod_ollama_chat.conf',       'OllamaChat.Model'),
        ('MaxConcurrentQueries', 'mod_ollama_chat.conf',       'OllamaChat.MaxConcurrentQueries'),
        ('AhBotSeller',          'mod_ahbot.conf',             'AuctionHouseBot.EnableSeller'),
        ('AutoBalance',          'AutoBalance.conf',           'AutoBalance.Enable'),
    )
    out['settings'] = {k: v for k, fn, ck in pairs for v in [g(fn, ck)] if v is not None}
    return out


def clear_cache(slug, include_wdb=False):
    """Clear a client's Cache folder.

    WDB is preserved by default and that default matters: itemcache.wdb is what
    makes AtlasLoot resolve items, it can only be rebuilt by the client seeing
    each item, and warming it in-game starves loot rolls.  Deleting it is a real
    cost, so it takes an explicit flag.
    """
    doc = profiles_load()
    r = _find(doc, slug)
    if not r:
        return False, ['unknown product: %s' % slug]
    cache = _hub(r.get('paths', {}).get('cache'))
    if not cache:
        return False, ['%s has no cache path' % slug]
    if not os.path.isdir(cache):
        return True, ['nothing to clear: %s does not exist' % cache]
    client = _hub(r.get('paths', {}).get('client')) or ''
    # Never delete outside the profile's own client directory.
    if not os.path.abspath(cache).startswith(os.path.abspath(client) + os.sep):
        return False, ['refused: cache path is outside the client directory']

    wdb = os.path.join(cache, 'WDB')
    log, freed = [], 0
    for name in os.listdir(cache):
        p = os.path.join(cache, name)
        if not include_wdb and os.path.normcase(p) == os.path.normcase(wdb):
            log.append('kept WDB (item/creature caches) - pass includeWdb to remove it')
            continue
        try:
            if os.path.isdir(p):
                for dp, _, fs in os.walk(p):
                    for fn in fs:
                        try:
                            freed += os.path.getsize(os.path.join(dp, fn))
                        except OSError:
                            pass
                shutil.rmtree(p, ignore_errors=True)
            else:
                freed += os.path.getsize(p)
                os.remove(p)
            log.append('removed %s' % name)
        except Exception as e:
            log.append('could not remove %s: %s' % (name, e))
    log.append('freed about %.1f MB' % (freed / 1048576.0))
    return True, log


def set_active(slug):
    doc = profiles_load()
    r = _find(doc, slug)
    if not r:
        return False, ['unknown realm: %s' % slug]
    if r.get('kind') != 'realm':
        return False, ['%s is a client, not a realm - it has no active state' % slug]
    if C.running('worldserver')['up'] and doc.get('activeRealm') != slug:
        # This endpoint only edits the registry. Moving the running realm is the
        # 'switch' action, which stops the current one first and saves characters.
        return False, ['a realm is running - use Switch (POST /v1/patch/%s '
                       '{"action":"switch"}) to move to %s safely' % (slug, slug)]
    doc['activeRealm'] = slug
    profiles_save(doc)
    return True, ['active realm is now %s' % slug]


# ---------------------------------------------------------------- launch

# The camera-snap bug is NOT yet fixed, and this helper no longer pretends to fix
# it. The original theory - that 3.3.5a only calls ClipCursor for fullscreen, so a
# pointer escaping a smaller window pins against the desktop edge and the
# discrepancy lands as one huge yaw delta - was tested on 2026-09-04 and failed:
# the clip was active, diagnostics recorded zero edge contacts, and the camera
# still snapped. Corner-snapping the window to null the other candidate term did
# not stop it either.
#
# So the helper runs observe-only (--noclip) with logging on (--diag), recording
# what the OS cursor actually does during every mouselook to
# logs/cursor-clip.log, to be correlated against the FlipLog addon's in-game
# facing record. Restore clipping by dropping --noclip if a longer run ever shows
# real edge contacts; until then, leaving it on would constrain the pointer for
# no measured benefit.
#
# Gated on the target being Wow.exe rather than on the profile's `kind`, because
# every 3.3.5a client here is kind=realm - `kind` does not distinguish them. That
# also excludes profiles that launch through a script rather than Wow.exe: those
# may point into a client directory this hub does not own, and a client that is
# not 3.3.5a has an entirely separate input path.
CURSOR_CLIP_REL = os.path.join('tools', 'wow-cursor-clip.py')
# Observe-only and logging. Kept as a named constant because which mode this runs
# in is the whole question, and it should be changed in one obvious place.
CURSOR_CLIP_ARGS = ['--diag', '--noclip']


def _start_cursor_clip():
    """Start the cursor instrument alongside a Wow.exe launch; returns a note.

    The script self-guards with a named mutex and waits for the client window to
    appear, so starting it here - before that window exists, and again on a
    second launch - is both safe and idempotent.

    Never raises: a cursor helper must not be the reason a client did not start.
    But it does report why it gave up, because the failure mode that matters is
    the quiet one - a helper that was never started looks identical, from the
    game, to a helper that started and did not work."""
    script = _hub(CURSOR_CLIP_REL)
    if not script or not os.path.exists(script):
        return 'cursor instrument NOT started: %s is missing' % CURSOR_CLIP_REL
    pyw = shutil.which('pythonw') or shutil.which('pythonw.exe')
    if not pyw:
        # Say so rather than skipping quietly. Frozen, this process is
        # AzerothControl.exe, so sys.executable is no help and PATH is the only
        # route to an interpreter - a silent no-op here would look exactly like
        # a helper that ran and did nothing.
        return 'cursor instrument NOT started: no pythonw.exe on PATH'
    try:
        import subprocess
        subprocess.Popen([pyw, script] + CURSOR_CLIP_ARGS,
                         cwd=os.path.dirname(script),
                         creationflags=C.NO_WINDOW, close_fds=True)
        return 'cursor instrument started (observe-only, logging mouselook)'
    except Exception as e:
        return 'cursor instrument NOT started: %s' % e


def launch(slug):
    doc = profiles_load()
    r = _find(doc, slug)
    if not r:
        return False, ['unknown product: %s' % slug]
    L = r.get('launch') or {}
    target = _hub(L.get('target'))
    if not target or not os.path.exists(target):
        return False, ['not installed: %s' % (target or '(no launch target)')]
    workdir = _hub(L.get('workdir')) or os.path.dirname(target)
    kind = L.get('kind', 'exe')

    # Which world this client needs is a named realm, not "a worldserver". Asked
    # unscoped, a client bound to SpellDraft launched without a warning because
    # vanilla was up - and then sat at the login screen anyway.
    warn = []
    need = r['slug'] if r.get('controls', {}).get('serverManaged') else r.get('sharesRealm')
    if need:
        wanted = _find(profiles_load(), need)
        if not (wanted and C.running('worldserver', R.server_dir(wanted))['up']):
            warn.append('%s is not running - the client will sit at the login screen'
                        % (wanted or {}).get('name', need))

    try:
        if kind == 'vbs':
            # wscript keeps the VBS launchers' hidden-window behaviour intact.
            import subprocess
            subprocess.Popen(['wscript.exe', target], cwd=workdir,
                             creationflags=C.NO_WINDOW, close_fds=True)
            return True, warn + ['launched %s' % os.path.basename(target)]
        C.spawn_detached(target, workdir, hidden=False)
        note = []
        if os.path.basename(target).lower() == 'wow.exe':
            started = _start_cursor_clip()
            if started:
                note.append(started)
        return True, warn + ['launched %s' % os.path.basename(target)] + note
    except Exception as e:
        return False, warn + ['launch failed: %s' % e]


# ---------------------------------------------------------------- jobs

# Long actions (start / stop / restart) run on a worker thread and report
# progress from observable state - process presence and world readiness - rather
# than from a guessed percentage.  Nothing here duplicates control.py's start
# logic; it only watches what that logic produces.
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 900


def _job_put(job):
    with _JOBS_LOCK:
        _JOBS[job['id']] = job
        cut = time.time() - _JOB_TTL
        for k in [k for k, v in _JOBS.items()
                  if v.get('finished') and v['finished'] < cut]:
            _JOBS.pop(k, None)


def job_get(job_id):
    with _JOBS_LOCK:
        j = _JOBS.get(job_id)
        return dict(j) if j else None


def job_latest(slug):
    with _JOBS_LOCK:
        js = [v for v in _JOBS.values() if v.get('slug') == slug]
    if not js:
        return None
    return dict(max(js, key=lambda v: v['started']))


# (label, percent, predicate) - the checkpoints an action passes through.
# Built per profile, because "world ready" has to be judged from THAT profile's
# log and config; a shared lambda would watch the vanilla realm every time.
def _start_steps(realm):
    # Scoped to this profile's own binaries for the same reason "world ready" is
    # scoped to its log: while another realm is up, an unscoped check is satisfied
    # by ITS worldserver and the bar walks straight to 60% for a start that has not
    # begun. The port collision would then surface as a stall at "World ready".
    srv = R.server_dir(realm)
    world = realm.get('world') or {}
    if world.get('authserver') is False:
        # This profile has no authserver.exe - a helper script serves auth
        # instead - so watching for the exe would park the bar at 40% for the
        # whole start. Its helpers are watched by port in the order
        # start_realm reaches them, which is after the world.
        steps = [('MySQL', 20, lambda: C.mysql_alive()),
                 ('World server', 40, lambda: C.running('worldserver', srv)['up'])]
        listed = [h for h in R.helpers(realm) if h.get('port')]
        for i, h in enumerate(listed):
            steps.append((h.get('label') or h.get('name') or 'Helper',
                          50 + int(30.0 * (i + 1) / (len(listed) + 1)),
                          lambda p=h['port']: C.port_open(p)))
        steps.append(('World ready', 95, lambda: R.ready(realm)))
        return steps
    return [
        ('MySQL',        20, lambda: C.mysql_alive()),
        ('Auth server',  40, lambda: C.running('authserver', srv)['up']),
        ('World server', 60, lambda: C.running('worldserver', srv)['up']),
        ('World ready',  95, lambda: R.ready(realm)),
    ]


_STOP_STEPS = [
    ('World server stopped', 60, lambda: not C.running('worldserver')['up']),
    ('Auth server stopped',  90, lambda: not C.running('authserver')['up']),
]


# A switch is a stop followed by a start, so its checkpoints are both sets in
# order. The stop half is compressed into the first third of the bar.
def _switch_steps(realm):
    return ([('World server stopped', 15, lambda: not C.running('worldserver')['up'])]
            + [(label, 20 + int(pct * 0.75), pred) for label, pct, pred in _start_steps(realm)])


def _run(fn, emit):
    """Run an engine call that reports progress through an emit callback.

    Lines are handed to `emit` as they happen rather than collected and returned:
    a switch can spend five minutes saving characters, and a log that only appears
    once it is over is no help to whoever is watching the bar.  An engine call
    that returns None - a graceful stop, which has nothing to fail at short of
    raising - counts as success.
    """
    out = fn(emit)
    return (True if out is None else bool(out)), []


def _run_job(job, work, steps):
    def emit(line):
        with _JOBS_LOCK:
            job['log'].append(line)

    result = {}

    def worker():
        try:
            out = work(emit)
            # work() may return either a plain log, or (ok, log) when the action
            # can fail without raising - a worldserver that never launches, say.
            if isinstance(out, tuple) and len(out) == 2:
                result['ok'], result['log'] = bool(out[0]), list(out[1] or [])
            else:
                result['ok'], result['log'] = True, list(out or [])
        except Exception as e:
            result['log'] = ['error: %s' % e]
            result['ok'] = False

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    done = set()
    while t.is_alive():
        # Checkpoints are strictly ordered: a later one is never credited before
        # an earlier one has been observed. Without this a switch would jump
        # straight to "World server up" - which it still is, from the realm we
        # are in the middle of shutting down.
        for label, pct, pred in steps:
            if label in done:
                continue
            try:
                hit = pred()
            except Exception:
                hit = False
            if hit:
                done.add(label)
                with _JOBS_LOCK:
                    job['step'] = label
                    job['pct'] = max(job['pct'], pct)
                emit(label)
            break
        time.sleep(0.6)
    t.join(timeout=5)

    for line in (result.get('log') or []):
        emit(str(line))
    with _JOBS_LOCK:
        job['ok'] = bool(result.get('ok'))
        job['state'] = 'done' if job['ok'] else 'failed'
        job['pct'] = 100
        job['step'] = 'Done' if job['ok'] else 'Failed'
        job['finished'] = time.time()


def patch_start(slug, action, deps=False):
    """'patch' in the Ascension sense: the long-running action for a product.

    Every action runs against THAT profile's own directory via realms.py.  The
    obvious shortcut - calling control.start_all() - is wrong here: it is bound
    to the vanilla SERVER_DIR, so pressing Start on SpellDraft would boot the
    vanilla binaries while the panel cheerfully said SpellDraft.
    """
    doc = profiles_load()
    r = _find(doc, slug)
    if not r:
        return None, 'unknown product: %s' % slug
    if not r.get('controls', {}).get('serverManaged'):
        return None, '%s does not manage a server' % slug
    # 'switch' is the one action whose whole purpose is to run against a realm
    # that is NOT active yet, so it is exempt from the active-realm check.
    if action != 'switch' and doc.get('activeRealm') != slug:
        return None, '%s is not the active realm - switch to it first' % slug
    if not _installed(r):
        return None, '%s is not installed yet' % slug

    with _JOBS_LOCK:
        for j in _JOBS.values():
            if j['state'] == 'running':
                return None, 'another action is already running (%s)' % j['action']

    if action == 'start':
        work, steps = (lambda emit: _run(lambda e: R.start_realm(r, e), emit)), _start_steps(r)
    elif action == 'stop':
        def work(emit):
            ok, log = _run(lambda e: R.stop_realm(r, e), emit)
            if deps:
                log.append(C.stop_mysql())
            return ok, log
        steps = _stop_steps(r)
    elif action == 'restart':
        def work(emit):
            ok1, log = _run(lambda e: R.stop_realm(r, e), emit)
            time.sleep(2)
            ok2, log2 = _run(lambda e: R.start_realm(r, e), emit)
            return (ok1 and ok2), log + log2
        steps = _start_steps(r)
    elif action == 'switch':
        # switch_to returns its log as well as streaming it; only the verdict is
        # passed on, or every line would be recorded twice.
        def work(emit):
            ok, _log = R.switch_to(slug, emit)
            return ok, []
        steps = _switch_steps(r)
    else:
        return None, 'unknown action: %s' % action

    job = {'id': uuid.uuid4().hex[:12], 'slug': slug, 'action': action,
           'state': 'running', 'pct': 5, 'step': 'Starting', 'log': [],
           'started': time.time(), 'finished': None, 'ok': None}
    _job_put(job)
    threading.Thread(target=_run_job, args=(job, work, steps), daemon=True).start()
    return job['id'], None


# ---------------------------------------------------------------- modules

def _addon_paths(slug):
    doc = profiles_load()
    r = _find(doc, slug) or {}
    live = _hub((r.get('paths') or {}).get('addons'))
    if not live:
        return None, None
    return live, live + '.disabled'


def _addon_targets():
    """Every client an addon action should be mirrored into.

    The catalog is 3.3.5a and so is every realm here, so an addon installed for
    one is wanted by all of them - and a per-realm addon list that drifts is
    worse than no separation at all, because the launcher would report a state
    the client does not have.  Only profiles whose AddOns folder actually exists
    are included, which is what keeps a planned realm out of the loop."""
    out = []
    for r in profiles_load().get('realms', []):
        if not ((r.get('controls') or {}).get('addonsAware')):
            continue
        live = _hub((r.get('paths') or {}).get('addons'))
        if not live or not os.path.isdir(live):
            continue
        out.append({'slug': r.get('slug'), 'name': r.get('name') or r.get('slug'),
                    'live': live, 'park': live + '.disabled'})
    return out


def modules(slug=None):
    """Catalog plus per-addon state, grouped for the launcher's addon view."""
    doc = profiles_load()
    slug = slug or doc.get('activeRealm') or 'vanilla'
    live, park = _addon_paths(slug)

    # Read the selected realm's own AddOns folder.  This used to report the
    # vanilla client's state for every profile and then blank it out for the
    # others, which is why a realm with a fully populated AddOns folder showed
    # an empty list while its addons were plainly working in-game.
    actionable = bool(live and os.path.isdir(live))
    state = C.addon_state(live)
    if not actionable:
        # A client-only entry, or a realm not built yet: addon_state falls back
        # to the default directory when given None, and reporting one client's
        # installs under a profile that has no AddOns folder would be a lie.
        for a in state.get('addons', []):
            a['installed'] = False
            a['folders'] = []
        state['foreign'] = []
    parked = set()
    if park and os.path.isdir(park):
        parked = {d for d in os.listdir(park)
                  if os.path.isdir(os.path.join(park, d))}

    led = C._ledger()
    groups = {}
    for a in state.get('addons', []):
        folders = a.get('folders') or led.get(a['name'], {}).get('folders') or []
        a['disabled'] = bool(folders) and all(f in parked for f in folders)
        if a['disabled']:
            a['installed'] = True
            a['folders'] = folders
        a['actionable'] = actionable
        groups.setdefault(a.get('category', 'Other'), []).append(a)

    cats = [{'name': k, 'addons': sorted(v, key=lambda x: x['name'].lower()),
             'installed': sum(1 for x in v if x.get('installed'))}
            for k, v in sorted(groups.items())]
    return {
        'slug': slug,
        'addonsDir': live,
        'disabledDir': park,
        'actionable': actionable,
        'clientPresent': actionable,
        'mirrors': [t['name'] for t in _addon_targets()],
        'count': len(state.get('addons', [])),
        'installed': sum(1 for a in state.get('addons', []) if a.get('installed')),
        'foreign': state.get('foreign', []),
        'categories': cats,
        'error': state.get('error'),
    }


def module_add(name, slug=None):
    """Install into every 3.3.5a client in the hub, from a single download."""
    targets = _addon_targets()
    if not targets:
        return False, ['no client with an AddOns folder to install into']

    entry, blob, log = C.addon_fetch(name)
    if not entry:
        return False, log

    folders, ok_any = [], False
    for t in targets:
        ok, got, sub_log = C.addon_unpack(entry, blob, t['live'])
        log.append('%s: %s' % (t['name'], '; '.join(sub_log) or 'no change'))
        if ok:
            ok_any = True
            folders = folders or got
            log += _autotick(t, got or folders)
    if not ok_any:
        return False, log

    # One ledger for the hub: the mirroring above is what makes that accurate.
    led = C._ledger()
    led[name] = {'folders': folders, 'file': entry['file']}
    C._ledger_save(led)
    return True, log


def module_remove(name, slug=None):
    """Remove from every client, so no realm keeps a copy that reappears later."""
    targets = _addon_targets()
    if not targets:
        return False, ['no client with an AddOns folder to remove from']

    # Read the folders before the first call: addon_remove drops the ledger row.
    folders = (C._ledger().get(name) or {}).get('folders', [])
    if not folders:
        return False, ['not tracked as installed: %s' % name]

    log, ok_any = [], False
    for t in targets:
        removed = []
        for f in folders:
            for root in (t['live'], t['park']):
                p = os.path.join(root, f)
                # Never delete outside that client's own Interface tree.
                if os.path.abspath(p).startswith(os.path.abspath(root)) and os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                    removed.append(f)
        ok_any = ok_any or bool(removed)
        log.append('%s: removed %s' % (t['name'], ', '.join(removed) or '(nothing on disk)'))

    led = C._ledger()
    led.pop(name, None)
    C._ledger_save(led)
    return True, log


def module_replace(name, enabled, slug=None):
    """Enable or disable an installed addon without deleting it.

    WoW's own enable state lives in per-character WTF files, which are fragile
    to edit and only take effect for the character they belong to.  Sidelining
    the folder is deterministic, applies to every character, and is reversible
    with the folder still on disk.

    _autotick is the one deliberate exception to that, and only for a folder
    that is useless without it - see its docstring.
    """
    rec = C._ledger().get(name)
    if not rec:
        return False, ['not tracked as installed: %s' % name]
    folders = rec.get('folders') or []
    if not folders:
        return False, ['%s has no folders recorded' % name]

    targets = _addon_targets()
    if not targets:
        return False, ['no client with an AddOns folder to toggle in']

    log = []
    for t in targets:
        ok, sub_log = _replace_one(t, name, folders, enabled, targets)
        if not ok:
            return False, log + sub_log
        log += sub_log
        if enabled:
            log += _autotick(t, folders)
    log.append('Restart the client to apply.')
    return True, log


# WoW keeps each addon's checkbox in a per-character AddOns.txt under
# WTF/Account/<ACCOUNT>/<REALM>/<CHARACTER>/, one "<Folder>: enabled" or
# "<Folder>: disabled" per line, CRLF.  A folder the file does not name falls
# back to its TOC's DefaultState.
_ADDONS_TXT = 'AddOns.txt'


def _toc_is_ondemand_default_off(folder_dir):
    """True for a load-on-demand companion module that ships switched off.

    The signature is two TOC headers together, '## LoadOnDemand: 1' and
    '## DefaultState: disabled'.  Grid2Options, Bagnon_GuildBank and
    Details_3DModelsPaths are the three folders in this hub that carry it.

    Moving such a folder into AddOns is not enough to make it work.  WoW honours
    DefaultState and leaves the box unticked; the parent addon then calls
    LoadAddOn() on it, WoW refuses a disabled addon, and the parent reports its
    own options module as missing - Grid2 says "You need Grid2Options addon
    enabled to be able to configure Grid2", which reads exactly like a broken
    install.  Ticking the box is what the user would otherwise have to do by
    hand, and because the module is load-on-demand it still costs nothing at
    startup: the tick only makes the later LoadAddOn() legal.

    A folder that is DefaultState: disabled WITHOUT LoadOnDemand is a different
    animal - an opt-in feature its author deliberately ships off, like
    Grid2AoeHeals - so it is left alone.
    """
    for toc in sorted(glob.glob(os.path.join(folder_dir, '*.toc'))):
        try:
            with open(toc, 'r', encoding='utf-8', errors='replace') as fh:
                text = fh.read()
        except OSError:
            continue
        if (re.search(r'^##\s*LoadOnDemand:\s*1\b', text, re.I | re.M) and
                re.search(r'^##\s*DefaultState:\s*disabled\b', text, re.I | re.M)):
            return True
    return False


def _addons_txt_files(live):
    """Every AddOns.txt belonging to the client that owns this AddOns folder."""
    # .../<client>/Interface/AddOns -> <client>
    client = os.path.dirname(os.path.dirname(os.path.abspath(live)))
    account = os.path.join(client, 'WTF', 'Account')
    if not os.path.isdir(account):
        return []
    out = []
    for pattern in (os.path.join(account, '*', _ADDONS_TXT),              # account
                    os.path.join(account, '*', '*', '*', _ADDONS_TXT)):   # character
        out += [p for p in glob.glob(pattern) if os.path.isfile(p)]
    return sorted(out)


def _addons_txt_enable(live, folders):
    """Tick `folders` in every AddOns.txt of this client; returns a log list.

    Only the lines naming those folders are touched: an existing entry is
    rewritten to 'enabled', one the file has never heard of is appended, and
    every other line is passed through unchanged so a character's own choices
    survive.  The client rewrites this file when it exits, so a tick applied
    while WoW is running is lost - which is why the caller's log still says to
    restart.
    """
    wanted = {f.lower(): f for f in folders}
    log = []
    for path in _addons_txt_files(live):
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as fh:
                lines = fh.read().splitlines()
        except OSError as exc:
            log.append('could not read %s: %s' % (path, exc))
            continue

        seen, out, changed = set(), [], False
        for line in lines:
            key = line.split(':', 1)[0].strip().lower()
            if key in wanted:
                seen.add(key)
                want = '%s: enabled' % wanted[key]
                if line.strip() != want:
                    line, changed = want, True
            out.append(line)
        for key, folder in wanted.items():
            if key not in seen:
                out.append('%s: enabled' % folder)
                changed = True
        if not changed:
            continue

        try:
            # Match what the client writes, so the file stays byte-familiar.
            with open(path, 'w', encoding='utf-8', newline='\r\n') as fh:
                fh.write('\n'.join(out) + '\n')
        except OSError as exc:
            log.append('could not write %s: %s' % (path, exc))
            continue
        who = os.path.basename(os.path.dirname(path))
        log.append('ticked %s for %s' % (', '.join(sorted(folders)), who))
    return log


def _autotick(target, folders):
    """Tick any load-on-demand companion module this package just brought in."""
    live = target['live']
    need = [f for f in folders
            if os.path.isdir(os.path.join(live, f)) and
            _toc_is_ondemand_default_off(os.path.join(live, f))]
    if not need:
        return []
    return ['%s: %s' % (target['name'], line)
            for line in _addons_txt_enable(live, need)]


def _folder_source(targets, folder):
    """Any client that still has this folder on disk, or None.

    Resolved on demand rather than up front: the first client handled moves the
    folder out of its own AddOns.disabled, which would leave a precomputed path
    pointing at somewhere the folder no longer is.  Scanning here also lets a
    client that was just enabled serve as the donor for the next one."""
    for t in targets or []:
        for root in (t['live'], t['park']):
            p = os.path.join(root, folder)
            if os.path.isdir(p):
                return p
    return None


def _replace_one(target, name, folders, enabled, targets=None):
    """Move one addon's folders between AddOns and AddOns.disabled for one client."""
    live, park = target['live'], target['park']
    os.makedirs(park, exist_ok=True)
    src_root, dst_root = (park, live) if enabled else (live, park)
    moved, missing, copied = [], [], []
    for f in folders:
        src, dst = os.path.join(src_root, f), os.path.join(dst_root, f)
        # Both ends must stay inside this client's Interface tree.
        base = os.path.abspath(os.path.dirname(live))
        if not (os.path.abspath(src).startswith(base) and
                os.path.abspath(dst).startswith(base)):
            return False, ['refused: %s resolves outside %s' % (f, base)]
        if not os.path.isdir(src):
            if os.path.isdir(dst):
                continue          # already in the requested state
            # Nothing to move here, but another client may still have it.
            donor = _folder_source(targets, f) if enabled else None
            if donor:
                shutil.copytree(donor, dst)
                copied.append(f)
                continue
            missing.append(f)
            continue
        if os.path.isdir(dst):
            shutil.rmtree(dst, ignore_errors=True)
        shutil.move(src, dst)
        moved.append(f)

    line = '%s: %s %s' % (target['name'], 'enabled' if enabled else 'disabled',
                          ', '.join(moved + copied) or '(already in that state)')
    out = [line]
    if copied:
        out.append('%s: copied in from another client: %s'
                   % (target['name'], ', '.join(copied)))
    if missing:
        out.append('%s: missing on disk: %s' % (target['name'], ', '.join(missing)))
    return True, out


# ---------------------------------------------------------------- misc

def changelog(slug):
    path = os.path.join(CHANGELOG_DIR, '%s.json' % slug)
    if not os.path.isfile(path):
        return {'slug': slug, 'entries': [],
                'note': 'no changelog file yet (%s)' % path}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            doc = json.load(f)
    except Exception as e:
        return {'slug': slug, 'entries': [], 'note': 'unreadable: %s' % e}
    doc['entries'] = sorted(doc.get('entries', []),
                            key=lambda e: e.get('date', ''), reverse=True)
    return doc


def disk_space():
    out = []
    for label, path in (('hub', ROOT),):
        try:
            u = shutil.disk_usage(path)
            out.append({'label': label, 'path': path, 'total': u.total,
                        'free': u.free, 'used': u.used})
        except Exception as e:
            out.append({'label': label, 'path': path, 'error': str(e)})
    return {'volumes': out}


def health():
    return {
        'ok': True,
        'hub': ROOT,
        'port': C.PORT,
        'panel': 'azeroth-control',
        'profiles': os.path.isfile(PROFILES_FILE),
        'time': int(time.time()),
    }


# ---------------------------------------------------------------- routing

def handle_get(h, path, query):
    """Return True if this module handled the request."""
    p = path.rstrip('/') or '/'
    parts = [s for s in p.split('/') if s]

    if p == '/v1/health':
        h._json(health()); return True
    if p == '/v1/available-disk-space':
        h._json(disk_space()); return True
    if p == '/v1/products':
        h._json(products()); return True
    # The status document itself. Everything that draws state should be reading
    # this - /v1/products answers "what can I click", not "what is running".
    # ?host=1 adds host memory, ?population=0 skips the MySQL query.
    if p == '/v1/status':
        h._json(S.snapshot(
            include_host=(query.get('host') or ['0'])[0] not in ('0', '', 'false'),
            include_population=(query.get('population') or ['1'])[0] not in ('0', 'false')))
        return True
    if p == '/v1/modules':
        h._json(modules((query.get('slug') or [None])[0])); return True

    if len(parts) >= 3 and parts[0] == 'v1' and parts[1] == 'products':
        slug = parts[2]
        tail = parts[3] if len(parts) > 3 else ''
        if not tail:
            d = product(slug)
            h._json(d or {'error': 'unknown product'}, 200 if d else 404); return True
        if tail == 'settings':
            d = product_settings(slug)
            h._json(d or {'error': 'unknown product'}, 200 if d else 404); return True
        if tail == 'changelog':
            h._json(changelog(slug)); return True

    if len(parts) >= 4 and parts[0] == 'v1' and parts[1] == 'patch' and parts[3] == 'state':
        slug = parts[2]
        job_id = (query.get('job') or [None])[0]
        job = job_get(job_id) if job_id else job_latest(slug)
        if len(parts) > 4 and parts[4] == 'stream':
            _stream(h, slug, job_id); return True
        h._json(job or {'state': 'idle', 'slug': slug, 'pct': 0, 'log': []}); return True

    # /v1/realm/<slug>/... - the old panel's tabs, scoped to one profile.
    if P.handle_get(h, path, query):
        return True
    return False


def handle_post(h, path, body):
    p = path.rstrip('/') or '/'
    parts = [s for s in p.split('/') if s]

    if len(parts) >= 3 and parts[0] == 'v1' and parts[1] == 'products' \
            and len(parts) > 3 and parts[3] == 'clear_cache':
        ok, log = clear_cache(parts[2], bool(body.get('includeWdb')))
        h._json({'ok': ok, 'log': log}); return True

    if len(parts) == 3 and parts[0] == 'v1' and parts[1] == 'launch':
        ok, log = launch(parts[2])
        h._json({'ok': ok, 'log': log}); return True

    if len(parts) == 3 and parts[0] == 'v1' and parts[1] == 'patch':
        job_id, err = patch_start(parts[2], str(body.get('action', 'start')),
                                  bool(body.get('deps')))
        if err:
            h._json({'ok': False, 'log': [err]}, 409); return True
        h._json({'ok': True, 'job': job_id}); return True

    if p == '/v1/realms/active':
        ok, log = set_active(str(body.get('slug', '')))
        h._json({'ok': ok, 'log': log}, 200 if ok else 409); return True

    if p in ('/v1/modules/add', '/v1/modules/remove', '/v1/modules/replace'):
        name = str(body.get('name', ''))
        slug = body.get('slug')
        if p.endswith('/add'):
            ok, log = module_add(name, slug)
        elif p.endswith('/remove'):
            ok, log = module_remove(name, slug)
        else:
            ok, log = module_replace(name, bool(body.get('enabled')), slug)
        h._json({'ok': ok, 'log': log}); return True

    if P.handle_post(h, path, body):
        return True
    return False


def _stream(h, slug, job_id):
    """Server-Sent Events: the stdlib stand-in for the daemon's progress socket."""
    try:
        h.send_response(200)
        h.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        h.send_header('Cache-Control', 'no-store')
        h.send_header('Connection', 'close')
        h.end_headers()
    except Exception:
        return
    last = None
    deadline = time.time() + 900
    while time.time() < deadline:
        job = job_get(job_id) if job_id else job_latest(slug)
        payload = job or {'state': 'idle', 'slug': slug, 'pct': 0, 'log': []}
        blob = json.dumps(payload)
        if blob != last:
            last = blob
            try:
                h.wfile.write(b'data: ' + blob.encode('utf-8') + b'\n\n')
                h.wfile.flush()
            except Exception:
                return
        if payload.get('state') in ('done', 'failed'):
            return
        time.sleep(0.5)


def _stop_steps(realm):
    """Stopping THIS realm is done when THIS realm's processes are gone.

    Scoped, unlike the switch below: a stop makes no claim about the ports, only
    about the profile it was aimed at, and an unscoped check would leave the bar
    parked at 60% because some other realm is still up.
    """
    srv = R.server_dir(realm)
    return [
        ('World server stopped', 60, lambda: not C.running('worldserver', srv)['up']),
        ('Auth server stopped',  90, lambda: not C.running('authserver', srv)['up']),
    ]


