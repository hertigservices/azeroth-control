# -*- coding: utf-8 -*-
"""Realm-scoped versions of the old control panel's features.

The old panel (ui.html + /api/*) can only ever drive ONE realm.  control.py
resolves CONF_DIR / MOD_CONF / LOG_DIR from a single ROOT at import time, and
SETTABLE, RATES, PB_CONF, _WC and _IP bake those absolute paths into
module-level tables.  So every knob on that page edits the DEFAULT realm's files
no matter which realm is selected - fine while there is only one realm, wrong the
moment a second one shares the hub.

This module re-exposes the same features per realm without editing control.py.
Two rules keep the two panels from drifting apart:

  * control.SETTABLE and control.RATES stay the single source of truth for WHAT
    is safe to change and within what bounds.  Here their paths are rebased onto
    the selected realm's config directory rather than being re-listed.
  * anything a realm does not actually have on disk is dropped from the response
    instead of being shown as a control that would silently miss.  A realm with
    worldserver.conf but no playerbots.conf gets the rate sliders and not the bot
    caps, without a per-realm allow-list to maintain.

Deliberately NOT generalised: quest-drop chance and gameobject respawn speed.
Those are ~150 lines of tuned UPDATEs against acore_world's loot tables, and a
heavily modified world - a class overhaul, a replaced item set - does not
necessarily carry the same class/subclass meaning in those rows.  They stay
available on a stock world and report why elsewhere - see _world_surgery_ok().
"""

import os
import re
import subprocess
import time

import realms as R

# control.py runs as __main__; importing it here would build a second module
# object.  bind() is handed the real one, exactly as launcher.py does.
C = None

# The world schema control.py's loot/spawn SQL was written against.  Realms on a
# different schema are not offered those two tools.
SURGERY_SCHEMA = 'acore_world'


def bind(control_module):
    global C
    C = control_module


# ------------------------------------------------------------------ context

def ctx(slug):
    """Everything a realm-scoped call needs, resolved from the registry.

    Read fresh each call rather than cached: profiles.json is edited by the
    launcher itself (activeRealm) and by hand, and a stale copy here would send
    a write to the previous realm's config directory.
    """
    doc = R.load()
    r = R.find(doc, slug)
    if not r:
        return None
    conf = R.conf_dir(r)
    paths = r.get('paths') or {}
    return {
        'slug': slug,
        'name': r.get('name', slug),
        'realm': r,
        'active': doc.get('activeRealm') == slug,
        'conf': conf,
        'confExists': bool(conf and os.path.isdir(conf)),
        'modconf': R.hub(paths.get('modconfigs')) or os.path.join(conf or '', 'modules'),
        'logs': R.log_dir(r),
        'server': R.server_dir(r),
        'db': r.get('db') or {},
        'controls': r.get('controls') or {},
    }


def _rebase(path, c):
    """Point one of control.py's vanilla-bound config paths at THIS realm.

    Module configs are rebased against the profile's own modconfigs path rather
    than assuming <configs>/modules, because a profile is free to declare them
    somewhere else.
    """
    mod = os.path.normpath(C.MOD_CONF)
    p = os.path.normpath(path)
    if p.lower().startswith(mod.lower() + os.sep):
        return os.path.normpath(os.path.join(c['modconf'], os.path.relpath(p, mod)))
    rel = os.path.relpath(p, C.CONF_DIR)
    if rel.startswith('..'):
        return p                      # not under the config tree at all; leave it
    return os.path.normpath(os.path.join(c['conf'], rel))


# Display metadata for the SETTABLE keys.  control.SETTABLE's own lambda stays
# the gate on what is accepted - this table only supplies the label and the
# slider hints, so a mismatch here can never let a bad value through.
SETTING_META = {
    'saveInterval': ('Character save interval', 'ms', 60000, 3600000, 60000),
    'mapThreads':   ('Map update threads', '', 1, 16, 1),
    'minBots':      ('Minimum random bots', '', 0, 5000, 5),
    'maxBots':      ('Maximum random bots', '', 0, 5000, 5),
    'concurrency':  ('LLM concurrent queries', '', 0, 64, 1),
}


