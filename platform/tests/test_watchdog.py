"""Watchdog vault supervision: one browser family per VAULT, judged by the
host partition's files (gen-conf `vault_hosts`, platform/gen-conf.py).

The dedup this pins is the one that removes two chromiums authenticating as the
same Google account. What has to stay true after it:

- decisions are keyed by vault id, so `vault: "owner"` on two partitions issues
  exactly one start, for program `vault-<vault>`;
- liveness is read from the HOST's files, so a member whose browser no longer
  exists is not asked for one (its jar is kept fresh by the host's fan-out);
- an idle stop targets the vault program once, not once per partition naming it;
- the backoff memory is keyed by vault too, so a retired slug does not hand a
  still-shared vault a fresh retry window, and a vault keeps one counter however
  many partitions name it.

`ctl`, `program_state` and `session_live` are the three outside contacts
`scan_vaults` has; everything below drives them with fakes.
"""
import importlib
import json
import os
import time

import pytest

wd = importlib.import_module("watchdog")


class FakeCtl:
    """supervisorctl stand-in: records every verb+program pair it is given."""

    def __init__(self, rc=0):
        self.calls: list[tuple[str, str]] = []
        self.rc = rc

    def __call__(self, verb, name=""):
        self.calls.append((verb, name))
        return type("R", (), {"returncode": self.rc, "stdout": "",
                              "stderr": ""})()

    def verbs(self, name):
        return [v for v, n in self.calls if n == name]


@pytest.fixture
def shared(monkeypatch, tmp_path):
    """Registry: `owner` (front) and `family`, both signing in as vault `owner`,
    plus one partition with a vault of its own, so "one family per vault" stays
    distinguishable from "one family per box". Host files under a tmpdir."""
    data = tmp_path / "data"
    for slug in ("owner", "family", "kidacct"):
        (data / "users" / slug / "cache").mkdir(parents=True)
    spec = lambda i, mode, name, vault: {  # noqa: E731  (test-local shorthand)
        "index": i, "mode": mode, "name": name, "owner": "owner",
        "radio": True, "vault": vault, "vault_slug": vault,
        "settings": {}, "quota": {}}
    reg = {"slugs": ["owner", "family", "kidacct"], "front": "owner",
           "partitions": {"owner": spec(0, "single", "Home", "owner"),
                          "family": spec(1, "shared", "Family", "owner"),
                          "kidacct": spec(2, "single", "Kid", "kidacct")}}
    monkeypatch.setattr(wd, "DATA", str(data))
    return {"data": data, "reg": reg}


def live_session(data, slug, now, state="up", cookie_age=10):
    """A partition home that looks like a host whose exporter is alive: a young
    cookies.txt plus a state file reporting `up` at `now`."""
    home = data / "users" / slug
    (home / "cookies.txt").write_text("# Netscape\n")
    os.utime(home / "cookies.txt", (now - cookie_age,) * 2)
    (home / "session-state.json").write_text(
        json.dumps({"state": state, "ts": int(now)}))


# ------------------------------------------------------------- one per vault
def test_one_start_for_a_vault_shared_by_two_partitions(shared, monkeypatch):
    """The bug the dedup removes: `vault: "owner"` on owner AND family used to
    start two chromiums on one Google account, each re-typing the password and
    each rotating the jar the other's consumers write back to."""
    ctl = FakeCtl()
    monkeypatch.setattr(wd, "ctl", ctl)
    monkeypatch.setattr(wd, "program_state", lambda name: ("STOPPED", 0.0))
    monkeypatch.setattr(wd, "session_live",
                        lambda slug, now: (False, "no cookies.txt yet"))
    monkeypatch.setattr(wd, "log", lambda msg: None)
    attempts: dict = {}
    wd.scan_vaults(shared["reg"], time.time(), attempts)
    started = sorted(n for v, n in ctl.calls if v == "start")
    # vault-owner ONCE (for both owner and family) plus vault-kidacct.
    assert started == ["vault-kidacct", "vault-owner"]
    assert sorted(attempts) == ["kidacct", "owner"]


def test_a_member_with_no_files_of_its_own_is_not_asked_for_a_browser(shared,
                                                                     monkeypatch):
    """Liveness is the HOST's, whose fan-out keeps every member's jar inside the
    age limits. Judging a member's own (never self-written) files would start a
    browser the dedup deliberately removed."""
    now = time.time()
    live_session(shared["data"], "owner", now)
    ctl = FakeCtl()
    monkeypatch.setattr(wd, "ctl", ctl)
    monkeypatch.setattr(wd, "program_state", lambda name: ("STOPPED", 0.0))
    monkeypatch.setattr(wd, "log", lambda msg: None)
    wd.scan_vaults(shared["reg"], now, {})
    assert ctl.calls == [("start", "vault-kidacct")]     # family: nothing issued


