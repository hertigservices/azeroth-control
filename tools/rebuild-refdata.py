"""Build a compact reference dataset PER REALM from that realm's own DB + DBC.

Every realm has its own world database and its own Data/dbc, so one shared
refdata.json showed vanilla's items and spells no matter which realm was
selected in the launcher. Output is now partitioned: refdata-<slug>.json, with
refdata.json kept as a copy of the active realm's so older callers still work.

Compact arrays rather than objects: with ~158k rows per realm, key names would
dominate the payload. Descriptions and flavour text are deliberately excluded -
an admin tool needs IDs to build commands, and omitting prose keeps the file
small and the search fast.

Row shapes (index -> meaning), kept in sync with the JS in launcher.html:
  commands [name, security, help]
  items    [id, name, quality, itemLevel, reqLevel, class]
  npcs     [id, name, minLevel, maxLevel, rank, type]
  objects  [id, name, type]
  quests   [id, title, questLevel, minLevel]
  spells   [id, name, rank]

Connection details are read from each realm's own worldserver.conf
(WorldDatabaseInfo), so no credentials live in this file.

Usage:  python rebuild-refdata.py [slug ...]      (default: every installed realm)
"""
import json, struct, subprocess, sys, os, re, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
HUB = os.path.dirname(HERE)
PROFILES = os.path.join(HUB, "realms", "profiles.json")
MYSQL = os.path.join(HUB, "mysql", "bin", "mysql.exe")
if not os.path.isfile(MYSQL):
    MYSQL = "mysql"


def load_profiles():
    with open(PROFILES, encoding="utf8") as f:
        return json.load(f)


def rebase(rel):
    return rel if os.path.isabs(rel) else os.path.join(HUB, rel)


def dsn_from_conf(conf_dir):
    """WorldDatabaseInfo = "host;port;user;pass;db" - the realm's own config is
    the single source of truth for how to reach its world DB."""
    p = os.path.join(conf_dir, "worldserver.conf")
    if not os.path.isfile(p):
        return None
    txt = open(p, encoding="utf8", errors="replace").read()
    m = re.search(r'^WorldDatabaseInfo\s*=\s*"([^"]+)"', txt, re.M)
    if not m:
        return None
    parts = m.group(1).split(";")
    if len(parts) < 5:
        return None
    return dict(host=parts[0], port=parts[1], user=parts[2],
                password=parts[3], db=parts[4])


def make_cnf(dsn, slug):
    """Pass credentials via a defaults file so they never reach a command line
    (and so they never show up in a process list)."""
    p = os.path.join(HERE, ".refdata-%s.cnf" % slug)
    with open(p, "w", encoding="utf8") as f:
        f.write("[client]\nhost=%s\nport=%s\nuser=%s\npassword=%s\n"
                % (dsn["host"], dsn["port"], dsn["user"], dsn["password"]))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def q(cnf, db, sql):
    p = subprocess.run([MYSQL, "--defaults-extra-file=" + cnf, "--batch",
                        "--skip-column-names", "--raw", "-e", sql, db],
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[:400])
    rows = []
    for line in p.stdout.decode("utf-8", "replace").split("\n"):
        line = line.rstrip("\r")
        if not line or line.startswith("mysql:"):
            continue
        rows.append(line.split("\t"))
    return rows


