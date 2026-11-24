#!/usr/bin/env python3
"""Operator CLI for the listen stack: identity, partitions, vault control.

`hub` is the only writer of the registry's partition list and of the identity
DB from the shell; the gateway/identity server are the read side. It runs where
the data lives (inside the container: `docker compose exec listen hub ...`), so
paths are the container's. Everything but `reload` only edits partitions.json
and lets the watchdog regenerate and apply the config; `reload` does that apply.
"""
import argparse
import getpass
import json
import os
import re
import subprocess
import sys

# __file__ is the REAL file when hub is imported, but through the image's
# `/usr/local/bin/hub` symlink it is the LINK - and the CLI is normally invoked
# that way. Every sibling lookup must resolve the link first: dirname(__file__)
# of the symlink is /usr/local/bin, where gen-conf.py does not live, and `hub
# reload` used to die with "can't open file /usr/local/bin/gen-conf.py".
HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import envfmt
import hubdb

# Derived, not a literal: HUB_DATA_DIR is what relocates the state directory
# (compose sets it from HUB_HOST_DIR), and a `hub` invoked inside the container
# must read the same registry the gate and gen-conf read. HUB_REGISTRY still
# overrides, which is what the tests and entrypoint use.
REGISTRY = os.environ.get("HUB_REGISTRY") or \
    os.environ.get("HUB_DATA_DIR", "/data").rstrip("/") + "/partitions.json"
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


