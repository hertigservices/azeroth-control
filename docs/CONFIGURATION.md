# Configuration

Everything describing *your* install lives in one file: `realms/profiles.json`. Generate it with
`python install/detect.py`, or copy [`templates/profiles.example.json`](../templates/profiles.example.json)
and edit.

There is no second config file, no environment to set up, and no database of its own.

---

## The one idea worth understanding

**The panel reads your server's own configuration rather than assuming anything about it.**

Database host, port, user, password and schema names are taken from the `WorldDatabaseInfo`,
`LoginDatabaseInfo` and `CharacterDatabaseInfo` lines of your `worldserver.conf`:

```ini
WorldDatabaseInfo     = "127.0.0.1;3306;acore;acore;acore_world"
                         host      port user  pass  schema
```

The world port comes from `WorldServerPort` and SOAP from `SOAP.Port` / `SOAP.IP` the same way.

This is why a Docker stack with MySQL on a mapped port, a database with a real password, a renamed
schema, or a server someone else installed all work without special handling — that file is the one
place the truth lives for every AzerothCore install, and it is already correct or the server would
not be running.

**Consequence:** if the panel is reading the wrong `worldserver.conf`, everything downstream is
wrong in confusing ways. `paths.configs` is the field to check first when something is inexplicable.

---

## Profile fields

### `paths`

Relative to the repo root, or absolute. Only `configs` is required.

| Field | What | If missing |
|---|---|---|
| `configs` | **Required.** The folder holding `worldserver.conf`. | Nothing works. |
| `modconfigs` | Module `.conf` files. Usually `configs/modules`. | All optional module features hidden. |
| `server` | Folder with `worldserver.exe` / `authserver.exe`. | Start/stop unavailable. |
| `logs` | Where `Server.log` is written. | Log tail empty; readiness falls back to a port probe. |
| `client` | Your 3.3.5a client folder. | Play button hidden. |
| `addons` | `<client>/Interface/AddOns`. | Addons tab cannot install. |
| `cache` | `<client>/Cache`. | Cache-clearing helpers unavailable. |

### `db`

**Schema names only.** Credentials are read from `worldserver.conf`, as above.

```json
"db": { "auth": "acore_auth", "world": "acore_world",
        "characters": "acore_characters", "user": "acore" }
```

`user` is the MySQL user this realm is queried as. It matters when you run more than one realm and
want each fenced off from the others' databases by MySQL grants.

### `controls`

Feature switches. All default to off except `serverManaged`.

| Flag | Effect |
|---|---|
| `serverManaged` | Whether this panel starts and stops the server. **Set it honestly** — see below. |
| `phaseAware` | Show the progression-tier control (needs mod-individual-progression). |
| `botsAware` | Show bot counts and the Tools ▸ Bots tab (needs mod-playerbots). |
| `addonsAware` | Show the Addons tab for this realm. |
| `independent` | This realm holds its own ports and runs *beside* the active one. See below. |

Setting a flag on does not create a feature. Each one is also gated on the relevant `.conf` actually
existing, so turning `botsAware` on without mod-playerbots changes nothing.

`independent` is the exception to `activeRealm`. That field exists because realms that all bind
3724/8085 can only take turns, so the panel starts and stops whichever one the registry points at.
A profile that declares its own auth, world and SOAP ports has no such conflict: it starts and stops
on its own, beside whatever is active, and it refuses `switch` outright because there is nothing to
switch away from. Use it for a build under test — a fork you are sending patches to, say — so trying
it does not take down the realm you actually play on. Give it its own databases too; sharing a world
database with the realm it was cloned from is how a test becomes an outage.

### `launch`

```json
"launch": { "kind": "exe", "target": "client/Wow.exe", "workdir": "client" }
```

What the Play button runs.

It may also be a **list**, when one realm can be played by more than one client — a server that
serves both a custom client and a stock one, say. Each entry adds a Play button: the first is the
default the main button uses, and the rest appear beside it and on the Server tab under *Clients*.

```json
"launch": [
  { "id": "custom", "label": "Play (custom client)", "kind": "vbs",
    "target": "tools/launch-custom.vbs", "workdir": "client-custom",
    "note": "through the bridge on 8088" },
  { "id": "stock", "label": "Play (stock client)", "kind": "exe",
    "target": "client/Wow.exe", "workdir": "client",
    "note": "straight to the world on 8086" }
]
```

