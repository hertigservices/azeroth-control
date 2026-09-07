"""Azeroth Control - local one-stop panel for the AzerothCore realm.

Serves the UI and exposes process control on 127.0.0.1. A published Artifact is
sandboxed and cannot touch local processes, so anything that starts/stops the
server has to be served from the machine itself.

Portable by design: every path derives from ROOT (this script's parent folder),
and MySQL runs as an in-tree process rather than a Windows service - the service
hardcoded an absolute basedir, which was the last thing pinning the install to
C:\\Program Files. Move the hub anywhere and this still works.
"""
import http.server, socketserver, json, os, re, socket, subprocess, threading, time, urllib.parse, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER_DIR = os.path.join(ROOT, 'server')
LOG_DIR = os.path.join(SERVER_DIR, 'logs')
CONF_DIR = os.path.join(SERVER_DIR, 'configs')
MOD_CONF = os.path.join(CONF_DIR, 'modules')
MYSQL_DIR = os.path.join(ROOT, 'mysql')
MYSQL_BIN = os.path.join(MYSQL_DIR, 'bin')
MY_INI = os.path.join(MYSQL_DIR, 'my.ini')
OLLAMA_DIR = os.path.join(ROOT, 'ollama')
CREDS = os.path.join(ROOT, 'credentials.txt')
PORT = int(os.environ.get('AZCTL_PORT', '8750'))

NO_WINDOW = 0x08000000
DETACHED = 0x00000008 | 0x00000010   # DETACHED_PROCESS | CREATE_NEW_CONSOLE


def _first(*paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return ''

MYSQL = _first(os.path.join(MYSQL_BIN, 'mysql.exe'), shutil.which('mysql'))
MYSQLD = _first(os.path.join(MYSQL_BIN, 'mysqld.exe'))
MYSQLADMIN = _first(os.path.join(MYSQL_BIN, 'mysqladmin.exe'))
OLLAMA = _first(os.path.join(OLLAMA_DIR, 'bin', 'ollama.exe'),
                os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Ollama', 'ollama.exe'),
                shutil.which('ollama'))
OLLAMA_APP = _first(os.path.join(OLLAMA_DIR, 'bin', 'ollama app.exe'))
OLLAMA_MODELS = os.path.join(OLLAMA_DIR, 'models')


def root_password():
    try:
        with open(CREDS, 'r', encoding='utf-8', errors='replace') as f:
            m = re.search(r'MySQL root password:\s*(.+)', f.read())
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return None


def ps(script, need_console=False):
    """Run PowerShell and return (rc, stdout).

    need_console=True omits CREATE_NO_WINDOW. The graceful-stop script calls
    AttachConsole/GenerateConsoleCtrlEvent, which require the caller to own a
    console - with NO_WINDOW there isn't one, AttachConsole fails, and the stop
    silently does nothing while reporting success.
    """
    flags = 0 if need_console else NO_WINDOW
    p = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
                       capture_output=True, creationflags=flags)
    return p.returncode, p.stdout.decode('utf-8', 'replace').strip()


def running(name):
    rc, out = ps("$p=Get-Process -Name '%s' -ErrorAction SilentlyContinue; if($p){ "
                 "($p | Select-Object -First 1 | ForEach-Object { \"$($_.Id)|$([int]($_.WorkingSet64/1MB))\" }) } "
                 "else { '' }" % name)
    if out and '|' in out:
        pid, ram = out.split('|')[:2]
        try:
            return {'up': True, 'pid': int(pid), 'ram_mb': int(ram)}
        except ValueError:
            pass
    return {'up': False}


# AzerothCore's stock credentials. Used only when worldserver.conf cannot be
# read at all - a fresh clone that has never been configured, say.
DB_FALLBACK = {'host': '127.0.0.1', 'port': '3306', 'user': 'acore',
               'pass': 'acore', 'name': ''}
_DB_CACHE = {}


def dbinfo(which='world', conf=None):
    """Connection details for one AzerothCore database, read from the server.

    Every install declares its own connections in worldserver.conf as

        WorldDatabaseInfo = "host;port;user;pass;dbname"

    and that line is the only place the truth lives for ALL of them: a Docker
    stack with MySQL on a mapped port, a remote database, a renamed schema and a
    non-default password each show up here and nowhere else.  Reading it is why
    this panel works against a server it did not install.

    `which` is 'world', 'login' or 'character'.  Results are cached per file
    mtime, so a password changed by hand is picked up without a restart.
    """
    conf = conf or os.path.join(CONF_DIR, 'worldserver.conf')
    key = (conf, which)
    try:
        stamp = os.path.getmtime(conf)
    except OSError:
        return dict(DB_FALLBACK)
    hit = _DB_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]

    field = {'world': 'WorldDatabaseInfo', 'login': 'LoginDatabaseInfo',
             'character': 'CharacterDatabaseInfo'}.get(which, 'WorldDatabaseInfo')
    raw = conf_get(conf, field) or ''
    # The value is quoted in the shipped .conf.dist and often unquoted after a
    # hand edit, so strip quotes rather than requiring them.
    parts = [p.strip() for p in raw.strip().strip('"').strip("'").split(';')]
    out = dict(DB_FALLBACK)
    for i, k in enumerate(('host', 'port', 'user', 'pass', 'name')):
        if i < len(parts) and parts[i]:
            out[k] = parts[i]
    _DB_CACHE[key] = (stamp, out)
    return out


def mysql_cmd(which='world', database=None, conf=None):
    """(argv, env) for a batch mysql client call against one database.

    The password goes in MYSQL_PWD, not on the command line: -p leaks the
    password to every other process on the box via the argument list, and makes
    the client print a warning that then has to be filtered back out of stderr.
    """
    d = dbinfo(which, conf)
    argv = [MYSQL, '--host=' + d['host'], '--port=' + str(d['port']),
            '--user=' + d['user'], '--batch', '--skip-column-names']
    name = database if database is not None else d['name']
    if name:
        argv.append('--database=' + name)
    return argv, dict(os.environ, MYSQL_PWD=d['pass'])


def mysql_scalar(sql, timeout=12):
    if not MYSQL:
        return None
    try:
        # No --database: callers of this one qualify their tables, which is what
        # lets a single query join across auth, world and characters.
        argv, env = mysql_cmd('world', database='')
        p = subprocess.run(argv + ['-e', sql], capture_output=True,
                           timeout=timeout, env=env, creationflags=NO_WINDOW)
        for line in p.stdout.decode('utf-8', 'replace').splitlines():
            line = line.strip()
            if line and not line.startswith('mysql:'):
                return line
    except Exception:
        pass
    return None


def mysql_alive():
    return mysql_scalar('SELECT 1;', timeout=8) == '1'


def conf_get(path, key):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                s = line.strip()
                if '=' in s and s.split('=')[0].strip() == key:
                    return s.split('=', 1)[1].strip()
    except Exception:
        pass
    return None


