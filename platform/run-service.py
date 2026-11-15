#!/usr/bin/env python3
"""One-shot program launcher: prepare a tenant's own state, then exec the app.

Supervisor restarts a program on a non-zero exit, so preparation failures must
be loud and cheap; but a schema step that runs on every restart is also a
serializing lock (prisma takes seconds), so it is done ONCE per partition per
container life and remembered with a stamp file next to the database it created.

Why this exists instead of a boot-time loop in entrypoint.sh: a partition whose
schema step fails must not stop the other partitions, and entrypoint.sh must not
have to know which partitions exist - gen-conf already derives that from the
registry, and this is the same information from the same file.
"""
import os
import subprocess
import sys

STACK = os.environ.get("STACK_DIR", "/srv/stack")


def stamp_path() -> str:
    home = os.environ.get("DATA_DIR") or ""
    return os.path.join(home, ".schema-stamp") if home else ""


def prepare_drivepod() -> None:
    """Prisma's schema is identical for every partition; only the database file
    differs (DATABASE_URL=file:<home>/database.db), so the push is per-home.
    `--skip-generate` because the client was generated at image build time."""
    home = os.environ.get("DATA_DIR") or ""
    db = os.environ.get("DATABASE_URL", "")
    stamp = stamp_path()
    marker = f"{db}|{os.environ.get('HUB_SLUG', '')}"
    if stamp and os.path.exists(stamp):
        try:
            with open(stamp) as fh:
                if fh.read().strip() == marker:
                    return
        except OSError:
            pass
    print(f"run-service: prisma db push for {os.environ.get('HUB_SLUG', '?')}",
          flush=True)
    r = subprocess.run(
        ["npx", "--no-install", "prisma", "db", "push", "--skip-generate",
         "--accept-data-loss"],
        cwd=os.path.join(STACK, "drivepod", "backend"), check=False)
    if r.returncode != 0:
        # Non-fatal on purpose: the backend itself reports the schema problem on
        # every request, and a crash loop here would hide it behind supervisor's
        # backoff. Let the app try anyway.
        print("run-service: prisma db push FAILED (continuing; see the "
              "backend's own errors)", file=sys.stderr, flush=True)
        return
    if stamp:
        try:
            with open(stamp, "w") as fh:
                fh.write(marker)
        except OSError:
            pass


def main() -> int:
    kind = sys.argv[1]
    argv = sys.argv[2:]
    if kind == "drivepod":
        prepare_drivepod()
    elif kind == "grabbytube":
        # Nothing to prepare: db.py creates its own sqlite file on first use,
        # and output/catalog dirs come from gen-conf's mkdir.
        pass
    os.execvp(argv[0], argv)
    return 127


if __name__ == "__main__":
    sys.exit(main())