`id` is what `POST /v1/launch/<slug>` takes as `{"target": "<id>"}`, `label` is the button text and
`note` its tooltip. Prefer a directory per client over one client whose realm list is rewritten at
launch: two clients that differ only in which server they dial rarely stay that way, and a wrapper
that edits files behind the launcher's back leaves the wrong realm selected any time it does not get
to run its restore step.

### `nestedUnder` — optional, for a second mode of the same server

```json
"slug": "freepick", "nestedUnder": "coa"
```

Presentation only. It says *this profile is another mode of that one*: the same client, the
same `paths.server`, the same binaries — started against a different `world.conf`, usually
with its own characters database. The child draws indented under its parent in the rail
instead of standing beside it as if it were a separate stack, and its Play page says
"a mode of ‹parent›" above the title.

Nothing is merged. The child keeps its own slug, ports, databases, Play button and Switch;
only where it is drawn changes.

The shape comes up constantly on Ascension-style servers, where one build serves several
game modes — a classless Free-Pick realm and a Conquest of Azeroth realm out of one
`server/` directory, one client, one auth database, taking turns on the worldserver. Two
top-level entries make that look like two installs; nesting says what it is.

Details worth knowing:

- The rail hoists a child to its parent whatever `order` says, so renumbering cannot break
  the nesting. Everything else that lists realms is still flat and sorted by `order`, so
  give a pair adjacent numbers — a fractional one (`3` and `3.5`) does that without
  renumbering the realms after it.
- A child whose parent slug is not in the list is drawn at the top level rather than
  vanishing: a typo cannot hide a realm.
- Nesting is one level deep. A child's own children are not drawn.

### `world` — optional, for servers that do not start the default way

Omit it entirely and the panel does what it always did: run `authserver.exe` and `worldserver.exe`
out of `paths.server`, using the `worldserver.conf` next to them.

| Field | What | Why you would set it |
|---|---|---|
| `conf` | The config to start worldserver with, passed as `-c`. | One server directory backing two realms that need different settings — a second world on another port, a different `DataDir`, a test realm with Warden off. Without this the binaries silently use the file sitting next to them and the wrong realm comes up. |
| `logs` | Log directory, overriding `paths.logs`. | A non-default config writes wherever *its* `LogsDir` points. Name the wrong one and the panel reads a world that is up and ready as "maps still loading" forever — the previous run's log is still there and still says `Halting process`. |
| `authserver` | `false` if this realm has no `authserver.exe`. | Auth served by something else. Starting one anyway takes port 3724 from your other realms to run a process nothing in this profile talks to. |
| `python` | Interpreter for the helpers below. | Only if the auto-detected one is wrong. |
| `realmRows` | Extra `realmlist` ids this realm keeps online. | One world reachable at two addresses. See below. |
| `helpers` | Extra processes to start with the realm. | See below. |

#### `world.realmRows`

```json
"realmRows": [2]
```

The ids of `realmlist` rows this profile owns *besides* its own `RealmID`. You need this when one
world has to be reachable at two addresses — because two different clients cannot use the same one,
for instance, so one row points at a translating proxy and the other straight at the world.

It exists because of how the offline flag is maintained: the authserver marks **every** realm offline
at startup (`UPDATE realmlist SET flag = flag | 2`, no `WHERE`), and each worldserver then clears the
flag for its own `RealmID` and no other. A second row pointing at the same world is therefore owned by
nothing, and disappears from the realm list at the next authserver start — not when you add it, which
is what makes it puzzling. Listing the id here has the panel clear the flag after the realm starts,
and print the rows back by name so you can see which ones it actually found.

#### `world.helpers`

```json
"helpers": [
  { "name": "authshim", "label": "Auth shim", "port": 3724,
    "script": "realms/myrealm/auth_shim.py", "args": "--verbose" }
]
```

Started **after** the world, and only if it came up — on their own they are worse than nothing, since
an auth helper will happily let a client log in and reach a realm list that has no world behind it,
which reads as a broken login rather than a world that failed. Stopped on the way out, before the
database is verified.