def settings(slug):
    c = ctx(slug)
    if not c:
        return None
    out = {'slug': slug, 'name': c['name'], 'active': c['active'],
           'configDir': c['conf'], 'configDirExists': c['confExists'],
           'modConfigDir': c['modconf'], 'controls': c['controls'],
           'items': [], 'missing': [], 'phase': None, 'phases': [],
           'db': {k: v for k, v in c['db'].items() if k != 'pass'}}
    if not c['confExists']:
        return out
    for key in sorted(C.SETTABLE):
        path, ckey, _valid = C.SETTABLE[key]
        p = _rebase(path, c)
        label, unit, lo, hi, step = SETTING_META.get(key, (ckey, '', None, None, 1))
        if not os.path.isfile(p):
            out['missing'].append({'key': key, 'label': label,
                                   'file': os.path.basename(p)})
            continue
        # 'default' is what the build shipped, read from this profile's own
        # <file>.conf.dist - so the number box can be pre-loaded with it and any
        # setting reset in one click without anyone memorising stock values.
        out['items'].append({'key': key, 'confKey': ckey, 'label': label,
                             'unit': unit, 'min': lo, 'max': hi, 'step': step,
                             'file': os.path.basename(p),
                             'value': C.conf_get(p, ckey),
                             'default': C.conf_default(p, ckey)})
    ip = os.path.join(c['modconf'], 'individualProgression.conf')
    if c['controls'].get('phaseAware') and os.path.isfile(ip):
        lvl = C.conf_get(ip, 'IndividualProgression.BotAccountsMaxLevel')
        out['phase'] = {'60': 'vanilla', '70': 'tbc', '80': 'wotlk'}.get(str(lvl or ''))
        out['phases'] = sorted(C.PHASES.keys())
    return out


def set_setting(slug, key, value):
    c = ctx(slug)
    if not c:
        return False, ['unknown realm: %s' % slug]
    if key not in C.SETTABLE:
        return False, ['unknown setting: %s' % key]
    path, ckey, valid = C.SETTABLE[key]
    p = _rebase(path, c)
    if not os.path.isfile(p):
        return False, ['%s has no %s' % (c['name'], os.path.basename(p))]
    v = str(value)
    try:
        if not valid(v):
            raise ValueError
    except Exception:
        return False, ['value out of range for %s' % key]
    ok = C.conf_set(p, ckey, v)
    if not ok:
        return False, ['%s is not present in %s' % (ckey, os.path.basename(p))]
    return True, ['OK  %s = %s  (%s)' % (ckey, v, os.path.basename(p)),
                  'Restart the world to apply.' if c['active']
                  else 'Applies the next time %s is started.' % c['name']]


# --------------------------------------------------------------- progression
# The VALUES stay in control.PHASES so tuning them keeps working from one place;
# only the routing - which key goes in which file - is expressed here, because in
# control.py it is code inside set_phase() rather than data.

def _phase_writes(p):
    return (('playerbots.conf',           'AiPlayerbot.RandomBotMaxLevel',              p['lvl']),
            ('playerbots.conf',           'AiPlayerbot.RandomBotMaps',                  p['maps']),
            ('playerbots.conf',           'AiPlayerbot.DisableDeathKnightLogin',        p['dk']),
            ('playerbots.conf',           'AiPlayerbot.TeleToShattrathCityWeight',      p['shat']),
            ('playerbots.conf',           'AiPlayerbot.TeleToDalaranWeight',            p['dala']),
            ('individualProgression.conf', 'IndividualProgression.BotAccountsMaxLevel', p['lvl']),
            ('mod_ahbot.conf', 'AuctionHouseBot.DisableItemsAboveReqLevel',      p['ah_req']),
            ('mod_ahbot.conf', 'AuctionHouseBot.DisableItemsAboveLevel',         p['ah_ilvl']),
            ('mod_ahbot.conf', 'AuctionHouseBot.DisableItemsAboveReqSkillRank',  p['ah_tg_skill']),
            ('mod_ahbot.conf', 'AuctionHouseBot.DisableTGsAboveReqSkillRank',    p['ah_tg_skill']),
            ('mod_ahbot.conf', 'AuctionHouseBot.DisableTGsAboveLevel',           p['ah_tg_ilvl']),
            ('mod_ahbot.conf', 'AuctionHouseBot.DisableDKItems',                 p['ah_dk']))


