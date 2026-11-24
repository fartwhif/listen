#!/usr/bin/env python3
"""One partition's cookie vault BROWSER, started on demand and torn down by itself.

This is the browser half of a vault family. The other two halves are always-on
programs of their own: the transport (Xvfb, x11vnc, websockify - what the noVNC
door actually connects to) in platform/vnc-run.py, and the session WATCHER
(stack/vault/cookie_exporter.py, supervised as `exporter-<vault>`) that owns the
account's session-state.json. This process owns only chromium, the VNC keyboard
driver and the auto sign-in daemon, and it starts them ONLY when something asked
for a browser. It does not run the exporter: the watcher that reports the session
has to outlive the browser it reports about, so it is not this process's child.

Supervisor starts `vault-<slug>` on two triggers (autostart/autorestart are false
in the generated config, so it runs zero chromium between them): the watchdog
starts it when the watcher's verdict says the session is not live (the sign-in
job), and the always-on transport daemon starts it the moment a human opens the
noVNC door (a healthy session, so the watchdog has nothing to fix, was exactly
the case that used to answer "connection is closed"). From either trigger this
process brings the browser up on the display the daemon owns and exits as soon
as its work is done. That is what enforces the owner's invariant - a healthy
partition with nobody watching runs zero chromium - by construction.

Start order is upstream ytcookies' own entrypoint.sh, minus the transport (which
the daemon owns) and minus the exporter (which is a program of its own), with the
literal numbers gen-conf hands us replacing the container names a split
deployment needed: chromium -> the VNC keyboard driver -> the auto sign-in daemon.
It clears chromium's own leftover profile locks, waits for the daemon's door to
be listening, then starts in that order. Teardown is the reverse, and it stops
ONLY the processes it started - it never touches the daemon's X.

Why a wrapper exists at all: the watcher can report a dead session but cannot
start or stop a browser, and supervisor cannot act on the contents of a file.
This is the piece that turns "cookies are stale" into "chromium is running" and
"the session is live and nobody is at the door" into "chromium is not" - inside
one process that reaps its own descendants (the shared machinery is platform/
family.py: a library fork nobody waited on became a zombie under a PID-1 node
that never reaps, which is why each family reaps its own).

Exit conditions, each meaning "nothing left to do here":
  - the watcher's report says `up`, THAT REPORT IS CURRENT (see VERDICT_FRESH),
    the browser has been up HUB_VAULT_GRACE seconds, and nobody is connected to
    the noVNC door - tearing down mid sign-in would throw away the attempt that
    just worked;
  - a process it started died (logged, exit 1; the watchdog spaces the retry);
  - HUB_VAULT_MAX_RUN elapsed with the session still not verifiably `up`: a human
    is needed (2FA, CAPTCHA) and nobody came, so stop re-running a doomed browser
    and let the watchdog back off;
  - the transport never became reachable (the daemon is down): exit non-zero so
    supervisor's next wake (watchdog or door) can retry once it is back.
"""
import json
import os
import signal
import sys
import time

import family

VAULT = os.environ.get("STACK_DIR", "/srv/stack") + "/vault"

GRACE = int(os.environ.get("HUB_VAULT_GRACE", "120"))
MAX_RUN = int(os.environ.get("HUB_VAULT_MAX_RUN", "3600"))
# How long to wait for the transport daemon's door to come up before giving up:
# generous enough to cover a daemon restart, short enough that a wedged daemon
# does not hang this program (autorestart is false; the next wake retries).
X_WAIT = int(os.environ.get("HUB_VAULT_X_WAIT", "30"))

# How young the watcher's `up` has to be for this process to accept it as proof.
# The report is a FILE, and its age is the only liveness signal anybody has: the
# watcher publishes `up` on a probe, so a verdict younger than the watcher's own
# probe interval is an answer about NOW. Older than that, it is an answer about
# some moment in the past - and the watcher may have died at that moment, which
# is the exact state a browser teardown must not trust. gen-conf hands this same
# number to the watcher's programs and to the watchdog (HUB_VAULT_VERDICT_FRESH),
# so "is this verdict an answer?" is one knob with one answer, not three.
VERDICT_FRESH = int(os.environ.get("HUB_VAULT_VERDICT_FRESH", "300"))


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def log(msg: str) -> None:
    print(f"[vault {env('HUB_SLUG', '?')}] {msg}", flush=True)


# ------------------------------------------------------------------- session
def state_of(path: str) -> tuple[str, float]:
    """(state, seconds since the exporter last wrote it)."""
    try:
        with open(path) as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return "missing", 1e9
    try:
        age = time.time() - float(payload.get("ts", 0))
    except (TypeError, ValueError):
        age = 1e9
    return str(payload.get("state", "")), age


