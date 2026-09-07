"""Switch which realm profile owns the shared ports - command line front-end.

The engine lives in control/realms.py so that this CLI and the launcher's Switch
button cannot drift apart: one graceful-stop path, one start path, one writer for
realms/profiles.json. Everything interesting is documented there.

Usage:
    python switch-realm.py --list
    python switch-realm.py --to spelldraft
    python switch-realm.py --to vanilla
    python switch-realm.py --stop
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'control'))
import control                                                   # noqa: E402
import realms                                                    # noqa: E402

realms.bind(control)


def say(line):
    print('  %s' % line)


def cmd_list(doc):
    active = doc.get('activeRealm')
    print('%-12s %-8s %-9s %-10s %s' % ('SLUG', 'KIND', 'STATUS', 'INSTALLED', 'NAME'))
    for r in sorted(doc.get('realms', []), key=lambda x: x.get('order', 99)):
        mark = '*' if r['slug'] == active else ' '
        inst = '-' if r.get('kind') != 'realm' else ('yes' if realms.installed(r) else 'no')
        print('%s%-11s %-8s %-9s %-10s %s' % (mark, r['slug'], r.get('kind', 'realm'),
                                              r.get('status', '?'), inst, r.get('name', '')))
    world, auth = control.running('worldserver'), control.running('authserver')
    print('\nworldserver: %s   authserver: %s'
          % ('up pid %s' % world.get('pid') if world['up'] else 'down',
             'up pid %s' % auth.get('pid') if auth['up'] else 'down'))
    print('* = activeRealm in profiles.json')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--list', action='store_true', help='show every profile and what is running')
    ap.add_argument('--to', metavar='SLUG', help='switch the running realm to SLUG')
    ap.add_argument('--stop', action='store_true', help='stop the running realm, start nothing')
    args = ap.parse_args()

    doc = realms.load()
    if doc.get('error'):
        print(doc['error'])
        return 1

    if args.stop:
        current = realms.find(doc, doc.get('activeRealm'))
        print('Stopping %s ...' % ((current or {}).get('name') or 'the running realm'))
        realms.stop_realm(current, say)
        return 0

    if args.to:
        ok, _ = realms.switch_to(args.to, say)
        return 0 if ok else 1

    return cmd_list(doc)


if __name__ == '__main__':
    sys.exit(main())
