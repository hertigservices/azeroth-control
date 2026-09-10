"""Switch which realm profile owns the shared ports - command line front-end.

The engine lives in control/realms.py so that this CLI and the launcher's Switch
button cannot drift apart: one graceful-stop path, one start path, one writer for
realms/profiles.json. Everything interesting is documented there.

Usage:
    python switch-realm.py --list
    python switch-realm.py --to <slug>       (slug as shown by --list)
    python switch-realm.py --stop
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'control'))
import control                                                   # noqa: E402
import realms                                                    # noqa: E402

import status
realms.bind(control)
status.bind(control, realms)


def say(line):
    print('  %s' % line)


def cmd_list(doc):
    """The registry's claims and the machine's answer, side by side.

    UP is observed - a worldserver started from that profile's own directory - and
    the * is what profiles.json says. The two agreeing is the normal case; them
    disagreeing is the thing this listing exists to show, so it is not resolved here
    in favour of either.
    """
    active = doc.get('activeRealm')
    snap = status.snapshot(include_population=False)
    seen = dict((r['slug'], r) for r in snap['realms'])
    print('%-12s %-8s %-9s %-10s %-4s %s'
          % ('SLUG', 'KIND', 'STATUS', 'INSTALLED', 'UP', 'NAME'))
    for r in sorted(doc.get('realms', []), key=lambda x: x.get('order', 99)):
        mark = '*' if r['slug'] == active else ' '
        inst = '-' if r.get('kind') != 'realm' else ('yes' if realms.installed(r) else 'no')
        me = seen.get(r['slug'])
        up = '-' if me is None else ('yes' if me['running'] else 'no')
        print('%s%-11s %-8s %-9s %-10s %-4s %s'
              % (mark, r['slug'], r.get('kind', 'realm'), r.get('status', '?'),
                 inst, up, r.get('name', '')))
    run = seen.get(snap['runningRealm'] or '')
    if run:
        w, a = run['processes']['worldserver'], run['processes']['authserver']
        print('\n%s: worldserver %s   authserver %s'
              % (run['slug'],
                 'pid %s' % w['pid'] if w['up'] else 'down',
                 'pid %s' % a['pid'] if a['up'] else 'down'))
    else:
        print('\nno realm is running')
    print('* = activeRealm in profiles.json')
    for c in snap['conflicts']:
        print('CONFLICT: %s' % c)
    for r in snap['realms']:
        if r['blockedBy']:
            print('%s cannot start: %s' % (r['slug'], r['blockedBy']))
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
