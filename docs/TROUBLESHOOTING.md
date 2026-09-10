# Troubleshooting

Indexed by the **symptom you actually see**, because that is what you have when something breaks.
Each entry names the cause, then the fix.

Run this first — it checks the things below in order and names what is wrong:

```bash
python install/verify.py
```

---

## Contents

- [Building the server](#building-the-server)
- [The database](#the-database)
- [Starting the server](#starting-the-server)
- [Connecting the client](#connecting-the-client)
- [The panel itself](#the-panel-itself)
- [Addons](#addons)
- [Tools and SOAP](#tools-and-soap)

---

## Building the server

### CMake configure fails on a bundled dependency

> `Compatibility with CMake < 3.5 has been removed from CMake`

**CMake 4.x.** AzerothCore's bundled dependencies declare `cmake_minimum_required` floors that 4.x
no longer accepts. The error names the dependency, so it reads like the dependency is broken.

Install **3.31.x** and delete the build directory before re-configuring — a stale
`CMakeCache.txt` remembers the old generator and will fail again for a different reason.

### Configure reports Boost missing, but Boost is installed

Three separate causes, in order of likelihood:

1. **The shell predates the environment variable.** `BOOST_ROOT` set at Machine scope is not visible
   to an already-open terminal. Open a new one.
2. **Backslashes.** The value must use forward slashes: `C:/local/boost_1_89_0`.
3. **The other spelling.** AzerothCore's CMake reads `ENV{Boost_ROOT}`; the wiki says `BOOST_ROOT`.
   Set both. They are different variables on a case-sensitive read.

### Link errors naming symbols rather than files

Toolset mismatch — the prebuilt Boost is `msvc-14.3` and must be built against Visual Studio 2022
(v143). Check which generator CMake selected in the configure output.

### The build takes over the whole machine for an hour

Cap it: **8 jobs at BelowNormal priority**. On a typical desktop this finishes in about the same
wall-clock time and leaves the machine usable. A running build can be re-throttled in flight.

---

## The database

### World SQL import fails partway with a constraint error

> `Incorrect date value: '0000-00-00'` · `Field 'x' doesn't have a default value`

**`sql_mode` is not relaxed.** AzerothCore's world SQL trips `STRICT_TRANS_TABLES` and `NO_ZERO_DATE`.
Set in `my.ini` under `[mysqld]`:

```ini
sql_mode="NO_ENGINE_SUBSTITUTION"
```

Restart MySQL, drop the partially imported schema, and re-import. A half-imported world database
fails later in ways that look like missing content rather than a failed import.

### `item_template` has 0 rows

The import did not finish. Do not start the server — re-import first. `verify.py` checks this
explicitly because a world database that exists but is empty produces a server that starts, runs,
and has no game in it.

### Import is extremely slow

Raise `max_allowed_packet` to `256M` and `innodb_buffer_pool_size` to a real fraction of RAM (4G on
a 16 GB machine), and set `innodb_flush_log_at_trx_commit=2` for the duration of the import.

### MySQL will not start after moving the install

The Windows service records an **absolute** `basedir`. Moving the folder breaks it.

Running MySQL as an in-tree process rather than a service avoids this entirely and is what keeps the
whole install relocatable.

---

## Starting the server

### worldserver exits about 30 seconds after start, with a clean log

Almost always a **module** problem, not data:

- A module was built but its `.conf` is missing from `configs/modules/`. Copy the `.conf.dist`.
- A module declares a **log appender** that the config does not define. The world fails while
  configuring logging, which happens before most of the log would have been written — so the log
  looks fine right up to the point it stops.

Compare `configs/modules/` against the modules actually present in `src/modules/`.

### worldserver starts but never reports ready

Watch for `World initialized` in `Server.log`. If it never appears, the map data (`Data/maps`,
`vmaps`, `mmaps`) is missing or incomplete. These are extracted from your own client, never
downloaded.

### Two realms fight over the ports

Only one realm can run at a time — each binds 3724 / 8085 / 7878. This is by design, and switching
realms stops the running one gracefully first. If a port is held after a crash, find the orphaned
`worldserver.exe` and end it before starting again.

---

## Connecting the client

### The realm is greyed out in the client, with no error

**`realmlist.flag = 2`** in `acore_auth`. Flag 2 means offline.

```sql
UPDATE acore_auth.realmlist SET flag = 0 WHERE id = 1;
```

### The client sits at "Connecting" and times out

The `address` column of `realmlist` is what the client is told to connect to *after* it authenticates.
For a local server it must be `127.0.0.1`; for a LAN server, the LAN IP. `localhost` does not always
resolve the way you expect from the client's process.

### Authentication fails with correct credentials

Account passwords are case-sensitive in a way the login box is not. Recreate the account at the
worldserver console with `account create NAME PASSWORD`.

---

## The panel itself

### "Could not reach the hub on this port"

The page loaded but `control.py` is not answering. Either it is not running, or something else holds
8750. Change it:

```bash
set AZCTL_PORT=9000 && python control/control.py
```

### The panel serves old code after you restarted it

Symptoms: a fix to `control\*.py` has no effect; the launcher shows behaviour that was
replaced on disk; refreshing gives an answer that alternates between the old and the new one.

`Get-NetTCPConnection -LocalPort 8750 -State Listen` names the process that is really serving
the page. Two causes, often together:

- **The panel you restarted was not the one listening.** A panel started from inside `control\`
  as `python control.py` has a different command line from the tray's
  `pythonw C:\...\control\control.py`, so anything matching on the command line missed it.
- **Two panels can listen at once.** Python's `HTTPServer` sets `SO_REUSEADDR`, and Windows
  lets a second process bind 8750 while the first still holds it. Connections then land on
  either, which is why the answers alternate.

Fix it from the tray: **Restart hub (control panel)**. It finds the owner *by port* rather
than by command line, adds any other `control.py` it can see, names them in the confirmation
dialog, waits for the port to come free, and starts nothing at all if something still holds
it — rather than adding a second listener and reporting success. By hand:

```powershell
Get-NetTCPConnection -LocalPort 8750 -State Listen |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
Start-Process pythonw.exe control\control.py -WorkingDirectory control -WindowStyle Hidden
Get-NetTCPConnection -LocalPort 8750 -State Listen   # expect exactly ONE
```

Restarting the panel does not touch the realm: the server, MySQL and anyone online keep
running.

### The panel shows the realm down while it is plainly up

The panel is reading a **different `worldserver.conf`** than the one the server started with. Check
`paths.configs` in `realms/profiles.json`. This is the single most common configuration mistake and
it makes every other reading wrong in a way that looks like several unrelated bugs.

```bash
python install/verify.py --db
```

prints the config it is actually reading.

### Population and Reference show nothing, everything else works

The `mysql` client is not on `PATH`, or the credentials in `worldserver.conf` are stale relative to
the actual MySQL user. `verify.py --db` distinguishes these: it tests TCP reachability separately
from credentials, so "cannot reach" and "wrong password" are never confused.

### A tab I expected is not there

Working as intended. Tabs whose backend reports the module absent are dropped rather than shown as
controls that would silently miss.

- **Server / World / Tools missing entirely** → `serverManaged` is `false` for this profile.
- **Tools ▸ Bots missing** → no `playerbots.conf` in `paths.modconfigs`.
- **Tools ▸ Raid missing** → no `mod_raid_roster.conf`.
- **Progression tier missing** → no `individualProgression.conf`.

Do not create empty `.conf` files to make tabs appear. The tab will show, and every control on it
will fail.

### The Reference tab is empty

`control/refdata.json` has not been generated. It is ~30 MB, built from your own DBCs and world
database, and deliberately not in this repo:

```bash
python tools/rebuild-refdata.py
```

### Changes to launcher.html do nothing

The frozen `.exe` loads the panel from disk and bundles none of it, so this should never happen —
unless there is a second copy of the repo and the exe found that one first. `AZCTL_HOME` pins which
hub is used.

---

## Addons

### An addon installs but does not appear in-game

WotLK checks the addon's `.toc` `## Interface:` version. Enable "Load out of date AddOns" at the
character select screen.

### The game breaks after installing several addons

Use the **toggle**, not Remove. It sidelines the folder to `Interface/AddOns.disabled` and moves it
back on demand, so you can bisect without re-downloading anything.

### A load-on-demand addon does nothing

Its companion needs a `<Folder>: enabled` line in that client's `WTF/.../AddOns.txt`. The panel
writes this when it installs; an addon added by hand needs it added by hand.

---

## Tools and SOAP

### Every Trainer command fails

SOAP is not enabled or not configured.

1. `SOAP.Enabled = 1` in `worldserver.conf`, then restart the world.
2. `control/soap.json` must hold a **GM level 3** account — see
   [CONFIGURATION.md](CONFIGURATION.md#controlsoapjson).

`verify.py` checks that port 7878 is listening; it cannot check the credentials without sending them.

### A command is marked COPY and will not run

Correct, and not a bug. Some handlers are declared console-capable but bail with "Run this in-world
as a player". The panel marks those `COPY` rather than offering a Run button that always fails.

### A bot command runs and nothing happens

Check the gate shown next to it. Bot behaviour is governed by config keys that can silently veto a
command — a level cap lower than what you asked for, a gear-quality limit, a pool size of zero. The
panel shows the relevant key and its current value beside the command for exactly this reason.

---

## Still stuck

Open an issue with:

- the output of `python install/verify.py` (it redacts passwords),
- what you ran and what you expected,
- the last 50 lines of `Server.log` if the server is involved.
