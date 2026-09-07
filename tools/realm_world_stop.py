"""Stop half of a restart-with-install.

action_restart.py stops the world and immediately starts it again, which leaves
no window to overwrite worldserver.exe. This is that script's first half, using
the same control/realms code path so characters are saved the same way
(CTRL_BREAK -> World::StopNow) and the save is verified before we touch the
binary. MySQL, Ollama and authserver are deliberately left running.
"""
import os
import sys

# The hub is this file's grandparent (tools/ is one level down), so a clone
# anywhere works. AZCTL_HOME overrides it for a hub on another drive.
ROOT = os.environ.get('AZCTL_HOME') or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'control'))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import control                                                   # noqa: E402
import realms                                                    # noqa: E402
from action_start import active_realm                            # noqa: E402

realms.bind(control)

if __name__ == '__main__':
    realm = active_realm()
    if not realm:
        print('ABORT: no active realm')
        sys.exit(1)

    slug = realm.get('slug')
    if slug != 'vanilla':
        # Guard rail: the binary we are about to install was built from the
        # vanilla progression tree. Installing it while a different profile is
        # active would overwrite that profile's server.
        print('ABORT: active realm is %r, expected vanilla' % slug)
        sys.exit(1)

    print('Stopping world for %s' % realm.get('name', slug))
    print(control.stop_graceful('worldserver'))
    realms.verify_saved(realm, print)
    print('world down - safe to install')
