# Canonical source and runtime ownership

Develop in this repository. The installed hub and intake directories are deployments,
not alternate source repositories. Preserve private runtime configuration and state.
Use the deployment manifests and Azeroth Control tools/deploy.py; do not copy an
entire runtime back into source. No client assets, live character state, credentials,
raw submissions or generated database backups belong in source control.

# AGENTS.md — installing this, for a coding agent

You are installing **Azeroth Control** (this repo) and, if the user needs one, the AzerothCore
3.3.5a server it drives, on a Windows machine.

Read this file completely before running anything. It is phased, and every phase ends in a
verification you must actually run. Do not report a phase complete on the strength of an installer's
exit code — several of the failures below exit zero.

---

## 0. Rules that override your defaults

**The version pins are constraints, not suggestions.** Each one below is pinned because the newer
version is *broken here*, not because nobody updated it. Do not upgrade them. Do not "fix" them.
If a pin cannot be installed, stop and tell the user rather than substituting.

| Component | Pin | Why not newer |
|---|---|---|
| CMake | **3.31.x** | 4.x removed compatibility with the `cmake_minimum_required` floors in AzerothCore's bundled dependencies. The configure step fails with errors that look like a missing dependency. |
| OpenSSL | **3.5.8 (LTS)** | 3.6+ changed headers AzerothCore has not caught up with, and slproweb pulled the 3.6.x Windows installers. 3.5.8 looks like a downgrade and is the correct choice. |
| Boost | **1.89.0, msvc-14.3, x64 prebuilt** | Must match the MSVC toolset. Building Boost from source here is a long detour with no payoff. |
| MySQL | **8.4.x** | 9.x drops the authentication plugin AzerothCore's connector expects. |
| Visual Studio | **2022, "Desktop development with C++"** | Toolset v143 is what the Boost binary above was built against. |

**Never do these:**

- Never download, torrent, or otherwise obtain a WoW game client or game data files. The user
  supplies their own. If they do not have one, stop and say so.
- Never expose port 8750 (or 3724 / 8085 / 7878) beyond `127.0.0.1`. This panel has no
  authentication and controls processes and databases by design.
- Never commit `realms/profiles.json`, `credentials.txt`, `control/soap.json`, or anything under
  `realms/changelog/`. They contain that machine's passwords and private layout.
- Never run a destructive database command (`DROP`, `TRUNCATE`, a re-import over a populated schema)
  without asking the user first. Characters live in there.

**When something fails, read `docs/TROUBLESHOOTING.md` before improvising.** It is indexed by the
verbatim error text, and it exists because each entry cost somebody hours.

---

## 1. Which path are you on?

Ask the user, or detect it:

```powershell
# Is there already an AzerothCore server on this machine?
Get-ChildItem -Path C:\ -Filter worldserver.exe -Recurse -Depth 4 -ErrorAction SilentlyContinue |
  Select-Object -First 5 FullName
```

