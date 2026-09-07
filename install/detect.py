# -*- coding: utf-8 -*-
"""Find an existing AzerothCore install and write a realm profile for it.

The panel's whole portability story rests on one idea: every AzerothCore install
already describes itself, in worldserver.conf.  Database host, port, user,
password and schema names live on the *DatabaseInfo lines; the world port lives
on WorldServerPort; SOAP has its own block.  Reading those beats asking the user
to retype them, and it is the only approach that survives a Docker stack, a
renamed schema or a real password.

So this script does not interrogate the machine so much as find that one file and
believe it.

Usage:
    python install/detect.py                 # search, then write realms/profiles.json
    python install/detect.py --conf PATH     # skip the search, use this conf
    python install/detect.py --dry-run       # print the profile, write nothing
"""
from __future__ import print_function

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HUB = os.path.dirname(HERE)
PROFILES = os.path.join(HUB, 'realms', 'profiles.json')

# Ordered by how likely a hit is to be the real install rather than a stray copy.
# .conf.dist is deliberately NOT matched: it is the shipped template, and its
# DatabaseInfo lines are the stock placeholders, not this machine's truth.
CONF_HINTS = [
    'server/configs/worldserver.conf',
    'server/etc/worldserver.conf',
    'etc/worldserver.conf',
    'configs/worldserver.conf',
    'bin/configs/worldserver.conf',
    'env/dist/etc/worldserver.conf',      # the layout acore-docker mounts
]
ROOT_HINTS = [
    r'C:\AzerothCore', r'C:\azerothcore', r'C:\acore', r'C:\acore-docker',
    r'C:\Server', r'C:\wow', r'C:\Games\AzerothCore',
    os.path.expanduser('~/azerothcore-wotlk'),
    os.path.expanduser('~/AzerothCore'),
    '/opt/azerothcore', '/srv/azerothcore', '/home/acore',
]


# --------------------------------------------------------------------- reading

def conf_get(path, key):
    """One value from an AzerothCore .conf. Returns None if absent.

    Comments in these files are '#' at the start of a line; a '#' mid-line is
    part of the value (passwords contain them), so this does not strip inline.
    """
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
    """host/port/user/pass/name from a "a;b;c;d;e" DatabaseInfo value."""
    raw = (conf_get(conf, field) or '').strip().strip('"').strip("'")
    parts = [p.strip() for p in raw.split(';')]
    keys = ('host', 'port', 'user', 'pass', 'name')
    out = dict(zip(keys, parts + [''] * (len(keys) - len(parts))))
    return out


# --------------------------------------------------------------------- finding

def find_conf():
    """Locate worldserver.conf without walking the whole filesystem.

    Hint paths first, then a bounded walk of each drive root. An unbounded
    Get-ChildItem -Recurse over C: takes minutes and turns up backups and build
    trees that are not the live install, so depth is capped hard.
    """
    seen = []
    for root in ROOT_HINTS:
        for rel in CONF_HINTS:
            p = os.path.join(root, rel.replace('/', os.sep))
            if os.path.isfile(p):
                seen.append(os.path.abspath(p))

    if not seen:
        for root in ROOT_HINTS:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                depth = dirpath[len(root):].count(os.sep)
                if depth >= 4:
                    dirnames[:] = []
                    continue
                dirnames[:] = [d for d in dirnames
                               if d not in ('.git', 'build', 'node_modules', 'deps')]
                if 'worldserver.conf' in filenames:
                    seen.append(os.path.join(dirpath, 'worldserver.conf'))

    # De-duplicate, keeping order.
    out = []
    for p in seen:
        if p not in out:
            out.append(p)
    return out


