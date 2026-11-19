"""The cookie-session gate's state file: one producer, one consumer, one name.

`session-state.json` is the vault's verdict about its own Google session, and in
this container two things act on it:

- DrivePod's harvest gate (`sessionGateOpen`) pauses harvest rounds while the
  verdict is `down`, because dead-cookie harvesting only hammers Google and the
  write-backs delay the vault's re-seed;
- `GET /api/harvest-status` reports the same verdict to the harvest tab's badge.

The bug this file locks out is a NAME mismatch, not a path typo. `gen-conf` has
named each partition's state file (`…/users/<slug>/session-state.json`) in the
tenant env since the hub's first commit, but the vendored backend read
a hardcoded `/shared-cookies/session-state.json` — a mount the hub never
creates. Both readers got ENOENT, so the badge said "session status unknown" for
the life of the container and the gate never blocked anything: 96 harvest rounds
in one container lifetime, every one admitted, while the vault's own file said
`up` (`grep -c "Cookie session dead" /var/log/sup/drivepod-owner.log` == 0). The
visible half was the badge; the load-bearing half is that a real `down` verdict
would have been ignored exactly as silently, because a missing file is
indistinguishable from a gate that was never wired. Upstream DrivePod `54620bee`
put the path behind `COOKIE_SESSION_STATE` — the name `gen-conf` and the
vendored exporter already used.

So the assertions are about a contract between files that must agree on one
string and one layout:

- the key `gen-conf` writes is the key the vendored backend and the vendored
  exporter read (a rename on either side fails here, not in production);
- the file sits beside that tenant's `COOKIES_FILE`, because the exporter writes
  a jar's liveness next to that jar and every consumer derives it from there;
- the host's fan-out list and each member's own env name the SAME file, so a
  member is judged by the browser that actually serves it;
- the backend has exactly ONE reader of that file, so the badge cannot disagree
  with the gate;
- a missing / torn / stale file is NOT a verdict (`fresh: false` gates nothing),
  which is also what a vault that stopped running must produce;
- unset, the vendored tree still reads the standalone mount — the env-gating
  invariant every patch here preserves.

The vendored app trees are baked into the image at the flattened path
`/srv/stack/<service>` (`COPY stack/drivepod ./drivepod`) while a host checkout
has them at `stack/<service>`, so both layouts are searched (the trick
`test_vault_fanout` uses): the suite says the same thing in both places.
"""
import importlib
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.dirname(HERE)
REPO = os.path.dirname(PLATFORM)

pytest.importorskip("importlib")
gen = importlib.import_module("gen-conf")

from conftest import clean_env  # noqa: E402,F401  (fixture, used below)
from test_gen_conf import TWO, env_of, program_blocks