- **A server already exists** → go to [Phase A](#phase-a--attach-to-an-existing-server). This is
  short, safe, and where you should start whenever it is possible.
- **Nothing exists and the user wants the whole stack** → [Phase B](#phase-b--build-a-server-from-nothing).
  This takes 1–3 hours, most of it compiling.

---

## Phase A — attach to an existing server

### A1. Locate the install

You need four things. `detect.py` finds them; confirm what it reports rather than trusting it blindly.

```bash
python install/detect.py
```

It looks for:

| | Typically |
|---|---|
| `worldserver.conf` | `<server>/configs/` or `<server>/etc/` |
| the server binaries | beside the configs, or `<server>/bin/` |
| the log directory | `<server>/logs/` |
| the game client | wherever `Wow.exe` is |

### A2. Verify what it read

**This is the checkpoint that matters.** The panel derives all database access from
`worldserver.conf`, so if this is wrong, everything downstream is wrong.

```bash
python install/verify.py --db
```

Expected: three lines showing host, port, user and schema name for the world, login and character
databases, then a successful `SELECT 1`.

If the connection fails, the cause is almost always one of:
- MySQL is not running (`Get-Service MySQL*`).
- The password in `worldserver.conf` is stale relative to the actual MySQL user.
- MySQL is in a container and the port in the conf is the *container's* port, not the mapped one.
  Use the mapped one.

### A3. Can the panel own the processes?

Ask the user how the server is started today.

- **They start `worldserver.exe` / `authserver.exe` directly on this machine** → leave
  `serverManaged: true`. Full control.
- **Docker, systemd, a hosting panel, another machine, or they are not sure** → set
  `"serverManaged": false` in the profile. The Server / World / Tools tabs disappear; Play, Addons,
  Reference and Changelog work normally.

Do not guess `true`. A panel that thinks it owns processes it does not will show Stop buttons that
do nothing, and the user will not know why.

### A4. Start it

```bash
python control/control.py
```

Verify: <http://127.0.0.1:8750/launcher> loads and the rail shows the realm you just configured.

Then go to [Phase C](#phase-c--optional-extras).

---

## Phase B — build a server from nothing

Long. Checkpoint at every step; a failure in step 3 that you do not catch until step 7 costs the
whole build.

### B1. Prerequisites

Install the pinned versions from the table in §0. `install/versions.json` holds the exact IDs.

```powershell
powershell -ExecutionPolicy Bypass -File install\install-prereqs.ps1
```

**Verify each one — the installers can exit 0 having done nothing:**

```powershell
cmake --version                                    # must start 3.31
Test-Path 'C:\Program Files\OpenSSL-Win64\include\openssl\ssl.h'   # must be True
Test-Path 'C:\local\boost_1_89_0\boost\version.hpp'                # must be True
[Environment]::GetEnvironmentVariable('BOOST_ROOT','Machine')      # must be set, forward slashes
```

`BOOST_ROOT` must be set with **forward slashes** and both spellings (`BOOST_ROOT` and `Boost_ROOT`)
exist in the wild — AzerothCore's CMake reads `ENV{Boost_ROOT}` while the wiki says `BOOST_ROOT`.
Set both. A machine-level environment variable is not visible to an already-open shell; open a new
one before configuring.

### B2. Clone

If the user wants playerbots — most people do — clone the **fork**, not upstream. mod-playerbots
requires core changes that are not in `azerothcore/azerothcore-wotlk`:

```bash
git clone https://github.com/mod-playerbots/azerothcore-wotlk.git src
git clone https://github.com/mod-playerbots/mod-playerbots.git src/modules/mod-playerbots
```

Verify: `src/modules/mod-playerbots/CMakeLists.txt` exists.

### B3. Configure and build

```powershell
powershell -ExecutionPolicy Bypass -File install\build.ps1
```

**Cap the build.** Use **8 cores at BelowNormal priority**. A full-core build at normal priority
starves everything else on the machine — including the tools you are using to watch it — for the
entire hour. A running build can be re-throttled in flight if you forgot.

Verify: `worldserver.exe` and `authserver.exe` exist and are newer than the clone.

### B4. Database

```powershell
powershell -ExecutionPolicy Bypass -File install\setup-db.ps1
```

`sql_mode` must be relaxed to `NO_ENGINE_SUBSTITUTION`. AzerothCore's world SQL trips
`STRICT_TRANS_TABLES` and `NO_ZERO_DATE`, and the import fails partway through with a constraint
error that reads like corrupt data.

Verify:

```sql
SHOW DATABASES;                                    -- acore_auth, acore_world, acore_characters
SELECT COUNT(*) FROM acore_world.item_template;    -- ~46,000, not 0
```

### B5. First boot

Verify, in this order:
1. `worldserver.exe` reaches `World initialized` in `logs/Server.log`.
2. It stays up for 60 seconds. A world that exits at 30–40s has a module problem, not a data problem.
3. The `realmlist` row points at an address the client can reach, and its `flag` is **0**.
   `flag = 2` means "offline" and the client will show the realm greyed out with no error worth reading.

### B6. Then Phase A

Run `python install/detect.py` and continue from [A2](#a2-verify-what-it-read).

---

## Phase C — optional extras

Each is independent, and each only lights up its own part of the UI. Install none, some or all.

| Want | Install | Lights up |
|---|---|---|
| Bots that play with you | `mod-playerbots` | Tools ▸ Bots, bot counts, level caps |
| Expansion-gated progression | `mod-individual-progression` | Server ▸ Progression tier |
| A stocked auction house | `mod-ah-bot` | Auction house tunables |
| LLM bot chatter | `mod-ollama-chat` + [Ollama](https://ollama.com) | Chat model, concurrency |
| Scaled dungeons | `mod-autobalance` | Difficulty tunables |

Anything not installed is hidden, not broken. Do not stub the configs to make tabs appear.

### The offline game-data reference

```bash
python tools/rebuild-refdata.py
```

Builds the searchable item/NPC/quest/spell index from the user's own DBCs and world database.
Takes a few minutes and writes ~30 MB that is deliberately **not** in this repo — it is derived
from game data that cannot be redistributed.

Verify: `control/refdata.json` exists and the Reference tab shows non-zero counts.

---

## When you are done

Report to the user:

1. Which phase you ran, and whether `serverManaged` ended up `true` or `false`, **and why**.
2. Which optional modules are present, and therefore which tabs they will and will not see.
3. Anything you had to work around, and where you wrote it down.

If you changed a version pin, or set `serverManaged: true` without confirming the panel actually owns
the processes, say so explicitly. Those are the two decisions that cause problems later.

---

## Common failures

`docs/TROUBLESHOOTING.md` is the full index, by verbatim symptom. The four that catch almost everyone:

| Symptom | Cause |
|---|---|
| CMake configure fails on a bundled dependency | CMake 4.x. Install 3.31.x. |
| Configure cannot find Boost, but Boost is installed | `BOOST_ROOT` not set, backslashed instead of forward-slashed, or set after the shell opened. |
| World SQL import fails partway with a constraint error | `sql_mode` not relaxed to `NO_ENGINE_SUBSTITUTION`. |
| Realm greyed out in the client, no error | `realmlist.flag = 2`. Set it to 0. |
| World exits ~30s after start, clean log | A module was built but its `.conf` is missing, or it declares a log appender the config does not define. |
| Panel shows the realm down while it is plainly up | The panel is reading a different `worldserver.conf` than the one the server started with. Check the profile's `paths.configs`. |
