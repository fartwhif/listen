#!/usr/bin/env python3
"""One partition's cookie vault family: started on demand, torn down by itself.

Supervisor starts `vault-<slug>` only when the watchdog finds the partition's
cookie session unusable (autostart/autorestart are false in the generated
config); this process owns the family from there - brings the browser stack up
in order, runs the cookie exporter inside it, and exits as soon as the session
is live again. That construction is what enforces the owner's invariant (the
vault tab idles on about:blank unless an auth procedure is running) by
construction: a healthy partition runs zero chromium.

Start order is upstream ytcookies' own entrypoint.sh, with the literal numbers
gen-conf hands us replacing the container names a split deployment needed:
Xvfb -> x11vnc -> websockify -> chromium -> the VNC keyboard driver -> the auto
sign-in daemon -> the exporter (the worker, and the only thing that knows
whether the session is alive). Teardown is the reverse.

Why a wrapper exists at all: the exporter can report a dead session but cannot
start or stop a browser, and supervisor cannot act on the contents of a file.
This is the piece that turns "cookies are stale" into "chromium is running" and
"the session is live" into "chromium is not" - inside one process that reaps its
own descendants (the CatWatch lesson: a library fork nobody waited on became a
zombie under a PID-1 node that never reaps).

Exit conditions, each meaning "nothing left to do here":
  - session-state.json says `up`, the family has been up HUB_VAULT_GRACE
    seconds, and nobody is connected to the noVNC door - tearing down mid
    sign-in would throw away the attempt that just worked;
  - a family member died (logged, exit 1; the watchdog spaces the retry);
  - HUB_VAULT_MAX_RUN elapsed with the session still not `up`: a human is
    needed (2FA, CAPTCHA) and nobody came, so stop re-running a doomed browser
    and let the watchdog back off.
"""
import ctypes
import glob
import json
import os
import signal
import subprocess
import sys
import time

VAULT = os.environ.get("STACK_DIR", "/srv/stack") + "/vault"
NOVNC_WEB = "/usr/share/novnc"
if not os.path.isdir(NOVNC_WEB):
    NOVNC_WEB = "/usr/share/webapps/noVNC"

GRACE = int(os.environ.get("HUB_VAULT_GRACE", "120"))
MAX_RUN = int(os.environ.get("HUB_VAULT_MAX_RUN", "3600"))


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def log(msg: str) -> None:
    print(f"[vault {env('HUB_SLUG', '?')}] {msg}", flush=True)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(f"could not remove {path}: {exc}")


# ------------------------------------------------------------------ children
class Family:
    """Ordered children, reaped.

    PR_SET_CHILD_SUBREAPER makes this process the adoptive parent of everything
    its children fork (chromium forks constantly): a helper that outlives its
    own parent is reparented HERE rather than to PID 1, so waitpid(-1) sees it
    and nothing accumulates as a zombie.
    """

    def __init__(self) -> None:
        self.procs: list[tuple[str, subprocess.Popen]] = []

    def spawn(self, name: str, argv: list[str], **kw) -> None:
        shown = " ".join(argv[:3]) + (" …" if len(argv) > 3 else "")
        log(f"start {shown}")
        self.procs.append((name, subprocess.Popen(argv, **kw)))

    def reap(self) -> list[str]:
        """Wait on every descendant that has exited. Returns deaths of OUR
        direct children (the ones whose failure means the family is broken);
        adopted grandchildren are reaped silently, which is the whole point."""
        finished: list[str] = []
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return finished
            except InterruptedError:
                continue
            if pid == 0:
                return finished
            code = os.waitstatus_to_exitcode(status)
            label = "continued" if code is None else str(code)
            for name, proc in self.procs:
                if proc.pid == pid:
                    finished.append(f"{name} exited ({label})")
                    break
            else:
                log(f"reaped orphaned pid {pid} status {status}")

    def dead(self) -> list[tuple[str, int | None]]:
        out = []
        for name, proc in self.procs:
            rc = proc.poll()
            if rc is not None:
                out.append((name, rc))
        return out

    def teardown(self, grace: float = 10.0) -> None:
        """Reverse order: the worker and the driver first (they would fight a
        dying browser), then the browser, then the plumbing."""
        order = list(reversed(self.procs))
        for name, proc in order:
            if proc.poll() is not None:
                continue
            log(f"stop {name}")
            try:
                proc.terminate()
            except ProcessLookupError:
                continue
        deadline = time.time() + grace
        for name, proc in order:
            if proc.poll() is not None:
                continue
            try:
                proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                log(f"kill {name} (still up after {int(grace)}s)")
                proc.kill()
                proc.wait()
        self.reap()


def install_subreaper() -> None:
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(38, 1, 0, 0, 0) != 0:      # PR_SET_CHILD_SUBREAPER
            log("prctl(PR_SET_CHILD_SUBREAPER) failed; orphans go to PID 1")
    except OSError as exc:
        log(f"subreaper unavailable: {exc}")


# ------------------------------------------------------------------- presence
def own_inodes() -> set[str]:
    """Socket inodes held by this process tree (websockify, the driver,
    chromium, and every fork of them).

    The gateway reaches websockify from 127.0.0.1 exactly as our own keyboard
    driver does, so a peer address cannot tell a human from a helper; ownership
    can. One /proc scan per poll, and only when the session is already live.
    """
    inodes: set[str] = set()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return inodes
    for pid in entries:
        if not pid.isdigit():
            continue
        try:
            with os.scandir(f"/proc/{pid}/fd") as it:
                for entry in it:
                    try:
                        link = os.readlink(entry.path)
                    except OSError:
                        continue
                    if link.startswith("socket:["):
                        inodes.add(link[8:-1])
        except OSError:
            continue    # a process that vanished mid-scan
    return inodes


