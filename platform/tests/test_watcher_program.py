"""The session WATCHER as its own always-on program (`exporter-<vault>`).

Why this split exists at all: the watcher used to be a child of the demand
browser, so a parked vault had no reporter. Its `up` verdict froze at the moment
the browser exited, every consumer's freshness test (a file AGE) went stale, and
a session that died with nobody looking announced itself to no one - no ntfy
post, no auto re-auth, no harvest pause - until the next time a browser happened
to start. As a program of its own it is the only writer of the account's
session-state.json and it sits beside a parked vault indefinitely.

What has to stay true, and what a mistake costs:

  * its env is EXACTLY what cookie_exporter.py reads (gen-conf EXPORTER_READS).
    A knob the exporter starts reading without being whitelisted silently keeps
    its compiled-in default; a whitelisted name the exporter renamed silently
    does nothing. Both are the failure class that made the harvest session gate
    read a path nothing wrote;
  * it gets the vault's JAR, verdict file, fan-out targets, consumer push lists
    and door URL, with the SAME values the browser family gets - the watcher
    speaks FOR the account, and two renderings of one build is what keeps them
    from disagreeing about which cookies they mean;
  * it gets NONE of the browser's world (no DISPLAY, profile dir, X display, VNC
    or websockify port). Those are what make a process able to open a chromium;
    a watcher that grew one would stop being the process that can run beside a
    parked vault for a week, and the always-on autostart would then mean
    "a browser is always running", which is the owner's forbidden warm tab;
  * the name supervisor knows it by is the name the watchdog probes
    (`exporter-<vault>`): a rename on one side alone leaves the reporter
    unsupervised, and a dead reporter is invisible from the files it owns;
  * vault-run owns only the browser half - it must not spawn the exporter, or
    the watcher dies with the browser again - and it may not tear the browser
    down on a verdict the watcher has not refreshed (VERDICT_FRESH).

The nginx/gateway half of the same generation lives in test_gen_conf.py; this
file is the vault-family supervision contract.
"""
import importlib
import json
import os
import re
import signal
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

gen = importlib.import_module("gen-conf")
vault_run = importlib.import_module("vault-run")
wd = importlib.import_module("watchdog")

from conftest import clean_env  # noqa: E402,F401  (fixture, used below)
from test_gen_conf import TWO, _with_index, env_of, program_blocks
# The vendored tree sits at a different depth in each layout: on the host it is
# stack/ beside the repo root, in the image the tests live in /srv/stack/platform
# with the apps one level up (/srv/stack/vault). test_session_gate.vendored()
# knows both, and the whitelist test must parse the file the programs actually
# run - a host-only path made that test fail in the image, where it matters most.
from test_session_gate import vendored


