#!/usr/bin/env python3
"""Registry watcher + vault lifecycle, so nothing in this stack needs a human.

Two jobs, one process, two cadences:

1. Registry -> config (every HUB_RELOAD_INTERVAL s). `hub partition add`
   rewrites partitions.json; the generator turns that into supervisor programs
   and nginx locations. A content hash is polled rather than an inotify watch
   so that a bind-mounted volume, a host-side edit and an atomic replace all
   behave identically. The apply path is the one `hub reload` uses, with one
   addition: `nginx -t` runs BEFORE the reload, so a generated file with a
   mistake leaves nginx serving the last good config (and says so every scan)
   instead of failing to start on the next container restart.

2. Vaults (every HUB_VAULT_SCAN s). Chromium runs only while a partition's
   cookie session is actually unusable, and stops as soon as it is usable
   again. platform/vault-run.py is the inner decider and normally exits by
   itself; what the watchdog owns that vault-run structurally cannot:
     - STARTING the family when a session goes stale - the exporter can only
       report the problem, and supervisor cannot act on the contents of a file;
     - STOPPING a family that is stuck RUNNING with a live session (the idle
       backstop, for a vault-run that got wedged);
     - spacing the retries when a vault keeps failing, so a dead Google account
       does not become a chromium restart every minute.

`session_live` is deliberately conservative: a session counts as usable only if
the exporter has written `up` AND that report AND the cookie file are both
younger than HUB_VAULT_MAX_AGE. The state file's timestamp is the liveness
signal precisely because it freezes when the exporter dies - trusting the
string alone would keep a three-day-old jar forever. A missing file, an
unreadable file, a state other than `up` and a stale report are all "needs the
browser".

The registry is read through gen-conf's own loader (imported from the same
directory), so "which partitions exist and which of them are ignored" has one
definition in this image.
"""
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
gen_conf = importlib.import_module("gen-conf")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("HUB_DATA_DIR", "/data")
REGISTRY = os.environ.get("HUB_REGISTRY", f"{DATA}/partitions.json")
SOCK = os.environ.get("HUB_SUP_SOCK", "/var/run/supervisor.sock")
GEN = os.path.join(HERE, "gen-conf.py")

RELOAD_INTERVAL = int(os.environ.get("HUB_RELOAD_INTERVAL", "10"))
SCAN_INTERVAL = int(os.environ.get("HUB_VAULT_SCAN", "60"))
MAX_AGE = int(os.environ.get("HUB_VAULT_MAX_AGE", "1800"))
IDLE = int(os.environ.get("HUB_VAULT_IDLE", "1800"))
RETRY = int(os.environ.get("HUB_VAULT_RETRY", "180"))
RETRY_MAX = int(os.environ.get("HUB_VAULT_RETRY_MAX", "1800"))

STATES = ("RUNNING", "STARTING", "STOPPING", "STOPPED", "EXITED", "FATAL",
          "UNKNOWN")


def log(msg: str) -> None:
    print(f"[watchdog {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------- supervisor
def ctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["supervisorctl", "-s", f"unix://{SOCK}", *args],
                          capture_output=True, text=True, check=False)


def program_state(name: str) -> tuple[str, float]:
    """(state, uptime_seconds) for one program.

    Unknown name = STOPPED, which is what the caller wants: a program gen-conf
    has not created yet (a partition added between two scans) gets started and
    supervisor says so if it cannot. Uptime is parsed because the idle backstop
    needs it, and it only means anything for RUNNING.
    """
    r = ctl("status", name)
    out = r.stdout.strip()
    _, sep, rest = out.partition(name)
    if not sep or "no such process" in (r.stderr or "").lower():
        return "STOPPED", 0.0
    fields = rest.split()
    state = fields[0].upper() if fields else "STOPPED"
    if state not in STATES:
        return "STOPPED", 0.0
    uptime = 0.0
    m = re.search(r"uptime (\d+):(\d+):(\d+)", rest)
    if m:
        uptime = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return state, uptime