def vault_of(reg: dict, slug: str) -> tuple[str, str]:
    """(vault id, host partition) for the vault `slug` signs in with.

    Supervisor programs are named after the VAULT (platform/gen-conf.py
    vault_hosts), because one vault id is one Google account: only one browser
    family may sign it in, and only one watcher may report it. So `hub vault start
    kid` for a partition that shares
    vault "owner" has to talk to program `vault-owner` - and the operator has to
    see that this starts sign-in for every partition named on that vault, not for
    `kid` alone.

    Mirrors gen-conf's rule (first partition in slug order naming the vault is
    the host) rather than importing it: gen-conf.py's hyphenated name is not an
    importable module, and this copy is three lines with a test pinning that both
    agree. Unknown slug -> the slug as its own vault, which is what the registry
    itself defaults to, so supervisorctl's own "no such process" is the answer.
    """
    parts = reg.get("partitions", {})
    spec = parts.get(slug) or {}
    vault = spec.get("vault") or slug
    for s in reg.get("slugs", []):
        sp = parts.get(s) or {}
        if (sp.get("vault") or s) == vault:
            return vault, s
    return vault, slug

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
        if a.vault and not SLUG_RE.match(a.vault):
            print(f"hub: bad vault id {a.vault!r} - a vault id becomes a path "
                  f"segment and a supervisor program name", file=sys.stderr)
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
        # A vault id is a Google account, and the account gets ONE browser
        # (platform/gen-conf.py vault_hosts). Say so when the new partition lands
        # on an account somebody already named: the operator who adds `kid`
        # expecting a browser of its own otherwise waits for a chromium that is
        # never started, and `hub vault start kid` in fact drives alpha's.
        vault = spec["vault"]
        siblings = [s for s in slugs if s != a.slug
                    and vault_of(reg, s)[0] == vault]
        if siblings:
            host = vault_of(reg, a.slug)[1]
            print(f"hub: partition {a.slug} added; it signs in as vault {vault} "
                  f"with {', '.join(siblings)}, so ONE browser (on {host}) serves "
                  f"all of them - no sign-in is needed here")
        else:
            print(f"hub: partition {a.slug} added; the watchdog reloads within ~10s")
    elif a.action == "rm":
        if a.slug not in slugs:
            print(f"hub: no such partition {a.slug}", file=sys.stderr)
            return 1
        vault, was_host = vault_of(reg, a.slug)
        rest = [s for s in slugs if s != a.slug]
        after = {"slugs": rest, "partitions": {k: parts[k] for k in rest}}
        # gen-conf's rule on the registry as it will be: whoever still names this
        # vault becomes its browser host, or nobody does.
        inherits = next((s for s in rest if vault_of(after, s)[0] == vault),
                        None)
        slugs.remove(a.slug)
        parts.pop(a.slug, None)
        if reg.get("defaultSlug") == a.slug:
            reg["defaultSlug"] = slugs[0] if slugs else None
        save_registry(reg)
        # The identity DB is keyed on the slug, so its rows have to leave with
        # the partition: a session row is looked up by (slug, username), so an
        # orphan keeps a dead browser cookie VALID, and re-using this slug for
        # some other tenant would hand that cookie straight to the new one.
        # Same for pair_codes (a code outlives its minting partition otherwise).
        conn = hubdb.init()
        pruned = {t: conn.execute(f"DELETE FROM {t} WHERE slug=?",
                                  (a.slug,)).rowcount
                  for t in ("memberships", "sessions", "pair_codes")}
        conn.commit()
        conn.close()
        live = sum(pruned.values())
        detail = ", ".join(f"{v} {k}" for k, v in pruned.items() if v)
        data = os.environ.get("HUB_DATA_DIR", "/data")
        print(f"hub: partition {a.slug} removed"
              + (f" ({live} identity row(s) pruned: {detail})" if live else "")
              + f". Its data under {data}/users/{a.slug} and vaults/{a.slug} "
                "is left in place; delete it by hand if you mean it.")
        if was_host:
            if inherits:
                print(f"     vault {vault} moves to {inherits}, which still names "
                      f"it: its browser starts there on the next reload")
            else:
                print(f"     no partition left names vault {vault}: nothing "
                      f"supervises vaults/{vault} now")
    elif a.action == "set":
        spec = parts.get(a.slug)
        if spec is None:
            print(f"hub: no such partition {a.slug}", file=sys.stderr)
            return 1
        if a.vault is not None and not SLUG_RE.match(a.vault):
            print(f"hub: bad vault id {a.vault!r} - a vault id becomes a path "
                  f"segment and a supervisor program name", file=sys.stderr)
            return 1
        pairs = setting_pairs(a)
        if not pairs and a.vault is None:
            print(f"hub: nothing to set for {a.slug}; pass KEY=VALUE "
                  f"(positionals or --set) or --vault <id>", file=sys.stderr)
            return 1
        refusals = apply_settings(spec, pairs)
        if refusals:
            for m in refusals:
                print(f"hub: refusing setting {m}", file=sys.stderr)
            return 1
        moved = None
        if a.vault is not None and (spec.get("vault") or a.slug) != a.vault:
            old, was_host = vault_of(reg, a.slug)
            spec["vault"] = a.vault
            moved = (old, was_host, vault_of(reg, a.slug))
        elif a.vault is not None:
            spec["vault"] = a.vault       # same vault, stated explicitly
        save_registry(reg)
        if moved:
            old, was_host, (new, new_host) = moved
            print(f"hub: {a.slug} now signs in as vault {new} (browser on "
                  f"{new_host}); its own cookies under users/{a.slug} keep "
                  f"serving it until that vault's browser exports over them")
            if was_host:
                still = [s for s in slugs if s != a.slug
                         and (parts[s].get("vault") or s) == old]
                if still:
                    print(f"     vault {old} moves to {still[0]}, which still "
                          f"names it: its browser starts there on the next reload")
                else:
                    print(f"     no partition left names vault {old}: nothing "
                          f"supervises vaults/{old} now")
        else:
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
        hosts: dict = {}
        for s in slugs:
            hosts.setdefault(parts[s].get("vault") or s, s)
        for s in slugs:
            sp = parts[s]
            vault = sp.get("vault") or s
            # The browser for a vault lives on ONE partition (gen-conf
            # vault_hosts); the rest get their cookies by fan-out. Say which is
            # which, because `hub vault start <slug>` follows this line.
            who = " (browser)" if hosts[vault] == s else f" (via {hosts[vault]})"
            print(f"  {s:12} {sp.get('mode','single'):7} radio="
                  f"{'yes' if sp.get('radio', True) else 'no ':3} "
                  f"vault={vault}{who}"
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
    """Vault control, addressed by any partition that uses the vault.

    `hub vault start kid` is the natural thing to type, and it means "get this
    account signed in", so it resolves to the vault's program (vault_of). Because
    one vault may serve several partitions, every action says which vault it
    acted on and which partitions share it - otherwise a start for one looks like
    a start for one.
    """
    reg = registry()
    vault, host = vault_of(reg, a.slug)
    name = f"vault-{vault}"
    watcher = f"exporter-{vault}"
    members = [s for s in reg.get("slugs", [])
               if vault_of(reg, s)[0] == vault]
    if a.action == "status":
        # All three halves of the vault, most-informative first.
        # `exporter-<vault>` is the session WATCHER (always on): the only writer
        # of session-state.json, so if IT is down nobody is reporting the account
        # and nothing can be announced to ntfy - the first thing to look at when
        # "the notifications stopped". `vnc-<vault>` is the always-on TRANSPORT
        # that makes the noVNC door answer (a door that says "connection is
        # closed" is this one). `vault-<vault>` is the demand BROWSER that signs
        # the account in - STOPPED is its healthy resting state, which is exactly
        # why looking at it alone tells you nothing.
        print(f"-- watcher (exporter-{vault}: session verdict + notifications) --")
        ctl("status", watcher)
        print(f"-- transport (vnc-{vault}: noVNC door) --")
        ctl("status", f"vnc-{vault}")
        print(f"-- browser (vault-{vault}: sign-in) --")
        rc = ctl("status", name)
        _vault_note(vault, host, members)
        return rc
    if a.action == "logs":
        logs = os.environ.get("HUB_VAULT_LOG_DIR", "/var/log/sup")
        # The watcher's log FIRST: it holds the session verdicts, the probes and
        # every ntfy/webhook push, and it keeps writing while the browser log sits
        # empty between sign-ins. Tailing only the browser log is how "no
        # notifications" looked like a dead channel instead of a stopped reporter.
        # A missing file is printed, not fatal: supervisor creates it on the
        # program's first start, so a never-started watcher reads as absent.
        rc = 0
        for label, prog in (("watcher", watcher), ("browser", name)):
            print(f"-- {label} ({prog}.log, last {a.lines} lines) --")
            try:
                with open(os.path.join(logs, f"{prog}.log")) as fh:
                    sys.stdout.write("".join(fh.readlines()[-int(a.lines):]))
            except OSError as exc:
                print(f"hub: {exc}")
                rc = 1
        _vault_note(vault, host, members)
        return rc
    if a.action in ("start", "stop", "restart"):
        rc = ctl(a.action, name)
        _vault_note(vault, host, members)
        return rc
    print("hub vault: unknown action", file=sys.stderr)
    return 1


def _vault_note(vault: str, host: str, members: list) -> None:
    """Say which vault was touched and who else depends on it."""
    if len(members) > 1:
        print(f"hub: vault {vault} (browser on {host}) serves {', '.join(members)}"
              f" - one sign-in covers all of them")
    elif members and members[0] != host:
        print(f"hub: vault {vault} is named by {host}; no partition owns it")


def cmd_reload(a) -> int:
    """Re-run the generator and apply it - the impatient version of what the
    watchdog does within ~10 s of a registry edit, so the same three steps.

    `nginx -s reload` IS one of them. Regenerate + `supervisorctl reread/update`
    alone gives a new partition its backends while the gateway still serves the
    OLD location set: live programs, no proxy, 404 at the mount. And validating
    before reloading is not decoration - it is what keeps a bad partitions.json
    edit from replacing a working gateway config with one that 404s every
    partition (nginx keeps running the config it already has). A failed `nginx
    -t` still gets the supervisor half applied, exactly as the watchdog does it:
    programs are validated by supervisor itself, and the old gateway keeps
    serving the partitions that were already live.
    """
    r = subprocess.run([sys.executable, os.path.join(HERE, "gen-conf.py")])
    if r.returncode:
        return r.returncode
    test = subprocess.run(["nginx", "-t"], capture_output=True, text=True,
                          check=False)
    bad = test.returncode
    if bad:
        print("hub reload: the generated config FAILED nginx -t, so nginx keeps "
              f"the previous config:\n{test.stderr.strip()}", file=sys.stderr)
    else:
        subprocess.run(["nginx", "-s", "reload"], check=False)
    sup = ctl("reread") or ctl("update")
    return 1 if bad else sup


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

    p = sub.add_parser("reload", help="regenerate config and apply it to nginx "
                       "and supervisor")
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