def i(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def spells_from_dbc(path):
    """Spell.dbc: field 0 = id, 136 = SpellName[enUS], 153 = Rank[enUS]."""
    if not os.path.isfile(path):
        return None
    blob = open(path, "rb").read()
    if blob[:4] != b"WDBC":
        return None
    rc, fc, rs, sbs = struct.unpack("<4I", blob[4:20])
    if fc <= 153:
        return None
    recs = blob[20:20 + rc * rs]
    sb = blob[20 + rc * rs: 20 + rc * rs + sbs]

    def sbstr(off):
        if off <= 0 or off >= len(sb):
            return ""
        end = sb.find(b"\x00", off)
        return sb[off:end].decode("utf-8", "replace")

    out = []
    for n in range(rc):
        b = n * rs
        sid = struct.unpack_from("<I", recs, b)[0]
        name = sbstr(struct.unpack_from("<I", recs, b + 136 * 4)[0])
        if not name:
            continue
        out.append([sid, name, sbstr(struct.unpack_from("<I", recs, b + 153 * 4)[0])])
    return out


def build(realm):
    slug = realm["slug"]
    paths = realm.get("paths") or {}
    conf = rebase(paths.get("configs", ""))
    dsn = dsn_from_conf(conf)
    if not dsn:
        print("  %-12s skipped - no worldserver.conf at %s" % (slug, conf))
        return None
    cnf = make_cnf(dsn, slug)
    db = dsn["db"]
    data = {"_realm": slug, "_db": db}
    try:
        rows = q(cnf, db, "SELECT name, security, IFNULL(help,'') FROM command ORDER BY name;")
        data["commands"] = [[r[0], i(r[1]), r[2].replace("\\n", "\n")]
                            for r in rows if len(r) >= 3]

        rows = q(cnf, db, "SELECT entry,name,Quality,ItemLevel,RequiredLevel,class "
                          "FROM item_template ORDER BY entry;")
        data["items"] = [[i(r[0]), r[1], i(r[2]), i(r[3]), i(r[4]), i(r[5])]
                         for r in rows if len(r) >= 6]

        # `rank` is a reserved word in MySQL 8 (window function) - must be backticked.
        rows = q(cnf, db, "SELECT entry,name,minlevel,maxlevel,`rank`,type "
                          "FROM creature_template ORDER BY entry;")
        data["npcs"] = [[i(r[0]), r[1], i(r[2]), i(r[3]), i(r[4]), i(r[5])]
                        for r in rows if len(r) >= 6]

        rows = q(cnf, db, "SELECT entry,name,type FROM gameobject_template ORDER BY entry;")
        data["objects"] = [[i(r[0]), r[1], i(r[2])] for r in rows if len(r) >= 3]

        rows = q(cnf, db, "SELECT ID,IFNULL(LogTitle,''),QuestLevel,MinLevel "
                          "FROM quest_template ORDER BY ID;")
        data["quests"] = [[i(r[0]), r[1], i(r[2]), i(r[3])] for r in rows if len(r) >= 4]
    finally:
        try:
            os.remove(cnf)
        except OSError:
            pass

    # Spells come from THIS realm's server DBC, not a shared one: a modified
    # realm ships 209,509 spells where vanilla ships 49,839.
    server = rebase(paths.get("server", ""))
    sp = spells_from_dbc(os.path.join(server, "Data", "dbc", "Spell.dbc"))
    data["spells"] = sp if sp is not None else []

    out = os.path.join(HERE, "refdata-%s.json" % slug)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print("  %-12s db=%-14s items=%-7d npcs=%-7d quests=%-6d spells=%-7d  %.1f MB"
          % (slug, db, len(data["items"]), len(data["npcs"]), len(data["quests"]),
             len(data["spells"]), os.path.getsize(out) / 1e6))
    return out


if __name__ == "__main__":
    doc = load_profiles()
    want = [s.lower() for s in sys.argv[1:]]
    built = {}
    for realm in doc.get("realms", []):
        if realm.get("kind") == "client":
            continue
        if want and realm["slug"] not in want:
            continue
        conf = rebase((realm.get("paths") or {}).get("configs", ""))
        if not want and not os.path.isdir(conf):
            continue
        try:
            p = build(realm)
            if p:
                built[realm["slug"]] = p
        except Exception as e:
            print("  %-12s FAILED: %s" % (realm["slug"], str(e)[:200]))
    # keep the unsuffixed file working for anything that still asks for it
    active = doc.get("activeRealm")
    if active in built:
        shutil.copyfile(built[active], os.path.join(HERE, "refdata.json"))
        print("\nrefdata.json <- refdata-%s.json (active realm)" % active)
    print("built %d realm datasets" % len(built))
