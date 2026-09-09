"""Realm profile lifecycle - the engine behind the launcher's Switch action and
tools/switch-realm.py.

Every profile binds 3724 / 8085 / 7878 on 127.0.0.1, so exactly one realm runs at
a time and the client's realmlist.wtf never changes. "Switching" therefore means:
stop whatever is running - gracefully, so characters are saved - then start the
target profile's own binaries, from its own directory, against its own configs
and its own databases.

Why this is not in control.py: that module's start_all/stop_all are bound to one
SERVER_DIR and query the character database by a fixed name. Pointing them at
another profile would mean threading a profile through all of them, and a
half-threaded version is worse than none - it would start the default realm's
binaries while the panel named a different one. So this module takes the profile
as data and reuses only control.py's process primitives:

  * spawn_detached - hides the console WINDOW but keeps the console OBJECT,
    which is the only reason a later CTRL_BREAK can be delivered at all;
  * stop_graceful  - CTRL_BREAK -> World::StopNow, which SAVES every character.

Neither is reimplemented here. Getting either subtly wrong loses progress.

control.py runs as __main__, so `import control` from here would build a SECOND
module object with its own globals. Callers pass the real one to bind() instead -
the same contract launcher.py uses.
"""

import json
import os
import re
import shutil
import subprocess
import time

C = None
ROOT = REGISTRY = CREDS = None

# Each profile is reached as its OWN MySQL user. control.mysql_scalar() hardcodes
# `acore`, which by design has no grant outside acore_* - using it to check an
# sd_* table fails and reads as "MySQL not answering" when MySQL is perfectly
# healthy. Map each non-default user to its credentials.txt label; the password
# is read at call time, passed via MYSQL_PWD, and never logged or echoed.
# Maps a MySQL user to the line that holds its password in credentials.txt,
# for realms whose user is NOT declared in their own worldserver.conf. Most
# installs need none of this - the config is consulted first. Add an entry
# only for a realm you reach as a user the server itself does not use.
CRED_LABEL = {}


def bind(control_module):
    global C, ROOT, REGISTRY, CREDS
    C = control_module
    ROOT = C.ROOT
    REGISTRY = os.path.join(ROOT, 'realms', 'profiles.json')
    CREDS = os.path.join(ROOT, 'credentials.txt')


# ---------------------------------------------------------------- registry

def hub(p):
    """Registry paths are hub-relative unless already absolute."""
    if not p:
        return None
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(ROOT, p.replace('/', os.sep)))


