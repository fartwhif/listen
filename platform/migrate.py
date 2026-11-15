#!/usr/bin/env python3
"""One-shot import of the pre-hub state into the first partition.

Runs once, from the entrypoint, before gen-conf and before any app starts (the
apps resolve their paths from /data/users/<slug>, so the data has to be there
already). The marker file `$HUB_DATA_DIR/.migrated` is what makes it one-shot;
delete it to re-run, and re-running is safe: every step is idempotent and every
step leaves the source alone.

Where the old state lives (split deployment, one container per app):
    ../DrivePod/cache      36 GB of media + drivepod/radio state json
    ../DrivePod/data       drivepod's prisma database.db
    ../GrabbyTube          grabby-tracks.json + grabbytube.db
    ../tunester/downloads  5 GB of tuner deliveries + .nzb-cache
    ../ytcookies/data      chromium profile + cookies.txt + session state
Those trees reach this container through the SAME bind mount that carries the
state directory, under the paths `HUB_LEGACY_*` name (see docker-compose.yml:
`environment:` entries, not `volumes:` entries). An unset or absent root is
simply skipped, so a fresh install with nothing to import stays quiet.

Two copy policies, and the difference between them is the whole design:

  * MEDIA (the per-video cache dirs, the tuner deliveries, the nzb cache) is
    HARDLINKED. Same inode, zero bytes copied, 36 GB imported in seconds, and
    `stat -c %h` = 2 is the proof that no second copy exists. That is safe for
    media because the apps publish a file by `os.replace` (write a temp, rename
    over the old name), which breaks the link by itself: the moment either stack
    rewrites a track, the two have separate inodes.

  * DATABASES and the churning state/catalog/cookie files are COPIED. A shared
    inode there is not a saving but a hazard: the migrated drivepod runs
    `prisma db push` against its database on first start, and an ALTER landing
    on the OLD container's live file mid-request is a corruption risk; the
    per-video `index.json` at the media root is rewritten several times a minute
    by two writers in the old container, and a hardlinked one would let the old
    stack keep editing the new stack's state through the rollback window. A
    database is snapshotted through the sqlite3 backup API (never linked)
    because grabbytube.db runs in WAL mode: linking a WAL database whose -wal is
    live reads a torn view of it.

Why the same mount matters: `os.link()` is a same-MOUNT operation, not a
same-filesystem one - two bind mounts of one ext4 (identical st_dev) raise EXDEV
just as a real cross-device source does, so a legacy tree mounted beside /data
costs a full copy. A genuinely cross-device source (another drive, the NAS) falls
back to `shutil.copy2` with timestamps carried over, so the ETag behaviour of
imported media is unchanged. Nothing is ever deleted from a source.

`--dry-run` walks every tree and reports what it WOULD do, writing nothing.
"""
import os
import shutil
import sqlite3
import stat as statmod
import sys
from typing import Callable, Optional

DATA = os.environ.get("HUB_DATA_DIR", "/data").rstrip("/")
LEGACY = os.environ.get("HUB_LEGACY_DIR", "/srv/legacy").rstrip("/")
SLUG = ((os.environ.get("HUB_MIGRATE_SLUG") or
         os.environ.get("HUB_DEFAULT_SLUG") or "owner").strip() or "owner")
# The vault whose browser state this partition inherits. Sharing a vault is a
# registry decision (gen-conf's vault_slug), so read the seeded registry when it
# is there; on a fresh box the partition is its own vault.
VAULT_OVERRIDE = (os.environ.get("HUB_MIGRATE_VAULT") or "").strip()
# The member `progress.json`'s resume point belongs to: nginx serves
# progress-<user>.json per member, so the one legacy file is renamed on the way
# in rather than left as a name every member would then share.
PROGRESS_OWNER = ((os.environ.get("HUB_MIGRATE_PROGRESS_USER") or
                   os.environ.get("HUB_OWNER_USER") or SLUG).strip() or "owner")

DRY = "--dry-run" in sys.argv


def legacy(name: str, sub: str) -> str:
    env = os.environ.get(f"HUB_LEGACY_{name}", "").strip()
    return env.rstrip("/") if env else f"{LEGACY}/{sub}"


DRIVEPOD_CACHE = legacy("DRIVEPOD_CACHE", "drivepod/cache")
DRIVEPOD_DATA = legacy("DRIVEPOD_DATA", "drivepod/data")
GRABBY = legacy("GRABBY", "grabbytube")
TUNESTER_DL = legacy("TUNESTER_DOWNLOADS", "tunester/downloads")
YTCOOKIES = legacy("YTCOOKIES_DATA", "ytcookies/data")

HOME = f"{DATA}/users/{SLUG}"
POOL = f"{DATA}/pool"