@pytest.fixture
def two(clean_env, tmp_path, monkeypatch):
    """A two-partition registry (owner + one `shared` member, both on vault
    `owner`) with the paths, the site domain and the `.env` fallback pinned to
    the tmpdir. gen-conf freezes its paths at import and reads the environment
    at render time, so both have to be pinned or a developer's own `.env`
    decides what these tests assert. The vault keys are not platform names
    (conftest's OWNED list), so they are blanked here; the credentials are then
    set because these tests ask whether they REACH the watcher."""
    path = tmp_path / "partitions.json"
    path.write_text(json.dumps(TWO, indent=2) + "\n")
    monkeypatch.setattr(gen, "REGISTRY", str(path))
    monkeypatch.setattr(gen, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(gen, "STACK", "/srv/stack")
    monkeypatch.setattr(gen, "SITE_DOMAIN", "portal.example.com")
    for name in gen.VAULT_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NTFY_TOPIC", "vault-topic")
    monkeypatch.setenv("GOOGLE_EMAIL", "someone@example.com")
    monkeypatch.setenv("GOOGLE_PASSWORD", "hunter2")
    monkeypatch.setenv("VNC_PASSWORD", "vncpw")
    return gen.load_registry()


def exporter_reads():
    """Every environment name stack/vault/cookie_exporter.py asks about.

    Parsed from the source rather than hardcoded: the point of the test is that
    the whitelist tracks the code, and the only trustworthy copy of "what the
    exporter reads" is the exporter.
    """
    src = open(vendored("vault", "cookie_exporter.py"), encoding="utf-8").read()
    literal = re.findall(
        r"""os\.environ(?:\.get\(|\[)\s*["']([A-Z][A-Z0-9_]*)["']""", src)
    # A read through a variable (`os.environ.get(name)`) would be invisible to
    # the pattern above, so the counts have to agree: a new indirection form
    # fails here instead of quietly dropping a name from the whitelist.
    assert src.count("os.environ") == len(literal), (
        "cookie_exporter.py grew an os.environ read this parser cannot name")
    return set(literal)


# ---------------------------------------------------------------- the whitelist
def test_the_watcher_env_whitelist_is_what_the_exporter_reads():
    """EXPORTER_READS == the exporter's own reads, in BOTH directions.

    Undeclared-but-read: supervisor never sets it, the exporter compiles in a
    default, and the operator's knob does nothing. Declared-but-not-read: the
    generated config carries a key nobody asks about, which is how a rename
    survives as a silent no-op.
    """
    reads = exporter_reads()
    declared = set(gen.EXPORTER_READS)
    missing = sorted(reads - declared)
    assert missing == [], f"exporter reads, not whitelisted: {missing}"
    extra = sorted(declared - reads)
    assert extra == [], f"whitelisted, never read: {extra}"
    # MUST_ISSUE is a claim about this list, so it has to be inside it: a name
    # gen-conf is forced to emit that the exporter does not read is a key nobody
    # asks about, which is the failure this test exists to catch.
    unneeded = sorted(set(gen.MUST_ISSUE) - reads)
    assert unneeded == [], f"gen-conf must emit, but nobody reads: {unneeded}"


def test_the_exporter_program_env_is_exactly_the_whitelist(two):
    """The generated env cannot smuggle in a name the watcher does not read, and
    may not hold back the wiring it cannot work without.

    A cadence knob gen-conf does NOT emit is not a bug: passthrough omits names
    the container's `.env` does not set, and the exporter's own compiled default
    is then exactly what the standalone container ran with. The wiring is the
    opposite - there is no default for "which jar", and a watcher that fell back
    to one would watch a file that does not exist.
    """
    issued = set(env_of(program_blocks(gen.supervisor_conf(two))["exporter-owner"]))
    smuggled = sorted(issued - set(gen.EXPORTER_READS))
    assert smuggled == [], f"the watcher was handed: {smuggled}"
    missing = sorted(gen.MUST_ISSUE - issued)
    assert missing == [], f"no default for these, and gen-conf omitted: {missing}"
    # Nothing optional is actually missing on THIS registry: family is a member.
    assert "COOKIE_FANOUT_FILES" in issued
    assert "COOKIE_SESSION_STATE_FANOUT" in issued


# ------------------------------------------------------ the program itself
def test_the_reporter_is_always_on_named_for_the_vault(two):
    """autostart/autorestart true is the whole change: the watcher exists whether
    or not a browser does, and supervisor brings it back when it crashes. Named
    after the VAULT (the Google account), once per account, because two reporters
    on one account rotate each other's jar - the same reason there is one browser.
    """
    blocks = program_blocks(gen.supervisor_conf(two))
    assert sorted(n for n in blocks if n.startswith("exporter-")) == \
        ["exporter-owner"]
    for vault, host in gen.vault_hosts(two).items():
        b = blocks[f"exporter-{vault}"]
        assert "autostart=true" in b and "autorestart=true" in b
        assert "command=python3 /srv/stack/vault/cookie_exporter.py" in b
        assert f"stdout_logfile=/var/log/sup/exporter-{vault}.log" in b
        # The reporter runs FIRST: a verdict has to exist before a browser can be
        # woken into a vault that has no opinion yet, and its CDP reads have to
        # start against a browser that is already up rather than racing it.
        prio = int(re.search(r"priority=(\d+)", b).group(1))
        for other in (f"vnc-{vault}", f"vault-{vault}"):
            assert prio < int(re.search(r"priority=(\d+)", blocks[other]).group(1))
        # The host partition is whose paths it watches (its jar, its profile).
        assert env_of(b)["COOKIES_FILE"] == f"{gen.DATA}/users/{host}/cookies.txt"


def test_a_dead_reporter_is_named_by_the_watchdog_that_revives_it(two):
    """The two sides have to agree on the NAME. The watchdog probes
    `exporter-<vault>` and a program by any other name answers nothing to
    `supervisorctl status`, so the reporter would sit FATAL forever - and the
    symptom (files that stopped ageing) points at the vault, not at supervision.
    """
    blocks = program_blocks(gen.supervisor_conf(two))
    for vault in gen.vault_hosts(two):
        assert f"exporter-{vault}" in blocks
        # The watchdog's own construction, not a string I typed here.
        assert wd_probe_name(vault) == f"exporter-{vault}"
    # Only the host's reporter exists: a shared vault has ONE watcher (a second
    # ACCOUNT gets a second one - test_a_second_account_gets_its_own_reporter).
    assert "exporter-family" not in blocks


def wd_probe_name(vault):
    """The program name `scan_vaults` asks supervisor about for a vault's
    reporter - taken from the watchdog's own source line."""
    src = open(os.path.join(REPO, "platform", "watchdog.py"),
               encoding="utf-8").read()
    hits = re.findall(r'watcher = f"(exporter-\{vault\})"', src)
    assert hits == ['exporter-{vault}'], "watchdog no longer derives the name"
    return "exporter-" + vault


# ------------------------------------------------ browser-world exclusion
BROWSER_WORLD = ("DISPLAY", "VAULT_PROFILE_DIR", "VAULT_DIR", "VAULT_SLUG",
                 "VNC_DISPLAY", "VNC_PORT", "VNC_DRIVER_PORT", "VNC_DRIVER_URL",
                 "VNC_PATH_PREFIX", "VNC_WS_URL", "WEBSOCKIFY_PORT", "HOME",
                 "PORT", "HOST", "DATA_DIR", "CACHE_DIR", "DATABASE_URL")


def test_the_reporter_gets_no_browser_world_but_every_vault_path(two):
    """The two halves of the invariant, from one rendering of one build.

    No browser knobs: those are what a process needs to open a chromium, and an
    autostart=true program that can open chromium means a chromium always runs.
    Every vault path identical to the browser family's: the watcher is the only
    writer of the account's jar and verdict, and a watcher pointing at a
    different file than the browser writes reports somebody else's session.
    """
    blocks = program_blocks(gen.supervisor_conf(two))
    exp = env_of(blocks["exporter-owner"])
    browser = env_of(blocks["vault-owner"])
    for key in BROWSER_WORLD:
        assert key not in exp, f"the watcher was handed {key}"
    assert "chromium" not in blocks["exporter-owner"]
    for key in ("COOKIES_FILE", "COOKIE_SESSION_STATE", "COOKIE_FANOUT_FILES",
                "COOKIE_SESSION_STATE_FANOUT", "DRIVEPOD_PUSH_URLS",
                "GRABBY_PUSH_URLS", "GRABBY_API_URL", "HUB_SLUG", "CDP_HOST",
                "CDP_PORT", "VNC_URL", "GOOGLE_EMAIL", "GOOGLE_PASSWORD"):
        assert exp[key] == browser[key], key
    # The push lists must be the ACTUAL loopback targets of both partitions:
    # absent, the exporter falls back to the standalone container names
    # (`http://drivepod:3000`), which do not resolve in this image, and the user
    # agent simply stops reaching the consumers.
    for key, port in (("DRIVEPOD_PUSH_URLS", "drivepod"),
                      ("GRABBY_PUSH_URLS", "grabby")):
        urls = exp[key].split(",")
        assert len(urls) == 2, key
        for slug in ("owner", "family"):
            p = gen.ports(two["partitions"][slug]["index"])[port]
            assert f"http://127.0.0.1:{p}" in urls, f"{key} missing {slug}"


def test_the_reporter_gets_the_channels_and_cadences_it_acts_on(two, monkeypatch):
    """A ntfy topic set on the container must reach the WATCHER, not only the
    browser: the watcher is the process that detects a death, and while it was a
    browser child a parked vault posted nothing at all. Same for the cadences -
    they decide when it re-proves, how long a verdict stays an answer, and how
    often it re-publishes."""
    exp = env_of(program_blocks(gen.supervisor_conf(two))["exporter-owner"])
    assert exp["NTFY_TOPIC"] == "vault-topic"
    for name in ("COOKIE_SESSION_PROBE", "COOKIE_STATE_HEARTBEAT",
                 "COOKIE_PROBE_ON_CHANGE", "COOKIE_SESSION_STALE",
                 "COOKIE_UA_SYNC", "COOKIE_POLL_INTERVAL"):
        assert name in set(gen.EXPORTER_READS), name
    # Unset `.env` names are OMITTED, not emitted empty: an empty NTFY_TOPIC is a
    # value the exporter reads as "no channel", while an absent one is the same
    # default the standalone container had.
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    exp = env_of(program_blocks(gen.supervisor_conf(two))["exporter-owner"])
    assert "NTFY_TOPIC" not in exp and "NOTIFY_WEBHOOK_URL" not in exp


# ------------------------------------------------------------- vault-run half
class FakeFamily:
    """family.Family stand-in: records spawns, reaps nothing, tears down."""

    def __init__(self, log):
        self.log = log
        self.spawned: list[str] = []
        self.last_argv: dict[str, list[str]] = {}
        self.torn = False

    def spawn(self, name, argv, **kw):
        self.spawned.append(name)
        self.last_argv = {name: argv}

    def reap(self):
        return []

    def dead(self):
        return []

    def teardown(self, grace=10.0):
        self.torn = True


@pytest.fixture
def browserless(monkeypatch, tmp_path):
    """vault-run's outside world: no subreaper, no profile sweep, a door that is
    already listening, and a fake family - so main() is the loop and nothing else.
    Returns a way to run main() against a verdict file written at will."""
    monkeypatch.setattr(vault_run.family, "install_subreaper", lambda log: None)
    monkeypatch.setattr(vault_run.family, "clear_profile", lambda p, log: None)
    monkeypatch.setattr(vault_run.family, "clear_x", lambda d, log: None)
    monkeypatch.setattr(vault_run.family, "wait_port", lambda p, t, log: True)
    fams: list[FakeFamily] = []

    def family_factory(log):
        fam = FakeFamily(log)
        fams.append(fam)
        return fam
    monkeypatch.setattr(vault_run.family, "Family", family_factory)
    monkeypatch.setattr(vault_run.family, "door_clients", lambda ws: 0)
    # time.sleep is a wall-clock tax here, not behaviour under test.
    monkeypatch.setattr(vault_run.time, "sleep", lambda s: None)
    state_path = tmp_path / "session-state.json"
    monkeypatch.setenv("COOKIE_SESSION_STATE", str(state_path))
    monkeypatch.setenv("VAULT_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("COOKIES_FILE", str(tmp_path / "cookies.txt"))
    monkeypatch.setenv("HUB_SLUG", "owner")
    monkeypatch.setenv("GOOGLE_PASSWORD", "hunter2")
    monkeypatch.setattr(vault_run, "GRACE", 0)
    monkeypatch.setattr(vault_run, "MAX_RUN", -1)      # one loop pass, then stop
    seen: list[str] = []
    monkeypatch.setattr(vault_run, "log", seen.append)
    olds = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}

    def run(state: str, ts: float):
        state_path.write_text(json.dumps({"state": state, "ts": int(ts)}))
        argv = sys.argv
        sys.argv = ["vault-run.py", "owner"]
        try:
            rc = vault_run.main()
        finally:
            sys.argv = argv
            for s, h in olds.items():
                signal.signal(s, h)
        return rc

    return {"run": run, "log": seen, "fams": fams}


def last_fam(browserless) -> FakeFamily:
    """The family instance the most recent run built (the fixture records all)."""
    assert browserless["fams"], browserless["log"]
    return browserless["fams"][-1]


def test_the_browser_does_not_restart_the_watcher_it_reports_on(browserless):
    """vault-run spawns exactly the browser half. Spawning the exporter here
    would put the reporter back under the process it reports about: it would die
    with every teardown, and the parked-vault silence this change exists to fix
    would return with nothing in the logs to explain it."""
    rc = browserless["run"]("up", time.time())
    fam = last_fam(browserless)
    assert rc == 0
    # bring_up's order, from upstream ytcookies' entrypoint, minus transport and
    # minus exporter; auto_signin only because GOOGLE_PASSWORD is set.
    assert fam.spawned == ["chromium", "vncdriver", "auto_signin"], fam.spawned
    assert all("cookie_exporter" not in a for argv in fam.last_argv.values()
               for a in argv)


def test_the_browser_needs_a_FRESH_verdict_to_tear_itself_down(browserless):
    """The freshness rule itself, on both sides of the bound.

    A verdict file that says `up` but stopped being written is precisely what a
    DEAD watcher leaves behind, and the browser ending its attempt on that say-so
    is how a parked vault ends up with no reporter and no proof. So `up` must be
    younger than VERDICT_FRESH to count - and a fresh `up` still tears down (the
    owner's zero-chromium invariant is not weakened into "keep the browser").
    """
    fresh = browserless["run"]("up", time.time())
    fam = last_fam(browserless)
    assert fam.spawned == ["chromium", "vncdriver", "auto_signin"]
    assert fresh == 0, browserless["log"]
    assert any("tearing down" in m for m in browserless["log"]), browserless["log"]
    # What separates the two runs is the EXIT CODE and the log, not the teardown:
    # `fam.teardown()` sits in the `finally`, so the family is reaped on every
    # exit path - an attempt that ends must not leave a chromium behind either
    # way. `rc == 1` is what tells the watchdog this attempt did not finish.

    browserless["log"].clear()
    browserless["fams"].clear()
    stale = browserless["run"]("up", time.time() - (vault_run.VERDICT_FRESH + 60))
    assert stale == 1, "a stale verdict must not be reported as a finished attempt"
    assert any("the watcher is not publishing" in m for m in browserless["log"]), \
        browserless["log"]
    assert not any("tearing down" in m for m in browserless["log"]), browserless["log"]


