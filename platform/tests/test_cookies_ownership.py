"""The partition's `cookies.txt` is the VAULT'S file, and the harvest may only
ever be a reader of it.

`gen-conf` maps each tenant's `COOKIES_FILE` to exactly
`<DATA_DIR>/users/<slug>/cookies.txt` — the same path the vendored exporter
writes and the same path DrivePod's harvest has always called
`TEMP_COOKIES_FILE` (`DATA_DIR/cookies.txt`, a name that made sense when the
harvest owned it). In the hub the two facts collide:

- `deleteTempCookiesFile()` unlinks that path at the END of every harvest round.
  On a hub tenant that deletes the vault's export, and the next harvest's yt-dlp
  runs are cookieless until the vault re-exports — which is how a bot check
  first bit. Upstream DrivePod `5ecb5c1` fixed the standalone shape by refusing
  to remove a jar it does not own; the hub needs the SAME refusal keyed on
  `COOKIES_FILE`, because here the file is owned by a different supervisor
  program, not by nobody.
- `writeTempCookiesFile()` overwrote it from the `cookies` CONFIG row. An upload
  from the Settings tab is not the vault's session, so seeding the vault's path
  from it lied to the vault's own liveness probe.

Both helpers therefore key one early return on `COOKIES_EXTERNALLY_OWNED`, and
unset (`COOKIES_FILE` blank, i.e. the standalone container) they stay exactly
what upstream ships.

The second hazard is the reason these tests care about the harvest lock at all.
`deleteTempCookiesFile` runs as the FIRST statement of the harvest's `finally`,
and a throw from a `finally` replaces the rest of that block — including the
`isHarvesting = false` below it — while node-cron swallows the rejection. An
EACCES/EBUSY/EROFS on that mount therefore used to freeze the queue for the life
of the process. So the helper is best-effort BY CONTRACT: nothing may throw out
of it, and the tests below say so in the same place they say the jar must not be
removed, because both statements are about the same line of code.

These are source-text tests on the vendored tree, which is where the guarantee
lives: the guard is a hub patch, and the thing that loses it is the next
re-vendor (upstream rewrote that function body a second time on 2026-11-20 and
this file's early return was the ONE conflict). The vendored app trees are baked
into the image at `/srv/stack/<service>` and live in a host checkout at
`stack/<service>`; both layouts are searched, like `test_vault_fanout`.
"""
import importlib
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.dirname(HERE)
REPO = os.path.dirname(PLATFORM)

gen = importlib.import_module("gen-conf")

from conftest import clean_env  # noqa: E402,F401  (fixture, used below)
from test_gen_conf import TWO, env_of, program_blocks


@pytest.fixture
def two(tmp_path, monkeypatch):
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
    """One declaration's text, from its opener to the first `end` after it, so
    the assertions survive the offsets moving under a re-vendor."""
    i = src.index(start)
    return src[i:src.index(end, i) + len(end)]


SRC = ("drivepod", "backend", "src", "server.ts")
GATE = "COOKIES_EXTERNALLY_OWNED"
ENV_COOKIE = "COOKIES_FILE"


# ------------------------------------------------------------------ the path
def test_the_jar_the_hub_assigns_is_the_jar_the_harvest_must_not_touch(two):
    """The whole guard rests on one identity: the path `gen-conf` names in the
    tenant env IS the path the backend calls `TEMP_COOKIES_FILE`. If the
    generator moved the jar (a new name, a subdir) while the backend kept
    `DATA_DIR/cookies.txt`, the guard would still pass every test and the vault
    would still be deleted — because the two files would no longer be the same
    file."""
    blocks = program_blocks(gen.supervisor_conf(two))
    src = read(*SRC)
    decl = re.search(r"const TEMP_COOKIES_FILE\s*=\s*(.+);", src)
    assert decl, "TEMP_COOKIES_FILE is no longer a single derivable path"
    m = re.search(r"path\.join\((DATA_DIR|CACHE_DIR),\s*'([^']+)'\)",
                  decl.group(1))
    for slug in ("owner", "family"):
        tenant = env_of(blocks[f"drivepod-{slug}"])
        base = os.path.dirname(tenant[ENV_COOKIE])
        assert m.group(1) == "DATA_DIR", \
            "the harvest's jar moved off DATA_DIR; the guard now guards elsewhere"
        assert f"{base}/{m.group(2)}" == tenant[ENV_COOKIE], (
            f"tenant {slug}: the harvest removes "
            f"{{DATA_DIR}}/{m.group(2)} but the vault writes {tenant[ENV_COOKIE]}")


