"""Start half of a restart-with-install. Mirrors action_restart.py's tail.

start_realm handles already-running dependencies, which is why action_restart.py
calls it after stopping only the world - authserver and MySQL were never taken
down here either.
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
        print('ABORT: active realm is %r, expected vanilla' % slug)
        sys.exit(1)

    print('Starting %s' % realm.get('name', slug))
    sys.exit(0 if realms.start_realm(realm, print) else 1)
