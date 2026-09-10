"""The one document that says what is running.

Four things used to answer "is the realm up?" independently - control.status() from
vanilla's own paths, panel.overview() from the registry's activeRealm flag,
launcher.products() from a bare process-name lookup, and switch-realm.py --list from
a fourth - and they disagreed at exactly the edges that matter: a SpellDraft world
showing as vanilla and up, a realm judged ready off the other realm's Server.log, a
count of 0 that was really a MySQL that could not be reached. This module is the
single producer. Everything else reads it.

Four rules, and they ARE the contract:

  1. ONE PRODUCER. snapshot() decides what "up" means. A caller that needs a
     different question answered adds a field here rather than asking Windows itself,
     because a second opinion is how the old disagreements got in.

  2. "UP" IS AN IMAGE PATH. A realm is running when a process whose EXE lives under
     that realm's own server directory is alive. Not when a process of the right NAME
     is alive - every profile ships its own worldserver.exe. Not when the registry
     says the realm is active - activeRealm is a statement of intent, written by the
     launcher, and a realm that died overnight leaves it untouched.

  3. null MEANS "NOT ASKED, OR COULD NOT BE READ". It is never 0 and never false.
     This one is load-bearing: an unreadable online count reported as 0 is what would
     let a force-kill decide a world had saved when nothing was known about it, and
     the same confusion has already been fixed twice in control.py.

  4. DISAGREEMENT IS DATA. Where the registry and the machine differ - the active
     realm is not the running one, two realms are up at once, a port a realm needs is
     held by somebody else - the finding goes in `conflicts` as a sentence, and the
     numeric fields still report what was actually observed. Nothing here quietly
     picks a winner.

Scope: the server side. Clients (kind != 'realm') are the launcher's product list,
not this - they have no up/down worth reporting here.

SHAPE - schema 1. Fields are added, never repurposed; a reader may ignore what it
does not know, and must not assume a field it does know has changed meaning.

    schema        int          1
    generated     str          local timestamp, for display
    epoch         int          seconds, for age checks
    root          str          hub root
    activeRealm   str|null     what profiles.json CLAIMS
    runningRealm  str|null     what the process table SHOWS; null for none, and
                               null for more than one - see `conflicts`
    services      {mysql, ollama}
                  mysql        {up, pid|null, ramMb|null}   `up` is a real query
                  ollama       {up, api, pid|null, ramMb|null}
                               `up` IS `api`, because "ollama app.exe" can sit in
                               the process list with a dead API and show a healthy
                               panel over silent bots
    host          {ramTotalMb, ramFreeMb}|null   only when asked for
    realms        [realm]      registry order
    conflicts     [str]        empty is the happy path

    realm:
    slug name kind status order    from the registry, verbatim
    installed    bool         worldserver.exe exists in serverDir
    usesAuthserver bool       false when the profile's auth is served by a helper,
                              in which case a missing authserver is not a fault
    active       bool         registry claim
    running      bool         OBSERVED - a worldserver from serverDir is alive
    ready        bool|null    null unless running
    serverDir logDir worldConf
    processes    {worldserver, authserver}
                 each {up, pid|null, ramMb|null, exe|null, others}
                 `others` counts same-named processes belonging to some other
                 profile. Nonzero with up=false is the signature of "another realm
                 is holding your ports"
    ports        {auth, world, soap, <helper name>...}
                 each {port, open, pid|null, mine}
                 `mine` = the listener is one of THIS realm's processes. Open
                 without mine is what blocks a start, and the whole reason the pid
                 is reported at all
    population   {online, players, bots}   any may be null; only asked when running
    blockedBy    str|null     why this realm cannot start right now, or null. Held
                              ports are the usual answer, and while exactly one realm
                              can hold 3724/8085 that is the NORMAL state of every
                              idle profile - which is why it lives here as a property
                              of the realm and not in `conflicts`
"""

import datetime
import os
import time

SCHEMA = 1

C = None
R = None


def bind(control_module, realms_module):
    """Same contract as realms.bind(): the caller owns the module objects.

    control.py runs as __main__, so importing it here would build a second module
    object with its own globals and its own idea of ROOT.
    """
    global C, R
    C = control_module
    R = realms_module


# ---------------------------------------------------------------- primitives

def listening_ports():
    """{port: owning pid} for every listening TCP socket, in one query.

    The pid is the point. "8085 is open" cannot tell a realm that is up from a realm
    that cannot start, and those are the two states anyone reading a status page
    actually needs to tell apart.
    """
    _rc, out = C.ps("Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
                    "ForEach-Object { \"$($_.LocalPort)|$($_.OwningProcess)\" }")
    m = {}
    for line in (out or '').splitlines():
        bits = line.strip().split('|')
        if len(bits) == 2 and bits[0].isdigit() and bits[1].isdigit():
            m.setdefault(int(bits[0]), int(bits[1]))
    return m


