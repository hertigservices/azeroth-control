"""Enable SOAP and create a dedicated GM account for the panel's Trainer tab.

SOAP is AzerothCore's remote-command interface. It executes commands in a CONSOLE
context, so only commands that accept a player NAME can be driven remotely -
anything relying on your in-game selection cannot be.

A dedicated account is used rather than the player's own: the password has to be
stored for the panel to authenticate, and that should never be the account you log
in with. SOAP is bound to 127.0.0.1 only.
"""
import hashlib, os, secrets, subprocess, sys

# The hub is this file's grandparent (tools/ is one level down), so a clone
# anywhere works. AZCTL_HOME overrides it for a hub on another drive.
ROOT = os.environ.get('AZCTL_HOME') or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
MYSQL = os.path.join(ROOT, 'mysql', 'bin', 'mysql.exe')
WCONF = os.path.join(ROOT, 'server', 'configs', 'worldserver.conf')
CREDS = os.path.join(ROOT, 'credentials.txt')
SOAPCFG = os.path.join(ROOT, 'control', 'soap.json')

USER = 'PANELGM'
N = int('894B645E89E1535BBDAD5B8B290650530801B18EBFBF5E8FAB3C82872A3E9BB7', 16)
G = 7


def sql(q):
    p = subprocess.run([MYSQL, '-h127.0.0.1', '-uacore', '-pacore', '--batch',
                        '--skip-column-names', '-e', q], capture_output=True, timeout=60)
    if p.returncode != 0:
        print('SQL FAILED:', p.stderr.decode('utf-8', 'replace')[:300]); sys.exit(1)
    return [l.strip() for l in p.stdout.decode('utf-8', 'replace').splitlines()
            if l.strip() and not l.startswith('mysql:')]


def srp6(user, pw):
    salt = secrets.token_bytes(32)
    h1 = hashlib.sha1(('%s:%s' % (user.upper(), pw.upper())).encode()).digest()
    h2 = hashlib.sha1(salt + h1).digest()
    return salt, pow(G, int.from_bytes(h2, 'little'), N).to_bytes(32, 'little')


def conf_set(key, value):
    with open(WCONF, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    hit = False
    for i, l in enumerate(lines):
        s = l.strip()
        if not hit and '=' in s and s.split('=')[0].strip() == key:
            lines[i] = '%s = %s\n' % (key, value); hit = True
    if hit:
        with open(WCONF, 'w', encoding='utf-8', newline='') as f:
            f.writelines(lines)
    print('  %-22s = %-14s %s' % (key, value, '' if hit else '(KEY NOT FOUND)'))
    return hit


row = sql("SELECT id FROM acore_auth.account WHERE username='%s';" % USER)
if row:
    acct = int(row[0]); pw = None
    print('account %s already exists (id %d)' % (USER, acct))
else:
    pw = secrets.token_hex(8).upper()[:12]
    salt, ver = srp6(USER, pw)
    sql("INSERT INTO acore_auth.account (username,salt,verifier,expansion,email,reg_mail) "
        "VALUES ('%s',UNHEX('%s'),UNHEX('%s'),2,'','');"
        % (USER, salt.hex().upper(), ver.hex().upper()))
    acct = int(sql("SELECT id FROM acore_auth.account WHERE username='%s';" % USER)[0])
    print('created account %s (id %d)' % (USER, acct))

# gmlevel 3 on all realms (-1) so every command is permitted.
sql("INSERT INTO acore_auth.account_access (id,gmlevel,RealmID) VALUES (%d,3,-1) "
    "ON DUPLICATE KEY UPDATE gmlevel=3;" % acct)
print('granted gmlevel 3')

print('worldserver.conf:')
conf_set('SOAP.Enabled', 1)
conf_set('SOAP.IP', '"127.0.0.1"')
conf_set('SOAP.Port', 7878)

if pw:
    import json
    with open(SOAPCFG, 'w', encoding='utf-8') as f:
        json.dump({'user': USER, 'pass': pw, 'host': '127.0.0.1', 'port': 7878}, f)
    with open(CREDS, 'a', encoding='utf-8') as f:
        f.write('\nPanel SOAP account (created by setup-soap.py)\n')
        f.write('-----------------------------------------------------------\n')
        f.write('Username: %s\nPassword: %s\n' % (USER, pw))
        f.write('Used only by the control panel to run GM commands remotely.\n')
        f.write('SOAP listens on 127.0.0.1:7878 - not reachable from outside this PC.\n')
    print('wrote', SOAPCFG, 'and appended to credentials.txt')
else:
    print('NOTE: account existed; soap.json left as-is')

print('\nRestart the world to activate SOAP.')
