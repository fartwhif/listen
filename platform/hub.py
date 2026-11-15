#!/usr/bin/env python3
"""Operator CLI for the listen stack: identity, partitions, vault control.

`hub` is the only writer of the registry's partition list and of the identity
DB from the shell; the gateway/identity server are the read side. It runs where
the data lives (inside the container: `docker compose exec listen hub ...`), so
paths are the container's, and it never touches nginx or supervisor config -
it edits partitions.json and lets the watchdog regenerate everything.
"""
import argparse
import getpass
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envfmt
import hubdb

# Derived, not a literal: HUB_DATA_DIR is what relocates the state directory
# (compose sets it from HUB_HOST_DIR), and a `hub` invoked inside the container
# must read the same registry the gate and gen-conf read. HUB_REGISTRY still
# overrides, which is what the tests and entrypoint use.
REGISTRY = os.environ.get("HUB_REGISTRY") or \
    os.environ.get("HUB_DATA_DIR", "/data").rstrip("/") + "/partitions.json"
DEFAULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "partitions.default.json")
SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def registry() -> dict:
    try:
        with open(REGISTRY) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"slugs": [], "defaultSlug": None, "partitions": {}}


def save_registry(reg: dict) -> None:
    """Atomic rewrite; the watchdog polls mtime, so a half-written file must
    never be observable."""
    tmp = REGISTRY + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(reg, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, REGISTRY)