# errno values that all mean "this filesystem cannot share my inode": EXDEV
# (another mount), EPERM/EACCES (a bind mount that forbids linking),
# ENOTSUP/EINVAL (a filesystem with no hardlinks - an NTFS tree, for instance).
NO_HARDLINK = (18, 1, 13, 95, 22)


class Report:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.problems: list[str] = []
        self.max_problems = 40

    def bump(self, token: str) -> None:
        self.counts[token] = self.counts.get(token, 0) + 1

    def problem(self, msg: str) -> None:
        self.bump("problem")
        if len(self.problems) < self.max_problems:
            self.problems.append(msg)
        elif len(self.problems) == self.max_problems:
            self.problems.append("… more problems folded")
            self.max_problems += 1

    def line(self, msg: str) -> None:
        print(f"migrate: {msg}", flush=True)


REPORT = Report()
# Set in main(): the device of DATA, so "can I hardlink this?" is one stat.
ROOT_DEV = -1


def registry_vault() -> str:
    """The vault the seeded registry assigns to this partition ('' = unknown)."""
    try:
        with open(os.environ.get("HUB_REGISTRY", f"{DATA}/partitions.json")) as fh:
            import json
            reg = json.load(fh)
        spec = (reg.get("partitions") or {}).get(SLUG) or {}
        return str(spec.get("vault") or "").strip()
    except Exception:
        return ""


def same_content(a: os.stat_result, b: os.stat_result) -> bool:
    return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)


def deliver(src: str, dst: str, st: os.stat_result, link: bool) -> str:
    """Place src's bytes at dst. Returns linked|copied.

    A hardlink is only possible on the destination's own filesystem, so the
    source's device decides: a bind-mounted NTFS tree or a second disk is
    reported as a copy, which costs time and space but is still correct.
    """
    hardlink = link and st.st_dev == ROOT_DEV
    if DRY:
        return "linked" if hardlink else "copied"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if hardlink:
        try:
            os.link(src, dst)
            return "linked"
        except OSError as exc:
            # EXDEV/EPERM/EACCES/ENOTSUP/EINVAL all mean "this filesystem
            # cannot share my inode"; copying is the fallback, not a failure.
            if exc.errno not in NO_HARDLINK:
                raise
            REPORT.line(f"{dst}: no hardlink here ({os.strerror(exc.errno)}),"
                        " copying")
    tmp = f"{dst}.migrate.tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)
    return "copied"


def put_file(src: str, dst: str, *, link: bool = False) -> str:
    """One regular file. Returns already|kept|conflict|linked|copied|problem.

    An absent source is `missing`, not a problem: which of the legacy trees
    exists is a property of the machine this runs on, and a split deployment
    that never had a file (or ran a different app) is a normal run, not an error.
    """
    if not os.path.exists(src):
        REPORT.bump("missing")
        return "missing"
    try:
        st = os.stat(src)
    except OSError as exc:
        REPORT.problem(f"cannot read {src}: {exc}")
        return "problem"
    if not statmod.S_ISREG(st.st_mode):
        REPORT.problem(f"skipping non-regular file {src}")
        return "problem"
    try:
        dt = os.stat(dst)
    except OSError:
        dt = None
    if dt is not None:
        if dt.st_dev == st.st_dev and dt.st_ino == st.st_ino:
            REPORT.bump("already")
            return "already"
        if same_content(dt, st):
            # A previous run placed identical bytes here. The destination wins:
            # it is the copy the apps are using, and re-linking a file they have
            # since rewritten would hand the old stack's version back.
            REPORT.bump("kept")
            return "kept"
        REPORT.problem(f"kept existing {dst} (differs from {src})")
        return "conflict"
    token = deliver(src, dst, st, link)
    REPORT.bump("planned" if DRY else token)
    return token


def put_tree(src: str, dst: str, *, link: bool = False,
             relmap: Optional[Callable[[str], Optional[str]]] = None,
             copy_when: Optional[Callable[[str], bool]] = None) -> None:
    """Every regular file under src, at the mapped relative path under dst.

    `relmap(rel) -> dst_rel | None` reroutes a path or drops it; `copy_when(rel)`
    forces a copy for one file inside an otherwise-hardlinked tree (the state
    json beside the media). Symlinks are skipped without complaint: in a
    chromium profile they are the Singleton* locks, and vault-run clears those
    before it starts a browser anyway.
    """
    if not os.path.isdir(src):
        REPORT.bump("missing")
        return
    for dirpath, names, files in os.walk(src, followlinks=False):
        names.sort()
        files.sort()
        rel_dir = os.path.relpath(dirpath, src)
        for name in files:
            rel = name if rel_dir == "." else os.path.relpath(
                os.path.join(dirpath, name), src)
            src_path = os.path.join(dirpath, name)
            if os.path.islink(src_path):
                REPORT.bump("skipped")
                continue
            dst_rel = relmap(rel) if relmap else rel
            if dst_rel is None:
                REPORT.bump("skipped")
                continue
            must_copy = bool(copy_when and copy_when(rel))
            put_file(src_path, os.path.join(dst, dst_rel), link=link and
                     not must_copy)
        if rel_dir != "." and not DRY:
            # Recreate the directory even where nothing landed, so the tree the
            # apps expect to find is there (a vid dir they write into next).
            os.makedirs(os.path.join(dst, rel_dir), exist_ok=True)


