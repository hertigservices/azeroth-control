"""Tray action: bounce the active realm's world only.

Characters are saved first (CTRL_BREAK -> World::StopNow). MySQL, Ollama and the
authserver are left alone so the gap is as short as possible - which is also why
this does not go through start_realm's dependency handling.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'control'))
import control                                                   # noqa: E402
import realms                                                    # noqa: E402
from action_start import active_realm                            # noqa: E402

realms.bind(control)

if __name__ == '__main__':
    realm = active_realm()
    if not realm:
        sys.exit(1)
    print('Restarting the world for %s' % realm.get('name', realm['slug']))
    print(control.stop_graceful('worldserver'))
    realms.verify_saved(realm, print)
    time.sleep(2)
    sys.exit(0 if realms.start_realm(realm, print) else 1)