@pytest.fixture
def two(tmp_path, monkeypatch):
    """A front partition with its own vault plus a shared member on that vault:
    the shape that has a fan-out, which is where a member reading the host's
    file instead of its own would look perfectly fine until the host dies."""
    path = tmp_path / "partitions.json"
    path.write_text(__import__("json").dumps(TWO, indent=2) + "\n")
    monkeypatch.setattr(gen, "REGISTRY", str(path))
    monkeypatch.setattr(gen, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(gen, "STACK", "/srv/stack")
    return gen.load_registry()


def vendored(*parts: str) -> str:
    for base in (os.path.join(REPO, "stack"),
                 os.path.join(PLATFORM, "stack"),
                 "/srv/stack"):
        cand = os.path.join(base, *parts)
        if os.path.isfile(cand):
            return cand
    raise AssertionError(f"no vendored {'/'.join(parts)} in stack/ or /srv/stack")


def read(*parts: str) -> str:
    with open(vendored(*parts), encoding="utf-8") as fh:
        return fh.read()


def body(src: str, start: str, end: str) -> str:
    """The text of one declaration, from its opener to the first `end` after
    it — enough to assert on what a function does without depending on the
    offsets of the hunk it lives in."""
    i = src.index(start)
    return src[i:src.index(end, i) + len(end)]


ENV_NAME = "COOKIE_SESSION_STATE"


# ------------------------------------------------------------------ the name
def test_the_key_gen_conf_writes_is_the_key_the_app_reads(two):
    """The failure was producer and consumer agreeing on a FILE but not on the
    env var naming it: `gen-conf` had set `COOKIE_SESSION_STATE` for every tenant
    for weeks while the vendored backend ignored it. Both spellings in one test
    is what makes a rename on either side an error here rather than a badge that
    quietly stops meaning anything."""
    blocks = program_blocks(gen.supervisor_conf(two))
    for slug in ("owner", "family"):
        assert ENV_NAME in env_of(blocks[f"drivepod-{slug}"]), \
            f"tenant {slug} got no state-file key"
    assert ENV_NAME in read("drivepod", "backend", "src", "server.ts"), \
        "the vendored backend no longer names the key the hub sets"


def test_the_state_file_sits_beside_that_tenants_jar(two):
    """The exporter publishes a jar's liveness NEXT TO that jar, so the consumer
    has to be told the sibling path rather than a shared mount: two partitions on
    one vault each get their own copy of the host's verdict, and a reader aimed
    at a shared path either misses (ENOENT) or judges one tenant by another's
    browser."""
    blocks = program_blocks(gen.supervisor_conf(two))
    for slug in ("owner", "family"):
        tenant = env_of(blocks[f"drivepod-{slug}"])
        state = tenant[ENV_NAME]
        assert state == f"{gen.DATA}/users/{slug}/session-state.json"
        assert os.path.dirname(state) == os.path.dirname(tenant["COOKIES_FILE"])
        assert state != tenant["COOKIES_FILE"]
    # The key reaches every app that harvests with that jar, not only the one
    # with the gate: the vendored grabbytube shares tenant_vals verbatim.
    assert env_of(blocks["grabbytube-owner"]) == env_of(blocks["drivepod-owner"])


def test_the_writing_exporter_reads_the_same_name(two):
    """`cookie_exporter.py` (vendored ytcookies) is the only writer of this file
    and takes its destination from the same env name. Writer and reader naming
    different variables is the silent half of this bug: the exporter happily
    writes a file nobody looks at while the consumer reports "unknown"."""
    exporter = read("vault", "cookie_exporter.py")
    assert re.search(rf'os\.environ\.get\("{ENV_NAME}"', exporter), \
        "the exporter no longer takes the state path from the tenant env"
    vault = env_of(program_blocks(gen.supervisor_conf(two))["vault-owner"])
    assert vault[ENV_NAME] == f"{gen.DATA}/users/owner/session-state.json"


def test_a_member_is_judged_by_the_browser_that_serves_it(two):
    """A member partition has no browser of its own; the host's exporter mirrors
    the verdict into the member's path. So the member's env must name the
    mirrored file (not the host's) and the fan-out list must name that same one
    — otherwise the member's badge and gate read a file no process ever writes,
    which is the ENOENT shape again."""
    blocks = program_blocks(gen.supervisor_conf(two))
    vault = env_of(blocks["vault-owner"])
    fanout = vault[f"{ENV_NAME}_FANOUT"]
    member = env_of(blocks["drivepod-family"])
    assert fanout == f"{gen.DATA}/users/family/session-state.json"
    assert member[ENV_NAME] == fanout.split(",")[0]
    assert member[ENV_NAME] != vault[ENV_NAME]


# ------------------------------------------------------------------ one reader
def test_the_badge_and_the_gate_share_one_reader():
    """The badge said "unknown" for exactly as long as the gate silently did
    nothing, because each had its own `fs.readFileSync` of a hardcoded path. One
    reader makes disagreement impossible; a second read of a literal mount re-opens
    the split."""
    src = read("drivepod", "backend", "src", "server.ts")
    assert "export function readSessionState(" in src, \
        "no shared session-state reader"
    for start in ("export function sessionGateOpen",
                  "app.get('/api/harvest-status'"):
        chunk = body(src, start, "});") if start.startswith("app.get") \
            else body(src, start, "\n}")
        assert "readSessionState(" in chunk, f"{start} bypasses the reader"
        assert "fs.readFileSync" not in chunk, \
            f"{start} grew its own file read; it must go through readSessionState"


def test_the_reader_is_env_gated_and_standalone_faithful():
    """Unset ⇒ the mount the standalone compose provides, byte-for-byte what this
    tree read before the hub existed. The fallback has to stay literal (never a
    tenant path) or a standalone DrivePod loses its gate too."""
    src = read("drivepod", "backend", "src", "server.ts")
    selector = re.search(
        rf'\(process\.env\.{ENV_NAME}[^)]*\)\.trim\(\)\s*\|\|\s*'
        r"'([^']+)'", src)
    assert selector, f"{ENV_NAME} no longer selects the state file"
    assert selector.group(1) == "/shared-cookies/session-state.json", \
        "the unset fallback moved; a standalone deployment loses its gate"


# --------------------------------------------------------- absence != verdict
def test_absence_is_not_a_verdict():
    """The vault writes the file only while it runs, so "no fresh file" has to
    mean "nothing gated": a vault that stopped must not freeze harvest on its
    last word, and the badge's "session status unknown" is the same statement.
    The age window and the freshness rule live in the vendored tree, where a
    change to either is a behaviour change, not a refactor."""
    src = read("drivepod", "backend", "src", "server.ts")
    assert "return { state: 'unknown', ts: null, fresh: false }" in src, \
        "a missing file is being reported as something other than no verdict"
    gate = body(src, "export function sessionGateOpen", "\n}")
    assert re.search(r"return !session\.fresh \|\| session\.state === 'up'",
                     gate), "the gate no longer opens when the file is stale"