def set_phase(slug, name):
    c = ctx(slug)
    if not c:
        return False, ['unknown realm: %s' % slug]
    if not c['controls'].get('phaseAware'):
        return False, ['%s does not run the progression module' % c['name']]
    p = C.PHASES.get(name)
    if not p:
        return False, ['unknown phase: %s' % name]
    out, wrote = [], 0
    for fn, key, val in _phase_writes(p):
        path = os.path.join(c['modconf'], fn)
        if not os.path.isfile(path):
            out.append('SKIP %s (no %s)' % (key, fn))
            continue
        ok = C.conf_set(path, key, val)
        wrote += 1 if ok else 0
        out.append('%s %s = %s' % ('OK  ' if ok else 'MISS', key, val))
    out.append('Restart the world to apply.')
    out.append('Auction listings already posted age out on their normal duration; '
               'the house shifts over hours rather than instantly.')
    return wrote > 0, out


# --------------------------------------------------------------------- rates

def rates(slug):
    c = ctx(slug)
    if not c:
        return None
    out = {'slug': slug, 'name': c['name'], 'active': c['active'],
           'groups': [], 'missingFiles': [],
           'questDrops': None, 'spawnRates': None, 'surgery': None}
    if not c['confExists']:
        return out
    groups, bucket, missing = [], {}, set()
    for path, key, label, group, lo, hi in C.RATES:
        p = _rebase(path, c)
        if not os.path.isfile(p):
            missing.add(os.path.basename(p))
            continue
        if group not in bucket:
            bucket[group] = []
            groups.append(group)
        bucket[group].append({'key': key, 'label': label, 'min': lo, 'max': hi,
                              'value': C.conf_get(p, key),
                              'default': C.conf_default(p, key),
                              'file': os.path.basename(p)})
    out['groups'] = [{'name': g, 'items': bucket[g]} for g in groups]
    out['missingFiles'] = sorted(missing)
    ok, why = _world_surgery_ok(c)
    out['surgery'] = {'available': ok, 'reason': why}
    if ok:
        out['questDrops'] = C.questdrops_state()
        out['spawnRates'] = C.spawnrates_state()
    return out


def set_rate(slug, key, value):
    c = ctx(slug)
    if not c:
        return False, ['unknown realm: %s' % slug]
    row = next((r for r in C.RATES if r[1] == key), None)
    if not row:
        return False, ['unknown rate: %s' % key]
    path, _k, label, _g, lo, hi = row
    p = _rebase(path, c)
    if not os.path.isfile(p):
        return False, ['%s has no %s' % (c['name'], os.path.basename(p))]
    try:
        v = float(value)
    except Exception:
        return False, ['not a number: %s' % value]
    if v < lo or v > hi:
        return False, ['%s must be between %s and %s' % (label, lo, hi)]
    txt = str(int(v)) if v == int(v) else ('%g' % v)
    if not C.conf_set(p, key, txt):
        return False, ['%s is not present in %s' % (key, os.path.basename(p))]
    msg = ['%s = %s  (%s)' % (key, txt, os.path.basename(p))]
    if not c['active']:
        msg.append('Applies the next time %s is started.' % c['name'])
        return True, msg
    if os.path.basename(p) == 'worldserver.conf':
        # _reload_config talks to the running world over SOAP, and only the
        # active realm has one.  Its movespeed guard still applies here.
        msg.extend(C._reload_config())
    else:
        msg.append('Module configs are read by their own module - restart the world to apply.')
    return True, msg


def _world_surgery_ok(c):
    """Is this realm one the loot/respawn SQL was actually written for?"""
    if not c['controls'].get('serverManaged'):
        return False, '%s does not manage a world database' % c['name']
    world = (c['db'] or {}).get('world')
    if world != SURGERY_SCHEMA:
        return False, ('Quest-drop and respawn edits are UPDATEs written and tested '
                       'against %s. %s uses %s, a different fork whose loot rows do '
                       'not carry the same meaning, so they are not offered here.'
                       % (SURGERY_SCHEMA, c['name'], world or 'no world database'))
    return True, None