def _documented_default(path, key):
    """Read the "Default:" line AzerothCore documents above each config key.

    Walks back from the assignment through the comment block that belongs to it;
    a non-comment, non-blank line means the previous setting has been reached, so
    one key can never inherit its neighbour's documented default.
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception:
        return None
    for i, line in enumerate(lines):
        s = line.strip()
        if '=' not in s or s.split('=')[0].strip() != key:
            continue
        for j in range(i - 1, max(-1, i - 40), -1):
            t = lines[j].strip()
            if t and not t.startswith('#'):
                break
            if 'Default:' in t:
                tail = t.split('Default:', 1)[1].split()
                if tail:
                    return tail[0]
    return None


def _is_number(v):
    try:
        float(str(v).strip())
        return True
    except Exception:
        return False


def conf_default(path, key):
    """The value this build SHIPPED for `key`, or None if it cannot be told.

    AzerothCore writes <name>.conf.dist beside every config, and the live .conf is
    that file with local edits, so the .dist assignment IS the stock default. That
    keeps this honest across upgrades - no hand-maintained table of defaults here
    to go stale the next time a module changes one.

    A hand-written module config with no .dist still carries the "Default:"
    comment, so that is the fallback. Only numeric defaults are returned: every
    control that shows one is a number box, and "Default: (empty)" would arrive as
    text that neither the slider nor the range check could use.
    """
    dist = path + '.dist'
    if os.path.isfile(dist):
        v = conf_get(dist, key)
        if _is_number(v):
            return str(v).strip()
    for p in (dist, path):
        if os.path.isfile(p):
            v = _documented_default(p, key)
            if _is_number(v):
                return str(v).strip()
    return None


def conf_set(path, key, value):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception:
        return False
    hit = False
    for i, line in enumerate(lines):
        s = line.strip()
        if not hit and '=' in s and s.split('=')[0].strip() == key:
            lines[i] = '%s = %s\n' % (key, value)
            hit = True
    if hit:
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.writelines(lines)
    return hit


def log_tail(n=180):
    p = os.path.join(LOG_DIR, 'Server.log')
    if not os.path.exists(p):
        return []
    try:
        with open(p, 'rb') as f:                 # worldserver holds this open for writing
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 220 * n))
            data = f.read()
        return [l.rstrip() for l in data.decode('utf-8', 'replace').splitlines() if l.strip()][-n:]
    except Exception as e:
        return ['(could not read log: %s)' % e]


def port_open(port, host='127.0.0.1', timeout=0.4):
    try:
        with socket.create_connection((host, int(port)), timeout):
            return True
    except Exception:
        return False


def world_ready():
    # The log marker is the fast path, but Server.log is chatty enough (Ollama
    # chat ticks, AHBot cycles) that "ready..." scrolls out of a 400-line tail
    # within minutes - after which a perfectly healthy realm read as still
    # loading, forever. Fall back to the world port, which is the authority:
    # worldserver only starts listening once the world has finished loading.
    for l in reversed(log_tail(400)):
        if 'ready...' in l:
            return True
        if 'Halting process' in l:
            return False
    if not running('worldserver')['up']:
        return False
    return port_open(conf_get(os.path.join(CONF_DIR, 'worldserver.conf'),
                              'WorldServerPort') or 8085)


def spawn_detached(exe, workdir, args='', hidden=True):
    """Win32_Process.Create escapes this process's job object, so servers outlive the panel.

    Paths go inside PowerShell SINGLE quotes: single-quoted PS strings treat both "
    and \\ literally, which avoids the escaping mess that previously produced a
    malformed command and silently started nothing.

    hidden=True passes Win32_ProcessStartup.ShowWindow = SW_HIDE(0). This hides the
    console WINDOW but the console OBJECT still exists - which matters, because the
    graceful stop attaches to that console to deliver CTRL_BREAK. Using
    CREATE_NO_WINDOW instead would remove the console entirely and silently break
    saving shutdowns. Each process also gets its own console, so a CTRL_BREAK aimed
    at worldserver can no longer take MySQL down with it.
    Returns (ok, detail).
    """
    cmdline = '"%s"' % exe
    if args:
        cmdline += ' ' + args
    if hidden:
        script = ("$si=([WMICLASS]'Win32_ProcessStartup').CreateInstance(); $si.ShowWindow=0; "
                  "$r=([WMICLASS]'Win32_Process').Create('%s','%s',$si); "
                  '"$($r.ReturnValue)|$($r.ProcessId)"') % (cmdline, workdir)
    else:
        script = ("$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                  "-Arguments @{CommandLine='%s'; CurrentDirectory='%s'}; "
                  '"$($r.ReturnValue)|$($r.ProcessId)"') % (cmdline, workdir)
    rc, out = ps(script)
    out = (out or '').strip().splitlines()[-1] if out else ''
    if '|' in out:
        ret, pid = out.split('|')[:2]
        if ret.strip() == '0' and pid.strip().isdigit():
            return True, 'pid %s' % pid.strip()
        return False, 'Win32_Process.Create returned %s' % ret.strip()
    return False, 'no response from Win32_Process.Create'


# ---------------- actions ----------------

def start_mysql():
    if mysql_alive():
        return 'MySQL: already accepting connections'
    if not MYSQLD:
        return 'MySQL: mysqld.exe not found in hub'
    ok, detail = spawn_detached(MYSQLD, MYSQL_DIR, '--defaults-file="%s"' % MY_INI)
    if not ok:
        return 'MySQL: FAILED to launch (%s)' % detail
    for _ in range(30):
        time.sleep(2)
        if mysql_alive():
            return 'MySQL: started, accepting connections (%s)' % detail
    return 'MySQL: launched (%s) but not answering yet' % detail


def ollama_alive():
    """The tray app can be running while the API is dead, so ask the API itself."""
    try:
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:11434/api/version', timeout=5):
            return True
    except Exception:
        return False


def ollama_resident(model=None):
    """Is a model already sitting in VRAM? /api/ps is the only honest answer."""
    try:
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:11434/api/ps', timeout=5) as r:
            loaded = json.loads(r.read().decode('utf-8')).get('models') or []
        if model is None:
            return bool(loaded)
        return any(m.get('name', '').startswith(model.split(':')[0]) for m in loaded)
    except Exception:
        return False


def ollama_prewarm():
    """Pull the chat model into VRAM in the background.

    Measured on this box: qwen2.5:14b is 8.82 GB and lands 100% on the 7900 XT,
    but a cold load costs 30 seconds. Without this, that 30 seconds is paid by
    whoever asks a bot the first question after a lull - the bot just says
    nothing for half a minute. Paying it at startup instead costs nobody
    anything, because startup is already a wait.

    Runs on a thread so start_all() returns immediately, and asks for a single
    token because the point is the load, not the answer.

    The thread is deliberately NOT a daemon. tools/action_start.py - what the
    tray's Start actually runs - exits the instant start_all() returns, and a
    daemon thread is killed at interpreter exit, so the prewarm was being
    cancelled mid-request every time. Non-daemon means the interpreter waits for
    it, which costs that short-lived script an invisible half minute and costs
    the long-running panel nothing.
    """
    model = conf_get(os.path.join(MOD_CONF, 'mod_ollama_chat.conf'),
                     'OllamaChat.Model') or 'qwen2.5:14b'

    if ollama_resident(model):
        return None

    def go():
        try:
            import urllib.request, json as _json
            body = _json.dumps({'model': model, 'prompt': 'hi', 'stream': False,
                                'options': {'num_predict': 1}}).encode()
            req = urllib.request.Request('http://127.0.0.1:11434/api/generate', data=body,
                                         headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=120).read()
        except Exception:
            pass  # Best effort. A failed prewarm just means the old cold start.

    threading.Thread(target=go, daemon=False).start()
    return model


def start_ollama():
    if not OLLAMA:
        return 'Ollama: not found'
    if ollama_alive():
        # The usual case. Still worth a prewarm: Ollama being up does not mean
        # the model is in VRAM, and with OLLAMA_KEEP_ALIVE=-1 the only reason it
        # would be missing is that nothing has asked for it since Ollama started.
        m = ollama_prewarm()
        return ('Ollama: already serving, loading %s into VRAM' % m) if m else                'Ollama: already serving, model resident'
    # Launch ollama.exe serve directly rather than "ollama app.exe": the tray wrapper
    # does not reliably bring the API up when started hidden, which left bot chat dead
    # while the process list looked healthy.
    ok, detail = spawn_detached(OLLAMA, os.path.dirname(OLLAMA), 'serve')
    if not ok:
        return 'Ollama: FAILED to launch (%s)' % detail
    for _ in range(20):
        time.sleep(2)
        if ollama_alive():
            m = ollama_prewarm()
            return ('Ollama: serving (%s), loading %s into VRAM' % (detail, m)) if m else                    'Ollama: serving (%s), model resident' % detail
    return 'Ollama: launched (%s) but API not answering - bot chat will be silent' % detail


def start_all():
    msgs = [start_mysql(), start_ollama()]
    for name in ('authserver', 'worldserver'):
        if running(name)['up']:
            msgs.append('%s: already running' % name)
            continue
        exe = os.path.join(SERVER_DIR, name + '.exe')
        if not os.path.exists(exe):
            msgs.append('%s: EXE MISSING (%s)' % (name, exe))
            continue
        if name == 'worldserver':
            # Drop the previous run's log: readiness is judged from this file, and a
            # stale one reads as "ready" for a server that never started.
            try:
                os.remove(os.path.join(LOG_DIR, 'Server.log'))
            except Exception:
                pass
        ok, detail = spawn_detached(exe, SERVER_DIR)
        msgs.append('%s: %s (%s)' % (name, 'launched' if ok else 'FAILED TO LAUNCH', detail))
        if ok:
            time.sleep(3)
            if not running(name)['up']:
                msgs.append('%s: WARNING - process not alive shortly after launch' % name)
    return msgs


GRACEFUL_PS = r'''
Add-Type -Namespace AzCtl -Name C -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool AttachConsole(uint p);
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool FreeConsole();
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool GenerateConsoleCtrlEvent(uint e, uint g);
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool SetConsoleCtrlHandler(IntPtr h, bool a);
'@
$p = Get-Process -Name '__NAME__' -ErrorAction SilentlyContinue
if(-not $p){ 'NOTRUNNING'; exit }
[void][AzCtl.C]::FreeConsole()
if(-not [AzCtl.C]::AttachConsole([uint32]$p.Id)){ [void][AzCtl.C]::FreeConsole(); 'NOCONSOLE'; exit }
[void][AzCtl.C]::SetConsoleCtrlHandler([IntPtr]::Zero,$true)
$ok = [AzCtl.C]::GenerateConsoleCtrlEvent(1,0)
[void][AzCtl.C]::FreeConsole()
[void][AzCtl.C]::SetConsoleCtrlHandler([IntPtr]::Zero,$false)
if($ok){ 'SENT' } else { 'FAILED' }
'''


def force_kill(name, wait_s=30):
    """Terminate a process outright. NOT a general-purpose stop.

    The only caller is the one branch of stop_graceful that has positively
    verified the process already saved everything: the shutdown marker is in the
    log AND the characters table reports nobody online. Do not call it from
    anywhere else - on a live worldserver this drops every unsaved character.
    """
    ps("Get-Process -Name '%s' -ErrorAction SilentlyContinue | Stop-Process -Force" % name)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if not running(name)['up']:
            return True
        time.sleep(1)
    return not running(name)['up']


def stop_graceful(name, wait_s=300):
    """CTRL_BREAK -> World::StopNow, which SAVES every character. Never force-kill:
    that loses everything since the last periodic save."""
    if not running(name)['up']:
        return '%s: not running' % name
    # Deliberately IGNORE the helper's stdout. The script calls FreeConsole() to
    # detach from its own console before attaching to the target's - which severs
    # its stdout, so its success message never comes back. Judging delivery from
    # that empty output produced false "signal NOT delivered" reports for stops
    # that had in fact worked. Observed process state is the only honest signal.
    ps(GRACEFUL_PS.replace('__NAME__', name), need_console=True)

    # A 1000-bot world takes minutes to drain its DB pools; exiting is the proof.
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if not running(name)['up']:
            saved = 'Halting process' in '\n'.join(log_tail(400)) if name == 'worldserver' else True
            return '%s: exited%s' % (name, ' after a clean shutdown' if saved else ' (no shutdown marker in log)')
        time.sleep(3)
    # Timing out does NOT imply the signal missed. The failure actually seen here is the
    # opposite: CTRL_BREAK lands, World::StopNow flushes every character and releases
    # MySQL, and the process THEN wedges on final exit at 0% CPU with every thread
    # parked. Reporting that as "signal may not have reached it" sends you hunting a
    # delivery problem that does not exist, so name a cause only from the evidence.
    # Same None-vs-'0' care as stop_all below: a count that could not be read is
    # unknown, never a failed save.
    #
    # The cause is no longer unknown (diagnosed 2026-09-05 on a 7h hang): mod-ollama-chat
    # spawns a DETACHED std::thread per bot utterance, and only the chat-reply path goes
    # through QueryManager's MaxConcurrentQueries cap - random.cpp and events.cpp call
    # QueryOllamaAPI() directly, uncapped. With ~1000 bots chattering at a local LLM that
    # answers one request at a time, those threads pile up blocked in an HTTP read; 1837
    # of them were live at the stop. ExitProcess then suspends every thread but the
    # exiting one, and the CRT's exit path deadlocks against a lock (heap/loader) held by
    # one of the suspended threads. Signature: 0.00s CPU, ~all threads "Wait, Suspended",
    # Server.log frozen mid-sentence. Terminating is the only way out of it, and by then
    # the save is long finished - so do that here instead of leaving the realm wedged.
    if name == 'worldserver' and 'Halting process' in '\n'.join(log_tail(400)):
        online = mysql_scalar(
            'SELECT COUNT(*) FROM acore_characters.characters WHERE online=1;')
        if online == '0':
            # Both rails are green here - the shutdown marker is in the log and no
            # character is flagged online - so nothing is pending and the process is
            # only still holding worldserver.exe and its ports. Leaving it up is what
            # blocks the next start, so kill it rather than just describing it.
            gone = force_kill(name)
            return ('%s: SAVED, then hung on exit - terminated after %ds. It took the '
                    'signal, flushed every character and released MySQL; nothing was '
                    'pending, so this lost nothing. %s' %
                    (name, wait_s, 'Process is gone.' if gone else
                     'WARNING: STILL PRESENT after Stop-Process -Force - investigate '
                     'before starting again, the ports are still held.'))
        if online is None or online == '':
            return ('%s: STILL RUNNING after %ds - it took the signal and began halting, '
                    'but MySQL could not be queried, so the save is UNCONFIRMED. Do not '
                    'kill it on this message alone.' % (name, wait_s))
        return ('%s: STILL RUNNING after %ds - it took the signal and began halting, but '
                '%s character(s) are still flagged online: still draining, or the save is '
                'incomplete. Give it longer.' % (name, wait_s, online))
    return ('%s: STILL RUNNING after %ds - no shutdown marker in the log, so the signal '
            'itself may not have reached it' % (name, wait_s))


def stop_mysql():
    if not mysql_alive():
        return 'MySQL: not running'
    pw = root_password()
    if MYSQLADMIN and pw:
        try:
            subprocess.run([MYSQLADMIN, '-h127.0.0.1', '-uroot', '-p' + pw, 'shutdown'],
                           capture_output=True, timeout=60, creationflags=NO_WINDOW)
        except Exception:
            pass
        for _ in range(20):
            time.sleep(2)
            if not mysql_alive():
                return 'MySQL: shut down cleanly'
        return 'MySQL: shutdown requested, still up'
    return 'MySQL: left running (no root password available for clean shutdown)'


def stop_all(include_auth=True, include_deps=False):
    msgs = [stop_graceful('worldserver')]
    if include_auth:
        msgs.append(stop_graceful('authserver', wait_s=60))
    still = mysql_scalar('SELECT COUNT(*) FROM `%s`.characters WHERE online=1;'
                                 % (dbinfo('character')['name'] or 'acore_characters'))
    # Distinguish "0" from "couldn't ask". An unreadable count previously came back
    # empty and was reported as a failed save, which was simply wrong.
    if still is None or still == '':
        msgs.append('characters flagged online: UNKNOWN (could not query MySQL)')
    elif still == '0':
        msgs.append('characters flagged online: 0  (saved cleanly)')
    else:
        msgs.append('characters flagged online: %s  <-- still draining, or save incomplete' % still)
    if include_deps:
        msgs.append(stop_mysql())
        if running('ollama')['up']:
            ps("Get-Process -Name 'ollama','ollama app' -ErrorAction SilentlyContinue | Stop-Process -Force")
            msgs.append('Ollama: stopped')
    return msgs


def status():
    world, auth = running('worldserver'), running('authserver')
    # Report Ollama by API reachability, not process presence: "ollama app.exe" can sit
    # in the process list with the API dead, which showed a healthy-looking panel while
    # bot chat was silent.
    oll = running('ollama')
    oll['up'] = ollama_alive()
    wconf = os.path.join(CONF_DIR, 'worldserver.conf')
    pb = os.path.join(MOD_CONF, 'playerbots.conf')
    oc = os.path.join(MOD_CONF, 'mod_ollama_chat.conf')
    ip = os.path.join(MOD_CONF, 'individualProgression.conf')
    ah = os.path.join(MOD_CONF, 'mod_ahbot.conf')
    ab = os.path.join(MOD_CONF, 'AutoBalance.conf')
    return {
        'root': ROOT,
        'portable': True,
        'mysql': {'up': mysql_alive(), 'mode': 'in-hub process', 'proc': running('mysqld')},
        'ollama': oll,
        'authserver': auth,
        'worldserver': world,
        'ready': world_ready() if world['up'] else False,
        'botsOnline': mysql_scalar('SELECT COUNT(*) FROM `%s`.characters WHERE online=1;'
                                 % (dbinfo('character')['name'] or 'acore_characters')),
        'settings': {
            'PlayerSaveInterval': conf_get(wconf, 'PlayerSaveInterval'),
            'MapUpdate.Threads': conf_get(wconf, 'MapUpdate.Threads'),
            'MinRandomBots': conf_get(pb, 'AiPlayerbot.MinRandomBots'),
            'MaxRandomBots': conf_get(pb, 'AiPlayerbot.MaxRandomBots'),
            'RandomBotMaxLevel': conf_get(pb, 'AiPlayerbot.RandomBotMaxLevel'),
            'BotAccountsMaxLevel': conf_get(ip, 'IndividualProgression.BotAccountsMaxLevel'),
            'EnableGuildTasks': conf_get(pb, 'AiPlayerbot.EnableGuildTasks'),
            'OllamaModel': conf_get(oc, 'OllamaChat.Model'),
            'MaxConcurrentQueries': conf_get(oc, 'OllamaChat.MaxConcurrentQueries'),
            'AhBotSeller': conf_get(ah, 'AuctionHouseBot.EnableSeller'),
            'AutoBalance': conf_get(ab, 'AutoBalance.Enable'),
        },
        'paths': {'mysql': MYSQL, 'mysqld': MYSQLD, 'ollama': OLLAMA, 'models': OLLAMA_MODELS},
    }


SETTABLE = {
    'saveInterval': (os.path.join(CONF_DIR, 'worldserver.conf'), 'PlayerSaveInterval',
                     lambda v: 60000 <= int(v) <= 3600000),
    'mapThreads':   (os.path.join(CONF_DIR, 'worldserver.conf'), 'MapUpdate.Threads',
                     lambda v: 1 <= int(v) <= 16),
    'minBots':      (os.path.join(MOD_CONF, 'playerbots.conf'), 'AiPlayerbot.MinRandomBots',
                     lambda v: 0 <= int(v) <= 5000),
    'maxBots':      (os.path.join(MOD_CONF, 'playerbots.conf'), 'AiPlayerbot.MaxRandomBots',
                     lambda v: 0 <= int(v) <= 5000),
    'concurrency':  (os.path.join(MOD_CONF, 'mod_ollama_chat.conf'), 'OllamaChat.MaxConcurrentQueries',
                     lambda v: 0 <= int(v) <= 64),
}

# 3.3.5a item_template has NO expansion column, so the auction house is gated by
# proxies. Measured against this realm's own data:
#   - entry-ID banding is USELESS here: ilvl>=200 items start at entry 17886 while
#     ilvl<=70 items run up to 56806, so the ranges overlap heavily.
#   - RequiredLevel gates wearable gear but alone still admits ilvl-435 unused/test
#     items, hence the separate ItemLevel ceiling.
#   - RequiredSkillRank bands professions cleanly (300 / 375 / 450 = vanilla / TBC /
#     WotLK caps). This is what keeps Northrend herbs and TBC gems out of a vanilla
#     economy: those carry RequiredLevel 0 and slip straight past a level filter.
# 0 means "no bound" to the module, so wotlk deliberately switches the caps off.
PHASES = {
    'vanilla': {'lvl': 60, 'maps': '0,1',         'dk': 1, 'shat': 0, 'dala': 0,
                'ah_req': 60, 'ah_ilvl': 100, 'ah_tg_skill': 300, 'ah_tg_ilvl': 80,  'ah_dk': 1},
    'tbc':     {'lvl': 70, 'maps': '0,1,530',     'dk': 1, 'shat': 1, 'dala': 0,
                'ah_req': 70, 'ah_ilvl': 164, 'ah_tg_skill': 375, 'ah_tg_ilvl': 130, 'ah_dk': 1},
    'wotlk':   {'lvl': 80, 'maps': '0,1,530,571', 'dk': 0, 'shat': 1, 'dala': 1,
                'ah_req': 80, 'ah_ilvl': 0,   'ah_tg_skill': 450, 'ah_tg_ilvl': 0,   'ah_dk': 0},
}


def set_phase(name):
    p = PHASES.get(name)
    if not p:
        return ['unknown phase: %s' % name]
    pb = os.path.join(MOD_CONF, 'playerbots.conf')
    ip = os.path.join(MOD_CONF, 'individualProgression.conf')
    ah = os.path.join(MOD_CONF, 'mod_ahbot.conf')
    out = []
    # Both bot level caps must agree or the lower silently wins, with nothing in the logs.
    for f, k, v in ((pb, 'AiPlayerbot.RandomBotMaxLevel', p['lvl']),
                    (pb, 'AiPlayerbot.RandomBotMaps', p['maps']),
                    (pb, 'AiPlayerbot.DisableDeathKnightLogin', p['dk']),
                    (pb, 'AiPlayerbot.TeleToShattrathCityWeight', p['shat']),
                    (pb, 'AiPlayerbot.TeleToDalaranWeight', p['dala']),
                    (ip, 'IndividualProgression.BotAccountsMaxLevel', p['lvl']),
                    # Auction house follows the same tier.
                    (ah, 'AuctionHouseBot.DisableItemsAboveReqLevel', p['ah_req']),
                    (ah, 'AuctionHouseBot.DisableItemsAboveLevel', p['ah_ilvl']),
                    # BOTH skill-rank filters are needed. TGs* covers class 7 (Trade Goods)
                    # only; recipes are class 9, so setting just the TG one let TBC/WotLK
                    # designs and formulas into a vanilla auction house.
                    (ah, 'AuctionHouseBot.DisableItemsAboveReqSkillRank', p['ah_tg_skill']),
                    (ah, 'AuctionHouseBot.DisableTGsAboveReqSkillRank', p['ah_tg_skill']),
                    (ah, 'AuctionHouseBot.DisableTGsAboveLevel', p['ah_tg_ilvl']),
                    (ah, 'AuctionHouseBot.DisableDKItems', p['ah_dk'])):
        out.append('%s %s = %s' % ('OK  ' if conf_set(f, k, v) else 'MISS', k, v))
    out.append('Restart the world to apply.')
    # Existing listings are not retroactively pulled; they age out on their normal
    # auction duration, so the AH shifts over hours rather than instantly.
    out.append('Existing auctions expire naturally - the AH shifts tier over hours, not at once.')
    return out


# ---------------- addon manager ----------------
ADDONS_DIR = os.path.join(ROOT, 'client', 'Interface', 'AddOns')
CATALOG_FILE = os.path.join(HERE, 'addons_catalog.json')
LEDGER_FILE = os.path.join(HERE, 'addons_installed.json')


def _ledger():
    try:
        with open(LEDGER_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _ledger_save(d):
    with open(LEDGER_FILE, 'w', encoding='utf-8') as f:
        json.dump(d, f, indent=1)


def toc_interface(folder):
    """A .toc's '## Interface:' is the authoritative client-version marker.
    3.3.5a is 30300 - anything else is a compatibility warning worth showing.

    Read as utf-8-sig: plenty of .toc files ship with a BOM, and under plain
    utf-8 the marker arrives with the BOM glued to the front, so the startswith
    below never matched and the addon reported no version at all."""
    try:
        for fn in os.listdir(folder):
            if fn.lower().endswith('.toc'):
                with open(os.path.join(folder, fn), 'r', encoding='utf-8-sig', errors='replace') as f:
                    for line in f:
                        if line.lower().startswith('## interface'):
                            return line.split(':', 1)[1].strip()
                break
    except Exception:
        pass
    return None


def addon_state(dest=None):
    """Catalog plus install state, read from `dest` (default: the vanilla client).

    Every realm in the hub runs a 3.3.5a client and shares this catalog, so the
    only thing that varies between them is which folders are on disk - hence the
    parameter.  The ledger stays global: it records what the manager installed,
    and the mirroring in launcher.py keeps that true of every client at once."""
    dest = dest or ADDONS_DIR
    try:
        with open(CATALOG_FILE, 'r', encoding='utf-8') as f:
            cat = json.load(f)
    except Exception as e:
        return {'error': 'catalog unavailable: %s' % e, 'addons': []}
    led = _ledger()
    present = set()
    if os.path.isdir(dest):
        present = {d for d in os.listdir(dest) if os.path.isdir(os.path.join(dest, d))}
    for a in cat.get('addons', []):
        folders = led.get(a['name'], {}).get('folders', [])
        a['installed'] = bool(folders) and any(f in present for f in folders)
        a['folders'] = folders
        if a['installed']:
            a['interface'] = toc_interface(os.path.join(dest, folders[0]))
    cat['addonsDir'] = dest
    cat['clientPresent'] = os.path.isdir(dest)
    # Folders we didn't install (stock Blizzard_* plus anything added by hand).
    cat['foreign'] = sorted(d for d in present
                            if not d.startswith('Blizzard_')
                            and not any(d in v.get('folders', []) for v in led.values()))
    return cat


def addon_fetch(name):
    """Download an addon once.  Returns (entry, blob, log) or (None, None, log).

    Split out of addon_install so a mirrored install across several clients pays
    for one download rather than one per client."""
    try:
        with open(CATALOG_FILE, 'r', encoding='utf-8') as f:
            cat = json.load(f)
    except Exception as e:
        return None, None, ['catalog unavailable: %s' % e]
    entry = next((a for a in cat.get('addons', []) if a['name'] == name), None)
    if not entry:
        return None, None, ['not in catalog: %s' % name]
    import urllib.request
    try:
        req = urllib.request.Request(entry['url'], headers={'User-Agent': 'AzerothControl'})
        with urllib.request.urlopen(req, timeout=180) as r:
            blob = r.read()
    except Exception as e:
        return None, None, ['download failed: %s' % e]
    return entry, blob, ['downloaded %s (%.1f KB)' % (entry['file'], len(blob) / 1024.0)]


def addon_unpack(entry, blob, dest):
    """Extract a fetched archive into one client. Returns (ok, folders, log)."""
    if not os.path.isdir(dest):
        return False, [], ['client AddOns folder missing: %s' % dest]
    import io as _io, zipfile
    log = []
    try:
        zf = zipfile.ZipFile(_io.BytesIO(blob))
    except Exception as e:
        return False, [], ['not a readable zip: %s' % e]

    tops = set()
    members = []
    for m in zf.namelist():
        p = m.replace('\\', '/')
        # Reject zip-slip: absolute paths or parent traversal must never extract.
        if p.startswith('/') or '..' in p.split('/') or (len(p) > 1 and p[1] == ':'):
            return False, [], ['refused: unsafe path in archive (%s)' % m]
        first = p.split('/')[0]
        # Mac archive cruft: __MACOSX carries resource forks, never addon code,
        # and used to land on disk and then get recorded in the ledger as one of
        # the addon's own folders. Drop it before anything is written.
        if first == '__MACOSX' or p.split('/')[-1] == '.DS_Store':
            continue
        members.append(m)
        if first:
            tops.add(first)
    try:
        zf.extractall(dest, members)
    except Exception as e:
        return False, [], ['extract failed: %s' % e]

    folders = sorted(t for t in tops if os.path.isdir(os.path.join(dest, t)))
    log.append('installed %d folder(s): %s' % (len(folders), ', '.join(folders) or '(none)'))
    for f in folders:
        iface = toc_interface(os.path.join(dest, f))
        if iface and iface != '30300':
            log.append('NOTE %s declares Interface %s (3.3.5a is 30300) - may misbehave' % (f, iface))
    return True, folders, log


def addon_install(name, dest=None):
    dest = dest or ADDONS_DIR
    entry, blob, log = addon_fetch(name)
    if not entry:
        return False, log
    ok, folders, unpack_log = addon_unpack(entry, blob, dest)
    log += unpack_log
    if not ok:
        return False, log
    led = _ledger()
    led[name] = {'folders': folders, 'file': entry['file']}
    _ledger_save(led)
    return True, log


def addon_remove(name, dest=None):
    dest = dest or ADDONS_DIR
    led = _ledger()
    rec = led.get(name)
    if not rec:
        return False, ['not tracked as installed: %s' % name]
    removed = []
    for f in rec.get('folders', []):
        p = os.path.join(dest, f)
        # Never delete outside the AddOns directory, whatever the ledger says.
        if os.path.abspath(p).startswith(os.path.abspath(dest)) and os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
            removed.append(f)
    led.pop(name, None)
    _ledger_save(led)
    return True, ['removed: %s' % (', '.join(removed) or '(nothing on disk)')]


# ---------------- trainer (GM commands) ----------------
SOAP_CFG = os.path.join(HERE, 'soap.json')


def soap_conf():
    try:
        with open(SOAP_CFG, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def soap_exec(command):
    """Run a GM command through AzerothCore's SOAP interface.

    SOAP runs commands in a CONSOLE context - there is no 'you' - so only commands
    taking an explicit player name work. Anything relying on in-game selection is
    offered as copy-to-clipboard in the UI instead of pretending to run here.
    """
    cfg = soap_conf()
    if not cfg:
        return False, 'SOAP not configured (control/soap.json missing)'
    import base64, urllib.request, urllib.error
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:ns1="urn:AC">'
        '<SOAP-ENV:Body><ns1:executeCommand><command>%s</command></ns1:executeCommand>'
        '</SOAP-ENV:Body></SOAP-ENV:Envelope>'
    ) % (command.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
    url = 'http://%s:%s/' % (cfg.get('host', '127.0.0.1'), cfg.get('port', 7878))
    auth = base64.b64encode(('%s:%s' % (cfg['user'], cfg['pass'])).encode()).decode()
    req = urllib.request.Request(url, data=body.encode('utf-8'), headers={
        'Content-Type': 'application/xml', 'Authorization': 'Basic ' + auth})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            txt = r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'replace')[:200] if hasattr(e, 'read') else ''
        if e.code == 401:
            return False, 'SOAP rejected the login - is the world restarted since SOAP was enabled?'
        return False, 'SOAP HTTP %s %s' % (e.code, detail)
    except Exception as e:
        return False, 'SOAP unreachable (%s) - restart the world to activate it' % e
    import re as _re
    m = _re.search(r'<result>(.*?)</result>', txt, _re.S)
    return True, (m.group(1).strip() if m else txt.strip())[:400]


def online_character():
    """The character the buttons act on. Excludes bot accounts."""
    row = mysql_scalar(
        "SELECT c.name FROM acore_characters.characters c "
        "JOIN acore_auth.account a ON a.id=c.account "
        "WHERE c.online=1 AND a.username NOT LIKE 'RNDBOT%' "
        "AND a.username NOT IN ('AHBOT','PANELGM') LIMIT 1;")
    if row:
        return row
    # Fall back to the most recently played character so buttons still work offline.
    return mysql_scalar(
        "SELECT c.name FROM acore_characters.characters c "
        "JOIN acore_auth.account a ON a.id=c.account "
        "WHERE a.username NOT LIKE 'RNDBOT%' AND a.username NOT IN ('AHBOT','PANELGM') "
        "ORDER BY c.logout_time DESC LIMIT 1;")


# Destinations verified present in game_tele. Silvermoon/Exodar deliberately absent -
# they have no game_tele row, so buttons for them would silently fail.
TELE_DESTS = [
    {'v': 'Stormwind',   't': 'Stormwind',    'tier': 'vanilla'},
    {'v': 'Ironforge',   't': 'Ironforge',    'tier': 'vanilla'},
    {'v': 'Darnassus',   't': 'Darnassus',    'tier': 'vanilla'},
    {'v': 'Orgrimmar',   't': 'Orgrimmar',    'tier': 'vanilla'},
    {'v': 'Undercity',   't': 'Undercity',    'tier': 'vanilla'},
    {'v': 'ThunderBluff','t': 'Thunder Bluff','tier': 'vanilla'},
    {'v': 'Gadgetzan',   't': 'Gadgetzan',    'tier': 'vanilla'},
    {'v': 'Shattrath',   't': 'Shattrath (TBC)',  'tier': 'tbc'},
    {'v': 'Dalaran',     't': 'Dalaran (WotLK)',  'tier': 'wotlk'},
]

# mode 'run'  -> executed server-side via SOAP (command takes a player name)
# mode 'copy' -> needs YOUR client session; copied for you to paste in chat
TRAINER = [
    {'id': 'tele', 'cat': 'Travel', 'mode': 'run', 'label': 'Teleport to',
     'desc': 'Send your character to a capital or hub.',
     'cmd': '.teleport name {name} {dest}',
     'args': [{'key': 'dest', 'type': 'select', 'label': 'Destination', 'options': TELE_DESTS}]},
    {'id': 'graveyard', 'cat': 'Travel', 'mode': 'run', 'label': 'Corpse recovery',
     'desc': 'Teleport to the nearest graveyard - the fix when your corpse is unreachable.',
     'cmd': '.unstuck {name} graveyard'},
    {'id': 'inn', 'cat': 'Travel', 'mode': 'run', 'label': 'Teleport to inn',
     'desc': 'Send your character to its hearthstone inn.',
     'cmd': '.unstuck {name} inn'},
    {'id': 'startzone', 'cat': 'Travel', 'mode': 'run', 'label': 'Teleport to start zone',
     'desc': 'Back to your race starting area.',
     'cmd': '.unstuck {name} startzone'},
    # Coordinates are looked up live from the corpse table; the command is copied
    # because .go xyz moves whoever runs it, and over SOAP that is nobody.
    {'id': 'corpse', 'cat': 'Travel', 'mode': 'copy', 'label': 'Teleport to my corpse',
     'desc': 'Reads where you actually died and builds a .go xyz for that exact spot. '
             'Only works after you have released; before releasing, just use Revive.',
     'cmd': '{corpse}'},

    {'id': 'level', 'cat': 'Character', 'mode': 'run', 'label': 'Set level',
     'desc': 'Instantly set your character level. Vanilla tier caps at 60.',
     'cmd': '.character level {name} {level}',
     'args': [{'key': 'level', 'type': 'number', 'label': 'Level', 'default': 60, 'min': 1, 'max': 80}]},

    {'id': 'gold', 'cat': 'Riches', 'mode': 'run', 'label': 'Mail yourself gold',
     'desc': 'Arrives by in-game mail. Works whether you are online or not.',
     'cmd': '.send money {name} "Gold" "Delivered by Azeroth Control" {copper}',
     'args': [{'key': 'gold', 'type': 'number', 'label': 'Gold', 'default': 100, 'min': 1, 'max': 100000}]},
    {'id': 'item', 'cat': 'Riches', 'mode': 'run', 'label': 'Add item',
     'desc': 'Item IDs are searchable in the Items tab.',
     'cmd': '.additem {name} {item} {count}',
     'args': [{'key': 'item', 'type': 'number', 'label': 'Item ID', 'default': 6948, 'min': 1, 'max': 60000},
              {'key': 'count', 'type': 'number', 'label': 'Count', 'default': 1, 'min': 1, 'max': 100}]},

    {'id': 'fly_on',   'cat': 'In-game only', 'mode': 'copy', 'label': 'Flight mode on',
     'desc': 'Needs your client session - paste into chat.', 'cmd': '.gm fly on'},
    {'id': 'fly_off',  'cat': 'In-game only', 'mode': 'copy', 'label': 'Flight mode off',
     'desc': 'Land before turning this off or you will fall.', 'cmd': '.gm fly off'},
    {'id': 'speed',    'cat': 'In-game only', 'mode': 'copy', 'label': 'Run speed',
     'desc': '1 is normal. Values above ~7 tend to desync the client.',
     'cmd': '.modify speed all {rate}',
     'args': [{'key': 'rate', 'type': 'number', 'label': 'Multiplier', 'default': 3, 'min': 0.1, 'max': 10}]},
    {'id': 'cooldown', 'cat': 'In-game only', 'mode': 'copy', 'label': 'Clear all cooldowns',
     'desc': 'Wipes every spell cooldown on you.', 'cmd': '.cooldown'},
    {'id': 'revive',   'cat': 'In-game only', 'mode': 'copy', 'label': 'Revive',
     'desc': 'Resurrect on the spot with full health.', 'cmd': '.revive'},
    {'id': 'money',    'cat': 'In-game only', 'mode': 'copy', 'label': 'Gold direct to bags',
     'desc': 'Skips the mailbox. Amount is in copper (10000 = 1 gold).',
     'cmd': '.modify money {copper}',
     'args': [{'key': 'gold', 'type': 'number', 'label': 'Gold', 'default': 100, 'min': 1, 'max': 100000}]},
    {'id': 'maxskill', 'cat': 'In-game only', 'mode': 'copy', 'label': 'Max all weapon skills',
     'desc': 'Sets every skill to the cap for your level.', 'cmd': '.maxskill'},
    {'id': 'learnall', 'cat': 'In-game only', 'mode': 'copy', 'label': 'Learn all class spells',
     'desc': 'Every spell your class can train at your level.', 'cmd': '.learn all my spells'},
    {'id': 'gm_on',    'cat': 'In-game only', 'mode': 'copy', 'label': 'GM mode on',
     'desc': 'Invulnerable and untargetable by creatures.', 'cmd': '.gm on'},
    {'id': 'gm_off',   'cat': 'In-game only', 'mode': 'copy', 'label': 'GM mode off',
     'desc': 'Return to normal play.', 'cmd': '.gm off'},
]


def corpse_command(name):
    """Build a '.go xyz' that lands on your body.

    The client only exposes corpse position as map-relative fractions, which is not
    what .go xyz wants, so the addon cannot do this alone. The server stores real
    world coordinates in acore_characters.corpse, so we read them here instead.
    """
    if not name:
        return None, 'no character to look up'
    row = mysql_scalar(
        "SELECT CONCAT(ROUND(c.posX,2),' ',ROUND(c.posY,2),' ',ROUND(c.posZ,2),' ',c.mapId) "
        "FROM acore_characters.corpse c "
        "JOIN acore_characters.characters ch ON ch.guid = c.guid "
        "WHERE ch.name = '%s' LIMIT 1;" % name.replace("'", ""))
    if not row:
        return None, ('no corpse found for %s - you are either alive, or you already '
                      'recovered it' % name)
    return '.go xyz %s' % row, None


def trainer_state():
    cfg = soap_conf()
    return {'character': online_character(),
            'soapConfigured': cfg is not None,
            'soapReady': soap_exec('server info')[0] if cfg else False,
            'commands': TRAINER}


def trainer_build(cmd_id, args, name):
    entry = next((t for t in TRAINER if t['id'] == cmd_id), None)
    if not entry:
        return None, 'unknown command: %s' % cmd_id
    if cmd_id == 'corpse':
        built, err = corpse_command(name)
        if err:
            return None, err
        return built, entry['mode']
    cmd = entry['cmd']
    vals = dict(args or {})
    # Gold is entered in gold but the commands take copper.
    if 'gold' in vals:
        try:
            vals['copper'] = int(float(vals['gold']) * 10000)
        except Exception:
            return None, 'gold must be a number'
    for a in entry.get('args', []):
        k = a['key']
        if k not in vals or vals[k] == '':
            if 'default' in a:
                vals[k] = a['default']
            else:
                return None, 'missing value: %s' % a.get('label', k)
    for k, v in list(vals.items()):
        cmd = cmd.replace('{%s}' % k, str(v))
    cmd = cmd.replace('{name}', name or '')
    if '{' in cmd:
        return None, 'command still has unfilled placeholders: %s' % cmd
    return cmd, entry['mode']


# ---------------- bots (playerbot commands) ----------------
#
# Every command here is copy-only, and that is a property of the server rather than
# a shortcut taken by this panel. PlayerbotCommandScript refuses the console
# outright - "You may only add bots from an active session" - because a bot is
# owned by a master's WorldSession, and over SOAP there is no session to own it.
# The chat orders (autogear, follow, ...) are routed by whisper or party channel,
# which likewise only exists inside a client. So this tab builds the exact text
# and hands it over; it never pretends to run it.

PB_CONF = os.path.join(MOD_CONF, 'playerbots.conf')

# addclass matches the class token with a raw strcmp, so these are case sensitive:
# "Shaman" is rejected where "shaman" is accepted.
BOT_CLASSES = [
    {'v': 'warrior', 't': 'Warrior', 'id': 1},
    {'v': 'paladin', 't': 'Paladin', 'id': 2},
    {'v': 'hunter',  't': 'Hunter',  'id': 3},
    {'v': 'rogue',   't': 'Rogue',   'id': 4},
    {'v': 'priest',  't': 'Priest',  'id': 5},
    {'v': 'dk',      't': 'Death Knight', 'id': 6},
    {'v': 'shaman',  't': 'Shaman',  'id': 7},
    {'v': 'mage',    't': 'Mage',    'id': 8},
    {'v': 'warlock', 't': 'Warlock', 'id': 9},
    {'v': 'druid',   't': 'Druid',   'id': 11},
]

GENDERS = [{'v': '', 't': 'Either'}, {'v': 'male', 't': 'Male'}, {'v': 'female', 't': 'Female'}]

# mod-raid-roster. Only these five sizes are accepted - anything else is rejected by
# PresetBotCount() returning 0. The sizes COUNT THE PLAYER, so 25 fields 24 bots.
RAID_SIZES = [{'v': '5', 't': '5-man (4 bots)'}, {'v': '10', 't': '10-man (9 bots)'},
              {'v': '20', 't': '20-man (19 bots)'}, {'v': '25', 't': '25-man (24 bots)'},
              {'v': '40', 't': '40-man (39 bots)'}]
# Blank means "use the slot's roster default", which is the normal case.
RAID_ROLES = [{'v': '', 't': 'Roster default'}, {'v': 'tank', 't': 'Tank'},
              {'v': 'heal', 't': 'Healer'}, {'v': 'dps', 't': 'DPS'}]

# init= and autogear do not share a vocabulary, which is the commonest way these
# commands get typed wrong. init= wants yellow for legendary and reads a bare
# number as a gear score; autogear wants orange, and its number is an item level.
# Each list below is the one its own command parses.
INIT_QUALITIES = [
    {'v': 'auto',      't': 'auto - match my own gear'},
    {'v': 'white',     't': 'white / common'},
    {'v': 'green',     't': 'green / uncommon'},
    {'v': 'blue',      't': 'blue / rare'},
    {'v': 'epic',      't': 'epic / purple'},
    {'v': 'legendary', 't': 'legendary / yellow'},
]

AUTOGEAR_MODES = [
    {'v': '',            't': 'to the server cap'},
    {'v': 'match',       't': 'match - my average item level'},
    {'v': 'white',       't': 'white / common'},
    {'v': 'green',       't': 'green / uncommon'},
    {'v': 'blue',        't': 'blue / rare'},
    {'v': 'purple',      't': 'purple / epic'},
    {'v': 'orange',      't': 'orange / legendary'},
    {'v': 'reset',       't': 'reset - strip, then regear'},
    {'v': 'reset match', 't': 'reset match'},
    {'v': 'reset green', 't': 'reset green'},
    {'v': 'reset blue',  't': 'reset blue'},
]

TALENT_SUBS = [
    {'v': '',          't': 'report my current spec'},
    {'v': 'spec list', 't': 'spec list - what I can pick'},
    {'v': 'autopick',  't': 'autopick - re-roll at random'},
    {'v': 'switch 1',  't': 'switch 1 - primary tree'},
    {'v': 'switch 2',  't': 'switch 2 - secondary tree'},
]

RTI_ICONS = [{'v': 'skull', 't': 'skull'}, {'v': 'cross', 't': 'cross'},
             {'v': 'square', 't': 'square'}, {'v': 'moon', 't': 'moon'},
             {'v': 'triangle', 't': 'triangle'}, {'v': 'diamond', 't': 'diamond'},
             {'v': 'circle', 't': 'circle'}, {'v': 'star', 't': 'star'}]


def target_arg():
    """A fresh copy each time - these end up in one shared catalogue."""
    return {'key': 'target', 'type': 'text', 'label': 'Bots', 'default': '',
            'placeholder': 'blank = your target'}


BOTS = [
    # -------- roster --------
    {'id': 'addclass', 'cat': 'Roster', 'label': 'Summon a class bot',
     'cmd': '.playerbots bot addclass {class} {gender}',
     'desc': 'Checks a ready-made character of that class out of the pool.',
     'tip': 'This does not create anything. The server keeps a pool of pre-made characters - '
            'AddClassAccountPoolSize of them per class - and addclass logs in the first free '
            'one of your faction that nobody else is using. Whatever level, spec and gear that '
            'character happens to carry is what walks up to you, which is why a fresh warrior '
            'arrives in greys with no tank spec. Follow it with init= for level and gear, then '
            'talents spec for the tree. The class token is matched with a case-sensitive strcmp, '
            'so "Shaman" gives you Invalid class and "shaman" works. Death Knight is refused '
            'until your own character reaches the heroic start level, 55.',
     'args': [{'key': 'class', 'type': 'select', 'label': 'Class', 'options': BOT_CLASSES},
              {'key': 'gender', 'type': 'select', 'label': 'Gender', 'options': GENDERS}],
     'gate': ['addClassCommand', 'poolSize']},
    {'id': 'add', 'cat': 'Roster', 'label': 'Log in one of your own characters',
     'cmd': '.playerbots bot add {names}',
     'desc': 'Brings a named alt online under your control.',
     'tip': 'Takes a character name, or several separated by commas with no spaces. The '
            'character has to be on your own account, or on an account linked to yours, unless '
            'AllowAccountBots is on. Aliases: add, login. MaxAddedBots is the ceiling.',
     'gate': ['maxAddedBots'],
     'args': [{'key': 'names', 'type': 'text', 'label': 'Character names', 'default': '',
               'placeholder': 'Alt,Alttwo', 'req': True}]},
    {'id': 'addaccount', 'cat': 'Roster', 'label': 'Log in a whole account',
     'cmd': '.playerbots bot addaccount {name}',
     'desc': 'Every character on that account, in one command.',
     'tip': 'Takes an account name, or the name of any character on it, and brings all of that '
            'account’s characters online at once. Same ownership rules as add.',
     'args': [{'key': 'name', 'type': 'text', 'label': 'Account or character', 'default': '',
               'placeholder': 'account name', 'req': True}]},
    {'id': 'remove', 'cat': 'Roster', 'label': 'Dismiss bots',
     'cmd': '.playerbots bot remove {target}',
     'desc': 'Logs the bot out and hands the character back to the pool.',
     'tip': 'Aliases: remove, logout, rm. Accepts the same targeting as every other bot command, '
            'so remove * clears your whole party in one line. Dismissing is how an addclass '
            'character is freed for next time - one left logged in stays checked out.',
     'args': [target_arg()]},
    {'id': 'list', 'cat': 'Roster', 'label': 'List my bots',
     'cmd': '.playerbots bot list',
     'desc': 'Which bots you currently have added, and their state.',
     'tip': 'Prints to your own chat frame; nobody else sees it.'},
    {'id': 'lookup', 'cat': 'Roster', 'label': 'List characters I could add',
     'cmd': '.playerbots bot lookup',
     'desc': 'The alts on your account that add would accept.',
     'tip': 'The companion to add: lookup tells you the names, add brings them in.'},
    {'id': 'self', 'cat': 'Roster', 'label': 'Toggle AI on myself',
     'cmd': '.playerbots bot self',
     'desc': 'Lets the bot AI drive your own character, and toggles back off.',
     'tip': 'SelfBotLevel decides who may use it: 1 means GM only, 2 means anyone, 0 turns it '
            'off. Useful for watching what the AI does with a spec, or for parking yourself '
            'while you tab out. Run it again to take back control.',
     'gate': ['selfBotLevel']},
    {'id': 'reload', 'cat': 'Roster', 'label': 'Reload playerbots.conf',
     'cmd': '.playerbots bot reload',
     'desc': 'Re-reads the bot config without a restart.',
     'tip': 'Covers playerbots.conf only. worldserver.conf needs .reload config and '
            'mod_ollama_chat.conf needs .ollama reload - the three are separate.'},

    # -------- gear and level --------
    {'id': 'init', 'cat': 'Gear and level', 'label': 'Regear and match my level',
     'cmd': '.playerbots bot init={quality} {target}',
     'desc': 'Re-rolls the bot at your level in gear of the quality you pick.',
     'tip': 'The command that fixes a freshly summoned bot. It sets the bot to your level and '
            're-rolls its whole kit, which also wipes the talent tree - so run init= first and '
            'talents spec second, or you undo your own spec. auto reads your own equipped gear '
            'and caps the bot just under it, which is what keeps a party in step as you level. '
            'Works on addclass bots only.',
     'args': [{'key': 'quality', 'type': 'select', 'label': 'Quality', 'options': INIT_QUALITIES},
              target_arg()],
     'gate': ['autoInitOnly']},
    {'id': 'initgs', 'cat': 'Gear and level', 'label': 'Regear to a gear score',
     'cmd': '.playerbots bot init={gs} {target}',
     'desc': 'The same, capped at an exact gear score.',
     'tip': 'init= with a number instead of a colour caps by gear score rather than by rarity. '
            'This is the lever for holding a raid deliberately under-geared for a progression '
            'tier. It is gear score, not item level - autogear’s number is item level.',
     'args': [{'key': 'gs', 'type': 'number', 'label': 'Gear score', 'default': 100,
               'min': 1, 'max': 6000},
              target_arg()]},
    {'id': 'levelup', 'cat': 'Gear and level', 'label': 'Re-roll at current level',
     'cmd': '.playerbots bot levelup {target}',
     'desc': 'Rebuilds the bot from scratch without changing its level.',
     'tip': 'Alias: level. Randomises the bot the way the server does when it levels one on its '
            'own, so gear, talents and spells are all replaced. Reach for it when a bot has '
            'drifted into a broken state, not as a way to gear one up.',
     'args': [target_arg()]},
    {'id': 'refresh', 'cat': 'Gear and level', 'label': 'Top up consumables',
     'cmd': '.playerbots bot refresh {target}',
     'desc': 'Restocks food, drink, reagents and ammo. Leaves gear alone.',
     'tip': 'The cheap one. It does not touch equipment, talents or level - it only refills what '
            'a bot burns through, so it is safe to fire between pulls.',
     'args': [target_arg()]},
    {'id': 'refreshraid', 'cat': 'Gear and level', 'label': 'Clear saved instances',
     'cmd': '.playerbots bot refresh=raid {target}',
     'desc': 'Unbinds the bot from raid lockouts.',
     'tip': 'The fix for "you are already saved to a different instance" when a bot has been in '
            'there with someone else. It is the one command that reaches random bots as well as '
            'addclass bots, but only while ResetInstanceIdForAltBots is on; with it off this is '
            'addclass bots only. A bot already inside a raid has to relog before the unbind '
            'takes effect.',
     'gate': ['resetInstanceAlt'],
     'args': [target_arg()]},
    {'id': 'random', 'cat': 'Gear and level', 'label': 'Randomise completely',
     'cmd': '.playerbots bot random {target}',
     'desc': 'Sends the bot back through the random-bot generator.',
     'tip': 'Rolls a new level within the random-bot band as well as new gear, so the bot will '
            'usually stop matching you. Rarely what you want for a party bot.',
     'args': [target_arg()]},
    {'id': 'quests', 'cat': 'Gear and level', 'label': 'Grant attunement quests',
     'cmd': '.playerbots bot quests {target}',
     'desc': 'Completes the keys and attunements a bot needs to get through a door.',
     'tip': 'The fix when a bot is bounced at an entrance you can walk through yourself. It '
            'grants the attunement chain rather than a key item, so it covers Karazhan, the '
            'Black Temple and the vanilla dungeon sets alike.',
     'args': [target_arg()]},
    {'id': 'initself', 'cat': 'Gear and level', 'label': 'Regear myself',
     'cmd': '.playerbots bot initself={quality}',
     'desc': 'Runs the same generator on your own character.',
     'tip': 'GM only, and it replaces everything you are wearing - no confirmation and no undo. '
            'Note the vocabulary differs from init=: this one takes uncommon, rare, epic or '
            'legendary, with no white/green/blue/yellow aliases.',
     'args': [{'key': 'quality', 'type': 'select', 'label': 'Quality', 'options': [
         {'v': 'uncommon', 't': 'uncommon'}, {'v': 'rare', 't': 'rare'},
         {'v': 'epic', 't': 'epic'}, {'v': 'legendary', 't': 'legendary'}]}]},

    # -------- chat orders: upkeep --------
    {'id': 'autogear', 'cat': 'Upkeep (party chat)', 'label': 'Auto-gear',
     'cmd': '/p autogear {mode}',
     'desc': 'Upgrades gear in place, keeping the spec and level the bot already has.',
     'tip': 'The counterpart to init=. This one is incremental: it keeps the talent tree and only '
            'swaps a slot when the candidate beats what is worn by the EquipUpgradeThreshold '
            'margin - 1.1 means a ten percent improvement. That makes it the safe one to repeat '
            'as you level. match tracks your own average item level across the slots you have '
            'actually filled, so empty slots do not drag the target down. reset strips '
            'everything first and regears from nothing, and combines with the others. Every '
            'request is capped at AutoGearQualityLimit (1 normal, 2 uncommon, 3 rare, 4 epic, '
            '5 legendary), so asking above the cap quietly gets you the cap.',
     'gate': ['autoGearQuality', 'equipUpgradeThresh', 'autoGearAltBots'],
     'args': [{'key': 'mode', 'type': 'select', 'label': 'Target', 'options': AUTOGEAR_MODES}]},
    {'id': 'autogear_ilvl', 'cat': 'Upkeep (party chat)', 'label': 'Auto-gear to an item level',
     'cmd': '/p autogear {ilvl}',
     'desc': 'Aim at a specific item level instead of a rarity.',
     'tip': 'Positive whole numbers only; anything else is rejected outright. Clamped down to '
            'AutoGearScoreLimit when that is non-zero; 0 means no ceiling. Useful tier floors: '
            '78 Molten Core, 92 Naxxramas 40, 125 Karazhan, 164 Sunwell, 200 Naxxramas 10, '
            '245 Ulduar, 290 heroic Icecrown.',
     'gate': ['autoGearScore'],
     'args': [{'key': 'ilvl', 'type': 'number', 'label': 'Item level', 'default': 200,
               'min': 1, 'max': 300}]},
    {'id': 'autogear_bis', 'cat': 'Upkeep (party chat)', 'label': 'Auto-gear best in slot',
     'cmd': '/p autogear bis',
     'desc': 'Hands out the BiS list for the bot’s class and spec.',
     'tip': 'Reads playerbots_bis_gear for the tier whose item-level floor matches '
            'AutoGearScoreLimit, and falls back to ordinary autogear when no tier matches. It '
            'needs AutoGearBisCommand = 1 and AutoGearQualityLimit = 4. Below either of those the '
            'server refuses the command outright.',
     'gate': ['autoGearBis', 'autoGearQuality']},
    {'id': 'maintenance', 'cat': 'Upkeep (party chat)', 'label': 'Maintenance',
     'cmd': '/p maintenance',
     'desc': 'Everything except gear: spells, talents, glyphs, gems, bags, repairs, food.',
     'tip': 'The catch-all upkeep pass. It trains available spells and skills, spends unspent '
            'talent points, sockets and enchants what is worn, repairs, restocks food, drink, '
            'reagents, potions and ammo, hands out bags and mounts, and fixes pets. Its talent '
            'pass is incremental, so it leaves a spec you chose alone and only rolls at random '
            'when nothing is spent - which makes it safe to run after talents spec, unlike '
            'init=. The enchanting part needs the bot above MinEnchantingBotLevel.',
     'gate': ['maintenanceCommand']},
    {'id': 'talents', 'cat': 'Upkeep (party chat)', 'label': 'Talents',
     'cmd': '/p talents {sub}',
     'desc': 'Report, list or re-roll the talent tree.',
     'tip': 'With no argument the bot tells you the spec it is running, which is the quick way to '
            'check whether an init= wiped a spec you set earlier. spec list prints the named '
            'builds it can take, with their point split.',
     'args': [{'key': 'sub', 'type': 'select', 'label': 'Subcommand', 'options': TALENT_SUBS}]},
    {'id': 'talents_spec', 'cat': 'Upkeep (party chat)', 'label': 'Set a named spec',
     'cmd': '/p talents spec {spec}',
     'desc': 'Switch the bot to one of the server’s pre-built trees.',
     'tip': 'The names live in playerbots.conf and differ by class - prot pve for a warrior or '
            'paladin tank, bear pve for a druid, blood pve for a death knight. The full '
            'per-class list is under Approved nomenclature at the foot of this page. Set the '
            'spec after gearing, never before: init= re-rolls talents and would undo it.',
     'args': [{'key': 'spec', 'type': 'text', 'label': 'Spec name', 'default': 'prot pve',
               'placeholder': 'prot pve'}]},
    {'id': 'trainer', 'cat': 'Upkeep (party chat)', 'label': 'Visit trainer',
     'cmd': '/p trainer',
     'desc': 'Learn everything available at the trainer the bot is standing at.',
     'tip': 'Needs a real trainer in range. maintenance does this and more, so this is for when '
            'you want the spells without the rest of the pass.'},
    {'id': 'repair', 'cat': 'Upkeep (party chat)', 'label': 'Repair',
     'cmd': '/p repair',
     'desc': 'Repair at the nearest vendor.',
     'tip': 'Bots pay for it themselves. repair cost asks what it would come to first.'},
    {'id': 'equip_upgrade', 'cat': 'Upkeep (party chat)', 'label': 'Equip upgrades',
     'cmd': '/p equip upgrade',
     'desc': 'Wear anything in the bags that beats what is worn.',
     'tip': 'Only looks at what the bot is already carrying, so it is the command for after a '
            'loot drop rather than a way to gear up from nothing.'},

    # -------- chat orders: movement and combat --------
    {'id': 'follow', 'cat': 'Orders (party chat)', 'label': 'Follow me',
     'cmd': '/p follow', 'desc': 'Return to formation behind you.',
     'tip': 'The default state. Cancels stay, grind and anything else that pinned the bot.'},
    {'id': 'stay', 'cat': 'Orders (party chat)', 'label': 'Stay put',
     'cmd': '/p stay', 'desc': 'Hold this exact spot until told otherwise.',
     'tip': 'Bots still fight and heal from where they stand; they only stop walking. The usual '
            'way to park healers clear of an incoming cleave before a pull.'},
    {'id': 'attack', 'cat': 'Orders (party chat)', 'label': 'Attack my target',
     'cmd': '/p attack', 'desc': 'Everyone onto the mob you have selected.',
     'tip': 'Sends the whole party, tanks included. Use tank attack when only the tank should '
            'go in.'},
    {'id': 'tankattack', 'cat': 'Orders (party chat)', 'label': 'Tank attack',
     'cmd': '/p tank attack', 'desc': 'Only the tank engages your target.',
     'tip': 'The controlled pull: the tank picks it up and the rest hold until threat settles.'},
    {'id': 'pull', 'cat': 'Orders (party chat)', 'label': 'Pull my target',
     'cmd': '/p pull', 'desc': 'Ranged pull, then bring it back to the group.',
     'tip': 'pull rti pulls whatever carries the raid target icon instead of your selection.'},
    {'id': 'maxdps', 'cat': 'Orders (party chat)', 'label': 'Max DPS',
     'cmd': '/p max dps', 'desc': 'Drop threat caution and burn everything.',
     'tip': 'Turns off the throttles that hold damage under the tank’s threat. Fine on a boss '
            'with a solid tank, expensive on trash.'},
    {'id': 'flee', 'cat': 'Orders (party chat)', 'label': 'Flee',
     'cmd': '/p flee', 'desc': 'Break off and run.',
     'tip': 'The aliases runaway and warning do the same thing. The wipe-recovery button.'},
    {'id': 'grind', 'cat': 'Orders (party chat)', 'label': 'Grind here',
     'cmd': '/p grind', 'desc': 'Kill things in this area on their own initiative.',
     'tip': 'Bots pick their own targets nearby instead of waiting on you. follow ends it.'},
    {'id': 'rti', 'cat': 'Orders (party chat)', 'label': 'Set focus icon',
     'cmd': '/p rti {icon}', 'desc': 'Which raid target icon the bots treat as the kill order.',
     'tip': 'Pair it with pull rti and with skull-first marking, and the party focuses without '
            'you having to retarget for them.',
     'args': [{'key': 'icon', 'type': 'select', 'label': 'Icon', 'options': RTI_ICONS}]},
    {'id': 'ready', 'cat': 'Orders (party chat)', 'label': 'Ready check',
     'cmd': '/p ready', 'desc': 'Everyone reports whether they are set.',
     'tip': 'Bots answer with health, mana and buff state, so it doubles as a pre-pull check.'},
    {'id': 'rebuff', 'cat': 'Orders (party chat)', 'label': 'Rebuff',
     'cmd': '/p rebuff', 'desc': 'Recast raid buffs now.',
     'tip': 'Forces the buff pass instead of waiting for the timer.'},
    {'id': 'release', 'cat': 'Orders (party chat)', 'label': 'Release spirit',
     'cmd': '/p release', 'desc': 'Corpse-run after a wipe.',
     'tip': 'revive instead takes the spirit healer, and its resurrection sickness with it.'},
    {'id': 'summon', 'cat': 'Orders (party chat)', 'label': 'Summon to me',
     'cmd': '/p summon', 'desc': 'Teleport the bots to where you stand.',
     'tip': 'The fix when a bot is stuck on terrain or left behind at a zone line.'},
    {'id': 'teleport', 'cat': 'Orders (party chat)', 'label': 'Teleport to master',
     'cmd': '/p teleport', 'desc': 'The same idea, driven from the bot side.',
     'tip': 'Works in places summon does not, such as across an instance portal.'},
    {'id': 'formation', 'cat': 'Orders (party chat)', 'label': 'Formation',
     'cmd': '/p formation {name}', 'desc': 'How the party arranges itself around you.',
     'tip': 'melee keeps everyone close, queue is single file for narrow corridors, arrow puts '
            'ranged behind. Send formation with no argument to hear the current one.',
     'args': [{'key': 'name', 'type': 'text', 'label': 'Name', 'default': '',
               'placeholder': 'melee / queue / arrow'}]},

    # -------- chat orders: information --------
    {'id': 'stats', 'cat': 'Ask (party chat)', 'label': 'Stats',
     'cmd': '/p stats', 'desc': 'Level, health, mana, gold, bag space, gear score.',
     'tip': 'The quickest way to see whether a gearing command actually landed.'},
    {'id': 'spells', 'cat': 'Ask (party chat)', 'label': 'Spells',
     'cmd': '/p spells', 'desc': 'What the bot knows how to cast.',
     'tip': 'Long output. Whisper it to one bot rather than asking the whole party.'},
    {'id': 'who', 'cat': 'Ask (party chat)', 'label': 'Who',
     'cmd': '/p who', 'desc': 'Name, class, spec and level.',
     'tip': 'Handy right after addclass, when you do not yet know what walked up.'},
    {'id': 'help', 'cat': 'Ask (party chat)', 'label': 'Help',
     'cmd': '/p help', 'desc': 'The bot lists the commands it accepts.',
     'tip': 'Authoritative for the build you are actually running, which is why it is worth '
            'checking against this page rather than trusting either alone.'},
    {'id': 'quests_chat', 'cat': 'Ask (party chat)', 'label': 'Quests',
     'cmd': '/p quests', 'desc': 'The bot’s quest log.',
     'tip': 'quests co narrows it to the completed ones.'},
    {'id': 'reputation', 'cat': 'Ask (party chat)', 'label': 'Reputation',
     'cmd': '/p reputation', 'desc': 'Standing with the factions that gate attunements.',
     'tip': 'Alias: rep.'},
    {'id': 'co', 'cat': 'Ask (party chat)', 'label': 'Combat strategies',
     'cmd': '/p co {arg}', 'desc': 'Read or edit what the bot does in a fight.',
     'tip': 'A bare ? lists the strategies currently running. + adds one, - removes one, ~ '
            'toggles and ! resets to the class default. Comma-separate to change several at '
            'once, with no spaces: co +threat,-aoe. Changes made with + - ~ or ! are saved and '
            'survive a relog.',
     'args': [{'key': 'arg', 'type': 'text', 'label': 'Argument', 'default': '?',
               'placeholder': '?  or  +threat,-aoe'}]},
    {'id': 'nc', 'cat': 'Ask (party chat)', 'label': 'Non-combat strategies',
     'cmd': '/p nc {arg}', 'desc': 'The same, for everything outside a fight.',
     'tip': 'Where looting, food, mounts and travel live. Random bots refuse loot changes from a '
            'non-GM master, so nc -loot is GM-only on pool bots.',
     'args': [{'key': 'arg', 'type': 'text', 'label': 'Argument', 'default': '?',
               'placeholder': '?  or  +loot'}]},
    {'id': 'de', 'cat': 'Ask (party chat)', 'label': 'Dead strategies',
     'cmd': '/p de {arg}', 'desc': 'What the bot does while dead.',
     'tip': 'Controls whether it corpse-runs on its own or waits to be resurrected.',
     'args': [{'key': 'arg', 'type': 'text', 'label': 'Argument', 'default': '?',
               'placeholder': '?'}]},
]

# The approved vocabulary. Every token below is one the server parses; the point of
# collecting them is that several of these lists look interchangeable and are not -
# init= and autogear disagree about legendary, and the class tokens are the only
# ones that are case sensitive.
NOMEN = [
    {'name': 'Targeting a bot',
     'note': 'The last argument of every .playerbots bot command. Same rules throughout.',
     'items': [
         {'t': '(leave blank)', 'd': 'Whoever you currently have targeted. The usual way.'},
         {'t': '*', 'd': 'Every member of your group except you. Needs you to be in a group.'},
         {'t': '!', 'd': 'Every bot you have added, anywhere. Needs security above GM.'},
         {'t': 'Name', 'd': 'One character by name. Capitalisation is normalised for you.'},
         {'t': 'One,Two', 'd': 'Several names, comma separated, with no spaces after the comma.'},
     ]},
    {'name': 'Class tokens',
     'note': 'For addclass, and the only list here that is case sensitive - lowercase only.',
     'items': [
         {'t': 'warrior', 'd': ''}, {'t': 'paladin', 'd': ''}, {'t': 'hunter', 'd': ''},
         {'t': 'rogue', 'd': ''}, {'t': 'priest', 'd': ''},
         {'t': 'dk', 'd': 'Death Knight. Not "deathknight". Needs you to be level 55.'},
         {'t': 'shaman', 'd': ''}, {'t': 'mage', 'd': ''}, {'t': 'warlock', 'd': ''},
         {'t': 'druid', 'd': ''},
     ]},
    {'name': 'Gender tokens',
     'note': 'Optional third word of addclass. Case insensitive, unlike the class.',
     'items': [{'t': 'male', 'd': 'Same as 0.'}, {'t': 'female', 'd': 'Same as 1.'},
               {'t': '0', 'd': 'Male.'}, {'t': '1', 'd': 'Female.'},
               {'t': '(omit)', 'd': 'Whatever the pool hands you.'}]},
    {'name': 'Quality tokens for init=',
     'note': 'Attached with an equals sign and no space: init=blue. A bare number is a GEAR '
             'SCORE cap, not an item level.',
     'items': [
         {'t': 'white', 'd': 'Also common.'},
         {'t': 'green', 'd': 'Also uncommon.'},
         {'t': 'blue', 'd': 'Also rare. The usual choice for a dungeon party.'},
         {'t': 'epic', 'd': 'Also purple.'},
         {'t': 'legendary', 'd': 'Also yellow. Note: NOT orange - that is autogear’s word.'},
         {'t': 'auto', 'd': 'Cap just under your own gear, scaled by '
                            'AutoInitEquipLevelLimitRatio. Keeps a party in step as you level.'},
         {'t': '350', 'd': 'Any positive number: a gear score ceiling.'},
     ]},
    {'name': 'Quality tokens for initself=',
     'note': 'A shorter list than init= - the colour aliases do not exist here.',
     'items': [{'t': 'uncommon', 'd': ''}, {'t': 'rare', 'd': ''}, {'t': 'epic', 'd': ''},
               {'t': 'legendary', 'd': ''},
               {'t': '350', 'd': 'Any positive number: a gear score ceiling.'}]},
    {'name': 'Arguments for autogear',
     'note': 'Separate word, no equals sign: autogear blue. A bare number is an ITEM LEVEL. '
             'Every request is capped at AutoGearQualityLimit.',
     'items': [
         {'t': '(none)', 'd': 'Gear to the configured caps.'},
         {'t': 'white', 'd': 'Also common.'}, {'t': 'green', 'd': 'Also uncommon.'},
         {'t': 'blue', 'd': 'Also rare.'}, {'t': 'purple', 'd': 'Also epic.'},
         {'t': 'orange', 'd': 'Also legendary. Note: NOT yellow - that is init=’s word.'},
         {'t': 'artifact', 'd': ''}, {'t': 'heirloom', 'd': ''},
         {'t': 'match', 'd': 'Match your own average item level over the slots you have filled.'},
         {'t': '200', 'd': 'Any positive whole number: a target item level.'},
         {'t': 'reset', 'd': 'Strip everything first. Combines: reset green, reset match, '
                             'reset 200.'},
         {'t': 'bis', 'd': 'Best in slot from playerbots_bis_gear. Needs AutoGearBisCommand = 1 '
                           'and AutoGearQualityLimit = 4.'},
     ]},
    {'name': 'Strategy prefixes for co, nc and de',
     'note': 'Comma separate several with no spaces: co +threat,-aoe',
     'items': [
         {'t': '?', 'd': 'List what is running. Changes nothing.'},
         {'t': '+name', 'd': 'Add a strategy. Saved, and survives a relog.'},
         {'t': '-name', 'd': 'Remove one. Saved.'},
         {'t': '~name', 'd': 'Toggle. Saved.'},
         {'t': '!', 'd': 'Reset to the class default for that state. Saved.'},
     ]},
    {'name': 'Where you type it',
     'note': 'Chat routing is what decides how many bots hear an order - the command text is '
             'identical either way.',
     'items': [
         {'t': '/w Botname cmd', 'd': 'That one bot only.'},
         {'t': '/p cmd', 'd': 'Every bot in your party.'},
         {'t': '/raid cmd', 'd': 'Every bot in your raid. The 40-man answer.'},
         {'t': '/g cmd', 'd': 'Every bot in your guild, in or out of the group.'},
         {'t': '/1 cmd', 'd': 'A custom channel reaches every bot you have added.'},
         {'t': '.playerbots bot ...', 'd': 'Typed anywhere; targeting is by the argument, not '
                                           'by the channel.'},
     ]},
]


# mod-raid-roster. A deterministic 39-bot raid pinned to fixed slots - the player is
# the 40th - so the same character fills the same job every session. Unlike the
# playerbot commands above these are a module of ours, not upstream, which is why the
# tips here cite our own source rather than the server's own help output.
#
# Every one of these is declared Console::Yes but every handler bails with "Run this
# in-world as a player", so console is not an option and copy is the only honest mode.
RAID = [
    # -------- roster --------
    {'id': 'rr_create', 'cat': 'Roster', 'label': 'Create the roster',
     'cmd': '.raidroster create',
     'desc': 'Pins 39 addclass characters to fixed slots. Run once, ever.',
     'tip': 'All-or-nothing: it pre-flights every class before writing a single row, so a '
            'shortage leaves no partial roster and reports every deficient class in one pass. '
            'A character only counts as available if it is an addclass character of the right '
            'class and faction, currently offline, not already pinned to another roster, not '
            'held by mod-arena-roster, and not in a real (non-synthetic) guild. Refuses '
            'outright if you already have a roster - delete it first.',
     'gate': ['raidEnable', 'poolSize']},
    {'id': 'rr_status', 'cat': 'Roster', 'label': 'Show the roster',
     'cmd': '.raidroster status',
     'desc': 'Every slot with its class, role and spec, plus role totals and online count.',
     'tip': 'The one to reach for first. It also flags stale slots - a slot whose character was '
            'deleted or left the pool - which is the usual reason a login comes up short.'},
    {'id': 'rr_remove', 'cat': 'Roster', 'label': 'Delete the roster',
     'cmd': '.raidroster remove confirm',
     'desc': 'Unpins all 39 characters and hands them back to the shared addclass pool.',
     'tip': 'The literal word confirm is required and is already included here - without it the '
            'command only prints a warning and changes nothing. This deletes the pinning, not '
            'the characters.'},

    # -------- session --------
    {'id': 'rr_login', 'cat': 'Bring them in', 'label': 'Log in a raid size',
     'cmd': '.raidroster login {size}',
     'desc': 'Brings the first N slots online for the raid size you pick.',
     'tip': 'Presets are strict subsets - the first N slots of one master table - so 10-man '
            'always contains the 5-man core, 25 contains 20, and so on. Sizes COUNT YOU, so 25 '
            'fields 24 bots. The command also accepts a trailing tank/heal/dps word, but it is '
            'deliberately ignored (deterministic mode always fields the same slots), so it is '
            'left off here rather than offering a control that does nothing.',
     'args': [{'key': 'size', 'type': 'select', 'label': 'Raid size',
               'options': RAID_SIZES, 'default': '25'}],
     'gate': ['raidEnable', 'maxAddedBots']},
    {'id': 'rr_logout', 'cat': 'Bring them in', 'label': 'Log out the roster',
     'cmd': '.raidroster logout',
     'desc': 'Sends every roster bot of yours offline.',
     'tip': 'Only touches bots in YOUR roster, so it is safe while random pool bots are '
            'running. Logging them out is also how you free the slots for a different size.'},

    # -------- gear and specs --------
    {'id': 'rr_sync', 'cat': 'Gear & specs', 'label': 'Sync the whole roster',
     'cmd': '.raidroster sync',
     'desc': 'Re-levels and re-gears every ONLINE roster bot to you, at its slot spec.',
     'tip': 'Gears to YOUR level and a band around your average item level, picking a tier set '
            'when one fits that band. Offline bots are skipped and counted in the reply, so log '
            'them in first. It resets each bot to its roster-default spec, which means it also '
            'undoes any role you forced with the single-bot sync below.',
     'gate': ['raidEnable', 'limitExpansion']},
    {'id': 'rr_syncone', 'cat': 'Gear & specs', 'label': 'Sync one bot',
     'cmd': '.raidroster syncone {name} {role}',
     'desc': 'The same, for one bot, optionally forcing a different role.',
     'tip': 'Leave the role on Roster default for the slot’s own spec. A forced role fails '
            'cleanly if that class cannot fill it. Remember that a full sync reverts it - the '
            'command tells you so itself when you use it.',
     'args': [{'key': 'name', 'type': 'text', 'label': 'Bot name', 'default': '',
               'placeholder': 'Botname', 'req': True},
              {'key': 'role', 'type': 'select', 'label': 'Force role', 'options': RAID_ROLES}],
     'gate': ['raidEnable', 'limitExpansion']},

    # -------- instances --------
    {'id': 'rr_reset', 'cat': 'Instances', 'label': 'Clear instance locks',
     'cmd': '.raidroster reset',
     'desc': 'Drops saved instance IDs for you and every roster bot at once.',
     'tip': 'Why this exists as one command: 39 bots each carry their own instance binds, and a '
            'raid that half-remembers last night’s lockout puts some bots in a fresh instance '
            'and some in the old one. Reports the total number of locks cleared.',
     'gate': ['raidEnable']},
]

# Preset -> bot count and role split, transcribed from RaidRosterComp.h's master slot
# table (which asserts these same numbers at compile time). Shown in the panel so the
# size picker means something before you commit to it.
RAID_PRESETS = [
    {'size': 5,  'bots': 4,  'tank': 1, 'heal': 1,  'dps': 2,  'note': '5-man core'},
    {'size': 10, 'bots': 9,  'tank': 2, 'heal': 2,  'dps': 5,  'note': 'core + 10-man'},
    {'size': 20, 'bots': 19, 'tank': 2, 'heal': 5,  'dps': 12, 'note': 'ZG / AQ20'},
    {'size': 25, 'bots': 24, 'tank': 3, 'heal': 6,  'dps': 15, 'note': '20-man + 25-man'},
    {'size': 40, 'bots': 39, 'tank': 4, 'heal': 10, 'dps': 25, 'note': 'every roster slot'},
]


def raid_build(cmd_id, args):
    return _cmd_build(RAID, cmd_id, args)


def bots_specs():
    """The named talent builds this server actually offers, read live.

    They come from AiPlayerbot.PremadeSpecName.<class>.<n> in playerbots.conf rather
    than from a list kept here, because 'talents spec' matches against whatever that
    file says - a copy kept in this file would go stale the day it is edited.
    """
    names = {c['id']: c['t'] for c in BOT_CLASSES}
    found = {}
    try:
        with open(PB_CONF, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                m = re.match(r'\s*AiPlayerbot\.PremadeSpecName\.(\d+)\.(\d+)\s*=\s*(.+)', line)
                if m:
                    found.setdefault(int(m.group(1)), []).append(
                        (int(m.group(2)), m.group(3).strip()))
    except Exception:
        return []
    out = []
    for c in BOT_CLASSES:
        rows = sorted(found.get(c['id'], []))
        if rows:
            out.append({'name': names[c['id']], 'items': [{'t': n, 'd': ''} for _, n in rows]})
    return out


def bots_gates():
    """Config values that decide whether a command works at all, read live.

    Several commands on this page are refused by the server depending on these, and
    a panel that showed them without saying so would be misrepresenting what will
    happen when you paste the line.
    """
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
    return {k: {'key': c.split('.', 1)[1], 'v': conf_get(PB_CONF, c)} for k, c in keys}


def bots_online():
    """How many characters are online right now, split into bots and people."""
    return {
        'bots': mysql_scalar(
            "SELECT COUNT(*) FROM acore_characters.characters c "
            "JOIN acore_auth.account a ON a.id=c.account "
            "WHERE c.online=1 AND a.username LIKE 'RNDBOT%';"),
        'players': mysql_scalar(
            "SELECT COUNT(*) FROM acore_characters.characters c "
            "JOIN acore_auth.account a ON a.id=c.account "
            "WHERE c.online=1 AND a.username NOT LIKE 'RNDBOT%' "
            "AND a.username NOT IN ('AHBOT','PANELGM');"),
    }


def bots_state():
    return {'commands': BOTS, 'nomenclature': NOMEN, 'specs': bots_specs(),
            'gates': bots_gates(), 'online': bots_online(),
            'character': online_character()}


def _cmd_build(catalogue, cmd_id, args):
    """Fill a catalogue entry in and hand back the exact line to paste.

    Blank optional arguments collapse away rather than leaving an empty token,
    because the server splits the command with strtok on spaces: a trailing blank
    would be read as the next argument - a gender, or a character name - and the
    whole command rejected.
    """
    entry = next((b for b in catalogue if b['id'] == cmd_id), None)
    if not entry:
        return None, 'unknown command: %s' % cmd_id
    cmd = entry['cmd']
    for a in entry.get('args', []):
        v = (args or {}).get(a['key'], '')
        v = '' if v is None else str(v).strip()
        if v == '':
            # A select with no explicit default falls back to its first option, which
            # is what a browser shows before anyone touches it. Optional selects lead
            # with a blank option, so those correctly resolve to nothing.
            if a.get('type') == 'select' and 'default' not in a:
                v = str((a.get('options') or [{}])[0].get('v', ''))
            elif str(a.get('default', '')) != '':
                v = str(a['default'])
        if v == '' and a.get('req'):
            return None, 'missing value: %s' % a.get('label', a['key'])
        cmd = cmd.replace('{%s}' % a['key'], v)
    if '{' in cmd:
        return None, 'command still has unfilled placeholders: %s' % cmd
    return ' '.join(cmd.split()), None


def bots_build(cmd_id, args):
    return _cmd_build(BOTS, cmd_id, args)


# ---------------- rates ----------------
_WC = os.path.join(CONF_DIR, 'worldserver.conf')
_IP = os.path.join(MOD_CONF, 'individualProgression.conf')

# Curated: the ~30 people actually change, out of 96 Rate.* settings. The rest stay
# in the config file rather than cluttering a UI. (file, key, label, group, lo, hi)
RATES = [
    (_WC, 'Rate.XP.Kill',      'Kill XP',        'Experience', 0.1, 100),
    (_WC, 'Rate.XP.Quest',     'Quest XP',       'Experience', 0.1, 100),
    (_WC, 'Rate.XP.Explore',   'Exploration XP', 'Experience', 0.1, 100),

    (_WC, 'Rate.Drop.Money',           'Money',      'Drop rates', 0.1, 100),
    (_WC, 'Rate.Drop.Item.Poor',       'Poor (grey)',  'Drop rates', 0.1, 100),
    (_WC, 'Rate.Drop.Item.Normal',     'Common (white)', 'Drop rates', 0.1, 100),
    (_WC, 'Rate.Drop.Item.Uncommon',   'Uncommon (green)', 'Drop rates', 0.1, 100),
    (_WC, 'Rate.Drop.Item.Rare',       'Rare (blue)',   'Drop rates', 0.1, 100),
    (_WC, 'Rate.Drop.Item.Epic',       'Epic (purple)', 'Drop rates', 0.1, 100),
    (_WC, 'Rate.Drop.Item.Legendary',  'Legendary (orange)', 'Drop rates', 0.1, 100),
    # Most boss loot comes through reference tables, so this is the one that matters.
    (_WC, 'Rate.Drop.Item.Referenced', 'Referenced (most boss loot)', 'Drop rates', 0.1, 100),

    (_WC, 'Rate.Creature.Normal.Damage',          'Trash damage',   'Difficulty', 0.1, 20),
    (_WC, 'Rate.Creature.Normal.HP',              'Trash health',   'Difficulty', 0.1, 20),
    (_WC, 'Rate.Creature.Elite.Elite.Damage',     'Elite damage',   'Difficulty', 0.1, 20),
    (_WC, 'Rate.Creature.Elite.Elite.HP',         'Elite health',   'Difficulty', 0.1, 20),
    (_WC, 'Rate.Creature.Elite.WORLDBOSS.Damage', 'Boss damage',    'Difficulty', 0.1, 20),
    (_WC, 'Rate.Creature.Elite.WORLDBOSS.HP',     'Boss health',    'Difficulty', 0.1, 20),

    (_WC, 'Rate.RewardQuestMoney',  'Quest money',        'Economy & rep', 0.1, 100),
    (_WC, 'Rate.Reputation.Gain',   'Reputation gain',    'Economy & rep', 0.1, 100),
    (_WC, 'Rate.Honor',             'Honor gain',         'Economy & rep', 0.1, 100),
    (_WC, 'Rate.Auction.Deposit',   'Auction deposit',    'Economy & rep', 0.0, 10),
    (_WC, 'Rate.Auction.Cut',       'Auction house cut',  'Economy & rep', 0.0, 10),

    (_WC, 'Rate.Rest.InGame',       'Rested XP accrual',  'Character', 0.1, 100),
    (_WC, 'Rate.Talent',            'Talent points',      'Character', 0.1, 10),
    (_WC, 'Rate.Skill.Discovery',   'Recipe discovery',   'Character', 0.1, 100),
    (_WC, 'Rate.MoveSpeed.Player',  'Player move speed',  'Character', 0.5, 5),

    # The progression module's own "authentic difficulty" knobs; its docs suggest
    # 0.5-0.6 because WotLK class scaling trivialises old content.
    (_IP, 'IndividualProgression.VanillaPowerAdjustment',   'Vanilla player damage',  'Progression feel', 0.1, 2),
    (_IP, 'IndividualProgression.VanillaHealingAdjustment', 'Vanilla player healing', 'Progression feel', 0.1, 2),
    (_IP, 'IndividualProgression.TBCPowerAdjustment',       'TBC player damage',      'Progression feel', 0.1, 2),
    (_IP, 'IndividualProgression.TBCHealingAdjustment',     'TBC player healing',     'Progression feel', 0.1, 2),
    (_IP, 'IndividualProgression.BotOnlyAdjustments',       'Apply to bots only (0/1)', 'Progression feel', 0, 1),
]


def rates_state():
    groups, out = [], {}
    for path, key, label, group, lo, hi in RATES:
        if group not in out:
            out[group] = []
            groups.append(group)
        out[group].append({'key': key, 'label': label, 'value': conf_get(path, key),
                           'min': lo, 'max': hi,
                           'file': os.path.basename(path)})
    return {'groups': [{'name': g, 'items': out[g]} for g in groups],
            'questDrops': questdrops_state(),
            'spawnRates': spawnrates_state()}


# Rates in worldserver.conf are re-read by ".reload config": every Rate.* is
# Reloadable::Yes in WorldConfig.cpp (only 7 of 493 settings are marked No, and
# none of them is a rate). Verified live - feeding Rate.Health an invalid value
# produced the reload-time validation error, which only fires if it is re-read.
#
# The one hazard is not the config system but World.cpp:
#     baseMoveSpeed[i] *= getRate(RATE_MOVESPEED_NPC);
# That mutates a global initialised once in Unit.cpp and never reset, so EVERY
# reload multiplies the previous result. It is harmless while the NPC rate is 1.0
# (x *= 1 is a no-op) and compounding at any other value - and it poisons any
# reload, not just a movespeed change, so the check belongs here rather than on
# one key. playerBaseMoveSpeed is assigned (=) not multiplied, so it only drifts
# because it reads the already-mutated baseMoveSpeed.


def _movespeed_reload_unsafe():
    v = conf_get(_WC, 'Rate.MoveSpeed.NPC')
    try:
        return float(v) != 1.0
    except (TypeError, ValueError):
        return False


def _reload_config():
    """Re-read worldserver.conf in the running world. Returns a list of notes."""
    if not running('worldserver'):
        return ['world is not running - applies at next start']
    if _movespeed_reload_unsafe():
        return ['NOT reloaded: Rate.MoveSpeed.NPC is not 1, and reloading compounds '
                'NPC speed each time (World.cpp). Restart the world to apply.']
    ok, out = soap_exec('.reload config')
    if not ok:
        return ['SOAP reload failed (%s) - restart the world to apply' % str(out)[:80]]
    return ['config reloaded - live now, no restart needed']


def set_rate(key, value):
    row = next((r for r in RATES if r[1] == key), None)
    if not row:
        return False, 'unknown rate: %s' % key
    path, _k, label, _g, lo, hi = row
    try:
        v = float(value)
    except Exception:
        return False, 'not a number: %s' % value
    if v < lo or v > hi:
        return False, '%s must be between %s and %s' % (label, lo, hi)
    # Keep integers looking like integers in the conf for readability.
    txt = str(int(v)) if v == int(v) else ('%g' % v)
    ok = conf_set(path, key, txt)
    if not ok:
        return False, 'key not found in %s' % os.path.basename(path)
    msg = '%s = %s' % (key, txt)
    if os.path.basename(path) == 'worldserver.conf':
        return True, msg + ' - ' + '; '.join(_reload_config())
    # Module configs are read by their own module, not by .reload config.
    return True, msg + ' - restart the world to apply'


# ---------------- quest drop chance (database, not config) ----------------
# There is no Rate.* for quest items: their chance lives in *_loot_template.Chance
# on rows flagged QuestRequired=1. Targeting that flag changes ONLY quest loot and
# leaves ordinary white drops untouched.
QD_TABLES = ('creature_loot_template', 'gameobject_loot_template')
QD_BACKUP = 'acore_world.questdrop_chance_backup'


LAST_SQL_ERROR = ''


def _sql(q, timeout=180):
    global LAST_SQL_ERROR
    if not MYSQL:
        LAST_SQL_ERROR = 'mysql client not found'
        return None
    try:
        argv, env = mysql_cmd('world')
        p = subprocess.run(argv + ['-e', q], capture_output=True,
                           timeout=timeout, env=env, creationflags=NO_WINDOW)
        if p.returncode != 0:
            err = p.stderr.decode('utf-8', 'replace')
            LAST_SQL_ERROR = '; '.join(
                l.strip() for l in err.splitlines()
                if l.strip() and 'password on the command line' not in l)[:300] \
                or ('mysql exited %d' % p.returncode)
            return None
        LAST_SQL_ERROR = ''
        return [l.strip() for l in p.stdout.decode('utf-8', 'replace').splitlines()
                if l.strip() and not l.startswith('mysql:')]
    except Exception as e:
        LAST_SQL_ERROR = ('%s: %s' % (type(e).__name__, e))[:300]
        return None


def _reload_loot():
    """Push the loot tables back into the running world.

    AzerothCore caches loot templates in memory at startup, so a DB change stays
    invisible until reloaded. Over SOAP that costs ~140ms and nobody gets
    disconnected; without SOAP the only route is a world restart. Report which of
    those actually happened - never let the caller assume the fast path worked.
    """
    if not running('worldserver'):
        return ['world is not running - the change applies at next start']
    log = []
    for t in QD_TABLES:
        ok, out = soap_exec('.reload %s' % t)
        if not ok:
            log.append('SOAP reload of %s failed (%s)' % (t, str(out)[:80]))
            log.append('Restart the world to apply.')
            return log
        log.append('reloaded %s in the running world' % t)
    log.append('Live now - no restart needed.')
    return log


def questdrops_state():
    rows = _sql("SELECT COUNT(*), ROUND(AVG(Chance),2), ROUND(MIN(Chance),4) "
                "FROM creature_loot_template WHERE QuestRequired=1;", timeout=60)
    have_backup = _sql("SELECT COUNT(*) FROM information_schema.tables WHERE "
                       "table_schema='acore_world' AND table_name='questdrop_chance_backup';", timeout=60)
    st = {'rows': None, 'avg': None, 'min': None, 'backedUp': False}
    if rows and '\t' in rows[0]:
        n, avg, mn = rows[0].split('\t')[:3]
        st.update({'rows': n, 'avg': avg, 'min': mn})
    if have_backup and have_backup[0] == '1':
        st['backedUp'] = True
    st['at100'] = (st['avg'] == '100.00')
    return st


def questdrops_set100():
    log = []
    # Snapshot originals once so this is reversible; never overwrite an existing backup.
    exists = _sql("SELECT COUNT(*) FROM information_schema.tables WHERE "
                  "table_schema='acore_world' AND table_name='questdrop_chance_backup';", timeout=60)
    if not exists or exists[0] != '1':
        parts = []
        for i, t in enumerate(QD_TABLES):
            sel = ("SELECT '%s' AS tbl, Entry, Item, Chance FROM %s WHERE QuestRequired=1" % (t, t))
            parts.append(sel)
        q = ("CREATE TABLE %s AS %s;" % (QD_BACKUP, ' UNION ALL '.join(parts)))
        if _sql(q) is None:
            return False, ['failed to create backup table - aborting, nothing changed']
        log.append('backed up original chances to questdrop_chance_backup')
    else:
        log.append('backup already exists - keeping the original snapshot')

    for t in QD_TABLES:
        if _sql("UPDATE %s SET Chance = 100 WHERE QuestRequired = 1;" % t) is None:
            return False, log + ['UPDATE failed on %s' % t]
        log.append('%s: quest rows set to 100%%' % t)
    log.extend(_reload_loot())
    return True, log


def questdrops_restore():
    exists = _sql("SELECT COUNT(*) FROM information_schema.tables WHERE "
                  "table_schema='acore_world' AND table_name='questdrop_chance_backup';", timeout=60)
    if not exists or exists[0] != '1':
        return False, ['no backup table found - cannot restore']
    log = []
    for t in QD_TABLES:
        q = ("UPDATE %s l JOIN %s b ON b.tbl='%s' AND b.Entry=l.Entry AND b.Item=l.Item "
             "SET l.Chance = b.Chance WHERE l.QuestRequired = 1;" % (t, QD_BACKUP, t))
        if _sql(q) is None:
            return False, log + ['restore failed on %s' % t]
        log.append('%s: original chances restored' % t)
    log.extend(_reload_loot())
    return True, log


# ---------------- spawn respawn speed (database, not config) ----------------
# There is no Rate.* for gameobject respawn either: every spawn row in
# `gameobject` carries its own spawntimesecs. Two curated categories, classified
# by what the object's loot contains rather than by name lists (covers every
# expansion): quest objects (chest-type whose loot holds a quest item - Doom
# Weed and friends) and gathering nodes (herbs / mining veins). A node that
# drops both counts as a node. Scripted spawns (spawntimesecs <= 0) are never
# touched. Originals are snapshotted once, and every apply recomputes FROM the
# snapshot, so changing speed twice never compounds.
SR_BACKUP  = 'acore_world.spawnrate_backup'
SR_APPLIED = 'acore_world.spawnrate_applied'
SR_FLOOR   = 10          # never push a respawn below 10s (and never above stock)
SR_CATS    = ('quest', 'nodes')

_SR_CLASSIFY = (
    "SELECT gt.entry,"
    " CASE WHEN MAX(it.class=7 AND it.subclass=9)=1 THEN 'nodes'"
    "      WHEN MAX(it.class=7 AND it.subclass=7)=1 THEN 'nodes'"
    "      WHEN MAX(it.class=12)=1 THEN 'quest'"
    " END AS cat"
    " FROM gameobject_template gt"
    " JOIN gameobject_loot_template glt ON glt.Entry=gt.Data1"
    " JOIN item_template it ON it.entry=glt.Item"
    " WHERE gt.type=3 GROUP BY gt.entry HAVING cat IS NOT NULL"
)


def _sr_ensure_backup():
    """Snapshot stock spawntimesecs for both categories; additive and idempotent."""
    if _sql("CREATE TABLE IF NOT EXISTS %s ("
            " guid INT UNSIGNED PRIMARY KEY,"
            " cat VARCHAR(8) NOT NULL,"
            " secs INT NOT NULL,"
            " KEY idx_cat (cat));" % SR_BACKUP) is None:
        return False
    if _sql("CREATE TABLE IF NOT EXISTS %s ("
            " cat VARCHAR(8) PRIMARY KEY,"
            " speed FLOAT NOT NULL,"
            " applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
            " ON UPDATE CURRENT_TIMESTAMP);" % SR_APPLIED) is None:
        return False
    # INSERT IGNORE keeps the first-ever snapshot even across re-applies.
    return _sql("INSERT IGNORE INTO %s (guid, cat, secs)"
                " SELECT g.guid, c.cat, g.spawntimesecs"
                " FROM gameobject g JOIN (%s) c ON c.entry = g.id"
                " WHERE g.spawntimesecs > 0;" % (SR_BACKUP, _SR_CLASSIFY),
                timeout=300) is not None


def spawnrates_state():
    st = {'cats': {}, 'backedUp': False, 'doomweedNow': None}
    have = _sql("SELECT COUNT(*) FROM information_schema.tables WHERE"
                " table_schema='acore_world' AND table_name='spawnrate_backup';",
                timeout=60)
    st['backedUp'] = bool(have and have[0] == '1')
    speeds = {}
    if st['backedUp']:
        rows = _sql("SELECT cat, speed FROM %s;" % SR_APPLIED, timeout=60)
        for r in (rows or []):
            if '\t' in r:
                c, s = r.split('\t')[:2]
                speeds[c] = s
        rows = _sql("SELECT b.cat, COUNT(*), ROUND(AVG(g.spawntimesecs)),"
                    " ROUND(AVG(b.secs))"
                    " FROM gameobject g JOIN %s b ON b.guid=g.guid"
                    " GROUP BY b.cat;" % SR_BACKUP, timeout=120)
    else:
        rows = _sql("SELECT c.cat, COUNT(*), ROUND(AVG(g.spawntimesecs)),"
                    " ROUND(AVG(g.spawntimesecs))"
                    " FROM gameobject g JOIN (%s) c ON c.entry=g.id"
                    " WHERE g.spawntimesecs > 0"
                    " GROUP BY c.cat;" % _SR_CLASSIFY, timeout=120)
    for r in (rows or []):
        parts = r.split('\t')
        if len(parts) >= 4:
            st['cats'][parts[0]] = {'spawns': parts[1], 'avgNow': parts[2],
                                    'avgStock': parts[3],
                                    'speed': speeds.get(parts[0], '1')}
    dw = _sql("SELECT MIN(spawntimesecs) FROM gameobject WHERE id=176753;", timeout=60)
    if dw and dw[0] not in ('', 'NULL'):
        st['doomweedNow'] = dw[0]
    return st


def spawnrates_set(cat, speed):
    """speed N = objects in this category respawn N times faster (1 = stock)."""
    if cat not in SR_CATS:
        return False, ['unknown category: %s' % cat]
    try:
        n = float(speed)
    except Exception:
        return False, ['not a number: %s' % speed]
    if n < 1:
        return False, ['speed must be at least 1 (stock)']
    if not _sr_ensure_backup():
        return False, ['failed to snapshot stock respawn times - nothing changed',
                       'mysql said: %s' % (LAST_SQL_ERROR or 'no error text')]
    log = ['stock respawn times snapshotted' if n != 1 else 'restoring stock']
    # Always derived from the snapshot: LEAST guards odd 2s outliers from being
    # RAISED to the floor, GREATEST keeps 1s-spam impossible at high speeds.
    q = ("UPDATE gameobject g JOIN %s b ON b.guid = g.guid"
         " SET g.spawntimesecs ="
         " LEAST(b.secs, GREATEST(CAST(b.secs / %f AS SIGNED), %d))"
         " WHERE b.cat = '%s';" % (SR_BACKUP, n, SR_FLOOR, cat))
    if _sql(q, timeout=300) is None:
        return False, log + ['UPDATE failed - run again or restore stock',
                             'mysql said: %s' % (LAST_SQL_ERROR or 'no error text')]
    _sql("REPLACE INTO %s (cat, speed) VALUES ('%s', %f);" % (SR_APPLIED, cat, n))
    # Drop saved respawn timers so a restart brings everything up immediately
    # instead of finishing old long waits. A running world may re-save a few at
    # shutdown; those still switch to the new delay after one cycle.
    _sql("DELETE gr FROM acore_characters.gameobject_respawn gr"
         " JOIN %s b ON b.guid = gr.guid WHERE b.cat = '%s';" % (SR_BACKUP, cat),
         timeout=120)
    label = 'stock speed' if n == 1 else ('%g' % n) + 'x faster'
    log.append('%s: %s applied in the database' % (cat, label))
    if running('worldserver'):
        log.append('Respawn times are cached per loaded map area - RESTART the '
                   'world to apply everywhere. Areas that load fresh use the new '
                   'times immediately.')
    else:
        log.append('World is stopped - the new times apply at next start.')
    return True, log


def spawnrates_restore(cat):
    return spawnrates_set(cat, 1)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path.startswith('/v1/'):
            try:
                if launcher.handle_get(self, u.path, q):
                    return
            except Exception as e:
                return self._json({'ok': False, 'log': ['error: %s' % e]}, 500)
            return self._json({'error': 'unknown endpoint'}, 404)
        if u.path in ('/launcher', '/launcher/'):
            self.path = '/' + launcher.LAUNCHER_PAGE
            return super().do_GET()
        if u.path == '/api/status':
            return self._json(status())
        if u.path == '/api/log':
            n = int((q.get('n') or ['180'])[0])
            return self._json({'lines': log_tail(max(20, min(n, 800)))})
        if u.path == '/api/addons':
            return self._json(addon_state())
        if u.path == '/api/rates':
            return self._json(rates_state())
        if u.path == '/api/trainer':
            return self._json(trainer_state())
        if u.path == '/api/bots':
            return self._json(bots_state())
        if u.path in ('/', '/index.html'):
            self.path = '/ui.html'
        return super().do_GET()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        ln = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(ln) or b'{}')
        except Exception:
            body = {}
        if u.path.startswith('/v1/'):
            try:
                if launcher.handle_post(self, u.path, body):
                    return
            except Exception as e:
                return self._json({'ok': False, 'log': ['error: %s' % e]}, 500)
            return self._json({'ok': False, 'log': ['unknown endpoint']}, 404)
        try:
            if u.path == '/api/start':
                return self._json({'ok': True, 'log': start_all()})
            if u.path == '/api/stop':
                return self._json({'ok': True, 'log': stop_all(True, bool(body.get('deps', False)))})
            if u.path == '/api/restart':
                msgs = [stop_graceful('worldserver')]
                time.sleep(2)
                msgs += start_all()
                return self._json({'ok': True, 'log': msgs})
            if u.path == '/api/setting':
                k, v = body.get('key'), str(body.get('value', ''))
                if k not in SETTABLE:
                    return self._json({'ok': False, 'log': ['unknown setting: %s' % k]}, 400)
                path, ckey, valid = SETTABLE[k]
                try:
                    if not valid(v):
                        raise ValueError
                except Exception:
                    return self._json({'ok': False, 'log': ['value out of range for %s' % k]}, 400)
                ok = conf_set(path, ckey, v)
                return self._json({'ok': ok, 'log': ['%s %s = %s' % ('OK  ' if ok else 'MISS', ckey, v),
                                                     'Restart the world to apply.']})
            if u.path == '/api/phase':
                return self._json({'ok': True, 'log': set_phase(body.get('phase', ''))})
            if u.path == '/api/addons/install':
                ok, log = addon_install(str(body.get('name', '')))
                return self._json({'ok': ok, 'log': log})
            if u.path == '/api/addons/remove':
                ok, log = addon_remove(str(body.get('name', '')))
                return self._json({'ok': ok, 'log': log})
            if u.path == '/api/rate':
                ok, msg = set_rate(str(body.get('key', '')), body.get('value'))
                return self._json({'ok': ok, 'log': [msg, 'Restart the world to apply.']})
            if u.path == '/api/trainer/run':
                name = online_character()
                if not name:
                    return self._json({'ok': False, 'log': ['no character found to target']})
                cmd, mode = trainer_build(str(body.get('id', '')), body.get('args') or {}, name)
                if cmd is None:
                    return self._json({'ok': False, 'log': [mode]}, 400)
                if mode == 'copy':
                    # Session-bound: hand it back for the user to paste rather than
                    # running it as console, where it would target nobody.
                    return self._json({'ok': True, 'copy': cmd,
                                       'log': ['paste this in game chat:', cmd]})
                ok, res = soap_exec(cmd)
                return self._json({'ok': ok, 'log': [cmd, res]})
            if u.path == '/api/bots/build':
                # No SOAP branch here on purpose: the world refuses every playerbot
                # command from a console session, so there is nothing to run.
                cmd, err = bots_build(str(body.get('id', '')), body.get('args') or {})
                if cmd is None:
                    return self._json({'ok': False, 'log': [err]}, 400)
                return self._json({'ok': True, 'copy': cmd,
                                   'log': ['paste this in game chat:', cmd]})
            if u.path == '/api/questdrops':
                act = str(body.get('action', ''))
                if act == 'set100':
                    ok, log = questdrops_set100()
                elif act == 'restore':
                    ok, log = questdrops_restore()
                else:
                    ok, log = False, ['unknown action: %s' % act]
                return self._json({'ok': ok, 'log': log})
            if u.path == '/api/spawnrates':
                ok, log = spawnrates_set(str(body.get('cat', '')),
                                         body.get('speed', 1))
                return self._json({'ok': ok, 'log': log})
        except Exception as e:
            return self._json({'ok': False, 'log': ['error: %s' % e]}, 500)
        self._json({'ok': False, 'log': ['unknown endpoint']}, 404)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------- launcher (/v1) ----------------
# Imported last and handed this module explicitly: control.py runs as __main__,
# so `import control` from inside launcher.py would create a second, unrelated
# copy of everything above.
import sys                                                    # noqa: E402
import launcher                                               # noqa: E402
launcher.bind(sys.modules[__name__])


if __name__ == '__main__':
    os.chdir(HERE)
    print('=' * 64)
    print(' Azeroth Control   (portable hub)')
    print(' hub    : %s' % ROOT)
    print(' mysql  : %s' % (MYSQLD or 'NOT FOUND'))
    print(' ollama : %s' % (OLLAMA or 'NOT FOUND'))
    print(' models : %s' % OLLAMA_MODELS)
    print(' open   : http://127.0.0.1:%d/' % PORT)
    print('=' * 64)
    print(' Leave this window open. Closing it stops the panel,')
    print(' but does NOT stop the game servers.')
    print('=' * 64)
    try:
        # 127.0.0.1 only: these endpoints control processes, never bind publicly.
        Server(('127.0.0.1', PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print('\nstopped.')