# ------------------------------------------------------------------- browser
def bring_up(fam: family.Family, disp: str, res: str, ws_port: int) -> bool:
    """Chromium and its helpers, on the display the transport daemon owns.

    Returns False if the door never came up (daemon down), True once the family
    is running. The transport is NOT started here: this browser must not be the
    thing holding the door's port, because it has to exit (down to zero chromium)
    the instant nobody needs it. So it waits for the daemon's websockify instead -
    that port is the daemon's last child, so a reachable ws means Xvfb and x11vnc
    are already behind it and chromium can open the display.
    """
    cdp_port = env("CDP_PORT", "9222")
    profile = env("VAULT_PROFILE_DIR") or f"{env('VAULT_DIR', '/data')}/profile"
    width, height = (res.split("x") + ["1920", "1080", "24"])[:2]

    os.makedirs(profile, exist_ok=True)
    os.makedirs(os.path.dirname(env("COOKIES_FILE", "/data/cookies.txt"))
                or "/data", exist_ok=True)
    # Chromium owns the profile, so chromium's stale locks are the browser's to
    # clear (the daemon clears X's artifacts).
    family.clear_profile(profile, log)

    if not family.wait_port(ws_port, X_WAIT, log):
        return False

    fam.spawn("chromium", [
        "chromium", "--no-sandbox", f"--user-data-dir={profile}",
        f"--remote-debugging-port={cdp_port}",
        "--remote-debugging-address=127.0.0.1", "--remote-allow-origins=*",
        f"--window-size={width},{height}", "--no-first-run",
        "--no-default-browser-check", "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble", "--disable-features=Translate",
        "--lang=en-US", "about:blank"], env={**os.environ, "DISPLAY": disp})
    time.sleep(2.0)

    # The driver answers x11vnc's DES challenge and drives the omnibox, so it
    # has to exist before the daemon that types into it.
    fam.spawn("vncdriver", ["python3", f"{VAULT}/vncdriver/vncdriver.py"])
    if env("GOOGLE_PASSWORD"):
        fam.spawn("auto_signin", ["python3", f"{VAULT}/auto_signin.py"])
    return True


# --------------------------------------------------------------------- main
def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 else env("HUB_SLUG")
    if not slug:
        print("vault-run: usage: vault-run.py <slug>", file=sys.stderr)
        return 2
    os.environ["HUB_SLUG"] = slug

    family.install_subreaper(log)
    disp = f":{int(env('VNC_DISPLAY', '10'))}"
    res = env("VNC_RESOLUTION", "1920x1080x24")
    ws_port = int(env("WEBSOCKIFY_PORT", "8181"))
    state_path = env("COOKIE_SESSION_STATE", "/data/session-state.json")

    fam = family.Family(log)
    stopping = False

    def on_signal(signum, _frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            log(f"{signal.Signals(signum).name} received; tearing down")

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    log(f"display {disp} {res} ws {ws_port} "
        f"cdp {env('CDP_PORT', '9222')} profile {env('VAULT_PROFILE_DIR')} "
        f"cookies {env('COOKIES_FILE')}")

    started = time.time()
    told = False
    stale_warned = False
    rc = 0
    # The daemon owns the transport; if its door is not there, this attempt is
    # pointless - exit non-zero (autorestart is false) and the next wake retries.
    if not bring_up(fam, disp, res, ws_port):
        fam.teardown()
        return 1
    try:
        while not stopping:
            finished = fam.reap()
            for line in finished:
                log(line)
            if finished:
                # The browser stack is not partially repairable: end the
                # attempt and let the watchdog decide whether to try again.
                rc = 1
                break
            state, stated = state_of(state_path)
            # `up` in that file is NOT proof by itself now that the watcher is a
            # separate program: the file's AGE is the only signal that a live
            # process reached the account recently. A stale `up` is exactly what a
            # dead watcher leaves behind, and tearing down on it would end the
            # sign-in on the say-so of a process that stopped answering.
            fresh = stated <= VERDICT_FRESH
            up_for = time.time() - started
            if state == "up" and fresh and up_for >= GRACE:
                clients = family.door_clients(ws_port)
                if clients:
                    if not told:
                        log(f"session live, {clients} noVNC client(s) up; "
                            "staying until they leave")
                        told = True
                else:
                    log(f"session up after {int(up_for)}s; tearing down")
                    break
            if state == "up" and not fresh and not stale_warned:
                log(f"report says 'up' but {int(stated)}s old (verdict-fresh is "
                    f"{VERDICT_FRESH}s): the watcher is not publishing; staying")
                stale_warned = True
            if up_for > MAX_RUN:
                log(f"session still '{state}' ({int(stated)}s since the watcher "
                    f"wrote it) after {int(MAX_RUN)}s: a human has to finish the "
                    "sign-in through the noVNC door")
                rc = 1
                break
            time.sleep(5)
    except Exception as exc:                  # never leave a browser behind
        log(f"vault-run crashed: {exc!r}")
        rc = 1
    finally:
        fam.teardown()

    if stopping:
        return 0
    state, stated = state_of(state_path)
    if state == "up" and stated <= VERDICT_FRESH:
        return 0
    deaths = ", ".join(f"{n}={c}" for n, c in fam.dead())
    log(f"leaving with session '{state}'"
          + (f"; dead: {deaths}" if deaths else ""))
    return rc


if __name__ == "__main__":
    sys.exit(main())