def questdrops(slug, action):
    c = ctx(slug)
    if not c:
        return False, ['unknown realm: %s' % slug]
    ok, why = _world_surgery_ok(c)
    if not ok:
        return False, [why]
    if action == 'set100':
        return C.questdrops_set100()
    if action == 'restore':
        return C.questdrops_restore()
    return False, ['unknown action: %s' % action]


def spawnrates(slug, cat, speed):
    c = ctx(slug)
    if not c:
        return False, ['unknown realm: %s' % slug]
    ok, why = _world_surgery_ok(c)
    if not ok:
        return False, [why]
    return C.spawnrates_set(cat, speed)


# ----------------------------------------------------------------- world log

def log(slug, n=180):
    c = ctx(slug)
    if not c:
        return {'lines': ['unknown realm: %s' % slug], 'file': None}
    p = os.path.join(c['logs'] or '', 'Server.log')
    if not c['logs'] or not os.path.exists(p):
        return {'lines': ['no Server.log for %s yet' % c['name']], 'file': p}
    n = max(20, min(int(n), 800))
    try:
        # Opened binary and seeked from the end: worldserver holds this file open
        # for writing, so reading it whole is both slow and pointless.
        with open(p, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 220 * n))
            data = f.read()
        lines = [l.rstrip() for l in data.decode('utf-8', 'replace').splitlines() if l.strip()]
        return {'lines': lines[-n:], 'file': p}
    except Exception as e:
        return {'lines': ['(could not read log: %s)' % e], 'file': p}


# ------------------------------------------------------------------ database

def _scalar(c, sql):
    """One value from this realm's own MySQL user.  (value, error)."""
    user = (c['db'] or {}).get('user')
    if not user:
        return None, 'no MySQL user declared for %s' % c['name']
    # This realm's own worldserver.conf, so host, port and password all come
    # from the install being queried rather than from the hub's defaults.
    conf = os.path.join(c['conf'], 'worldserver.conf') if c.get('conf') else None
    return R.scalar_as(user, sql, conf=conf)


def _q(name):
    return (name or '').replace('`', '')


def online_character(c):
    """The character the trainer buttons act on, from THIS realm's schemas."""
    ch, auth = _q(c['db'].get('characters')), _q(c['db'].get('auth'))
    if not ch or not auth:
        return None
    base = ("SELECT c.name FROM %s.characters c JOIN %s.account a ON a.id=c.account "
            "WHERE %%s a.username NOT LIKE 'RNDBOT%%%%' "
            "AND a.username NOT IN ('AHBOT','PANELGM') %%s LIMIT 1;" % (ch, auth))
    v, _e = _scalar(c, base % ('c.online=1 AND', ''))
    if v:
        return v
    # Fall back to the most recently played character so the buttons still build
    # a usable command while nobody is logged in.
    v, _e = _scalar(c, base % ('', 'ORDER BY c.logout_time DESC'))
    return v


def population(c):
    ch, auth = _q(c['db'].get('characters')), _q(c['db'].get('auth'))
    if not ch or not auth:
        return {'bots': None, 'players': None}
    j = ("SELECT COUNT(*) FROM %s.characters c JOIN %s.account a ON a.id=c.account "
         "WHERE c.online=1 AND " % (ch, auth))
    bots, _ = _scalar(c, j + "a.username LIKE 'RNDBOT%';")
    ppl, _ = _scalar(c, j + "a.username NOT LIKE 'RNDBOT%' "
                            "AND a.username NOT IN ('AHBOT','PANELGM');")
    return {'bots': bots, 'players': ppl}


# --------------------------------------------------------------------- trainer
# control.soap_exec is bound to control/soap.json.  A realm may need its own GM
# account, so a soap-<slug>.json is honoured first and the shared file is the
# fallback - which is what every realm gets until one needs otherwise.

