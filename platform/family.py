#!/usr/bin/env python3
"""The parts of a vault family that more than one supervised program needs.

A vault used to be ONE process (platform/vault-run.py): it owned the transport
(Xvfb, x11vnc, websockify) and the browser (chromium, the keyboard driver, the
auto sign-in daemon, the exporter) and, when the session came up healthy, tore
the whole thing down - which put the owner's "a healthy partition runs zero
chromium" invariant in force but also took websockify's listener with it, so the
noVNC door answered "connection is closed" for the next human to click.

The split is what that invariant and a reachable door both need: an always-on
transport daemon (platform/vnc-run.py) keeps Xvfb/x11vnc/websockify bound and
wakes the browser, and a demand browser family (platform/vault-run.py) starts
chromium only when asked and exits when the human leaves. Both are child-owning
processes with the same three jobs - reap descendants so nothing zombies, tell a
human at the door from one of our own connections, and clean a leftover lock
before the thing that owns it starts - so those jobs live here, once.

This module is hub-native (not a vendored copy), so it carries no standalone-
deployment fallback: it is only ever imported from inside the hub container, by
programs gen-conf starts with the full tenant environment.

Everything here is a plain function or a small class; nothing reads the
environment or touches a path at import time, so importing it is side-effect
free (the tests import it directly). A callable is passed IN wherever a line is
printed, because each program keeps its own log prefix (`[vault owner]`,
`[vnc owner]`, `[watchdog ...]`).
"""
import ctypes
import glob
import os
import subprocess
import time


def _unlink(path: str, log=lambda _m: None) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(f"could not remove {path}: {exc}")


# ------------------------------------------------------------------ children
class Family:
    """Ordered children, reaped.

    PR_SET_CHILD_SUBREAPER (install_subreaper) makes the owning process the
    adoptive parent of everything its children fork: chromium forks constantly,
    and a helper that outlives its own parent is reparented THERE rather than to
    PID 1, so waitpid(-1) sees it and nothing accumulates as a zombie. (The
    CatWatch lesson: a one-off library fork nobody waited on became a permanent
    zombie under a PID-1 that never reaps; supervisord reaps its OWN children but
    not a grandchild orphaned under a program, so each family reaps its own.)

    `log` is the owning program's printer, so the start/stop lines keep its
    prefix; Family never prints on its own.
    """

    def __init__(self, log) -> None:
        self.log = log
        self.procs: list[tuple[str, subprocess.Popen]] = []

    def spawn(self, name: str, argv: list[str], **kw) -> None:
        shown = " ".join(argv[:3]) + (" …" if len(argv) > 3 else "")
        self.log(f"start {shown}")
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
                self.log(f"reaped orphaned pid {pid} status {status}")

    def dead(self) -> list[tuple[str, int | None]]:
        out = []
        for name, proc in self.procs:
            rc = proc.poll()
            if rc is not None:
                out.append((name, rc))
        return out

    def teardown(self, grace: float = 10.0) -> None:
        """Reverse order: the thing that would fight a dying sibling goes first.
        For the browser family that is the worker and the driver before chromium;
        for the transport it is websockify before x11vnc before Xvfb. Each owner
        stops only what it spawned - the transport daemon never touches chromium
        and the browser never touches X."""
        order = list(reversed(self.procs))
        for name, proc in order:
            if proc.poll() is not None:
                continue
            self.log(f"stop {name}")
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
                self.log(f"kill {name} (still up after {int(grace)}s)")
                proc.kill()
                proc.wait()
        self.reap()


def install_subreaper(log) -> None:
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(38, 1, 0, 0, 0) != 0:      # PR_SET_CHILD_SUBREAPER
            log("prctl(PR_SET_CHILD_SUBREAPER) failed; orphans go to PID 1")
    except OSError as exc:
        log(f"subreaper unavailable: {exc}")


