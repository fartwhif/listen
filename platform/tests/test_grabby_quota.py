"""`quota.concurrentDownloads` as a real ceiling on one partition's harvests.

The hub renders a partition's quota into that tenant's environment as
`GRABBY_MAX_CONCURRENT` (see gen-conf `tenant_vals`, and test_gen_conf asserts
the rendering); GrabbyTube's worker loop turns that number into how many
harvests it lets run at once. Both ends have to hold or the quota is decorative,
so this module checks the chain on the vendored code:

- the variable gen-conf emits is the one `settings.max_concurrent` reads
  (`env_prefix="GRABBY_"` + the field name, no alias to get wrong);
- the worker's slot count is honoured: with 1, harvest bodies NEVER overlap,
  with 2 exactly two do, and every job is still claimed once and only once.

The worker is exercised against fake `db` functions and a fake `process_job`
that only records when each body was live, so the assertion is about
concurrency, not about yt-dlp, the network, or YouTube's bot checks. The
`grabbytube` tree lives in `stack/grabbytube` in the repo and at the top of
`/srv/stack` in the image; importing `processor` needs mutagen, yt_dlp and
pydantic-settings, which only the image has, so this module skips without them.
"""
import asyncio
import importlib
import os
import sys
import time

import pytest

pytest.importorskip("yt_dlp")
pytest.importorskip("mutagen")
pytest.importorskip("pydantic_settings")

BASE = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
# The repo keeps the vendored tree under `stack/grabbytube`; the image flattens
# `stack/*` to the top of /srv/stack (Dockerfile `COPY stack/grabbytube
# ./grabbytube`). Both are searched so the suite says the same thing in a host
# checkout and inside the image.
for _cand in (os.path.join(BASE, "stack", "grabbytube"),
              os.path.join(BASE, "grabbytube")):
    if os.path.isdir(_cand):
        GRABBY_DIR = _cand
        break
else:
    GRABBY_DIR = os.path.join(BASE, "stack", "grabbytube")  # names it in skips


@pytest.fixture(scope="module")
def grabby():
    """(config, processor) from the vendored tree, with the tree's generic
    module names (`config`, `db`, …) kept out of everyone else's import path
    afterwards: `processor` must import its own siblings by bare name, which
    needs the directory on sys.path, and this is the only test that wants it."""
    sys.path.insert(0, GRABBY_DIR)
    saved = {}
    try:
        for name in ("config", "db", "processor"):
            saved[name] = sys.modules.get(name)
        cfg = importlib.import_module("config")
        proc = importlib.import_module("processor")
        yield cfg, proc
    finally:
        sys.path.remove(GRABBY_DIR)
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


def test_quota_env_name_is_the_setting_the_worker_reads(grabby, monkeypatch):
    """gen-conf writes `GRABBY_MAX_CONCURRENT`; a name that did not reach
    `settings.max_concurrent` would leave every partition on the default 2."""
    cfg, _ = grabby
    monkeypatch.setenv("GRABBY_MAX_CONCURRENT", "1")
    assert cfg.Settings().max_concurrent == 1
    monkeypatch.setenv("GRABBY_MAX_CONCURRENT", "3")
    assert cfg.Settings().max_concurrent == 3
    # unset = upstream's own default, which is what an env-less container runs
    monkeypatch.delenv("GRABBY_MAX_CONCURRENT", raising=False)
    assert cfg.Settings().max_concurrent == 2


class FakeQueue:
    """`db`'s claiming contract in memory: only an unclaimed job is claimable,
    and a second claim of the same id loses."""

    def __init__(self, ids):
        self.claimed: set[int] = set()
        self.ids = list(ids)

    def claimable(self):
        return [{"id": i} for i in self.ids if i not in self.claimed]

    def claim(self, jid):
        if jid in self.claimed:
            return False
        self.claimed.add(jid)
        return True


def run_worker(proc, slots: int, jobs: int, hold: float = 0.25) -> list[tuple]:
    """Run the vendored worker loop over `jobs` fake harvests with
    `max_concurrent = slots`, and return each body's (id, start, finish)."""
    queue = FakeQueue(range(1, jobs + 1))
    live: list[int] = []                   # bodies executing right now
    peak = 0
    spans: list[tuple] = []               # (id, start, finish)

    async def fake_process(jid):
        nonlocal peak
        start = time.monotonic()
        live.append(jid)
        peak = max(peak, len(live))
        await asyncio.sleep(hold)
        spans.append((jid, start, time.monotonic()))
        live.remove(jid)

    proc.reclaim_stale_jobs = lambda: 0
    proc.get_claimable_jobs = queue.claimable
    proc.claim_job = queue.claim
    proc.process_job = fake_process
    configured = proc.settings.max_concurrent
    proc.settings.max_concurrent = slots

    async def _tick():
        # One pass of the worker loop. Cheap: the worker can never sit longer
        # than `hold` while a claimable job is waiting, so this cannot spin.
        await asyncio.sleep(0.01)

    async def drive():
        task = asyncio.create_task(proc.worker())
        try:
            while len(spans) < jobs:
                await _tick()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return spans

    try:
        spans_out = asyncio.run(asyncio.wait_for(drive(), timeout=30))
    finally:
        proc.settings.max_concurrent = configured
    assert queue.claimed == set(range(1, jobs + 1)), "a job was never claimed"
    assert peak == slots, f"peak overlap {peak} != configured {slots}"
    return sorted(spans_out)


def test_quota_of_one_serializes_the_partition_downloaders(grabby):
    """The requirement: `quota.concurrentDownloads=1` means ONE download at a
    time for that tenant, so two harvests of one partition cannot both hit
    Google on the same session - which is what trips the bot check."""
    _, proc = grabby
    spans = run_worker(proc, slots=1, jobs=4)
    assert [s[0] for s in spans] == [1, 2, 3, 4], "jobs must run in id order"
    for a, b in zip(spans, spans[1:]):
        assert b[1] >= a[2] - 1e-9, (
            f"job {b[0]} started {a[2] - b[1]:.3f}s before job {a[0]} finished")


def test_quota_of_two_overlaps_harvests_but_never_more(grabby):
    """The ceiling is the configured number, not 1 and not unbounded: at 2 the
    loop genuinely runs a second harvest beside the first (claim-before-spawn is
    what makes the slot count honest), and never a third."""
    _, proc = grabby
    spans = run_worker(proc, slots=2, jobs=4)
    assert len(spans) == 4
    assert any(b[1] < a[2] for a, b in zip(spans, spans[1:])), \
        "slots=2 must actually overlap harvests"
