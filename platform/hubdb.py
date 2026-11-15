"""Shared identity store for the listen stack: schema, password hashing,
sessions, pairing codes. Imported by hubauth.py (the HTTP gate) and hub.py
(the admin CLI) so the two can never disagree about a hash format.

SQLite is the right tool here: single-writer (this box, one process family),
tiny rows, and a crash-safe WAL. WAL also matters operationally: the hub DB
lives on the same volume as the media, and DELETE-journal mode would block
every reader behind a writer's lock.

Password storage: pbkdf2-hmac-sha256, 240k iterations, 16-byte random salt,
format "pbkdf2$sha256$<iters>$<salt-hex>$<hash-hex>". Only logins pay the KDF;
the per-request gate path looks up a random token by its SHA-256, so an
auth_request subrequest costs one indexed query.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time

# Same relocation rule as every other reader of the state directory: compose owns
# HUB_DATA_DIR, so derive from it and let HUB_DB override for a test or a split
# layout. A literal here would leave the identity store behind when the data dir
# moved, and `hub user add` would silently edit a different database than the one
# the gate authenticates against.
DB_PATH = os.environ.get("HUB_DB") or \
    os.environ.get("HUB_DATA_DIR", "/data").rstrip("/") + "/hub.db"
# A username is used as a filename component (progress-<user>.json), so it is
# restricted to characters that are safe in a path on every filesystem.
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")
ITERATIONS = 240_000
SESSION_TTL = 400 * 86400          # a car stereo should not need to re-login yearly
CODE_TTL = 600                     # pairing codes live 10 minutes
# Prefix hubdb gives to the member a pairing mints for itself: it must satisfy
# USERNAME_RE because it becomes a progress filename.
PAIR_PREFIX = "pair-"
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  username   TEXT PRIMARY KEY,
  pass_hash  TEXT NOT NULL,
  api_key    TEXT,                    -- sha256 hex of the bearer key, NULL = none
  created    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
  slug       TEXT NOT NULL,
  username   TEXT NOT NULL,
  role       TEXT NOT NULL DEFAULT 'member',   -- 'admin' may mint pairing codes
  created    INTEGER NOT NULL,
  PRIMARY KEY (slug, username)
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  slug       TEXT NOT NULL,
  username   TEXT NOT NULL,
  device     TEXT NOT NULL DEFAULT '',
  created    INTEGER NOT NULL,
  last_seen  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_slug ON sessions(slug, username);
CREATE TABLE IF NOT EXISTS pair_codes (
  code_hash  TEXT PRIMARY KEY,
  slug       TEXT NOT NULL,
  created    INTEGER NOT NULL,
  expires    INTEGER NOT NULL,
  used       INTEGER NOT NULL DEFAULT 0,
  member     TEXT NOT NULL DEFAULT ''      -- who the code signs in as; '' = mint one
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(path: str | None = None) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS cannot change a table that already exists, so
    # a hub DB written before pairing could name a member keeps its old
    # pair_codes and the mint/redeem INSERT would fail with "no column named
    # member". Add it in place; the default is the same '' the new schema uses,
    # so pre-existing codes keep redeeming as an auto-created member.
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(pair_codes)")]
    if "member" not in cols:
        conn.execute("ALTER TABLE pair_codes ADD COLUMN member TEXT NOT NULL "
                     "DEFAULT ''")
    conn.commit()
    return conn


# ---- passwords ---------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2$sha256${ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, algo, iters, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2" or algo != "sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# ---- tokens ------------------------------------------------------------
def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_code() -> str:
    """8 uppercase characters, no vowels or confusables: typeable on a phone
    in a car, and unambiguous over the phone to someone at home."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        if "FUK" not in code and "SEX" not in code and "ASS" not in code:
            return code