def find_near(conf_dir, names, extra_roots=()):
    """First existing path among `names`, searched upward from the config dir.

    Layouts differ - binaries beside the configs, one level up in bin/, or in a
    sibling - so this walks up rather than assuming any one of them.
    """
    d = conf_dir
    roots = []
    for _ in range(4):
        roots.append(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    roots.extend(extra_roots)
    for r in roots:
        for n in names:
            p = os.path.join(r, n.replace('/', os.sep))
            if os.path.exists(p):
                return os.path.abspath(p)
    return None


def find_client():
    """A WoW 3.3.5a client directory, by its executable.

    Never downloads anything and never looks inside a Battle.net install: this
    is the user's own 3.3.5a copy or nothing.
    """
    for root in [r'C:\AzerothCore\client', r'C:\WoW', r'C:\WoW335',
                 r'C:\Games\WoW', r'C:\World of Warcraft 3.3.5a',
                 r'C:\Program Files (x86)\World of Warcraft 3.3.5a',
                 os.path.expanduser('~/WoW'), os.path.expanduser('~/Desktop/WoW')]:
        exe = os.path.join(root, 'Wow.exe')
        if os.path.isfile(exe):
            return os.path.abspath(root)
    return None


# --------------------------------------------------------------------- profile

def rel(path, base):
    """Hub-relative where possible - profiles.json prefers it - else absolute."""
    if not path:
        return None
    try:
        r = os.path.relpath(path, base)
    except ValueError:                      # different drive on Windows
        return path.replace('\\', '/')
    if r.startswith('..'):
        return path.replace('\\', '/')
    return r.replace('\\', '/')


def build_profile(conf, managed):
    conf_dir = os.path.dirname(os.path.abspath(conf))
    world = dbinfo(conf, 'WorldDatabaseInfo')
    login = dbinfo(conf, 'LoginDatabaseInfo')
    chars = dbinfo(conf, 'CharacterDatabaseInfo')

    server_bin = find_near(conf_dir, ['worldserver.exe', 'worldserver',
                                      'bin/worldserver.exe', 'bin/worldserver'])
    server_dir = os.path.dirname(server_bin) if server_bin else os.path.dirname(conf_dir)
    logs = find_near(conf_dir, ['logs', 'Logs']) or os.path.join(server_dir, 'logs')
    client = find_client()

    prof = {
        'slug': 'default',
        'name': 'My Realm',
        'tagline': 'AzerothCore 3.3.5a',
        'order': 0,
        'kind': 'realm',
        'enabled': True,
        'status': 'live',
        'accent': '#c9aa71',
        'paths': {
            'server': rel(server_dir, HUB),
            'configs': rel(conf_dir, HUB),
            'modconfigs': rel(os.path.join(conf_dir, 'modules'), HUB),
            'logs': rel(logs, HUB),
        },
        'db': {
            'auth': login.get('name') or 'acore_auth',
            'world': world.get('name') or 'acore_world',
            'characters': chars.get('name') or 'acore_characters',
            'user': world.get('user') or 'acore',
        },
        'controls': {
            # serverManaged drives whether Server/World/Tools appear at all. It
            # is asked for rather than guessed: a panel that believes it owns
            # processes it cannot signal shows Stop buttons that do nothing.
            'serverManaged': bool(managed),
            'phaseAware': os.path.isfile(
                os.path.join(conf_dir, 'modules', 'individualProgression.conf')),
            'botsAware': os.path.isfile(
                os.path.join(conf_dir, 'modules', 'playerbots.conf')),
            'addonsAware': bool(client),
        },
        'notes': 'Detected by install/detect.py. Edit freely - see docs/CONFIGURATION.md.',
    }
    if client:
        prof['paths']['client'] = rel(client, HUB)
        prof['paths']['addons'] = rel(os.path.join(client, 'Interface', 'AddOns'), HUB)
        prof['paths']['cache'] = rel(os.path.join(client, 'Cache'), HUB)
        prof['launch'] = {'kind': 'exe',
                          'target': rel(os.path.join(client, 'Wow.exe'), HUB),
                          'workdir': rel(client, HUB)}
    return prof, world, login, chars


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--conf', help='path to worldserver.conf; skips the search')
    ap.add_argument('--dry-run', action='store_true', help='print, write nothing')
    ap.add_argument('--unmanaged', action='store_true',
                    help='this panel will NOT start/stop the server (Docker, remote, systemd)')
    ap.add_argument('--slug', default='default')
    ap.add_argument('--name', default='My Realm')
    a = ap.parse_args()

    if a.conf:
        conf = os.path.abspath(a.conf)
        if not os.path.isfile(conf):
            sys.exit('no such file: %s' % conf)
    else:
        found = find_conf()
        if not found:
            sys.exit('No worldserver.conf found.\n'
                     'Pass one explicitly:  python install/detect.py --conf PATH\n'
                     'Looked under: %s' % ', '.join(ROOT_HINTS[:6]))
        conf = found[0]
        if len(found) > 1:
            print('Found %d configs; using the first. Others:' % len(found))
            for p in found[1:]:
                print('   ', p)
            print()

    print('worldserver.conf : %s' % conf)
    prof, world, login, chars = build_profile(conf, managed=not a.unmanaged)
    prof['slug'], prof['name'] = a.slug, a.name

    print()
    print('databases (from the config, not assumed):')
    for label, d in (('world', world), ('login', login), ('character', chars)):
        print('   %-10s %s:%s  user=%s  db=%s  pass=%s'
              % (label, d.get('host') or '?', d.get('port') or '?',
                 d.get('user') or '?', d.get('name') or '?',
                 '*' * len(d.get('pass') or '') or '(none)'))
    print()
    print('paths:')
    for k, v in sorted(prof['paths'].items()):
        print('   %-11s %s' % (k, v))
    if 'client' not in prof['paths']:
        print('   client      NOT FOUND - the Play button and Addons need one.')
        print('               Add paths.client by hand; nothing is downloaded for you.')
    print()
    print('controls:')
    for k, v in sorted(prof['controls'].items()):
        print('   %-13s %s' % (k, v))
    print()

    doc = {'schema': 1, 'activeRealm': prof['slug'], 'realms': [prof]}
    text = json.dumps(doc, indent=2)

    if a.dry_run:
        print(text)
        return

    if os.path.isfile(PROFILES):
        print('%s already exists - not overwriting.' % PROFILES)
        print('Merge this in by hand, or move the old one aside and re-run:')
        print()
        print(text)
        return

    os.makedirs(os.path.dirname(PROFILES), exist_ok=True)
    with open(PROFILES, 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    print('wrote %s' % PROFILES)
    print()
    print('Next:  python install/verify.py --db')


if __name__ == '__main__':
    main()