def _soap_cfg(c):
    for fn in ('soap-%s.json' % c['slug'], 'soap.json'):
        p = os.path.join(C.HERE, fn)
        if os.path.isfile(p):
            try:
                import json
                with open(p, 'r', encoding='utf-8') as f:
                    return json.load(f), fn
            except Exception:
                return None, fn
    return None, None


def soap_exec(c, command):
    """Run a GM command over SOAP against the running world.

    Same protocol as control.soap_exec; written out here only because that one
    reads its credentials from a fixed path and this one has to pick the file
    that belongs to the realm.
    """
    cfg, _fn = _soap_cfg(c)
    if not cfg:
        return False, 'SOAP not configured for %s' % c['name']
    import base64
    import urllib.error
    import urllib.request
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:ns1="urn:AC">'
        '<SOAP-ENV:Body><ns1:executeCommand><command>%s</command></ns1:executeCommand>'
        '</SOAP-ENV:Body></SOAP-ENV:Envelope>'
    ) % (command.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
    url = 'http://%s:%s/' % (cfg.get('host', '127.0.0.1'), cfg.get('port', 7878))
    auth = base64.b64encode(('%s:%s' % (cfg.get('user', ''), cfg.get('pass', ''))).encode()).decode()
    req = urllib.request.Request(url, data=body.encode('utf-8'), headers={
        'Content-Type': 'application/xml', 'Authorization': 'Basic ' + auth})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            txt = r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, 'SOAP rejected the login - is the world restarted since SOAP was enabled?'
        detail = e.read().decode('utf-8', 'replace')[:200] if hasattr(e, 'read') else ''
        return False, 'SOAP HTTP %s %s' % (e.code, detail)
    except Exception as e:
        return False, 'SOAP unreachable (%s) - restart the world to activate it' % e
    m = re.search(r'<result>(.*?)</result>', txt, re.S)
    return True, (m.group(1).strip() if m else txt.strip())[:400]


def trainer(slug):
    c = ctx(slug)
    if not c:
        return None
    cfg, fn = _soap_cfg(c)
    out = {'slug': slug, 'name': c['name'], 'active': c['active'],
           'commands': C.TRAINER, 'soapConfigured': cfg is not None,
           'soapFile': fn, 'soapReady': False, 'character': None,
           'note': None}
    if not c['active']:
        out['note'] = ('%s is not the active realm. Commands can still be built and '
                       'copied, but nothing can be run until you switch to it.' % c['name'])
        return out
    out['character'] = online_character(c)
    if cfg:
        out['soapReady'] = soap_exec(c, 'server info')[0]
    return out


def corpse_command(c, name):
    """Build a '.go xyz' that lands on the body, from this realm's corpse table."""
    if not name:
        return None, 'no character to look up'
    ch = _q(c['db'].get('characters'))
    if not ch:
        return None, 'no characters database declared for %s' % c['name']
    row, err = _scalar(c, (
        "SELECT CONCAT(ROUND(c.posX,2),' ',ROUND(c.posY,2),' ',ROUND(c.posZ,2),' ',c.mapId) "
        "FROM %s.corpse c JOIN %s.characters ch ON ch.guid = c.guid "
        "WHERE ch.name = '%s' LIMIT 1;" % (ch, ch, name.replace("'", ""))))
    if not row:
        return None, (err or ('no corpse found for %s - you are either alive, or you '
                              'already recovered it' % name))
    return '.go xyz %s' % row, None


def trainer_run(slug, cmd_id, args):
    c = ctx(slug)
    if not c:
        return False, ['unknown realm: %s' % slug], None
    if not c['active']:
        return False, ['%s is not the active realm' % c['name']], None
    name = online_character(c)
    if not name:
        return False, ['no character found to target in %s' % c['db'].get('characters')], None
    if cmd_id == 'corpse':
        built, err = corpse_command(c, name)
        if err:
            return False, [err], None
        return True, ['paste this in game chat:', built], built
    cmd, mode = C.trainer_build(cmd_id, args or {}, name)
    if cmd is None:
        return False, [mode], None
    if mode == 'copy':
        # Session-bound: SOAP runs as console, where there is no "you" to target.
        return True, ['paste this in game chat:', cmd], cmd
    ok, res = soap_exec(c, cmd)
    return ok, [cmd, res], None


# ------------------------------------------------------------------------ bots

def bots(slug):
    c = ctx(slug)
    if not c:
        return None
    pb = os.path.join(c['modconf'], 'playerbots.conf')
    out = {'slug': slug, 'name': c['name'], 'active': c['active'],
           'available': bool(c['controls'].get('botsAware') and os.path.isfile(pb)),
           'reason': None, 'commands': C.BOTS, 'nomenclature': C.NOMEN,
           'specs': [], 'gates': {}, 'online': {'bots': None, 'players': None},
           'character': None}
    if not out['available']:
        out['reason'] = ('%s does not run mod-playerbots (no playerbots.conf in %s)'
                         % (c['name'], os.path.basename(c['modconf'])))
        return out
    out['specs'] = _specs(pb)
    out['gates'] = _gates(pb)
    if c['active']:
        out['online'] = population(c)
        out['character'] = online_character(c)
    return out


def _specs(pb):
    """Named talent builds, read from this realm's own playerbots.conf."""
    names = {b['id']: b['t'] for b in C.BOT_CLASSES}
    found = {}
    try:
        with open(pb, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                m = re.match(r'\s*AiPlayerbot\.PremadeSpecName\.(\d+)\.(\d+)\s*=\s*(.+)', line)
                if m:
                    found.setdefault(int(m.group(1)), []).append(
                        (int(m.group(2)), m.group(3).strip()))
    except Exception:
        return []
    out = []
    for b in C.BOT_CLASSES:
        rows = sorted(found.get(b['id'], []))
        if rows:
            out.append({'name': names[b['id']], 'items': [{'t': n, 'd': ''} for _, n in rows]})
    return out


def _gates(pb):
    keys = [
        ('addClassCommand',    'AiPlayerbot.AddClassCommand'),
        ('maxAddedBots',       'AiPlayerbot.MaxAddedBots'),
        ('poolSize',           'AiPlayerbot.AddClassAccountPoolSize'),
        ('selfBotLevel',       'AiPlayerbot.SelfBotLevel'),
        ('autoInitOnly',       'AiPlayerbot.AutoInitOnly'),
        ('resetInstanceAlt',   'AiPlayerbot.ResetInstanceIdForAltBots'),
        ('autoGearCommand',    'AiPlayerbot.AutoGearCommand'),
        ('autoGearAltBots',    'AiPlayerbot.AutoGearCommandAltBots'),
        ('autoGearBis',        'AiPlayerbot.AutoGearBisCommand'),
        ('autoGearQuality',    'AiPlayerbot.AutoGearQualityLimit'),
        ('autoGearScore',      'AiPlayerbot.AutoGearScoreLimit'),
        ('maintenanceCommand', 'AiPlayerbot.MaintenanceCommand'),
        ('equipUpgradeThresh', 'AiPlayerbot.EquipUpgradeThreshold'),
        ('randomBotMaxLevel',  'AiPlayerbot.RandomBotMaxLevel'),
    ]
    return {k: {'key': c.split('.', 1)[1], 'v': C.conf_get(pb, c)} for k, c in keys}


def bots_build(slug, cmd_id, args):
    c = ctx(slug)
    if not c:
        return False, ['unknown realm: %s' % slug], None
    pb = os.path.join(c['modconf'], 'playerbots.conf')
    if not (c['controls'].get('botsAware') and os.path.isfile(pb)):
        return False, ['%s does not run mod-playerbots' % c['name']], None
    # No SOAP branch on purpose: the world refuses every playerbot command from a
    # console session, so there would be nothing to run.
    cmd, err = C.bots_build(cmd_id, args or {})
    if cmd is None:
        return False, [err], None
    return True, ['paste this in game chat:', cmd], cmd


# ------------------------------------------------------------------------ raid
# mod-raid-roster is ours, not upstream, and only the vanilla profile carries its
# config, so every other realm reports why instead of showing eight commands that
# would come back "Incorrect syntax".

def raid(slug):
    c = ctx(slug)
    if not c:
        return None
    rr = os.path.join(c['modconf'], 'mod_raid_roster.conf')
    pb = os.path.join(c['modconf'], 'playerbots.conf')
    out = {'slug': slug, 'name': c['name'], 'active': c['active'],
           'available': os.path.isfile(rr), 'reason': None,
           'commands': C.RAID, 'presets': C.RAID_PRESETS,
           'gates': {}, 'roster': {}, 'online': {'bots': None, 'players': None},
           'character': None}
    if not out['available']:
        out['reason'] = ('%s does not run mod-raid-roster (no mod_raid_roster.conf in %s)'
                         % (c['name'], os.path.basename(c['modconf'])))
        return out
    out['gates'] = _raid_gates(rr, pb)
    out['roster'] = _roster(c)
    if c['active']:
        out['online'] = population(c)
        out['character'] = online_character(c)
    return out


def _raid_gates(rr, pb):
    """The config values that decide whether these eight commands do anything.

    Split across two files because the module owns only its own on/off switch and
    then leans on mod-playerbots for the character pool it pins and the gear it
    hands out - so a roster can be refused by a playerbots.conf key with nothing
    wrong in mod_raid_roster.conf.
    """
    keys = [
        ('raidEnable',     rr, 'RaidRoster.Enable'),
        ('poolSize',       pb, 'AiPlayerbot.AddClassAccountPoolSize'),
        ('maxAddedBots',   pb, 'AiPlayerbot.MaxAddedBots'),
        ('limitExpansion', pb, 'AiPlayerbot.LimitGearExpansion'),
        ('gearQuality',    pb, 'AiPlayerbot.RandomGearQualityLimit'),
    ]
    # Only the AiPlayerbot. prefix is dropped. RaidRoster.Enable stays qualified -
    # a bare "Enable" beside four playerbot keys reads as one of them.
    return {k: {'key': key.replace('AiPlayerbot.', ''),
                'v': C.conf_get(f, key) if os.path.isfile(f) else None}
            for k, f, key in keys}


def _count(c, sql):
    """A COUNT() as an int. mysql --batch hands back text, and 'NULL' for a null."""
    v, err = _scalar(c, sql)
    if err or v is None or v == 'NULL':
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _roster(c):
    """What is actually pinned right now, so status is readable before pasting it.

    Counted from the module's own table rather than inferred from the bot pool:
    a slot survives its character being deleted, and that stale row is the usual
    reason a login comes up short of the preset.
    """
    ch = _q(c['db'].get('characters'))
    out = {'rosters': None, 'slots': None, 'online': None, 'stale': None, 'owners': None}
    if not ch:
        return out
    t = '%s.mod_raid_roster' % ch
    out['rosters'] = _count(c, 'SELECT COUNT(DISTINCT owner_guid) FROM %s;' % t)
    if out['rosters'] is None:
        # The table only exists once the module's SQL has run. Report nothing
        # rather than an error row - the commands themselves are still correct.
        return out
    out['slots'] = _count(c, 'SELECT COUNT(*) FROM %s;' % t)
    out['online'] = _count(
        c, 'SELECT COUNT(*) FROM %s r JOIN %s.characters ch ON ch.guid=r.bot_guid '
           'WHERE ch.online=1;' % (t, ch))
    out['stale'] = _count(
        c, 'SELECT COUNT(*) FROM %s r LEFT JOIN %s.characters ch ON ch.guid=r.bot_guid '
           'WHERE ch.guid IS NULL;' % (t, ch))
    owners, err = _scalar(
        c, "SELECT GROUP_CONCAT(DISTINCT ch.name ORDER BY ch.name SEPARATOR ', ') "
           "FROM %s r JOIN %s.characters ch ON ch.guid=r.owner_guid;" % (t, ch))
    out['owners'] = None if (err or owners == 'NULL') else owners
    return out


def raid_build(slug, cmd_id, args):
    c = ctx(slug)
    if not c:
        return False, ['unknown realm: %s' % slug], None
    if not os.path.isfile(os.path.join(c['modconf'], 'mod_raid_roster.conf')):
        return False, ['%s does not run mod-raid-roster' % c['name']], None
    # No SOAP branch, same as bots: every .raidroster handler is declared
    # Console::Yes but bails with "Run this in-world as a player", so a console
    # session has nothing to run.
    cmd, err = C.raid_build(cmd_id, args or {})
    if cmd is None:
        return False, [err], None
    return True, ['paste this in game chat:', cmd], cmd


# ------------------------------------------------------------------- overview

def overview(slug):
    """The old dashboard, scoped: process state plus this realm's own numbers."""
    c = ctx(slug)
    if not c:
        return None
    world, auth = C.running('worldserver'), C.running('authserver')
    oll = C.running('ollama')
    oll['up'] = C.ollama_alive()
    mine = bool(c['active'])
    out = {
        'slug': slug, 'name': c['name'], 'active': c['active'],
        'serverDir': c['server'], 'logDir': c['logs'],
        'controls': c['controls'],
        'mysql': {'up': C.mysql_alive()},
        'ollama': oll,
        # Process rows describe the hub's single running world.  They belong to
        # whichever realm is active, so they are reported as this realm's only
        # when it is the active one - otherwise the panel would show an idle realm
        # "up" because another realm's worldserver.exe is in the process list.
        'authserver': auth if mine else {'up': False},
        'worldserver': world if mine else {'up': False},
        'ready': (R.ready(c['realm']) if (mine and world['up']) else False),
        'population': population(c) if (mine and world['up']) else
                      {'bots': None, 'players': None},
        # Listening ports are the hub's, not a realm's: reporting them for an
        # inactive profile would show its auth port "open" because the OTHER
        # realm is holding it.
        'ports': ({'auth': C.port_open(3724), 'world': C.port_open(8085),
                   'soap': C.port_open(7878)} if mine else {}),
    }
    return out


# --------------------------------------------------------------------- routing
# Mounted under /v1/realm/<slug>/... so it can never collide with the launcher's
# own /v1/products and /v1/patch trees.

def handle_get(h, path, query):
    parts = [s for s in path.strip('/').split('/') if s]
    if len(parts) < 4 or parts[0] != 'v1' or parts[1] != 'realm':
        return False
    slug, tail = parts[2], parts[3]
    if tail == 'overview':
        d = overview(slug)
    elif tail == 'settings':
        d = settings(slug)
    elif tail == 'rates':
        d = rates(slug)
    elif tail == 'trainer':
        d = trainer(slug)
    elif tail == 'bots':
        d = bots(slug)
    elif tail == 'raid':
        d = raid(slug)
    elif tail == 'log':
        d = log(slug, int((query.get('n') or ['200'])[0]))
    else:
        return False
    h._json(d if d is not None else {'error': 'unknown realm'},
            200 if d is not None else 404)
    return True


def handle_post(h, path, body):
    parts = [s for s in path.strip('/').split('/') if s]
    if len(parts) < 4 or parts[0] != 'v1' or parts[1] != 'realm':
        return False
    slug, tail = parts[2], parts[3]
    if tail == 'setting':
        ok, log_ = set_setting(slug, str(body.get('key', '')), body.get('value', ''))
    elif tail == 'phase':
        ok, log_ = set_phase(slug, str(body.get('phase', '')))
    elif tail == 'rate':
        ok, log_ = set_rate(slug, str(body.get('key', '')), body.get('value'))
    elif tail == 'questdrops':
        ok, log_ = questdrops(slug, str(body.get('action', '')))
    elif tail == 'spawnrates':
        ok, log_ = spawnrates(slug, str(body.get('cat', '')), body.get('speed', 1))
    elif tail == 'trainer':
        ok, log_, copy = trainer_run(slug, str(body.get('id', '')), body.get('args') or {})
        h._json({'ok': ok, 'log': log_, 'copy': copy})
        return True
    elif tail == 'bots':
        ok, log_, copy = bots_build(slug, str(body.get('id', '')), body.get('args') or {})
        h._json({'ok': ok, 'log': log_, 'copy': copy})
        return True
    elif tail == 'raid':
        ok, log_, copy = raid_build(slug, str(body.get('id', '')), body.get('args') or {})
        h._json({'ok': ok, 'log': log_, 'copy': copy})
        return True
    else:
        return False
    h._json({'ok': ok, 'log': log_})
    return True