def code_hash(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def new_api_key() -> str:
    return "lk_" + secrets.token_hex(20)


# ---- operations --------------------------------------------------------
def add_user(conn: sqlite3.Connection, username: str, password: str) -> None:
    if not USERNAME_RE.match(username):
        raise ValueError(f"invalid username {username!r}: use lowercase "
                         f"[a-z0-9_.-], starting with a letter or digit")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    conn.execute("INSERT INTO users(username,pass_hash,created) VALUES(?,?,?)",
                 (username, hash_password(password), int(time.time())))
    conn.commit()


def set_password(conn: sqlite3.Connection, username: str, password: str) -> bool:
    cur = conn.execute("UPDATE users SET pass_hash=? WHERE username=?",
                       (hash_password(password), username))
    conn.commit()
    return cur.rowcount > 0


def set_membership(conn, slug: str, username: str, role: str = "member") -> None:
    conn.execute("INSERT OR IGNORE INTO memberships(slug,username,role,created) "
                 "VALUES(?,?,?,?)", (slug, username, role, int(time.time())))
    conn.commit()


def ensure_pair_member(conn, slug: str, name: str = "") -> str:
    """The member a pairing signs a device in as, created here if it is new.

    `name` empty means "invent one": `pair-` plus 6 random hex (16.7M names, so a
    clash just re-rolls). Either way the row is an ordinary member, so a paired
    car gets its OWN progress file - the filename comes from the username, not
    from the session - and `hub user rm <name>` removes it like any other.

    A named member is created on demand rather than required to pre-exist: the
    admin typing `--as car` (or a car typing a name at `/pair`) has already
    decided that name exists, and rejecting it would only force a second command
    for the same decision. Revocation is still honoured, just later - see
    `redeem_code`, which refuses a name its partition dropped in between.

    The password is a throwaway random token nobody keeps, so the member cannot
    sign in by password, only by presenting the code: that IS the pairing
    contract, and `hub user passwd <name>` gives it a password on request."""
    if not name:
        while True:
            name = PAIR_PREFIX + secrets.token_hex(3)
            if not conn.execute("SELECT 1 FROM users WHERE username=?",
                                (name,)).fetchone():
                break
    if not USERNAME_RE.match(name):
        raise ValueError(f"invalid member name {name!r}: use lowercase "
                         f"[a-z0-9_.-], starting with a letter or digit")
    if not conn.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
        add_user(conn, name, secrets.token_urlsafe(24))
    set_membership(conn, slug, name)
    return name


def check_credentials(conn, username: str, password: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if row and verify_password(password, row["pass_hash"]):
        return row
    return None


def mint_session(conn, slug: str, username: str, device: str) -> str:
    token = new_token()
    now = int(time.time())
    conn.execute("INSERT INTO sessions(token_hash,slug,username,device,created,last_seen) "
                 "VALUES(?,?,?,?,?,?)", (token_hash(token), slug, username,
                                          device[:80], now, now))
    conn.commit()
    return token


def touch_session(conn, token_hash_value: str) -> None:
    """Self-throttling write-ahead bump: at most one UPDATE per session per
    5 minutes, so a busy media page cannot serialize the hub DB behind
    last_seen churn."""
    now = int(time.time())
    conn.execute("UPDATE sessions SET last_seen=? WHERE token_hash=? "
                 "AND last_seen < ?", (now, token_hash_value, now - 300))
    conn.commit()


def lookup_session(conn, token: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM sessions WHERE token_hash=?",
                       (token_hash(token),)).fetchone()
    if not row:
        return None
    if row["created"] + SESSION_TTL < time.time():
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (row["token_hash"],))
        conn.commit()
        return None
    return row


def drop_session(conn, token: str) -> bool:
    cur = conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))
    conn.commit()
    return cur.rowcount > 0


def mint_code(conn, slug: str, member: str = "") -> tuple[str, int]:
    """Pairing code for `slug`, to be redeemed as `member`.

    `member` '' (the default) leaves the identity undecided: the pairing creates
    a member of its own when the device types the code. A name is created here if
    the partition has not heard of it yet - `--as car` is the admin's decision
    that this device IS `car`, and making them run `hub user add car` first would
    only be a second command for a decision already made - so an admin can name a
    car without pre-creating it, and re-pairing that car with a fresh code keeps
    its position."""
    if member:
        ensure_pair_member(conn, slug, member)
    code = new_code()
    now = int(time.time())
    conn.execute("INSERT INTO pair_codes(code_hash,slug,created,expires,member) "
                 "VALUES(?,?,?,?,?)", (code_hash(code), slug, now, now + CODE_TTL,
                                       member))
    # prune expired/used codes so the table cannot grow without bound
    conn.execute("DELETE FROM pair_codes WHERE expires < ? OR used = 1", (now - 86400,))
    conn.commit()
    return code, CODE_TTL


def redeem_code(conn, slug: str, code: str, device: str) -> str | None:
    """Returns a session token, or None if the code is unknown/expired/used,
    belongs to another partition, or names a member the partition dropped between
    minting and typing it in. The UPDATE ... WHERE used=0 guard makes redemption
    single-use even if two devices race on the same code.

    A member revoked in that window burns its code rather than walking back in:
    dropping a member is the admin's last word, and a session minted afterwards
    would silently undo it. An unassigned code takes a member of its own, which is
    what makes "a paired device gets its own progress file" true for a second car
    in ONE partition rather than true only across partitions."""
    h = code_hash(code)
    row = conn.execute("SELECT * FROM pair_codes WHERE code_hash=?", (h,)).fetchone()
    if not row or row["used"] or row["slug"] != slug or row["expires"] < time.time():
        return None
    cur = conn.execute("UPDATE pair_codes SET used=1 WHERE code_hash=? AND used=0", (h,))
    if cur.rowcount != 1:
        conn.commit()
        return None
    username = (row["member"] if "member" in row.keys() else "") or ""
    if username:
        keep = conn.execute("SELECT 1 FROM memberships WHERE slug=? AND username=?",
                            (slug, username)).fetchone()
        if not keep:
            conn.commit()
            return None
    else:
        username = ensure_pair_member(conn, slug)
    conn.commit()
    return mint_session(conn, slug, username, device)


def lookup_api_key(conn, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE api_key=?",
                        (hashlib.sha256(key.encode()).hexdigest(),)).fetchone()


def regen_api_key(conn, username: str) -> str:
    key = new_api_key()
    conn.execute("UPDATE users SET api_key=? WHERE username=?",
                 (hashlib.sha256(key.encode()).hexdigest(), username))
    conn.commit()
    return key