def host_memory():
    """{ramTotalMb, ramFreeMb}, or None if it could not be read.

    Off by default: it is a third PowerShell round trip and most callers do not need
    it. It exists because this box runs 1000 playerbots and a local LLM on 32 GB, and
    "the whole computer became unusable" has had a memory answer before.
    """
    _rc, out = C.ps("$o = Get-CimInstance Win32_OperatingSystem; "
                    "\"$([int]($o.TotalVisibleMemorySize/1KB))|"
                    "$([int]($o.FreePhysicalMemory/1KB))\"")
    for line in (out or '').splitlines():
        bits = line.strip().split('|')
        if len(bits) == 2 and bits[0].isdigit() and bits[1].isdigit():
            return {'ramTotalMb': int(bits[0]), 'ramFreeMb': int(bits[1])}
    return None


def _row(rows, exe_dir):
    """One process row for a realm, from an already-fetched procs_many() list.

    The same image-path rule control.running() applies, applied to rows already in
    hand so a whole snapshot costs one CIM query rather than one per realm per
    process.
    """
    mine = [p for p in rows if C._under(p['exe'], exe_dir)] if exe_dir else list(rows)
    if mine:
        p = mine[0]
        return {'up': True, 'pid': p['pid'], 'ramMb': p['ram_mb'], 'exe': p['exe'],
                'others': len(rows) - len(mine)}
    return {'up': False, 'pid': None, 'ramMb': None, 'exe': None,
            'others': len(rows) - len(mine)}


def _int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


# ---------------------------------------------------------------- per realm

def _ports(realm, listen, my_pids):
    aconf = os.path.join(R.conf_dir(realm) or '', 'authserver.conf')
    wconf = R.world_conf(realm)
    spec = [('auth', C.conf_get(aconf, 'RealmServerPort') or 3724),
            ('world', C.conf_get(wconf, 'WorldServerPort') or 8085),
            ('soap', C.conf_get(wconf, 'SOAP.Port') or 7878)]
    # A helper is identified by its PORT, never by process name: every one of them is
    # python.exe, so a name check cannot tell the Ascension auth shim from the world
    # bridge, or from an unrelated script that happens to be running.
    for h in R.helpers(realm):
        if h.get('port'):
            spec.append((h.get('name') or 'helper', h['port']))
    out = {}
    for label, port in spec:
        port = _int(port)
        if port is None:
            continue
        owner = listen.get(port)
        out[label] = {'port': port, 'open': owner is not None, 'pid': owner,
                      'mine': bool(owner) and owner in my_pids}
    return out


def _population(realm):
    """{online, players, bots} for a realm, in ONE query, as ITS OWN MySQL user.

    Bots are the RNDBOT accounts; AHBOT and PANELGM are ours and are neither. Asked
    as the profile's user because `acore` has no grant outside acore_* - see
    realms.online_counter for the same reason at greater length.
    """
    blank = {'online': None, 'players': None, 'bots': None}
    dbcfg = realm.get('db') or {}
    ch, au = dbcfg.get('characters'), dbcfg.get('auth')
    if not ch or not au:
        return blank
    sql = ("SELECT COUNT(*), "
           "SUM(a.username LIKE 'RNDBOT%%'), "
           "SUM(a.username NOT LIKE 'RNDBOT%%' AND a.username NOT IN ('AHBOT','PANELGM')) "
           "FROM `%s`.characters c JOIN `%s`.account a ON a.id = c.account "
           "WHERE c.online = 1;" % (ch, au))
    row, err = R.scalar_as(dbcfg.get('user', 'acore'), sql, conf=R.world_conf(realm))
    if row is None:
        return dict(blank, error=err)
    bits = row.split('\t')
    if len(bits) != 3:
        return dict(blank, error='unexpected result %r' % row)
    online = _int(bits[0])
    # SUM() over no rows is NULL, not 0. On an empty realm that is a true zero and not
    # an unknown - the one place a null must NOT be passed through.
    def parse(v):
        return 0 if online == 0 else _int(v)
    return {'online': online, 'bots': parse(bits[1]), 'players': parse(bits[2])}


# ---------------------------------------------------------------- snapshot

