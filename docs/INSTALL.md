# Install

Three routes. Pick by what you already have.

| Your situation | Route | Time |
|---|---|---|
| An AzerothCore server that already runs | [A — attach the panel](#a--attach-the-panel-to-a-server-you-already-have) | 5 minutes |
| Nothing yet, and you want to build it yourself | [B — bootstrap from nothing](#b--bootstrap-from-nothing) | An afternoon |
| Nothing yet, and you would rather an AI did it | [C — hand it to an agent](#c--hand-it-to-an-agent) | An afternoon, less of it yours |

If you are on Docker, Linux, or a server someone else runs, read
[Route A](#a--attach-the-panel-to-a-server-you-already-have) and then
[CONFIGURATION.md](CONFIGURATION.md#servermanaged-the-field-to-get-right) — most of the panel works,
and the part that does not is process control.

---

## A — Attach the panel to a server you already have

This changes nothing about your server. It reads your config, learns where things are, and writes
exactly one file of its own.

### 1. Get the repo and check Python

```bash
git clone https://github.com/YOUR-GITHUB-USERNAME/azeroth-control.git
cd azeroth-control
python --version
```

3.8 or newer. There is nothing to install — the panel is standard library only.

### 2. Detect your install

```bash
python install/detect.py
```

It looks for a `worldserver.conf`, and from there works out where your server binaries, module
configs, logs and client live, and which databases you use. It prints what it found before it writes
anything, and it will **never overwrite an existing `realms/profiles.json`**.

If it cannot find your config, point it at one:

```bash
python install/detect.py --conf "D:\wow\server\configs\worldserver.conf"
```

Useful flags:

| Flag | Why |
|---|---|
| `--dry-run` | Print the profile it would write, write nothing. |
| `--unmanaged` | Set `serverManaged: false` — for Docker, Linux, or a server you do not start from this machine. |
| `--name "My Realm"` | The display name in the rail. |
| `--slug myrealm` | The internal id, used in URLs. |

### 3. Check it

```bash
python install/verify.py --db
```

Every line is `OK`, `WARN` or `FAIL`.

- **FAIL** is something that will not work until you fix it.
- **WARN** is almost always an optional module you do not have. That is fine — those features hide
  themselves rather than breaking.

Read the config path it prints. If that is not the `worldserver.conf` your server actually started
with, fix `paths.configs` in `realms/profiles.json` before going further: everything downstream reads
from that file, so pointing at the wrong one produces several unrelated-looking bugs at once.

### 4. Start it

```bash
python control/control.py
```

Open <http://127.0.0.1:8750/launcher>.

Leave that window open — closing it stops the panel. It does **not** stop your game servers.

### 5. Optional extras

<details>
<summary>A real desktop window instead of a browser tab</summary>

```bash
pip install pywebview
python control/launcher_app.py
```

Needs the Microsoft Edge WebView2 runtime, which Windows 11 already has. Without pywebview the panel
still runs; you just open it in a browser.

To freeze it into an `.exe` with a tray icon:

```bash
pip install pyinstaller pillow
python tools/make-launcher-icon.py
python tools/build-launcher.py
```

The exe is a shell that loads the panel from disk — it bundles none of the HTML or Python, so editing
`control/launcher.html` takes effect without rebuilding.
</details>

<details>
<summary>The Reference tab (offline game-data search)</summary>

```bash
python tools/rebuild-refdata.py
```

Builds a ~30 MB index from **your own** DBCs and world database. It is not in this repo and cannot
be — it is derived from Blizzard's data. Takes a few minutes. Until you run it, the Reference tab is
empty.
</details>

<details>
<summary>Tools ▸ Trainer (GM commands over SOAP)</summary>

1. In `worldserver.conf`: `SOAP.Enabled = 1`, then restart the world.
2. Create an account for the panel and give it GM level 3 — **not** your own account, because the
   panel sends its password on every call:

   ```
   account create PANELGM some-long-password
   account set gmlevel PANELGM 3 -1
   ```

3. Write `control/soap.json`:

   ```json
   { "user": "PANELGM", "pass": "some-long-password", "host": "127.0.0.1", "port": 7878 }
   ```

That file is gitignored. There is a helper that does the same thing: `python tools/setup-soap.py`.
</details>

<details>
<summary>Start with Windows, from the tray</summary>

```powershell
powershell -ExecutionPolicy Bypass -File tools\tray.ps1
```

Puts an icon in the notification area that starts and stops the hub and opens the launcher. Add
`tools\launch-tray.vbs` to your Startup folder to have it come up with Windows — the `.vbs` exists
purely so no console window flashes on login.
</details>

---

## B — Bootstrap from nothing

Builds AzerothCore from source on Windows and wires the panel to it.

```powershell
powershell -ExecutionPolicy Bypass -File install\bootstrap.ps1 -WithPlayerbots
```

**Read this before you start it:**

- It takes hours, most of it compiling and extracting map data.
- It **will** be interrupted — an installer wanting a reboot, a download failing, you closing the
  laptop. That is expected. Run the same command again; finished phases skip themselves.
- The prerequisites phase installs software and needs an elevated shell.
- Budget ~100 GB: about 30 GB of build tree, 25 GB of databases, plus your client.

Run one phase at a time if you would rather watch it:

```powershell
powershell -File install\bootstrap.ps1 -Phase prereqs
powershell -File install\bootstrap.ps1 -Phase clone -WithPlayerbots
powershell -File install\bootstrap.ps1 -Phase build
powershell -File install\bootstrap.ps1 -Phase db
powershell -File install\bootstrap.ps1 -Phase data -Client "C:\path\to\your\client"
powershell -File install\bootstrap.ps1 -Phase panel
powershell -File install\bootstrap.ps1 -Phase verify
```

`-WhatIf` prints what a phase would do and changes nothing.

### The three things that go wrong

**1. The versions are pinned, and two of them look like downgrades.**

| | Pinned | Why not newer |
|---|---|---|
| CMake | **3.31.x** | 4.x rejects the `cmake_minimum_required` floors in AzerothCore's bundled dependencies. The error names the dependency, so it reads like the dependency is broken. |
| OpenSSL | **3.5.8 LTS** | 3.6+ moved headers AzerothCore has not caught up with, and the 3.6 Windows installers were pulled. |
| MySQL | **8.4** | 9.x drops the auth plugin the connector expects. |
| Boost | **1.89.0 msvc-14.3 x64** | Must match the VS2022 (v143) toolset. |

Full reasoning in [`install/versions.json`](../install/versions.json).

**2. Boost "is missing" while plainly installed.** Three causes, in order of likelihood: your shell
predates the environment variable (open a new one); the value uses backslashes (it must use forward
slashes); only one of the two spellings is set (`BOOST_ROOT` **and** `Boost_ROOT` — AzerothCore's
CMake reads one, the wiki tells you the other).

**3. The world import dies partway through** with something like
`Incorrect date value: '0000-00-00'`. That is `sql_mode`, not corrupt data. In `my.ini`, under
`[mysqld]`:

```ini
sql_mode="NO_ENGINE_SUBSTITUTION"
```

Restart MySQL, drop the half-imported schema, and start over. A partially imported world database
fails later in ways that look like missing content rather than a failed import.

### Client data

Maps, vmaps and mmaps are **extracted from your own 3.3.5a client**. Nothing is downloaded and
nothing ships here. The `data` phase runs the extractors the build produced; mmap generation is by
far the slowest part.

---

## C — Hand it to an agent

The repo ships [`AGENTS.md`](../AGENTS.md): a phased runbook written to be executed by a coding
agent, with a verification command and an expected result at every checkpoint, and the version pins
stated as constraints *with their reasons* — so the agent does not helpfully upgrade past them.

Point [Claude Code](https://claude.com/claude-code), or any agent that reads `AGENTS.md` /
`CLAUDE.md`, at a clone and tell it to install the server.

The README's [copy-paste prompt](../README.md#-copy-paste-prompt-have-your-own-ai-read-this-code-before-you-run-it)
does that with a security audit of this repository in front of it — worth using, since you are about
to let something you did not write touch your database.

---

## Uninstall

There is no installer, so there is nothing to uninstall.

Delete the folder. The only things outside it that the panel ever created are the ones you asked it
to: addons in your client's `Interface/AddOns`, edits to your own `.conf` files, and rows in your own
database. If you built a server with Route B, that lives in `src/`, `build/` and `server/` and goes
with the folder.

---

## Still stuck

[docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md) is indexed by the symptom you actually see. If it is
not there, open an issue with the output of `python install/verify.py` — it redacts passwords.