def test_the_gate_is_derived_from_the_same_env_key(two):
    """`COOKIES_EXTERNALLY_OWNED` must come from `COOKIES_FILE` and nothing else
    (not a new hub-only variable a standalone deployment never sets): that is
    what makes the patched tree behave like the standalone one when the hub is
    absent."""
    src = read(*SRC)
    m = re.search(rf"const {GATE} = Boolean\(\(process\.env\.({ENV_COOKIE})"
                  r"[^)]*\)\.trim\(\)\)", src)
    assert m, f"{GATE} is no longer `Boolean((process.env.{ENV_COOKIE}…).trim())`"
    assert m.group(1) == ENV_COOKIE
    # Every hub tenant gets the key, so the guard is live in this container.
    blocks = program_blocks(gen.supervisor_conf(two))
    for slug in ("owner", "family"):
        assert env_of(blocks[f"drivepod-{slug}"])[ENV_COOKIE], \
            f"tenant {slug} has no COOKIES_FILE, so its vault jar is unprotected"


# ------------------------------------------------- the harvest never deletes it
def test_delete_returns_before_it_can_reach_the_filesystem():
    """The early return has to precede any syscall — a guard placed after the
    `existsSync`/`unlinkSync` it is meant to skip protects nothing."""
    src = read(*SRC)
    fn = body(src, f"export async function deleteTempCookiesFile", "\n}")
    i_gate = fn.index(GATE)
    i_sys = min(fn.index(t) for t in ("fs.existsSync", "fs.unlinkSync"))
    assert i_gate < i_sys, "the ownership check moved below the unlink"
    line = fn[i_gate:fn.index("\n", i_gate)]
    assert "return" in line, f"the ownership branch no longer returns: {line!r}"


def test_delete_is_best_effort_and_cannot_strand_the_harvest_lock():
    """Three guarantees on one line of code, because they are one line's job.
    (a) The helper cannot throw out: it runs as the FIRST statement of the
    harvest's `finally`, and a throw from a `finally` replaces everything the
    block still had to do — including the `isHarvesting = false` below it.
    node-cron swallows the rejection, so the queue stays frozen for the life of
    the process. (b) The harvest takes its lock as the first act INSIDE the try,
    so there is no window where the flag is held with no `finally` armed (a
    `sessionGateOpen()` read, a `prisma` call, before the assignment). (c) It is
    exported, so DrivePod's own T2 §20 can drive a harvest to a dead end and ask
    whether the next one may still run — the only way (a) is ever noticed."""
    src = read(*SRC)
    fn = body(src, "export async function deleteTempCookiesFile", "\n}")
    assert "catch" in fn, "a throwing unlink strands the harvest lock again"
    harvest = body(src, "export async function harvestAndPurge", "\n}")
    assert harvest.index("try {") < harvest.index("isHarvesting = true"), \
        "the lock is taken before the try again, so a throw between the two "\
        "leaves it held with no finally armed"
    fin = harvest[harvest.rindex("finally {"):]
    assert "await deleteTempCookiesFile()" in fin, \
        "the harvest no longer cleans up through the best-effort helper"
    assert fin.index("await deleteTempCookiesFile()") < fin.index("isHarvesting = false"), \
        "the cleanup moved below the release, so it can no longer strand the lock"


def test_write_never_seeds_the_vaults_jar():
    """In the owned case the helper reports success without writing: an upload in
    the config table is not the vault's session, and a fresh file there would
    make the vault's liveness probe report a dead session as live."""
    src = read(*SRC)
    fn = body(src, "async function writeTempCookiesFile", "\n}")
    branch = fn[fn.index(GATE):fn.index("return true", fn.index(GATE)) + 11]
    assert "fs.writeFileSync" not in branch, \
        "the owned branch writes the jar again (an upload is not the session)"


# -------------------------------------------------------- standalone faithful
def test_unset_leaves_the_standalone_behavior_intact():
    """Both guards are early returns on a set env var, so with `COOKIES_FILE`
    unset (the standalone container) the helpers do exactly what upstream does:
    write the config jar and remove it afterwards. A patch that changed the
    unowned path too would be invisible in the hub and fatal on a standalone
    install."""
    src = read(*SRC)
    w = body(src, "async function writeTempCookiesFile", "\n}")
    d = body(src, f"export async function deleteTempCookiesFile", "\n}")
    assert "fs.writeFileSync(TEMP_COOKIES_FILE" in w, \
        "the unowned write path disappeared"
    assert "fs.unlinkSync(TEMP_COOKIES_FILE)" in d, \
        "the unowned cleanup path disappeared — a standalone jar now leaks"
