#!/usr/bin/env python3
"""Seed partitions.json on first start (never overwrites an existing one).

Copy of platform/partitions.default.json with the front slug renamed, so a
stack that starts under a different owner slug still has exactly one partition
and the same front/root contract every later start relies on.
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = os.path.join(HERE, "partitions.default.json")


def main(slug: str | None = None) -> int:
    """`slug` is a parameter, not just argv[1], because `main()` is also the
    in-process entry point (the tests seed through it): read from argv there, a
    pytest run would seed a partition named after the test file."""
    if slug is None:
        slug = sys.argv[1] if len(sys.argv) > 1 else "owner"
    # Same derivation as gen-conf/hub/hubauth: the state directory is what
    # relocates, HUB_REGISTRY only overrides it.
    dest = os.environ.get("HUB_REGISTRY") or \
        os.environ.get("HUB_DATA_DIR", "/data").rstrip("/") + "/partitions.json"
    if os.path.exists(dest):
        return 0
    with open(DEFAULTS) as fh:
        reg = json.load(fh)
    if slug != "owner":
        spec = reg["partitions"].pop("owner")
        spec["vault"] = slug
        reg["partitions"][slug] = spec
        reg["slugs"] = [slug]
        reg["defaultSlug"] = slug
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(reg, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, dest)
    print(f"seed-registry: wrote {dest} (front={reg['defaultSlug']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
