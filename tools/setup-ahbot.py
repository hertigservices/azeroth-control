"""Create a dedicated AH bot account + 'Auctions' character, then wire up mod_ahbot.conf.

Why a dedicated account rather than the player's own: AuctionHouseBotWorldScript does

    SELECT guid FROM characters WHERE account = {account}
    ... if (player == 0) gBotsId.insert(botId);   // EVERY character on the account

so with GUID=0 every character on that account becomes an auction seller. Isolating the
bot on its own account makes that failure mode harmless instead of turning a real
character into a vendor.

Run with the server STOPPED: the core initialises its GUID counter from MAX(guid) at
startup, so inserting beforehand avoids any chance of a reused GUID.
"""
import hashlib, os, re, secrets, subprocess, sys

# The hub is this file's grandparent (tools/ is one level down), so a clone
# anywhere works. AZCTL_HOME overrides it for a hub on another drive.
ROOT = os.environ.get('AZCTL_HOME') or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
MYSQL = os.path.join(ROOT, 'mysql', 'bin', 'mysql.exe')
CONF = os.path.join(ROOT, 'server', 'configs', 'modules', 'mod_ahbot.conf')
CREDS = os.path.join(ROOT, 'credentials.txt')

USER = 'AHBOT'          # AccountMgr uppercases both before hashing
CHAR = 'Auctions'

N = int('894B645E89E1535BBDAD5B8B290650530801B18EBFBF5E8FAB3C82872A3E9BB7', 16)
G = 7


def sql(q, want_rows=True):
    p = subprocess.run([MYSQL, '-h127.0.0.1', '-uacore', '-pacore', '--batch',
                        '--skip-column-names', '-e', q],
                       capture_output=True, timeout=60)
    if p.returncode != 0:
        print('SQL FAILED:', p.stderr.decode('utf-8', 'replace')[:400])
        sys.exit(1)
    out = [l.strip() for l in p.stdout.decode('utf-8', 'replace').splitlines()
           if l.strip() and not l.startswith('mysql:')]
    return out if want_rows else None


def srp6(username, password):
    """v = g ^ H(salt || H(UPPER(user):UPPER(pass))) mod N, little-endian both ends.
    Byte order was determined empirically against a known-good account, not assumed."""
    salt = secrets.token_bytes(32)
    h1 = hashlib.sha1(('%s:%s' % (username.upper(), password.upper())).encode()).digest()
    h2 = hashlib.sha1(salt + h1).digest()
    ver = pow(G, int.from_bytes(h2, 'little'), N).to_bytes(32, 'little')
    return salt, ver


def conf_set(key, value):
    with open(CONF, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    hit = False
    for i, line in enumerate(lines):
        s = line.strip()
        if not hit and '=' in s and s.split('=')[0].strip() == key:
            lines[i] = '%s = %s\n' % (key, value)
            hit = True
    if hit:
        with open(CONF, 'w', encoding='utf-8', newline='') as f:
            f.writelines(lines)
    print('  %-46s = %-14s %s' % (key, value, '' if hit else '(KEY NOT FOUND)'))
    return hit


# ---------- account ----------
existing = sql("SELECT id FROM acore_auth.account WHERE username='%s';" % USER)
if existing:
    acct = int(existing[0])
    print('account %s already exists (id %d)' % (USER, acct))
    pw = None
else:
    pw = secrets.token_hex(8).upper()[:12]
    salt, ver = srp6(USER, pw)
    sql("INSERT INTO acore_auth.account (username,salt,verifier,expansion,email,reg_mail) "
        "VALUES ('%s',UNHEX('%s'),UNHEX('%s'),2,'','');"
        % (USER, salt.hex().upper(), ver.hex().upper()), False)
    acct = int(sql("SELECT id FROM acore_auth.account WHERE username='%s';" % USER)[0])
    print('created account %s (id %d)' % (USER, acct))

# ---------- character ----------
row = sql("SELECT guid FROM acore_characters.characters WHERE account=%d AND name='%s';" % (acct, CHAR))
if row:
    guid = int(row[0])
    print("character '%s' already exists (guid %d)" % (CHAR, guid))
else:
    nxt = sql("SELECT IFNULL(MAX(guid),0)+1 FROM acore_characters.characters;")
    guid = int(nxt[0])
    # Human warrior, level 1. Never played - the module only stamps this GUID as the
    # auction owner - but valid values keep the row sane if anything else reads it.
    sql("INSERT INTO acore_characters.characters "
        "(guid,account,name,race,class,gender,level,innTriggerId,taximask) "
        "VALUES (%d,%d,'%s',1,1,0,1,0,'');" % (guid, acct, CHAR), False)
    print("created character '%s' (guid %d) on account %d" % (CHAR, guid, acct))

# ---------- config ----------
print('mod_ahbot.conf:')
conf_set('AuctionHouseBot.Account', acct)
conf_set('AuctionHouseBot.GUID', guid)          # pin to this character only
conf_set('AuctionHouseBot.EnableSeller', 1)
conf_set('AuctionHouseBot.EnableBuyer', 1)      # bots bidding makes the market feel live
conf_set('AuctionHouseBot.ItemsPerCycle', 200)

if pw:
    with open(CREDS, 'a', encoding='utf-8') as f:
        f.write('\nAuction House bot account (created by setup-ahbot.py)\n')
        f.write('-----------------------------------------------------------\n')
        f.write('Username: %s\nPassword: %s\n' % (USER, pw))
        f.write("Character: %s (guid %d) - do NOT play this character;\n" % (CHAR, guid))
        f.write("browsing the AH on it can hang on 'Searching for items...'\n")
    print('appended AHBOT credentials to', CREDS)

print('\nDone. Restart the world to start populating the auction house.')