def put_db(src: str, dst: str) -> str:
    """A SQLite database, imported as a consistent snapshot copy."""
    if not os.path.isfile(src):
        REPORT.bump("missing")
        return "missing"
    try:
        st = os.stat(src)
        dt = os.stat(dst)
        if (dt.st_dev == st.st_dev and dt.st_ino == st.st_ino) or \
                same_content(dt, st):
            REPORT.bump("kept" if dt.st_size != st.st_size or
                        int(dt.st_mtime) != int(st.st_mtime) else "already")
            return "already"
        REPORT.problem(f"kept existing {dst} (differs from {src})")
        return "conflict"
    except OSError:
        pass
    if DRY:
        REPORT.bump("planned")
        return "planned"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = f"{dst}.migrate.tmp"
    try:
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as exc:
        REPORT.problem(f"cannot open {src} read-only: {exc}")
        return "problem"
    try:
        target = sqlite3.connect(tmp)
    except sqlite3.Error as exc:
        REPORT.problem(f"cannot create {tmp}: {exc}")
        source.close()
        return "problem"
    ok = "not-ok"
    try:
        source.backup(target)
        target.commit()
        # The snapshot must stand on its own: a source mid-write can still be
        # healthy, a corrupted one is not worth importing at all.
        ok = (target.execute("PRAGMA integrity_check").fetchone() or
              ("?",))[0]
    except sqlite3.Error as exc:
        REPORT.problem(f"snapshot of {src} failed: {exc}")
    finally:
        target.close()
        source.close()
    if ok != "ok":
        REPORT.problem(f"{src} failed integrity_check ({ok}); left in place")
        for junk in (tmp, *(tmp + s for s in ("-wal", "-shm"))):
            unlink(junk)
        REPORT.bump("problem")
        return "problem"
    shutil.copystat(src, tmp)
    os.replace(tmp, dst)
    # The source's -wal/-shm belong to the OLD process. They must not follow the
    # snapshot, or the new app replays a foreign WAL onto its own database.
    for junk in (dst + s for s in ("-wal", "-shm")):
        unlink(junk)
    REPORT.bump("copied")
    return "copied"


def unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def cache_map(rel: str) -> Optional[str]:
    """cache/ -> users/<slug>/cache/.

    `progress.json` is the one rename this migration owes the per-member
    progress contract; drivepod's rotated backups (`index.json.bak`,
    `.old`, the `-wal`/`-shm` of a json written in place) are its private
    churn and are dropped. Everything else keeps its path: per-video dirs are
    media, the `rss/` feeds are the radio's channel list, and the root-level
    state json belongs where the new stack reads it.
    """
    base = rel.rsplit("/", 1)[-1]
    if rel == "progress.json":
        return f"progress-{PROGRESS_OWNER}.json"
    if base in ("index.json.bak", "index.json.old") or \
            rel.endswith((".json-wal", ".json-shm")):
        return None
    return rel


def cache_copy_when(rel: str) -> bool:
    """The media tree's churning state: everything at the root of cache/.

    Nested files are per-video media and per-video metadata, written once and
    renamed into place (a hardlink is right). At the root sit `index.json`,
    `status.json`, `shufflebag.json`, `grabby-played.json`, `completed.json` and
    `grabby-index.json`: drivepod's and FrogRock's live state, rewritten seconds
    apart by two writers in the old container, and each one the new stack must
    own outright. 2.7 MB of copies, and the reason the two stacks cannot edit
    each other's library through the rollback window.
    """
    return "/" not in rel


def nzb_map(rel: str) -> Optional[str]:
    """Pool `out/`: the tuner's `*.mp3` deliveries.

    The tuner's NZB cache is routed elsewhere (see nzb_cache_map) because the
    pool keeps it as a sibling of `out/` - `TUNESTER_NZB_CACHE` points at
    `/data/pool/nzb-cache`, not inside the delivery dir.
    """
    if rel.startswith(".nzb-cache/"):
        return None
    return rel


def nzb_cache_map(rel: str) -> Optional[str]:
    """`.nzb-cache/` (old layout, a dot-dir inside downloads) -> `nzb-cache/`.

    It is the fetched-NZB cache: importing it is what keeps a re-run of an old
    job from re-querying an indexer for a release the pool already resolved.
    """
    if rel.startswith(".nzb-cache/"):
        return rel[len(".nzb-cache/"):]
    return None