A helper is identified by its **port**, not its process name: every one of them is `python.exe`, so a
name check cannot tell one from another — or from an unrelated script you happen to be running. Give
each one a `port` and the panel will skip it if it is already listening, wait up to 10 seconds for it
after launching, show it as its own step on the start progress bar, and stop exactly the right
process on the way out. A helper with no `port` is started but never waited for or stopped.

Helpers are force-stopped, unlike the servers. They hold no unsaved game state, so there is nothing
for a graceful signal to flush.

---

## `serverManaged`: the field to get right

This decides whether the Server, World and Tools tabs appear at all.

**`true`** — the panel owns the processes. It starts them, and stops them with a graceful
`CTRL_BREAK` into `World::StopNow`, which saves every character. Requires: the binaries on this
machine, at `paths.server`, started by this panel.

**`false`** — something else owns them. Docker, systemd, a hosting panel, another machine, a script
you run by hand. Server / World / Tools disappear. Play, Addons, Reference and Changelog keep working.

**Set `false` if you are not sure.** A panel that believes it owns processes it cannot signal shows
Stop buttons that do nothing, and there is no error to explain why. The failure is silent, which is
the worst kind.

---

## Recipes

### Non-default ports

Nothing to do. Ports are read from `worldserver.conf`.

The one exception is the panel's own listener, which defaults to 8750:

```bash
set AZCTL_PORT=9000 && python control/control.py
```

### MySQL with a real password

Nothing to do, as long as `worldserver.conf` has the password the server actually uses. If you
changed the MySQL user's password without updating that file, the server is failing to connect too —
fix the config, not the panel.

### MySQL on another host or a mapped container port

Nothing to do. The host and port in `WorldDatabaseInfo` are used verbatim.

For Docker, make sure the value is the **mapped** port as seen from the host, not the container's
internal 3306. If the server runs inside the same compose network it may legitimately say `3306` and
a service name — in that case the panel cannot reach the database, and you should either publish the
port or accept that population counts and the Reference tab will be unavailable.

### Docker or Linux, generally

Set `serverManaged: false` and point `paths.configs` at the mounted config directory on the host side.
Everything except lifecycle control works.

Full lifecycle support would mean a process adapter — see
[the README](../README.md#docker-linux-remote-servers). The seam is two functions in `realms.py`.

### More than one realm

Add another entry to `realms[]` with its own slug, paths and databases. Only one runs at a time;
switching stops the running one gracefully before starting the next. Give each its own MySQL user
and grant it access only to its own schemas — that way a mistake on a test realm cannot reach the
one you actually play.

### A realm that starts a different config, or needs a helper process

See [`world`](#world--optional-for-servers-that-do-not-start-the-default-way). In short: `world.conf`
names the config to pass as `-c`, `world.logs` follows it, `world.authserver: false` covers a realm
whose auth comes from elsewhere, and `world.helpers[]` lists extra processes to bring up with it.

### Two modes of one server

One `server/` directory, two configs, two characters databases — the pattern in
[`world`](#world--optional-for-servers-that-do-not-start-the-default-way). Add
[`nestedUnder`](#nestedunder--optional-for-a-second-mode-of-the-same-server) to the second
profile and the rail draws it under the first instead of beside it.

### A client with no server

Set `kind: "client"` and omit the server-side paths. It appears under CLIENTS in the rail with just
a Play button.

---

## The other config files

| File | What | In git? |
|---|---|---|
| `realms/profiles.json` | Your install. | No — gitignored. |
| `control/soap.json` | GM account credentials for the Tools tab. | No — gitignored. |
| `control/addons_installed.json` | Which addons you installed. Generated. | No. |
| `control/refdata*.json` | The offline game-data index. ~30 MB, generated from your own data. | No. |
| `realms/changelog/*.md` | Your per-realm change log. | No — it is yours. |

### `control/soap.json`

Needed only for Tools ▸ Trainer. Create a GM account in-game or at the worldserver console, then:

```json
{ "user": "PANELGM", "pass": "your-password", "host": "127.0.0.1", "port": 7878 }
```

SOAP must be enabled in `worldserver.conf` (`SOAP.Enabled = 1`). The account needs GM level 3.
Use an account created for this purpose, not your own — the panel sends its password on every call.