def snapshot(include_host=False, include_population=True):
    """The whole document.

    Two PowerShell round trips, plus one MySQL query per RUNNING realm; nothing is
    asked of a realm that is not up, and every field it would have filled stays null
    rather than becoming a zero.
    """
    doc = R.load()
    pm = C.procs_many(['worldserver', 'authserver', 'mysqld', 'ollama'])
    listen = listening_ports()

    out = {
        'schema': SCHEMA,
        'generated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'epoch': int(time.time()),
        'root': C.ROOT,
        'activeRealm': doc.get('activeRealm'),
        'runningRealm': None,
        'services': {}, 'host': None, 'realms': [], 'conflicts': [],
    }
    if doc.get('error'):
        out['conflicts'].append(doc['error'])

    my = (pm.get('mysqld') or [None])[0]
    ol = (pm.get('ollama') or [None])[0]
    alive = C.ollama_alive()
    out['services'] = {
        'mysql': {'up': C.mysql_alive(),
                  'pid': my['pid'] if my else None,
                  'ramMb': my['ram_mb'] if my else None},
        # Reachability, not presence: "ollama app.exe" can sit in the process list
        # with the API dead, which showed a healthy panel while bot chat was silent.
        'ollama': {'up': alive, 'api': alive,
                   'pid': ol['pid'] if ol else None,
                   'ramMb': ol['ram_mb'] if ol else None},
    }
    if include_host:
        out['host'] = host_memory()

    up_realms = []
    for r in sorted(doc.get('realms', []), key=lambda x: x.get('order', 99)):
        if r.get('kind') != 'realm':
            continue
        srv = R.server_dir(r)
        world = _row(pm.get('worldserver') or [], srv)
        auth = _row(pm.get('authserver') or [], srv)
        pids = set(p['pid'] for p in (world, auth) if p['pid'])
        entry = {
            'slug': r.get('slug'), 'name': r.get('name', r.get('slug')),
            'kind': r.get('kind'), 'status': r.get('status'), 'order': r.get('order'),
            'installed': R.installed(r),
            'active': doc.get('activeRealm') == r.get('slug'),
            'running': world['up'],
            'ready': None,
            # A profile whose auth is served by a helper has no authserver.exe at
            # all, so "world up, auth down" is its normal healthy state and must
            # not be reported as a fault.
            'usesAuthserver': (r.get('world') or {}).get('authserver') is not False,
            'serverDir': srv, 'logDir': R.log_dir(r), 'worldConf': R.world_conf(r),
            'processes': {'worldserver': world, 'authserver': auth},
            'ports': _ports(r, listen, pids),
            'population': {'online': None, 'players': None, 'bots': None},
            'blockedBy': None,
        }
        if world['up']:
            up_realms.append(r.get('slug'))
            entry['ready'] = R.ready(r)
            if include_population:
                entry['population'] = _population(r)
        out['realms'].append(entry)

    # More than one is not a winner to pick, it is a conflict to report.
    out['runningRealm'] = up_realms[0] if len(up_realms) == 1 else None
    _blockers(out)
    out['conflicts'].extend(_conflicts(out, up_realms))
    return out


def _blockers(out):
    """Fill each idle realm's blockedBy: what is holding the ports it needs.

    Named rather than merely detected. "Port 8085 is in use" sends you to netstat;
    "held by pid 14452, vanilla's worldserver" is the whole answer, and while every
    profile binds the same three ports it is the answer for every idle realm on the
    box - normal, expected, and not a fault.
    """
    owner = {}
    for r in out['realms']:
        for p in r['processes'].values():
            if p['pid']:
                owner[p['pid']] = r['slug']
    for r in out['realms']:
        if r['running']:
            continue
        held = [(label, p) for label, p in sorted(r['ports'].items())
                if label in ('auth', 'world') and p['open']]
        if not held:
            continue
        r['blockedBy'] = '; '.join(
            '%s port %d is held by pid %s%s'
            % (label, p['port'], p['pid'],
               ' (%s)' % owner[p['pid']] if p['pid'] in owner else '')
            for label, p in held)


def _conflicts(out, up_realms):
    """Everywhere the registry and the machine disagree, one sentence each.

    Deliberately separate from the numbers: a caller drawing a dashboard ignores this
    list, and a caller explaining why a start failed reads nothing else.
    """
    found = []
    active = out.get('activeRealm')
    if len(up_realms) > 1:
        found.append('%d realms are running at once (%s). The ports are shared, so at '
                     'most one of them can be reachable.'
                     % (len(up_realms), ', '.join(up_realms)))
    if up_realms and active not in up_realms:
        found.append('profiles.json says the active realm is %s, but the world that is '
                     'running belongs to %s - a switch that did not finish, or a realm '
                     'started by hand.' % (active or '(none)', ', '.join(up_realms)))
    for r in out['realms']:
        w = r['processes']['worldserver']
        a = r['processes']['authserver']
        if r['usesAuthserver'] and w['up'] and not a['up'] and not (a['others'] or 0):
            found.append('%s: worldserver is up but authserver is not. Characters are '
                         'safe; nobody new can log in.' % r['slug'])
        for label, p in sorted(r['ports'].items()):
            if r['running'] and label in ('auth', 'world') and p['open'] and not p['mine']:
                found.append('%s: port %d (%s) is held by pid %s, which is not this '
                             'realm.' % (r['slug'], p['port'], label, p['pid']))
    return found
