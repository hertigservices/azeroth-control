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

Setting a flag on does not create a feature. Each one is also gated on the relevant `.conf` actually
existing, so turning `botsAware` on without mod-playerbots changes nothing.

### `launch`

```json
"launch": { "kind": "exe", "target": "client/Wow.exe", "workdir": "client" }
```

What the Play button runs.

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