def door_clients(ws_port: int) -> int:
    """Established connections TO the websockify port that are not ours.

    At least one of those is a human with noVNC open. Only the connecting side
    is counted (destination port == ws), which skips websockify's own accepted
    socket and the listener, and inode ownership skips this family's clients
    (the driver's persistent RFB connection, the browser's).
    """
    mine = own_inodes()
    count = 0
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as fh:
                lines = fh.readlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "01":      # ESTABLISHED
                continue
            try:
                dport = int(fields[2].rsplit(":", 1)[1], 16)
            except (ValueError, IndexError):
                continue
            if dport == ws_port and fields[9] not in mine:
                count += 1
    return count


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


def clear_stale(disp: str, profile: str) -> None:
    """The profile dir lives on a volume that outlives the container, and /tmp
    outlives a `docker restart`, so both kinds of leftover abort the next
    start: "the profile appears to be in use" and "Server is already active
    for display N" (which then takes x11vnc down with it).

    X's artifacts are named after the DISPLAY NUMBER, not the string: display
    :10 means /tmp/.X10-lock, /tmp/.X11-unix/X10 and /tmp/.X10-auth*."""
    for junk in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        _unlink(f"{profile}/{junk}")
    num = disp.lstrip(":")
    _unlink(f"/tmp/.X{num}-lock")
    _unlink(f"/tmp/.X11-unix/X{num}")
    for path in glob.glob(f"/tmp/.X{num}-auth*"):
        _unlink(path)


def bring_up(fam: Family, disp: str, res: str) -> None:
    vnc_port = env("VNC_PORT", "5901")
    ws_port = env("WEBSOCKIFY_PORT", "8181")
    cdp_port = env("CDP_PORT", "9222")
    profile = env("VAULT_PROFILE_DIR") or f"{env('VAULT_DIR', '/data')}/profile"
    width, height = (res.split("x") + ["1920", "1080", "24"])[:2]

    os.makedirs(profile, exist_ok=True)
    os.makedirs(os.path.dirname(env("COOKIES_FILE", "/data/cookies.txt"))
                or "/data", exist_ok=True)
    clear_stale(disp, profile)

    fam.spawn("xvfb", ["Xvfb", disp, "-screen", "0", res, "-nolisten", "tcp"])
    time.sleep(1.0)

    args = ["x11vnc", "-display", disp, "-forever", "-shared",
            "-rfbport", vnc_port]
    if env("VNC_PASSWORD"):
        # -storepasswd writes the DES-hashed file and exits; x11vnc then reads
        # it at startup, so it has to finish first. One file per vault keeps
        # two vault families in one container out of each other's password.
        rfbauth = f"/tmp/vncpasswd-{env('HUB_SLUG', 'default')}"
        subprocess.run(["x11vnc", "-storepasswd", env("VNC_PASSWORD"), rfbauth],
                       check=False, stderr=subprocess.DEVNULL)
        _chmod_quiet(rfbauth, 0o600)
        args += ["-rfbauth", rfbauth]
    else:
        log("WARNING: VNC_PASSWORD unset, the noVNC door has no RFB password")
        args += ["-nopw"]
    fam.spawn("x11vnc", args)

    # Loopback only: the gateway proxies the door from inside this container,
    # so nothing outside needs this port (unpublished ports are unreachable
    # from the WireGuard peer regardless of bind).
    fam.spawn("websockify", ["websockify", "--web", NOVNC_WEB,
                             f"127.0.0.1:{ws_port}", f"127.0.0.1:{vnc_port}"])

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
    fam.spawn("exporter", ["python3", f"{VAULT}/cookie_exporter.py"])


def _chmod_quiet(path: str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:
        log(f"chmod {path}: {exc}")


# --------------------------------------------------------------------- main
def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 else env("HUB_SLUG")
    if not slug:
        print("vault-run: usage: vault-run.py <slug>", file=sys.stderr)
        return 2
    os.environ["HUB_SLUG"] = slug

    install_subreaper()
    disp = f":{int(env('VNC_DISPLAY', '10'))}"
    res = env("VNC_RESOLUTION", "1920x1080x24")
    ws_port = int(env("WEBSOCKIFY_PORT", "8181"))
    state_path = env("COOKIE_SESSION_STATE", "/data/session-state.json")

    fam = Family()
    stopping = False

    def on_signal(signum, _frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            log(f"{signal.Signals(signum).name} received; tearing down")

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    log(f"display {disp} {res} rfb {env('VNC_PORT', '5901')} ws {ws_port} "
        f"cdp {env('CDP_PORT', '9222')} profile {env('VAULT_PROFILE_DIR')} "
        f"cookies {env('COOKIES_FILE')}")
    bring_up(fam, disp, res)

    started = time.time()
    told = False
    rc = 0
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
            state, _stated = state_of(state_path)
            up_for = time.time() - started
            if state == "up" and up_for >= GRACE:
                clients = door_clients(ws_port)
                if clients:
                    if not told:
                        log(f"session live, {clients} noVNC client(s) up; "
                            "staying until they leave")
                        told = True
                else:
                    log(f"session up after {int(up_for)}s; tearing down")
                    break
            if up_for > MAX_RUN:
                log(f"session still '{state}' after {int(MAX_RUN)}s: a human "
                    "has to finish the sign-in through the noVNC door")
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
    state, _ = state_of(state_path)
    if state == "up":
        return 0
    deaths = ", ".join(f"{n}={c}" for n, c in fam.dead())
    log(f"leaving with session '{state}'"
          + (f"; dead: {deaths}" if deaths else ""))
    return rc


if __name__ == "__main__":
    sys.exit(main())