def test_a_stale_host_file_starts_the_vault_program_once_for_both_tenants(shared,
                                                                         monkeypatch):
    """The family comes up because the ACCOUNT's session is unusable, and that
    one start serves both partitions. The log has to name the host, or an
    operator reading it looks for a chromium that was never meant to exist."""
    now = time.time()
    live_session(shared["data"], "owner", now, state="down")
    ctl = FakeCtl()
    seen: list[str] = []
    monkeypatch.setattr(wd, "ctl", ctl)
    monkeypatch.setattr(wd, "program_state", lambda name: ("STOPPED", 0.0))
    monkeypatch.setattr(wd, "log", seen.append)
    wd.scan_vaults(shared["reg"], now, {})
    assert ctl.verbs("vault-owner") == ["start"]
    assert any("vault-owner" in m and "host owner" in m for m in seen), seen


def test_an_idle_stop_targets_the_vault_program_not_every_partition(shared,
                                                                   monkeypatch):
    """`owner` and `family` share the running family, so the idle stop has to be
    ONE call: a second stop for the same program is a supervisor flap on a
    process that is already gone."""
    now = time.time()
    live_session(shared["data"], "owner", now)
    ctl = FakeCtl()
    monkeypatch.setattr(wd, "ctl", ctl)
    def state(name):
        return ("RUNNING", wd.IDLE + 60) if name == "vault-owner" \
            else ("STOPPED", 0.0)
    monkeypatch.setattr(wd, "program_state", state)
    monkeypatch.setattr(wd, "log", lambda msg: None)
    attempts: dict = {}
    wd.scan_vaults(shared["reg"], now, attempts)
    assert ctl.verbs("vault-owner") == ["stop"]
    assert ctl.verbs("vault-family") == []
    assert attempts["owner"].running is True


def test_a_shared_vault_counts_ONE_attempt_per_scan(shared, monkeypatch):
    """The failure counter drives exponential backoff, so it must advance once
    per ATTEMPT, not once per partition that happens to name the vault: two
    tenants sharing vault "owner" doubling the counter would send a working
    account into the 30-minute retry ceiling after four starts it never made.
    """
    ctl = FakeCtl()
    state: dict = {}
    monkeypatch.setattr(wd, "ctl", ctl)
    monkeypatch.setattr(wd, "program_state",
                        lambda name: state.get(name, ("STOPPED", 0.0)))
    monkeypatch.setattr(wd, "session_live",
                        lambda slug, now: (False, "state 'down'"))
    monkeypatch.setattr(wd, "log", lambda msg: None)
    attempts: dict = {}
    now = time.time()
    wd.scan_vaults(shared["reg"], now, attempts)
    assert ctl.verbs("vault-owner") == ["start"]        # once, for both tenants
    assert attempts["owner"].failures == 0              # the attempt is running
    state["vault-owner"] = ("RUNNING", 40)
    wd.scan_vaults(shared["reg"], now + 60, attempts)   # still up, nothing to do
    assert attempts["owner"].running is True
    assert ctl.verbs("vault-owner") == ["start"]
    state["vault-owner"] = ("STOPPED", 0.0)
    wd.scan_vaults(shared["reg"], now + 70, attempts)   # the attempt ENDED
    assert attempts["owner"].failures == 1
    assert attempts["owner"].backoff() == min(wd.RETRY * 2, wd.RETRY_MAX)
    assert ctl.verbs("vault-owner") == ["start"], "the retry is held back"


def test_a_retired_vault_clears_only_its_own_memory(shared):
    """main() drops attempts for vaults no partition names any more. What has to
    hold is that the SURVIVING vaults keep theirs: a `hub partition rm kidacct`
    must not reset the counter of the account the rest of the box shares."""
    reg = json.loads(json.dumps(shared["reg"]))
    reg["slugs"] = ["owner", "family"]
    reg["partitions"].pop("kidacct")
    attempts = {"owner": wd.Attempt(), "kidacct": wd.Attempt()}
    attempts["kidacct"].failures = 3
    attempts["owner"].failures = 2
    gone = [v for v in attempts if v not in wd.gen_conf.vault_hosts(reg)]
    assert gone == ["kidacct"]
    assert attempts["owner"].failures == 2


# ------------------------------------------------------------------ liveness
def test_session_live_separates_the_reasons_a_vault_is_unusable(shared):
    """The watchdog starts a browser for any non-`up` reading and stops it when
    `up`, so each reason has to be its own distinguishable answer: no jar, a
    state that is not `up`, a state file that stopped being refreshed (a crashed
    exporter leaves a young jar beside a stale report), and a jar too old for
    the state file's claim to mean anything."""
    data = shared["data"]
    now = time.time()
    live, why = wd.session_live("family", now)
    assert (live, "no cookies.txt" in why) == (False, True)
    live_session(data, "owner", now, cookie_age=10)
    live, why = wd.session_live("owner", now)
    assert live is True and "live" in why
    live_session(data, "owner", now, state="dead")
    live, why = wd.session_live("owner", now)
    assert live is False and "'dead'" in why
    live_session(data, "owner", now - (wd.MAX_AGE + 60))
    live, why = wd.session_live("owner", now)
    assert live is False and "unverified" in why
    live_session(data, "owner", now, cookie_age=wd.MAX_AGE + 120)
    live, why = wd.session_live("owner", now)
    assert live is False and f"cookies {wd.MAX_AGE + 120}s" in why
