#!/usr/bin/env python3
"""Content-addressed pool blob store: one download, many partitions.

Why this exists: in the merged stack ONE tuner process (`:8102`) serves every
partition, and two partitions that ask for the same verified rip must not cost
two downloads, two yt-dlp runs against one Google session, or two copies of the
same bytes on disk. So a finished file is moved into a blob store under the
hash OF ITS BYTES - the identity that makes sharing safe - and each partition
receives it as a hardlink into its own cache. Two partitions holding the same
track is then two directory entries and one inode (`stat -c %h` = 2); the
libraries stay independent because a library entry is a name, not bytes, and an
app that ever rewrites its copy breaks the link by itself (the apps publish
with write-temp + os.replace).

Two halves, both usable from a shell:

    blob_for(src)      hash + move the bytes to <blobs>/<sha256><ext>; a repeat
                       of bytes we already hold lands on the existing blob
    deliver(blob, dst) publish a blob into one partition as a hardlink, with a
                       byte copy when a link is impossible (another mount, a
                       filesystem that refuses them)

`deliver` is called by the app that owns the destination tree (GrabbyTube
hardlinks `X-Pool-Path` into its partition cache); this module owns no
partition and writes nothing outside the path it is handed.
"""
import argparse
import errno
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path

# Where blobs live when the caller does not say: the same default the tuner's
# blob serving uses (TUNESTER_BLOBS), so an app invoked by hand finds the pool
# the container is actually using.
BLOBS_DEFAULT = os.environ.get("TUNESTER_BLOBS") or "/data/pool/blobs"
# Blob READS are refused outside this root: the blob path arrives from an HTTP
# header (`X-Pool-Path`), and a store rooted at one directory should not be an
# invitation to read any file the container can see. Overridable with the
# store's own variable so a relocated pool still validates against itself.
BLOB_ROOT = os.environ.get("TUNESTER_BLOBS") or "/data/pool/blobs"

# A blob name is a hash plus the source's extension (so a delivered file keeps
# the suffix its consumer's tools key on). Anything looser than that is dropped
# rather than trusted: the extension comes from a filename the tuner built out
# of an artist and a title.
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}\Z")


def sha256_file(path: str) -> str:
    """sha256 of a file's bytes, streamed (a delivery is tens of megabytes)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _require_regular(path: str, what: str) -> str:
    """Reject anything that is not a regular file. A directory or a device here
    means a caller passed the wrong path, and hashing one either hangs or
    stores nonsense under a content hash it does not have."""
    if not os.path.isfile(path):
        raise ValueError(f"{what} is not a regular file: {path}")
    return path


def _under_root(path: str) -> str:
    """Refuse a blob path outside the store (see BLOB_ROOT). Symlinks are
    resolved, so a symlink out of the pool is refused, not followed."""
    real = os.path.realpath(path)
    root = os.path.realpath(BLOB_ROOT)
    if real != root and not real.startswith(root + os.sep):
        raise ValueError(f"blob path outside the pool store ({root}): {path}")
    return real


def blob_for(path: str, blobs_dir: str | None = None) -> str:
    """Move `path`'s bytes into the blob store; return the blob path.

    The source is copied into the store on a temp name and only renamed into
    place once its hash is known, so a blob is never a partially written file
    and a crash leaves the source (which this function never deletes) intact.
    Bytes already in the store land on the EXISTING blob - the temp is removed
    and its name returned - which is the whole point: a second partition's copy
    of one song costs one inode, not another download.

    `blobs_dir` defaults to $TUNESTER_BLOBS, else /data/pool/blobs.
    """
    blobs_dir = blobs_dir or BLOBS_DEFAULT
    _require_regular(path, "blob source")
    os.makedirs(blobs_dir, exist_ok=True)
    ext = os.path.splitext(path)[1]
    name = sha256_file(path) + (ext if _EXT_RE.match(ext) else "")
    blob = os.path.join(blobs_dir, name)
    # Copy (never link) the source in: the source tree belongs to whoever made
    # the file, and this must not reach into it afterwards.
    tmp = os.path.join(blobs_dir, f".{name}.tmp{os.getpid()}")
    try:
        shutil.copyfile(path, tmp)
        if os.path.exists(blob):
            return blob
        os.replace(tmp, blob)
        return blob
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _same_content(a: str, b: str) -> bool:
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    return sha256_file(a) == sha256_file(b)


def deliver(blob_path: str, dest_path: str) -> tuple[str, str]:
    """Put `blob_path` at `dest_path`; return ("link" | "copy", dest_path).

    A hardlink is the point of the pool (no second copy of the bytes), but a
    link is impossible across a mount or on a filesystem that does not support
    them, so both fall back to a byte copy instead of failing the delivery.
    Either way the bytes land on a temp name in the destination directory and
    are `os.replace`'d over the destination: the destination is always either
    the old file or the complete new one, never a half-written track, which is
    what lets a partition's index.json name a file that is already whole.

    Re-delivering bytes that are already there is a no-op, and the reported
    method describes what the destination actually IS relative to the blob
    (`link` when it is the same inode, `copy` when it is an identical
    separate file), so a caller can log the truth after a retry.
    """
    blob = _under_root(_require_regular(blob_path, "pool blob"))
    dest = os.path.abspath(dest_path)
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    if os.path.isfile(dest):
        if os.path.samefile(dest, blob):
            return "link", dest
        if _same_content(dest, blob):
            return "copy", dest
    tmp = os.path.join(parent, f".{os.path.basename(dest)}.tmp{os.getpid()}")
    method = "link"
    try:
        try:
            os.link(blob, tmp)
        except OSError as exc:
            # EXDEV: the pool and this partition's cache are on different
            # mounts. EPERM/EOPNOTSUPP/ENOSYS/95: an overlayfs or fuse mount
            # that will not hardlink. Neither is a reason to lose the track.
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP,
                                 errno.ENOSYS, 95):
                raise
            shutil.copyfile(blob, tmp)
            method = "copy"
        os.replace(tmp, dest)
        return method, dest
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="poolimport.py",
        description="Pool blob store: hash a finished rip into the shared "
                    "blob store, or deliver one into a partition.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("blob", help="store FILE in the blob store")
    b.add_argument("path")
    b.add_argument("--blobs", default=None, help="blob store directory")
    d = sub.add_parser("deliver", help="hardlink (or copy) BLOB to DEST")
    d.add_argument("blob")
    d.add_argument("dest")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "blob":
            print(blob_for(args.path, args.blobs))
        else:
            print(deliver(args.blob, args.dest)[0])
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