# ------------------------------------------------------------------- registry
def registry_hash() -> str | None:
    try:
        with open(REGISTRY, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def apply_registry() -> bool:
    """Regenerate and apply. Validate before you reload, so a bad edit to
    partitions.json degrades to 'the new partition is not live yet' instead of
    a container that will not start."""
    gen = subprocess.run([sys.executable, GEN], capture_output=True, text=True,
                         check=False)
    for line in (gen.stdout + gen.stderr).splitlines():
        log(f"gen-conf: {line}")
    if gen.returncode:
        log("apply SKIPPED: gen-conf failed, keeping the current config")
        return False
    test = subprocess.run(["nginx", "-t"], capture_output=True, text=True,
                          check=False)
    if test.returncode:
        log("apply: generated config FAILED nginx -t, NOT reloaded "
            f"(nginx keeps the previous config):\n{test.stderr.strip()}")
    else:
        subprocess.run(["nginx", "-s", "reload"], check=False)
    for word in ("reread", "update"):
        r = ctl(word)
        if r.stdout.strip():
            log(f"supervisor {word}: {r.stdout.strip()}")
        if r.returncode:
            log(f"supervisor {word} failed: {r.stderr.strip()}")
    return True


# ------------------------------------------------------------------- sessions
def session_live(slug: str, now: float) -> tuple[bool, str]:
    """(usable, reason). See the module docstring for why every age counts."""
    home = f"{DATA}/users/{slug}"
    try:
        cookie_age = now - os.stat(f"{home}/cookies.txt").st_mtime
    except OSError:
        return False, "no cookies.txt yet"
    try:
        with open(f"{home}/session-state.json") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return False, f"cookies {int(cookie_age)}s, no session-state.json"
    state = str(payload.get("state", ""))
    try:
        stated = now - float(payload.get("ts", 0))
    except (TypeError, ValueError):
        stated = 1e9
    if state != "up":
        return False, f"state '{state}' (cookies {int(cookie_age)}s)"
    if stated > MAX_AGE:
        return False, f"state 'up' unverified for {int(stated)}s"
    if cookie_age > MAX_AGE:
        return False, f"state 'up' but cookies {int(cookie_age)}s old"
    return True, f"live (cookies {int(cookie_age)}s, reported {int(stated)}s)"


# ------------------------------------------------------------------- vaults
class Attempt:
    """Per-partition memory of the last start: how many ran in a row failed
    (which drives the backoff) and whether the family is up right now."""
    __slots__ = ("failures", "last_start", "running")

    def __init__(self) -> None:
        self.failures = 0
        self.last_start = 0.0
        self.running = False

    def backoff(self) -> float:
        return min(RETRY * (2 ** min(self.failures, 4)), RETRY_MAX)


def scan_vaults(reg: dict, now: float, attempts: dict[str, Attempt]) -> None:
    for slug in reg["slugs"]:
        name = f"vault-{slug}"
        st = attempts.setdefault(slug, Attempt())
        live, why = session_live(slug, now)
        state, uptime = program_state(name)
        running = state in ("RUNNING", "STARTING", "STOPPING")

        if running:
            st.running = True
            if state == "RUNNING" and live and uptime > IDLE:
                log(f"{name}: {why} but the family has been up {int(uptime)}s "
                    f"> idle {IDLE}s; stopping it (vault-run did not exit)")
                ctl("stop", name)
            continue

        if st.running:
            # The attempt just ended; grade it before deciding anything else.
            st.running = False
            if live:
                st.failures = 0
                log(f"{name}: finished, {why}")
            else:
                st.failures += 1
                log(f"{name}: ended with the session still needing work "
                    f"({why}); {st.failures} failure(s) in a row")

        if live:
            st.failures = 0
            continue
        delay = st.backoff()
        since = now - st.last_start
        if st.last_start and since < delay:
            log(f"{slug}: {why}; retry in {int(delay - since)}s "
                f"(backoff after {st.failures} failure(s))")
            continue
        log(f"{slug}: {why}; starting {name}")
        r = ctl("start", name)
        if r.returncode:
            log(f"{name}: start failed: {r.stdout.strip() or r.stderr.strip()}")
            st.failures += 1
        st.last_start = now


# ----------------------------------------------------------------------- main
def main() -> int:
    log(f"watchdog: registry {RELOAD_INTERVAL}s, vault scan {SCAN_INTERVAL}s, "
        f"max_age {MAX_AGE}s, idle {IDLE}s, retry {RETRY}s..{RETRY_MAX}s")
    last_hash: str | None = None
    last_scan = 0.0
    attempts: dict[str, Attempt] = {}
    while True:
        current = registry_hash()
        if current != last_hash:
            # First pass applies unconditionally: a container restart must
            # re-derive everything from the registry, and gen-conf is
            # idempotent (identical bytes rewrite, atomic replace).
            apply_registry()
            last_hash = current
        try:
            reg = gen_conf.load_registry()
        except Exception as exc:
            log(f"registry unreadable: {exc!r}")
            reg = {"slugs": []}
        for gone in [s for s in attempts if s not in reg["slugs"]]:
            attempts.pop(gone)
        now = time.time()
        if now - last_scan >= SCAN_INTERVAL:
            last_scan = now
            try:
                scan_vaults(reg, now, attempts)
            except Exception as exc:
                log(f"vault scan failed: {exc!r}")
        time.sleep(RELOAD_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
