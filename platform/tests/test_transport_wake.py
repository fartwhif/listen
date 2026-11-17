"""The transport daemon's wake gate (platform/vnc-run.py wake_once).

`vnc-<vault>` is the always-on websockify that makes the noVNC door answer, and
the ONLY thing that brings the demand browser up when a human opens that door.
wake_once is the whole decision, and it is a pure function: the human count, the
supervisor call and the last-fire state are all injected, so it tests without X,
a gateway, or a real browser.

What has to stay true, because each failure is silent at the door:

  * no client connected -> nothing woken (a daemon that starts a chromium for an
    empty display is the ambient-warm-tab bug the owner forbade, just relocated);
  * a client connected -> exactly one `start` for the browser, on the first scan
    that sees them (this is the difference between the door showing a blank root
    window forever and showing the sign-in);
  * the debounce floor -> while a client holds the door open and the browser is
    still coming up, the wake is NOT re-issued every poll (supervisor's start is
    idempotent on a RUNNING program, but re-firing every 3s floods the log and a
    crash-looping browser turns into a spin); after the floor elapses a client
    still connected re-fires, so a browser that died under them comes back.

`clients_fn` is injected so the test never has to fake /proc; the real one
(family.door_clients) counts established legs to the door port minus this
family's own sockets, and test_watchdog.py wires it to the live count.
"""
import importlib

vr = importlib.import_module("vnc-run")


class _R:
    """supervisorctl's CompletedProcess, just the fields wake_once logs."""

    def __init__(self, rc=0, out="starting"):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


def _ctl(calls, rc=0):
    def ctl_fn(verb, name):
        calls.append((verb, name))
        return _R(rc)
    return ctl_fn


def test_no_client_at_the_door_wakes_nothing(monkeypatch):
    monkeypatch.setattr(vr, "log", lambda m: None)
    monkeypatch.setattr(vr, "FIRE_DEBOUNCE", 30.0)
    calls: list = []
    state: dict = {}
    assert vr.wake_once("vault-owner", _ctl(calls), state, 1000.0,
                        clients_fn=lambda ws: 0) is False
    assert calls == []
    assert state == {}


def test_a_client_at_the_door_wakes_the_browser_once(monkeypatch):
    monkeypatch.setattr(vr, "log", lambda m: None)
    monkeypatch.setattr(vr, "FIRE_DEBOUNCE", 30.0)
    calls: list = []
    state: dict = {}
    assert vr.wake_once("vault-owner", _ctl(calls), state, 1000.0,
                        clients_fn=lambda ws: 1) is True
    assert calls == [("start", "vault-owner")]
    assert state["last"] == 1000.0


def test_the_wake_is_not_refired_while_a_client_is_coming_up(monkeypatch):
    """A human who opens the door and waits keeps their socket up across many
    polls while chromium boots; re-issuing `start` every 3 s is the log flood +
    crash-spin the debounce floor exists to stop."""
    monkeypatch.setattr(vr, "log", lambda m: None)
    monkeypatch.setattr(vr, "FIRE_DEBOUNCE", 30.0)
    calls: list = []
    state: dict = {"last": 1000.0}          # already woke it at t=1000
    # 10 s later, still connected, still within the floor -> no second start.
    assert vr.wake_once("vault-owner", _ctl(calls), state, 1010.0,
                        clients_fn=lambda ws: 1) is False
    assert calls == []
    assert state["last"] == 1000.0          # floor untouched by a suppressed fire


def test_a_client_still_there_after_the_floor_refires(monkeypatch):
    """The flip side: a browser that died under a human who is still watching has
    to be woken again once the floor passes, or the door shows a blank display to
    someone who is sitting right there."""
    monkeypatch.setattr(vr, "log", lambda m: None)
    monkeypatch.setattr(vr, "FIRE_DEBOUNCE", 30.0)
    calls: list = []
    state: dict = {"last": 1000.0}
    assert vr.wake_once("vault-owner", _ctl(calls), state, 1031.0,
                        clients_fn=lambda ws: 1) is True
    assert calls == [("start", "vault-owner")]
    assert state["last"] == 1031.0


def test_a_lost_wake_retry_does_not_suppress_the_next(monkeypatch):
    """`start` answering non-zero (supervisor busy, browser in FATAL) still counts
    as a fired wake: it stamps the floor so the retry is paced, not hammered - the
    crash-loop pacing the floor is for. The next scan past the floor tries again."""
    monkeypatch.setattr(vr, "log", lambda m: None)
    monkeypatch.setattr(vr, "FIRE_DEBOUNCE", 30.0)
    calls: list = []
    state: dict = {}
    fired = vr.wake_once("vault-owner", _ctl(calls, rc=1), state, 1000.0,
                         clients_fn=lambda ws: 1)
    assert fired is True
    assert state["last"] == 1000.0          # paced even though start failed
    assert vr.wake_once("vault-owner", _ctl(calls), state, 1010.0,
                        clients_fn=lambda ws: 1) is False
