# -*- coding: utf-8 -*-
"""Check that an install is actually usable, and say precisely what is not.

Written to be run by an agent as much as by a person: every check prints one
line beginning OK / WARN / FAIL, and the exit code is non-zero only if something
is genuinely broken.  A missing optional module is a WARN, never a FAIL - "you
do not run mod-playerbots" is information, not a fault.

Usage:
    python install/verify.py           # everything
    python install/verify.py --db      # just the database (the important one)
"""
from __future__ import print_function

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HUB = os.path.dirname(HERE)
PROFILES = os.path.join(HUB, 'realms', 'profiles.json')

FAILURES = []


def ok(msg):
    print('OK   %s' % msg)


def warn(msg, hint=None):
    print('WARN %s' % msg)
    if hint:
        print('     %s' % hint)


def fail(msg, hint=None):
    print('FAIL %s' % msg)
    if hint:
        print('     %s' % hint)
    FAILURES.append(msg)


def conf_get(path, key):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith('#'):
                    continue
                if '=' in s and s.split('=')[0].strip() == key:
                    return s.split('=', 1)[1].strip()
    except Exception:
        pass
    return None


def dbinfo(conf, field):
    raw = (conf_get(conf, field) or '').strip().strip('"').strip("'")
    parts = [p.strip() for p in raw.split(';')]
    keys = ('host', 'port', 'user', 'pass', 'name')
    return dict(zip(keys, parts + [''] * (len(keys) - len(parts))))


def hub(p):
    if not p:
        return None
    return p if os.path.isabs(p) else os.path.join(HUB, p.replace('/', os.sep))


def load_profile():
    if not os.path.isfile(PROFILES):
        fail('no realms/profiles.json',
             'Run:  python install/detect.py')
        return None
    try:
        doc = json.load(open(PROFILES, encoding='utf-8'))
    except Exception as e:
        fail('profiles.json is not valid JSON: %s' % e)
        return None
    realms = doc.get('realms') or []
    if not realms:
        fail('profiles.json declares no realms')
        return None
    active = doc.get('activeRealm')
    for r in realms:
        if r.get('slug') == active:
            ok('profile %r (of %d)' % (active, len(realms)))
            return r
    ok('profile %r (activeRealm not matched; using the first)' % realms[0].get('slug'))
    return realms[0]


# ------------------------------------------------------------------ the checks

def check_python():
    v = sys.version_info
    if v[:2] < (3, 8):
        fail('Python %d.%d.%d - 3.8+ required' % v[:3])
    else:
        ok('Python %d.%d.%d' % v[:3])


def check_paths(r):
    paths = r.get('paths') or {}
    conf_dir = hub(paths.get('configs'))
    if not conf_dir or not os.path.isdir(conf_dir):
        fail('configs directory missing: %s' % conf_dir,
             'paths.configs in profiles.json must point at the folder holding worldserver.conf')
        return None
    conf = os.path.join(conf_dir, 'worldserver.conf')
    if not os.path.isfile(conf):
        fail('no worldserver.conf in %s' % conf_dir)
        return None
    ok('worldserver.conf   %s' % os.path.normpath(conf))

    for key, label, hard in (('server', 'server binaries', False),
                             ('logs', 'log directory', False),
                             ('client', 'game client', False),
                             ('addons', 'addon folder', False)):
        p = hub(paths.get(key))
        if not p:
            warn('%s not declared (paths.%s)' % (label, key))
        elif not os.path.exists(p):
            warn('%s declared but missing: %s' % (label, p))
        else:
            ok('%-18s %s' % (label, os.path.normpath(p)))
    return conf


