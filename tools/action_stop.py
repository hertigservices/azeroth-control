"""Tray action: soft shutdown of the active realm, dependencies included.

Every character is saved first, and MySQL goes down through a clean mysqladmin
shutdown rather than a kill. Ollama is only stopped if it was started for this
profile's bots.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'control'))
import control                                                   # noqa: E402
import realms                                                    # noqa: E402
from action_start import active_realm                            # noqa: E402

realms.bind(control)

if __name__ == '__main__':
    realm = active_realm()
    print('Stopping %s' % ((realm or {}).get('name') or 'the running realm'))
    # A missing profile is not a reason to refuse: the servers are found by name,
    # and leaving a running world up because the registry is unreadable would be
    # the worse failure. Only the save verification needs the profile.
    realms.stop_realm(realm, print)
    print(control.stop_mysql())
    if control.running('ollama')['up']:
        control.ps("Get-Process -Name 'ollama','ollama app' -ErrorAction SilentlyContinue | Stop-Process -Force")
        print('Ollama: stopped')