# ------------------------------------------------------------------- presence
def proc_inodes(match) -> set[str]:
    """Socket inodes held by processes whose /proc/<pid>/comm matches `match`.

    One /proc scan; a predicate on the comm string lets one helper serve every
    question the lifecycle code asks (is a human connected?). A vanished process
    is skipped mid-scan.
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
            with open(f"/proc/{pid}/comm") as fh:
                if not match(fh.read().strip()):
                    continue
            with os.scandir(f"/proc/{pid}/fd") as it:
                for entry in it:
                    try:
                        link = os.readlink(entry.path)
                    except OSError:
                        continue
                    if link.startswith("socket:["):
                        inodes.add(link[8:-1])
        except OSError:
            continue
    return inodes


def door_clients(ws_port: int) -> int:
    """Humans with the noVNC door open: established connections to `ws_port`
    that this vault's OWN automation is not holding.

    Only the client side of the door's port is counted (destination port == ws),
    which already skips websockify's own accepted socket and the listener. The
    hard part is human-vs-automation, and the answer is SUBTRACTION from every
    leg on the port, not attribution to the gateway:

      * the keyboard driver is this family's OWN VNC-over-websocket client of
        x11vnc (the omnibox): it dials ws://127.0.0.1:ws directly, so its leg is
        a comm `python3` socket - and the scanner can always see it, because the
        daemon, the browser and the driver all run as this same uid and read
        each other's fds. That is the only attribution this function relies on.
      * a human reaches websockify ONLY through the portal gateway
        (browser -> wss -> nginx -> ws to 127.0.0.1:ws). That relayed leg is NOT
        reliably attributable to any process: nginx's worker holds it under a
        different uid, so the scanner (and even `ss -t`, measured on this
        container) shows no owner on it - the human's leg looks ownerless.

    So counting everything established to `ws_port` EXCEPT the sockets this
    family owns turns that ownerlessness into the discriminator: the human's leg
    (not ours) is counted, the driver's (ours) is subtracted. It needs only to
    see OUR processes - which is exactly what is reliable here - where counting
    only nginx's legs (the old rule, and the one `ss` cannot even confirm) read
    every real human as 0 and the door never woke.

    No leg on the port (a test harness with no gateway, or nobody watching) -> 0,
    the safe answer for every consumer: no human to wake for, none to stay up
    for, nothing holding the watchdog from stopping a stuck browser.
    """
    ours = proc_inodes(lambda comm: comm == "python3" or comm.startswith("vncdriver"))
    count = 0
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as fh:
                lines = fh.readlines()[1:]
        except OSError:
            continue
        count += _count_door_sockets(lines, ours, ws_port)
    return count


def _count_door_sockets(lines: list[str], ours: set[str], ws_port: int) -> int:
    """Established legs to `ws_port` from the raw `/proc/net/tcp*` body lines,
    minus the sockets `ours` owns. Pure (no /proc read) so the human-vs-our-
    automation discrimination - the whole reason this function is subtraction
    and not "count the gateway's legs" - is unit-testable without a gateway,
    a browser, or root-visible fds.

    A line's destination port is the REMOTE port (fields[2], hex after the ':'):
    both the human's gateway leg and the driver's direct leg dial `ws_port`, so
    the port alone cannot tell them apart - only ownership can. fields[9] is the
    socket inode; the family's own sockets (the keyboard driver's comm `python3`
    leg) are in `ours` and are subtracted, while the gateway's leg (a different
    uid, not in `ours`) is counted. A leg on another port, or in any non-
    ESTABLISHED state, never counts."""
    count = 0
    for line in lines:
        fields = line.split()
        if len(fields) < 10 or fields[3] != "01":          # ESTABLISHED
            continue
        try:
            dport = int(fields[2].rsplit(":", 1)[1], 16)
        except (ValueError, IndexError):
            continue
        if dport == ws_port and fields[9] not in ours:
            count += 1
    return count


# ------------------------------------------------------------------- readiness
def wait_port(port: int, timeout: float, log) -> bool:
    """Block until 127.0.0.1:port accepts a TCP connection (up to `timeout`).

    The browser family used to start its own Xvfb and know the display was up;
    now the transport daemon owns it, so the browser has to wait for the daemon
    to be listening (websockify is the daemon's LAST child, so a bound port means
    Xvfb and x11vnc are behind it) before pointing chromium at the display.
    Without this, a browser started during a daemon restart dies on
    "cannot open display". Returns False if the port never came up."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.5)
    log(f"port {port} never became reachable within {int(timeout)}s")
    return False


# ----------------------------------------------------------------- stale locks
def clear_profile(profile: str, log) -> None:
    """Drop chromium's leftover "profile in use" guards.

    The profile dir lives on a volume that outlives the container, so a hard
    kill or a container recreate can leave the Singleton* files behind and the
    next chromium refuses to start with "the profile appears to be in use". The
    browser owns chromium, so the browser clears chromium's locks; it never
    touches X's artifacts (the transport daemon owns those)."""
    for junk in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        _unlink(f"{profile}/{junk}", log)


def clear_x(disp: str, log) -> None:
    """Drop X's leftover artifacts from a previous daemon.

    /tmp outlives a `docker restart` but not a container recreate; a stale lock
    aborts the next Xvfb with "Server is already active for display N" (which
    takes x11vnc down with it). X's artifacts are named after the DISPLAY NUMBER,
    not the string: display :10 means /tmp/.X10-lock, /tmp/.X11-unix/X10 and
    /tmp/.X10-auth*. The transport daemon owns Xvfb, so it clears X's locks."""
    num = disp.lstrip(":")
    _unlink(f"/tmp/.X{num}-lock", log)
    _unlink(f"/tmp/.X11-unix/X{num}", log)
    for path in glob.glob(f"/tmp/.X{num}-auth*"):
        _unlink(path, log)