def check_db(conf):
    mysql = shutil.which('mysql') or shutil.which('mysql.exe')
    for cand in (os.path.join(HUB, 'mysql', 'bin', 'mysql.exe'),
                 r'C:\Program Files\MySQL\MySQL Server 8.4\bin\mysql.exe'):
        if not mysql and os.path.isfile(cand):
            mysql = cand

    any_bad = False
    for field, label in (('WorldDatabaseInfo', 'world'),
                         ('LoginDatabaseInfo', 'login'),
                         ('CharacterDatabaseInfo', 'character')):
        d = dbinfo(conf, field)
        if not d.get('host'):
            fail('%s missing from worldserver.conf' % field)
            any_bad = True
            continue
        ok('%-9s %s:%s  user=%s  db=%s  pass=%s'
           % (label, d['host'], d['port'], d['user'], d['name'],
              '*' * len(d['pass']) if d['pass'] else '(EMPTY)'))
    if any_bad:
        return

    d = dbinfo(conf, 'WorldDatabaseInfo')

    # Reachability first: a refused TCP connect is a different problem from bad
    # credentials, and saying so saves the user from re-checking the password.
    try:
        s = socket.create_connection((d['host'], int(d['port'] or 3306)), 4)
        s.close()
        ok('MySQL reachable at %s:%s' % (d['host'], d['port']))
    except Exception as e:
        fail('cannot reach MySQL at %s:%s (%s)' % (d['host'], d['port'], e),
             'Is it running? In a container, use the MAPPED port, not the internal one.')
        return

    if not mysql:
        warn('no mysql client on PATH - cannot test credentials',
             'The panel needs it for the Reference tab and population counts.')
        return

    env = dict(os.environ, MYSQL_PWD=d['pass'])
    argv = [mysql, '--host=' + d['host'], '--port=' + str(d['port'] or 3306),
            '--user=' + d['user'], '--batch', '--skip-column-names', '-e', 'SELECT 1;']
    try:
        p = subprocess.run(argv, capture_output=True, timeout=15, env=env)
    except Exception as e:
        fail('mysql client failed to run: %s' % e)
        return
    if p.returncode != 0:
        err = p.stderr.decode('utf-8', 'replace').strip().splitlines()
        fail('SELECT 1 failed: %s' % (err[-1] if err else 'exit %d' % p.returncode),
             'The password in worldserver.conf may be stale relative to the MySQL user.')
        return
    ok('SELECT 1 as %s' % d['user'])

    if d['name']:
        argv[-1] = 'SELECT COUNT(*) FROM `%s`.item_template;' % d['name']
        p = subprocess.run(argv, capture_output=True, timeout=30, env=env)
        n = p.stdout.decode('utf-8', 'replace').strip()
        if p.returncode == 0 and n.isdigit() and int(n) > 1000:
            ok('%s.item_template has %s rows' % (d['name'], n))
        else:
            fail('%s.item_template looks empty or missing' % d['name'],
                 'The world database import did not finish. Re-run it before starting the server.')


def check_ports(r, conf):
    if not (r.get('controls') or {}).get('serverManaged', True):
        ok('serverManaged is false - lifecycle checks skipped by design')
        return
    world = conf_get(conf, 'WorldServerPort') or '8085'
    soap = conf_get(conf, 'SOAP.Port') or '7878'
    for port, label in ((3724, 'auth'), (int(world), 'world'), (int(soap), 'SOAP')):
        s = socket.socket()
        s.settimeout(0.5)
        up = s.connect_ex(('127.0.0.1', port)) == 0
        s.close()
        (ok if up else warn)('%-5s port %-5d %s' % (label, port, 'listening' if up else 'closed'))


def check_modules(r):
    modconf = hub((r.get('paths') or {}).get('modconfigs'))
    if not modconf or not os.path.isdir(modconf):
        warn('no module config directory - all optional features will be hidden')
        return
    known = [('playerbots.conf', 'mod-playerbots', 'Tools > Bots'),
             ('individualProgression.conf', 'mod-individual-progression', 'progression tier'),
             ('mod_ahbot.conf', 'mod-ah-bot', 'auction house tunables'),
             ('mod_ollama_chat.conf', 'mod-ollama-chat', 'LLM chat settings'),
             ('AutoBalance.conf', 'mod-autobalance', 'difficulty tunables'),
             ('mod_raid_roster.conf', 'mod-raid-roster', 'Tools > Raid')]
    for fn, mod, what in known:
        if os.path.isfile(os.path.join(modconf, fn)):
            ok('%-28s -> %s' % (mod, what))
        else:
            print('  -  %-28s not installed (%s hidden)' % (mod, what))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', action='store_true', help='database checks only')
    a = ap.parse_args()

    print('Azeroth Control - install check')
    print('hub: %s' % HUB)
    print()

    if not a.db:
        check_python()
    r = load_profile()
    if not r:
        sys.exit(1)

    print()
    conf = check_paths(r)
    if not conf:
        sys.exit(1)

    print()
    check_db(conf)

    if not a.db:
        print()
        check_ports(r, conf)
        print()
        check_modules(r)

    print()
    if FAILURES:
        print('%d problem(s):' % len(FAILURES))
        for f in FAILURES:
            print('  - %s' % f)
        print()
        print('See docs/TROUBLESHOOTING.md, indexed by the error text above.')
        sys.exit(1)
    print('All checks passed.  Start the panel:  python control/control.py')


if __name__ == '__main__':
    main()