def ctl(*args: str) -> int:
    """supervisorctl, so vault start/stop goes through the same supervisor that
    supervises everything else (logs, respawn policy, one place to look)."""
    sock = os.environ.get("HUB_SUP_SOCK", "/var/run/supervisor.sock")
    r = subprocess.run(["supervisorctl", "-s", f"unix://{sock}", *args],
                       capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


def apply_settings(spec: dict, pairs: list) -> list:
    """Write `KEY=VALUE` strings into a partition's settings, rejecting any the
    supervisor env format or the env naming rules cannot carry.

    A setting becomes that partition's programs' environment, and the two
    things that make one unusable are decided elsewhere: gen-conf ignores a
    name it cannot use as an env var, and envfmt cannot express some values.
    Checking here turns both into a CLI error at the moment of typing instead
    of a dropped line in the watchdog's log ten seconds later, and keeps the
    registry from holding a setting no program will ever read. Returns the
    refusals as message strings; the caller prints them and exits non-zero
    without saving, so one bad pair never lands halfway.
    """
    out = {}
    refusals = []
    for kv in pairs:
        k, sep, v = kv.partition("=")
        if not sep:
            refusals.append(f"{kv!r}: expected KEY=VALUE")
            continue
        if not ENV_NAME_RE.match(k):
            refusals.append(f"{k}: a setting is passed through as an env var, "
                            f"so the name must match {ENV_NAME_RE.pattern}")
            continue
        if not envfmt.renderable(v):
            refusals.append(f"{k}={v!r}: {envfmt.REASON}")
            continue
        out[k] = v
    if refusals:
        return refusals
    merged = dict(spec.get("settings") or {})
    merged.update(out)
    spec["settings"] = merged
    return []


# ---------------------------------------------------------------- identity
def cmd_bootstrap(a) -> int:
    conn = hubdb.init()
    reg = registry()
    owner = a.username
    if not conn.execute("SELECT 1 FROM users WHERE username=?",
                        (owner,)).fetchone():
        pw = a.password or os.environ.get("HUB_OWNER_PASSWORD") or ""
        if not pw and sys.stdin.isatty():
            pw = getpass.getpass(f"password for {owner}: ")
        if len(pw) < 8:
            print("hub bootstrap: password must be >= 8 characters; giving up "
                  "(set HUB_OWNER_PASSWORD or pass --password)", file=sys.stderr)
            return 1
        hubdb.add_user(conn, owner, pw)
        print(f"hub: user {owner} created")
    for slug in reg.get("slugs", []):
        if not conn.execute("SELECT 1 FROM memberships WHERE slug=? AND username=?",
                            (slug, owner)).fetchone():
            hubdb.set_membership(conn, slug, owner, "admin")
            print(f"hub: {owner} is admin of {slug}")
    conn.close()
    return 0


def cmd_user(a) -> int:
    conn = hubdb.init()
    if a.action == "add":
        pw = a.password or (getpass.getpass("password: ") if sys.stdin.isatty() else "")
        try:
            hubdb.add_user(conn, a.name, pw)
        except ValueError as exc:
            print(f"hub: {exc}", file=sys.stderr)
            return 1
        print(f"hub: user {a.name} created")
    elif a.action == "passwd":
        pw = a.password or (getpass.getpass(f"new password for {a.name}: ")
                           if sys.stdin.isatty() else "")
        if not hubdb.set_password(conn, a.name, pw):
            print(f"hub: no such user {a.name}", file=sys.stderr)
            return 1
        print(f"hub: password updated for {a.name} (existing sessions stay valid)")
    elif a.action == "rm":
        conn.execute("DELETE FROM memberships WHERE username=?", (a.name,))
        conn.execute("DELETE FROM sessions WHERE username=?", (a.name,))
        conn.execute("DELETE FROM users WHERE username=?", (a.name,))
        conn.commit()
        print(f"hub: user {a.name} removed (and their sessions)")
    elif a.action == "key":
        key = hubdb.regen_api_key(conn, a.name)
        print(f"{key}" if a.quiet else
              f"hub: API key for {a.name}: {key}\n"
              "use it as ?key=<key> or 'Authorization: Bearer <key>'.")
    conn.close()
    return 0


def cmd_members(a) -> int:
    conn = hubdb.init()
    if a.action == "add":
        hubdb.set_membership(conn, a.slug, a.name, a.role)
        print(f"hub: {a.name} is now {a.role} of {a.slug}")
    elif a.action == "rm":
        conn.execute("DELETE FROM memberships WHERE slug=? AND username=?",
                     (a.slug, a.name))
        conn.commit()
        print(f"hub: {a.name} no longer a member of {a.slug}")
    else:
        rows = conn.execute("SELECT username, role, created FROM memberships "
                            "WHERE slug=? ORDER BY role, username",
                            (a.slug,)).fetchall()
        for r in rows:
            print(f"{r['role']:6} {r['username']}")
        if not rows:
            print(f"(no members in {a.slug})")
    conn.close()
    return 0


# ----------------------------------------------------------------- settings
def setting_pairs(a) -> list:
    """Every KEY=VALUE the partition command was given, both spellings.

    Settings can be typed as positionals (`set q1 KEY=v`) or with `--set`
    (`add q1 --set KEY=v`), and the two actions used to read only one of them
    each: `add` ignored positionals, `set` ignored `--set` — and both printed
    "updated" while saving nothing, which is worse than an error. Reading both
    in both actions means whatever spelling a user reaches for takes effect.
    """
    return list(a.kv or []) + list(getattr(a, "set", None) or [])


# -------------------------------------------------------------- partitions
def cmd_partition(a) -> int:
    reg = registry()
    parts = reg.setdefault("partitions", {})
    slugs = reg.setdefault("slugs", [])
    if a.action == "add":
        if not SLUG_RE.match(a.slug):
            print(f"hub: bad slug {a.slug!r}", file=sys.stderr)
            return 1
        if a.slug in slugs:
            print(f"hub: {a.slug} already exists", file=sys.stderr)
            return 1
        spec = {"mode": a.mode, "name": a.name or a.slug.capitalize(),
                "owner": a.owner, "radio": not a.no_radio,
                "vault": a.vault or a.slug}
        if a.quota:
            spec["quota"] = {"concurrentDownloads": int(a.quota)}
        refusals = apply_settings(spec, setting_pairs(a))
        if refusals:
            for m in refusals:
                print(f"hub: refusing setting {m}", file=sys.stderr)
            return 1
        parts[a.slug] = spec
        slugs.append(a.slug)
        if a.owner:
            conn = hubdb.init()
            hubdb.set_membership(conn, a.slug, a.owner, "admin")
            conn.close()
        reg["defaultSlug"] = reg.get("defaultSlug") or a.slug
        save_registry(reg)
        print(f"hub: partition {a.slug} added; the watchdog reloads within ~10s")
    elif a.action == "rm":
        if a.slug not in slugs:
            print(f"hub: no such partition {a.slug}", file=sys.stderr)
            return 1
        slugs.remove(a.slug)
        parts.pop(a.slug, None)
        if reg.get("defaultSlug") == a.slug:
            reg["defaultSlug"] = slugs[0] if slugs else None
        save_registry(reg)
        print(f"hub: partition {a.slug} removed. Its data under "
              f"{os.environ.get('HUB_DATA_DIR', '/data')}/users/{a.slug} and "
              f"vaults/{a.slug} is left in place; delete it by hand if you mean it.")
    elif a.action == "set":
        spec = parts.get(a.slug)
        if spec is None:
            print(f"hub: no such partition {a.slug}", file=sys.stderr)
            return 1
        pairs = setting_pairs(a)
        if not pairs:
            print(f"hub: nothing to set for {a.slug}; pass KEY=VALUE "
                  f"(positionals or --set)", file=sys.stderr)
            return 1
        refusals = apply_settings(spec, pairs)
        if refusals:
            for m in refusals:
                print(f"hub: refusing setting {m}", file=sys.stderr)
            return 1
        save_registry(reg)
        print(f"hub: settings updated for {a.slug}")
    elif a.action == "default":
        if a.slug not in slugs:
            print(f"hub: no such partition {a.slug}", file=sys.stderr)
            return 1
        reg["defaultSlug"] = a.slug
        save_registry(reg)
        print(f"hub: {a.slug} is now the front partition (root paths)")
    else:
        print(f"slugs: {', '.join(slugs) or '(none)'}")
        print(f"front: {reg.get('defaultSlug')}")
        for s in slugs:
            sp = parts[s]
            print(f"  {s:12} {sp.get('mode','single'):7} radio="
                  f"{'yes' if sp.get('radio', True) else 'no ':3} vault={sp.get('vault', s)}"
                  f" name={sp.get('name', s)}")
    return 0


def cmd_code(a) -> int:
    conn = hubdb.init()
    try:
        code, ttl = hubdb.mint_code(conn, a.slug, getattr(a, "as_", "") or "")
    except ValueError as exc:
        print(f"hub: {exc}", file=sys.stderr)
        return 1
    conn.close()
    who = a.as_ or "a new passwordless member (pair-…), created at pairing"
    print(f"hub: pairing code for {a.slug}: {code}  (valid {ttl}s)")
    print(f"     enter it at /pair after picking {a.slug}")
    print(f"     the device signs in as {who}, so it gets its own progress file")
    return 0


def cmd_vault(a) -> int:
    slug = a.slug
    if a.action == "status":
        return ctl("status", f"vault-{slug}")
    if a.action == "logs":
        logs = os.environ.get("HUB_VAULT_LOG_DIR", "/var/log/sup")
        path = os.path.join(logs, f"vault-{slug}.log")
        try:
            with open(path) as fh:
                sys.stdout.write("".join(fh.readlines()[-int(a.lines):]))
        except OSError as exc:
            print(f"hub: {exc}", file=sys.stderr)
            return 1
        return 0
    if a.action in ("start", "stop", "restart"):
        return ctl(a.action, f"vault-{slug}")
    print("hub vault: unknown action", file=sys.stderr)
    return 1


def cmd_reload(a) -> int:
    """Re-run the generator and apply it. The watchdog does this automatically
    within ~10 s of a registry edit; this is the impatient version."""
    r = subprocess.run([sys.executable,
                        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "gen-conf.py")])
    if r.returncode:
        return r.returncode
    return ctl("reread") or ctl("update")


