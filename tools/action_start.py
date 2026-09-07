"""Tray action: start whichever realm profiles.json says is active.

control.start_all() is shorter but bound to the DEFAULT realm's SERVER_DIR, so
with a second realm active it would launch the first realm's binaries while the
tray, the launcher and profiles.json all said otherwise. realms.py takes the
profile as data and starts that profile's own servers, from its own directory.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'control'))
import control                                                   # noqa: E402
import realms                                                    # noqa: E402

realms.bind(control)


def active_realm():
    doc = realms.load()
    if doc.get('error'):
        print(doc['error'])
        return None
    r = realms.find(doc, doc.get('activeRealm'))
    if not r:
        print('no active realm in realms/profiles.json - pick one in the launcher')
    return r


if __name__ == '__main__':
    realm = active_realm()
    if not realm:
        sys.exit(1)
    print('Starting %s' % realm.get('name', realm['slug']))
    sys.exit(0 if realms.start_realm(realm, print) else 1)
