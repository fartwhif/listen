#!/usr/bin/env python3
"""One vault's noVNC transport, kept up so the door is always reachable.

This is the half of the old vault-run family that makes the noVNC door answer.
The bug it fixes: when a session came up healthy, vault-run tore down the WHOLE
family - including websockify's listener - to honour "a healthy partition runs
zero chromium". The next human who clicked the dock's "Cookie vault" got the lite
page (a static file, served fine) but its WebSocket hit the gateway's
proxy_pass to 127.0.0.1:<ws> with NO listener behind it, so noVNC reported
"Something went wrong, connection is closed", and nothing woke the door: the
auth gate only says yes/no, and the watchdog starts a browser for a STALE session
file, not for a click. A dead-session sign-in therefore looked fine (the
watchdog started the family) but a healthy-session visit never worked.

The split (the owner's decision): transport is CHEAP and always on here; the
browser is EXPENSIVE and demand-supervised in vault-run. This process owns Xvfb
-> x11vnc -> websockify and holds the door's port permanently, so a dock click
always completes the WebSocket handshake. When a human actually connects, this
process asks supervisor to start the browser family (vault-<vault>), which brings
chromium up on the display behind the already-open door - so the first thing the
human sees is the X root, then chromium's window as it appears, then the sign-in.
That is exactly the accepted "blank for a few seconds until the browser shows"
behaviour, and it replaces "connection is closed" with a working door.

Why the daemon wakes rather than the browser owning the port:
  - the browser must run zero chromium when nobody is here (the owner's
    invariant), so it cannot be the thing holding a port 24/7;
  - a wake trigger has to answer the handshake immediately, which a demand
    browser cannot (spinning up Xvfb+x11vnc takes several seconds and the very
    first proxied connect would still fail);
  - so an always-listening websockify IS the wake trigger: it shows the human a
    (blank) screen, and a connected client is the signal that a browser is wanted.

It stays alive on its own terms (autostart/autorestart in the generated config):
if one of its own children dies, this process exits non-zero and supervisor
brings the transport back; it never starts or stops chromium itself beyond the
single "start" wake (which supervisor makes idempotent), and it never tears
chromium down - that is vault-run's job, on its own door-aware exit.
"""
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import family


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def log(msg: str) -> None:
    print(f"[vnc {env('HUB_SLUG', '?')}] {msg}", flush=True)


# supervisorctl, so the wake goes through the same supervisor that supervises
# the browser family (see watchdog.ctl / hub.ctl: one mechanism, the socket is
# supervisord's own).
def ctl(*args: str) -> subprocess.CompletedProcess:
    sock = env("HUB_SUP_SOCK", "/var/run/supervisor.sock")
    return subprocess.run(["supervisorctl", "-s", f"unix://{sock}", *args],
                          capture_output=True, text=True, check=False)


# The door's blank-but-listening state is what the human lands on, so polling
# faster means less time staring at the root window before chromium appears.
POLL = float(env("HUB_VNC_POLL", "3"))
# Don't re-issue the wake every poll while a client is connected and the browser
# is still coming up: `start` is idempotent on a RUNNING program, but a 30s floor
# keeps the log honest and a crash-looping browser to a slow retry, not a spin.
FIRE_DEBOUNCE = float(env("HUB_VNC_FIRE", "30"))