def cookies_map(rel: str) -> Optional[str]:
    """The three files of ytcookies' state this partition wants in its home.

    `cookies.txt` is the jar the apps read, `session-state.json` is what the
    watchdog reads to decide whether a browser is needed (and whose mtime is the
    liveness signal), `attempted.json` is auto_signin's "we already tried this
    password recently" marker. Importing the last two is what stops a cutover
    from launching a needless re-auth: without them the watchdog sees no state
    file and starts chromium at once.
    """
    if base(rel) in ("cookies.txt", "session-state.json", "attempted.json"):
        return rel
    return None


def profile_map(rel: str) -> Optional[str]:
    """The chromium profile, into this partition's own profile dir.

    Per PARTITION even when two partitions share one vault: two chromiums on one
    profile dir fight over its SingletonLock and the second dies with "profile
    is already in use". Everything under `profile/` moves, nothing else does (the
    cookies and state files belong to cookies_map, the logs are the old
    container's stdout).
    """
    if rel.startswith("profile/"):
        return rel[len("profile/"):]
    return None


def base(rel: str) -> str:
    return rel.rsplit("/", 1)[-1]


def main() -> int:
    global ROOT_DEV
    vault = VAULT_OVERRIDE or registry_vault() or SLUG
    vault_dir = f"{DATA}/vaults/{vault}"
    try:
        ROOT_DEV = os.stat(DATA).st_dev
    except OSError as exc:
        REPORT.line(f"{DATA} not present ({exc}); nothing to import")
        return 0

    for path in (f"{HOME}/cache", f"{HOME}/library", f"{HOME}/radio",
                 f"{vault_dir}/profiles/{SLUG}", f"{POOL}/out",
                 f"{POOL}/nzb-cache", f"{POOL}/jobs", f"{POOL}/blobs",
                 f"{POOL}/cache"):
        if not DRY:
            os.makedirs(path, exist_ok=True)
    if not DRY:
        os.chmod(vault_dir, 0o700)  # cookie jars, per the data layout contract

    present = [name for name, path in
               (("drivepod cache", DRIVEPOD_CACHE),
                ("drivepod data", DRIVEPOD_DATA),
                ("grabbytube", GRABBY),
                ("tunester downloads", TUNESTER_DL),
                ("ytcookies data", YTCOOKIES))
               if os.path.isdir(path)]
    REPORT.line(f"partition '{SLUG}' -> {HOME} (vault {vault})")
    REPORT.line(f"legacy roots present: {', '.join(present) or 'none'}"
                f"{' [dry run]' if DRY else ''}")
    if not present:
        REPORT.line("nothing to import (fresh box)")
        return 0

    # ---- media: hardlinked, state json copied ----------------------
    put_tree(DRIVEPOD_CACHE, f"{HOME}/cache", link=True, relmap=cache_map,
             copy_when=cache_copy_when)
    put_tree(TUNESTER_DL, f"{POOL}/out", link=True, relmap=nzb_map)
    # Second pass over the same tree, other destination: the tuner's dot-dir
    # NZB cache belongs BESIDE out/, not under it, because that is where
    # TUNESTER_NZB_CACHE points.
    put_tree(TUNESTER_DL, f"{POOL}/nzb-cache", link=True,
             relmap=nzb_cache_map)

    # ---- databases + live catalog: snapshots -----------------------
    put_db(f"{DRIVEPOD_DATA}/database.db", f"{HOME}/database.db")
    put_db(f"{GRABBY}/grabbytube.db", f"{HOME}/grabbytube.db")
    put_file(f"{GRABBY}/grabby-tracks.json",
             f"{HOME}/library/grabby-tracks.json")
    # GrabbyTube used to keep its index beside the media in DrivePod's cache;
    # the per-partition layout puts it in the new cache root, where gen-conf's
    # exact-match location serves it no-cache.
    put_file(f"{DRIVEPOD_CACHE}/grabby-index.json",
             f"{HOME}/cache/grabby-index.json")
    put_file(f"{GRABBY}/grabby-index.json", f"{HOME}/cache/grabby-index.json")

    # ---- vault state -----------------------------------------------
    put_tree(YTCOOKIES, HOME, relmap=cookies_map)
    put_tree(YTCOOKIES, f"{vault_dir}/profiles/{SLUG}", relmap=profile_map)

    for problem in REPORT.problems:
        REPORT.line(f"PROBLEM {problem}")
    REPORT.line("done: " + " ".join(f"{k}={v}" for k, v in
                                    sorted(REPORT.counts.items())))
    if DRY:
        return 0
    # Conflicts and problems are reported, not fatal: the entrypoint keeps going
    # and the apps run on what is in place. A half-imported tree is recoverable
    # by hand; a refused start is an outage.
    return 0


if __name__ == "__main__":
    sys.exit(main())