def load():
    try:
        with open(REGISTRY, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        # A missing or malformed registry must not take the panel down - it is
        # reported as data so the UI can say so instead of returning a 500.
        return {'schema': 0, 'activeRealm': None, 'realms': [],
                'error': 'profiles.json unavailable: %s' % e}


def save(doc):
    # Write through a temp file: a half-written registry would strand both the
    # launcher and the CLI, and this is the one file they genuinely share.
    os.makedirs(os.path.dirname(REGISTRY), exist_ok=True)
    tmp = REGISTRY + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write('\n')
    os.replace(tmp, REGISTRY)


def find(doc, slug):
    return next((r for r in doc.get('realms', []) if r.get('slug') == slug), None)


def installed(realm):
    srv = hub((realm.get('paths') or {}).get('server'))
    return bool(srv) and os.path.exists(os.path.join(srv, 'worldserver.exe'))


def server_dir(realm):
    return hub((realm.get('paths') or {}).get('server'))


def log_dir(realm):
    paths = realm.get('paths') or {}
    world = realm.get('world') or {}
    # world.logs wins: a profile that starts a non-default config writes wherever
    # that config's LogsDir points, and readiness is judged from that file. Naming
    # the wrong directory does not fail loudly - the previous run's 'Halting
    # process' is still sitting in the other log, so the panel reads a world that
    # is up and ready as "maps still loading" forever.
    return (hub(world.get('logs')) or hub(paths.get('logs'))
            or os.path.join(server_dir(realm) or '', 'logs'))


def conf_dir(realm):
    paths = realm.get('paths') or {}
    return hub(paths.get('configs')) or os.path.join(server_dir(realm) or '', 'configs')


def world_conf(realm):
    """The config worldserver is actually started with.

    Defaults to the profile's own configs/worldserver.conf. A profile may name a
    different file instead, which is how one server directory can back two realms
    that need different settings - a second world on another port so it can run
    beside the first, a DataDir pointing at a different extraction, or a test
    realm with Warden and the overspeed kick turned off. Without this the binaries
    silently use the config sitting next to them, and the realm comes up as the
    wrong one.
    """
    world = realm.get('world') or {}
    return hub(world.get('conf')) or os.path.join(conf_dir(realm) or '', 'worldserver.conf')


def python_exe(realm):
    """Interpreter for this profile's helper scripts, or None if none is found.

    Deliberately NOT sys.executable: when the panel runs from the frozen exe,
    sys.executable IS that exe, so using it would relaunch the panel rather than
    run a script.
    """
    world = realm.get('world') or {}
    named = hub(world.get('python'))
    if named and os.path.exists(named):
        return named
    found = []
    # A per-user install is often absent from the PATH a detached process
    # inherits, so look where the official Windows installer puts it, newest
    # first, before falling back to PATH.
    base = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Python')
    if os.path.isdir(base):
        found += [os.path.join(base, d, 'python.exe')
                  for d in sorted(os.listdir(base), reverse=True)]
    found += [shutil.which('python'), shutil.which('py')]
    # WindowsApps holds a stub that opens the Microsoft Store instead of running
    # anything, and it is on PATH by default. Spawning it looks like a launch that
    # worked and never listens on the port.
    return next((c for c in found
                 if c and 'WindowsApps' not in c and os.path.exists(c)), None)


def helpers(realm):
    """Extra processes this profile's realm needs, beside the two servers."""
    return (realm.get('world') or {}).get('helpers') or []


def _pid_on_port(port):
    """PID of whatever is listening on `port`, or None."""
    _rc, out = C.ps("(Get-NetTCPConnection -LocalPort %d -State Listen "
                    "-ErrorAction SilentlyContinue | Select-Object -First 1)"
                    ".OwningProcess" % port)
    for line in reversed((out or '').strip().splitlines()):
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


# ---------------------------------------------------------------- database

def db_password(user, conf=None):
    """The password for a MySQL user, preferring the realm's own config.

    Order matters.  A realm's worldserver.conf carries the credentials the
    SERVER actually connects with, so it is right even when the hub was never
    told about this install - which is the normal case for someone pointing the
    panel at a server they already had.  credentials.txt is the fallback for a
    user the config does not mention, and the stock acore/acore is the last
    resort so a freshly cloned, never-configured tree still answers.
    """
    if conf:
        for which in ('world', 'login', 'character'):
            d = C.dbinfo(which, conf)
            if d.get('user') == user and d.get('pass'):
                return d['pass']
    label = CRED_LABEL.get(user)
    if label:
        try:
            with open(CREDS, 'r', encoding='utf-8', errors='replace') as f:
                m = re.search(re.escape(label) + r':\s*(.+)', f.read())
            if m:
                return m.group(1).strip()
        except Exception:
            pass
    return 'acore' if user == 'acore' else None


def scalar_as(user, sql, timeout=12, conf=None):
    """One-value query as `user`. Returns (value, error); value is None on failure.

    The error is returned rather than swallowed so callers can tell "MySQL is
    down" apart from "this user is not allowed to look" - the second is the
    grant fence working, not a fault.
    """
    if not C.MYSQL:
        return None, 'mysql client not found'
    pw = db_password(user, conf)
    if pw is None:
        return None, 'no password known for MySQL user %r' % user
    env = dict(os.environ, MYSQL_PWD=pw)
    # Host and port come from the same config as the password. A stack whose
    # MySQL is a container on a mapped port is reached correctly; assuming
    # 127.0.0.1:3306 would just time out with nothing to point at.
    d = C.dbinfo('world', conf) if conf else C.DB_FALLBACK
    try:
        p = subprocess.run([C.MYSQL, '--host=' + d['host'], '--port=' + str(d['port']),
                            '--user=' + user,
                            '--batch', '--skip-column-names', '-e', sql],
                           capture_output=True, timeout=timeout, env=env,
                           creationflags=C.NO_WINDOW)
    except Exception as e:
        return None, str(e)
    if p.returncode != 0:
        err = p.stderr.decode('utf-8', 'replace').strip().splitlines()
        return None, (err[-1] if err else 'exit %d' % p.returncode)
    for line in p.stdout.decode('utf-8', 'replace').splitlines():
        line = line.strip()
        if line and not line.startswith('mysql:'):
            return line, None
    return None, 'empty result'


# ---------------------------------------------------------------- readiness

def ready(realm):
    """Has this profile's world finished loading?

    Profile-aware twin of control.world_ready(), which reads the vanilla log and
    config. The log marker is the fast path; Server.log is chatty enough that
    'ready...' scrolls out of a tail within minutes, so the world port is the
    fallback authority - worldserver only listens once loading is done.
    """
    path = os.path.join(log_dir(realm) or '', 'Server.log')
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            tail = f.readlines()[-400:]
    except Exception:
        tail = []
    for line in reversed(tail):
        if 'ready...' in line:
            return True
        if 'Halting process' in line:
            return False
    if not C.running('worldserver')['up']:
        return False
    port = C.conf_get(world_conf(realm), 'WorldServerPort') or 8085
    return C.port_open(port)


# ---------------------------------------------------------------- lifecycle

def _noop(_line):
    pass


def verify_saved(realm, emit=_noop):
    """A clean shutdown clears characters.online. Leftovers mean it did not save."""
    dbcfg = realm.get('db') or {}
    db = dbcfg.get('characters')
    if not db:
        return
    user = dbcfg.get('user', 'acore')
    n, err = scalar_as(user, 'SELECT COUNT(*) FROM `%s`.characters WHERE online=1;' % db)
    if n is None:
        emit('could not verify %s as %s: %s' % (db, user, err))
    elif n.strip() == '0':
        emit('verified: every character in %s is flushed and offline' % db)
    else:
        emit('WARNING: %s characters still flagged online in %s' % (n, db))


def stop_realm(realm, emit=_noop):
    """Graceful stop of whichever realm owns the ports. Never force-kills.

    The two servers are found by name, since only one realm can be running.
    `realm` decides which database to verify against and which helper scripts
    belong to it - a profile with none is unaffected.
    """
    for name in ('worldserver', 'authserver'):
        if not C.running(name)['up']:
            emit('%s: not running' % name)
            continue
        emit(C.stop_graceful(name, wait_s=300 if name == 'worldserver' else 60))
    if realm:
        stop_helpers(realm, emit)
        verify_saved(realm, emit)


def start_realm(realm, emit=_noop):
    """Start this profile's auth and world servers from its own directory."""
    srv = server_dir(realm)
    if not srv or not os.path.isdir(srv):
        emit('no server directory for %s' % realm.get('slug'))
        return False

    if not C.mysql_alive():
        emit('MySQL is down - starting it')
        emit(C.start_mysql())

    # Ollama backs playerbot chat, and only mod-ollama-chat ever speaks to it.
    # botsAware is the wrong test: a realm can run playerbots without that
    # module, and starting Ollama for it costs VRAM and up to 40s of waiting on
    # a service nothing will call.
    if os.path.isfile(os.path.join(conf_dir(realm), 'modules', 'mod_ollama_chat.conf')):
        emit(C.start_ollama())

    world = realm.get('world') or {}

    # A profile whose auth is served by a helper has no authserver.exe to start.
    # Starting one anyway would take 3724 from the other realms to run a process
    # nothing in this profile talks to.
    names = ['authserver', 'worldserver']
    if world.get('authserver') is False:
        names.remove('authserver')
        emit('authserver: not used by this profile - auth is served by a helper')

    ok = True
    for name in names:
        exe = os.path.join(srv, name + '.exe')
        if not os.path.exists(exe):
            emit('%s: EXE MISSING (%s)' % (name, exe))
            ok = False
            continue
        if C.running(name)['up']:
            emit('%s: already running' % name)
            continue
        args = ''
        if name == 'worldserver':
            # Readiness is judged from this file, and a stale one from the last
            # run reads as "ready" for a server that has not started yet.
            try:
                os.remove(os.path.join(log_dir(realm), 'Server.log'))
            except Exception:
                pass
            conf = world_conf(realm)
            if world.get('conf'):
                if not os.path.exists(conf):
                    emit('worldserver: CONFIG MISSING (%s)' % conf)
                    ok = False
                    continue
                # Passed explicitly rather than relied on by name: worldserver
                # defaults to configs/worldserver.conf next to the exe, which for
                # this profile is the config it must NOT use.
                args = '-c "%s"' % conf
                emit('worldserver: config %s' % conf)
        spawned, detail = C.spawn_detached(exe, srv, args=args)
        emit('%s: %s (%s)' % (name, 'launched' if spawned else 'FAILED TO LAUNCH', detail))
        if spawned:
            # A worldserver that dies during "Initialize Data Stores" - a missing
            # DBC-backed table, say - exits within a second or two. Without this
            # check the launch reads as successful and the failure only surfaces
            # minutes later as a world that never becomes ready.
            time.sleep(3)
            if not C.running(name)['up']:
                emit('%s: WARNING - died within seconds of launching; see %s'
                     % (name, os.path.join(log_dir(realm) or srv, 'Server.log')))
                spawned = False
        ok = ok and spawned

    # Helpers last, and only if the world came up. Started on their own they are
    # worse than nothing: an auth helper would let the client log in and reach the
    # realm list, and whatever sits behind it would then find no world and drop
    # the session - which reads as a broken login rather than a world that failed.
    if ok and helpers(realm):
        ok = start_helpers(realm, emit) and ok
    return ok


def start_helpers(realm, emit=_noop):
    """Start this profile's helper scripts, skipping any already listening."""
    py = python_exe(realm)
    if not py:
        emit('helpers: NO PYTHON FOUND - cannot start %s'
             % ', '.join(h.get('name', '?') for h in helpers(realm)))
        return False
    ok = True
    for h in helpers(realm):
        name = h.get('label') or h.get('name') or '?'
        port = h.get('port')
        script = hub(h.get('script'))
        if not script or not os.path.exists(script):
            emit('%s: SCRIPT MISSING (%s)' % (name, script))
            ok = False
            continue
        # Identity is the port, not the process name: every helper is python.exe,
        # so a name check cannot tell one from another - or from an unrelated
        # script the user happens to be running.
        if port and C.port_open(port):
            emit('%s: already listening on %d' % (name, port))
            continue
        args = '"%s"' % script
        if h.get('args'):
            args += ' ' + h['args']
        spawned, detail = C.spawn_detached(py, os.path.dirname(script), args=args)
        emit('%s: %s (%s)' % (name, 'launched' if spawned else 'FAILED TO LAUNCH', detail))
        if spawned and port:
            for _ in range(10):
                time.sleep(1)
                if C.port_open(port):
                    break
            else:
                emit('%s: WARNING - not listening on %d after 10s' % (name, port))
                spawned = False
        ok = ok and spawned
    return ok


def stop_helpers(realm, emit=_noop):
    """Stop this profile's helper scripts.

    Force-stopped, unlike the servers: these hold no unsaved game state, so there
    is nothing for a graceful signal to flush. They are stopped on the way out
    because a helper left running after its world is gone can still accept a
    connection and then drop it, which is a far more confusing failure than a
    refused one.
    """
    for h in helpers(realm):
        name = h.get('label') or h.get('name') or '?'
        port = h.get('port')
        pid = _pid_on_port(port) if port else None
        if not pid:
            emit('%s: not running' % name)
            continue
        C.ps('Stop-Process -Id %d -Force -ErrorAction SilentlyContinue' % pid)
        emit('%s: stopped (pid %d)' % (name, pid))


def switch_to(slug, emit=_noop):
    """Stop the running realm, point the registry at `slug`, start it.

    activeRealm is written between the stop and the start, not after it. If the
    start then fails, the registry still describes the realm that owns the ports,
    so the panel offers "Start realm" for the right profile instead of quietly
    aiming the next action at the one that is no longer running.
    """
    doc = load()
    if doc.get('error'):
        return False, [doc['error']]
    target = find(doc, slug)
    if not target:
        return False, ['no such profile: %s' % slug]
    if target.get('kind') != 'realm':
        return False, ["'%s' is a client, not a realm - launch it against a running realm" % slug]
    if not target.get('enabled'):
        return False, ["profile '%s' is disabled in profiles.json (status: %s)"
                       % (slug, target.get('status'))]
    if not installed(target):
        return False, ["profile '%s' is not installed - no worldserver.exe under %s"
                       % (slug, server_dir(target))]

    log = []

    def say(line):
        log.append(str(line))
        emit(str(line))

    # Switching to the realm that is already up would stop and restart it, which
    # kicks anyone playing for no gain. Asking for the current realm means "make
    # sure this one is running", so only start what is missing.
    if doc.get('activeRealm') == slug and C.running('worldserver')['up']:
        say('%s is already the running realm' % target.get('name', slug))
        return True, log

    current = find(doc, doc.get('activeRealm'))
    if C.running('worldserver')['up'] or C.running('authserver')['up']:
        say('Stopping %s ...' % ((current or {}).get('name') or 'the running realm'))
        stop_realm(current, say)

    doc['activeRealm'] = slug
    save(doc)
    say('active realm is now %s' % slug)

    say('Starting %s from %s' % (target.get('name', slug), server_dir(target)))
    ok = start_realm(target, say)
    if ok:
        say('%s is starting - the world takes a few minutes to load.' % target.get('name', slug))
    return ok, log