def start_transport(fam: family.Family) -> None:
    """Xvfb -> x11vnc -> websockify, the transport vault-run used to own.

    websockify is LAST on purpose: wait_port() in vault-run treats a listening
    ws port as "the display is ready", which is only true once everything under
    it is up. Loopback bind only - the gateway proxies the door from inside this
    container, so nothing outside needs these ports (and unpublished ports are
    unreachable from the WireGuard peer regardless of bind)."""
    disp = f":{int(env('VNC_DISPLAY', '10'))}"
    res = env("VNC_RESOLUTION", "1920x1080x24")
    vnc_port = env("VNC_PORT", "5901")
    ws_port = env("WEBSOCKIFY_PORT", "8181")
    novnc_web = "/usr/share/novnc"
    if not os.path.isdir(novnc_web):
        novnc_web = "/usr/share/webapps/noVNC"

    family.clear_x(disp, log)
    fam.spawn("xvfb", ["Xvfb", disp, "-screen", "0", res, "-nolisten", "tcp"])
    time.sleep(1.0)

    args = ["x11vnc", "-display", disp, "-forever", "-shared",
            "-rfbport", vnc_port]
    if env("VNC_PASSWORD"):
        # -storepasswd writes the DES-hashed file and exits; x11vnc then reads
        # it at startup, so it has to finish first. One file per vault keeps two
        # vault families in one container out of each other's password.
        rfbauth = f"/tmp/vncpasswd-{env('HUB_SLUG', 'default')}"
        subprocess.run(["x11vnc", "-storepasswd", env("VNC_PASSWORD"), rfbauth],
                       check=False, stderr=subprocess.DEVNULL)
        try:
            os.chmod(rfbauth, 0o600)
        except OSError as exc:
            log(f"chmod {rfbauth}: {exc}")
        args += ["-rfbauth", rfbauth]
    else:
        log("WARNING: VNC_PASSWORD unset, the noVNC door has no RFB password")
        args += ["-nopw"]
    fam.spawn("x11vnc", args)

    fam.spawn("websockify", ["websockify", "--web", novnc_web,
                             f"127.0.0.1:{ws_port}", f"127.0.0.1:{vnc_port}"])


def wake_once(browser: str, ctl_fn, state: dict, now: float,
              clients_fn=None) -> bool:
    """Start the browser family if a human is at the door and it is not already
    woken. Returns True if a wake was issued.

    door_clients() counts every established leg to the door port except this
    family's own sockets, so "1" really means a human's noVNC is connected to
    this daemon's websockify (the keyboard driver's direct leg is subtracted).
    `ctl start` on a RUNNING/STARTING program is supervisor's no-op, so
    re-waking during the browser's startup is harmless; the debounce
    floor is what keeps this from firing every poll."""
    clients = (clients_fn or family.door_clients)(int(env("WEBSOCKIFY_PORT", "8181")))
    if clients and now - state.get("last", 0.0) > FIRE_DEBOUNCE:
        r = ctl_fn("start", browser)
        log(f"wake {browser}: a noVNC client is connected "
            f"(start rc {r.returncode}: "
            f"{(r.stdout or r.stderr).strip()[:80] or 'ok'})")
        state["last"] = now
        return True
    return False


def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 else env("HUB_SLUG")
    if not slug:
        print("vnc-run: usage: vnc-run.py <host-slug>", file=sys.stderr)
        return 2
    os.environ["HUB_SLUG"] = slug
    # The browser program this transport wakes. VAULT_SLUG is the vault id
    # (gen-conf tenant_vals), which is what names vault-<vault>; the transport is
    # per vault too (vnc-<vault>) so a member partition never wakes a browser it
    # does not share.
    browser = f"vault-{env('VAULT_SLUG') or slug}"
    ws_port = int(env("WEBSOCKIFY_PORT", "8181"))

    family.install_subreaper(log)
    fam = family.Family(log)
    stopping = False

    def on_signal(signum, _frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            log(f"{signal.Signals(signum).name} received; tearing down transport")

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    log(f"display :{int(env('VNC_DISPLAY', '10'))} rfb {env('VNC_PORT', '5901')} "
        f"ws {ws_port} wakes {browser}")
    start_transport(fam)

    state: dict = {}
    try:
        while not stopping:
            finished = fam.reap()
            for line in finished:
                log(line)
            if finished:
                # A transport child (Xvfb / x11vnc / websockify) died: the door
                # is no longer trustworthy. Exit non-zero and let supervisor's
                # autorestart bring the whole transport back clean.
                log("transport broken; exiting for supervisor to restart it")
                return 1
            wake_once(browser, ctl, state, time.time())
            time.sleep(POLL)
    except Exception as exc:                    # never leave X running broken
        log(f"vnc-run crashed: {exc!r}")
        return 1
    finally:
        fam.teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