def cmd_status(a) -> int:
    rc = ctl("status")
    conn = hubdb.init()
    rows = conn.execute("SELECT slug, username, device, last_seen FROM sessions "
                        "ORDER BY last_seen DESC LIMIT 12").fetchall()
    now = __import__("time").time()
    print("\nrecent sessions:")
    for r in rows:
        print(f"  {r['slug']:12} {r['username']:12} {int(now - r['last_seen'])//3600:>4}h ago  "
              f"{r['device'][:40]}")
    conn.close()
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(prog="hub", description=__doc__.strip().split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("bootstrap", help="create the owner user + memberships")
    p.add_argument("--username", default=os.environ.get("HUB_OWNER_USER", "owner"))
    p.add_argument("--password", default=None)
    p.set_defaults(fn=cmd_bootstrap)

    p = sub.add_parser("user", help="users: add/passwd/rm/key")
    p.add_argument("action", choices=["add", "passwd", "rm", "key"])
    p.add_argument("name")
    p.add_argument("password", nargs="?", default=None)
    p.add_argument("--quiet", action="store_true", help="print only the key")
    p.set_defaults(fn=cmd_user)

    p = sub.add_parser("members", help="members of a partition")
    p.add_argument("action", choices=["add", "rm", "list"])
    p.add_argument("slug")
    p.add_argument("name", nargs="?", default=None)
    p.add_argument("--role", default="member", choices=["admin", "member"])
    p.set_defaults(fn=cmd_members)

    p = sub.add_parser("partition", help="partitions: add/rm/set/default/list")
    p.add_argument("action", choices=["add", "rm", "set", "default", "list"])
    p.add_argument("slug", nargs="?", default=None)
    p.add_argument("--name", default=None)
    p.add_argument("--mode", default="single", choices=["single", "shared"])
    p.add_argument("--owner", default=os.environ.get("HUB_OWNER_USER", "owner"))
    p.add_argument("--vault", default=None)
    p.add_argument("--no-radio", action="store_true")
    p.add_argument("--quota", default=None, help="concurrent downloads for this partition")
    p.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="partition setting passed through as env (repeatable)")
    p.add_argument("kv", nargs="*", default=[],
                   help="KEY=VALUE settings (the same values --set takes)")
    p.set_defaults(fn=cmd_partition)

    p = sub.add_parser("code", help="mint a pairing code for a partition")
    p.add_argument("slug")
    p.add_argument("--as", dest="as_", default="", metavar="MEMBER",
                   help="sign the device in as this existing member instead of "
                        "creating a new passwordless one")
    p.set_defaults(fn=cmd_code)

    p = sub.add_parser("vault", help="cookie vault control")
    p.add_argument("action", choices=["start", "stop", "restart", "status", "logs"])
    p.add_argument("slug")
    p.add_argument("--lines", default="80")
    p.set_defaults(fn=cmd_vault)

    p = sub.add_parser("reload", help="force supervisor to re-read generated config")
    p.set_defaults(fn=cmd_reload)

    p = sub.add_parser("status", help="programs + recent sessions")
    p.set_defaults(fn=cmd_status)

    a = ap.parse_args()
    if getattr(a, "slug", None) in (None, "") and a.cmd in ("code", "vault"):
        ap.error("a slug is required")
    if a.cmd == "partition" and a.action != "list" and not a.slug:
        ap.error("a slug is required")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
