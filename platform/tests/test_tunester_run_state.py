"""The tunester retune queue outlives the process that ran it.

The operator's complaint: "whenever I come back to GrabbyTube the list in the
UI that represents the queue of items for retune is empty even though I left it
running." That panel is fed by `GET /tracks/tunester`, which had exactly one
source: `_progress`, a module-level dict. So every container start - hub
recreate, `supervisorctl restart`, a crash - emptied it, and a batch that was
mid-flight when the process went away was not merely unlisted, it was gone:
never resumed. (Under the hub, restarting the app underneath a left-running
thing is normal and frequent.)

The fix under test: the batch is written to GrabbyTube's own `config` table on
every status change, and `main.py`'s lifespan calls `tunester.restore()`.

- an interrupted batch resumes with exactly the `queued`/`running` rows, so the
  work is redone and the list is re-shown;
- a FINISHED batch comes back as a list with nothing owed, so last night's
  results are readable instead of an empty panel;
- a queue the operator CLEARED stays cleared (or a restart would resurrect it
  and re-run it);
- the state lives in `db.DB_PATH`, which the hub renders per partition
  (`GRABBY_DB_PATH`, asserted by test_gen_conf), so tenants share no queue.

Everything runs against the VENDORED tree with its DB, catalog and output dir in
a tmpdir and the tuner's HTTP layer stubbed: no tuner, no network, no yt-dlp, no
DrivePod. The assertions are about the save/restore contract only. Like
test_grabby_quota, this needs the image's dependencies, so it skips without them.
"""
import asyncio
import importlib
import json
import os
import sqlite3
import sys

import pytest

pytest.importorskip("yt_dlp")
pytest.importorskip("mutagen")
pytest.importorskip("pydantic_settings")
pytest.importorskip("aiohttp")

BASE = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
# Same two search paths as test_grabby_quota: `stack/grabbytube` in the repo,
# flattened to the top of /srv/stack in the image.
for _cand in (os.path.join(BASE, "stack", "grabbytube"),
              os.path.join(BASE, "grabbytube")):
    if os.path.isdir(_cand):
        GRABBY_DIR = _cand
        break
else:
    GRABBY_DIR = os.path.join(BASE, "stack", "grabbytube")  # names it in skips

MODS = ("config", "db", "processor", "tunester")


@pytest.fixture
def runmod(tmp_path, monkeypatch):
    """`tunester` + `db` from the vendored tree, everything under `tmp_path`.

    The tree freezes `DB_PATH`, `CATALOG_PATH` and its settings object at import
    time, so the env is set and the modules imported FRESH per test - each "last
    night" is a real re-import of the same code, which is exactly what a restart
    is. The tree's bare module names (`config`, `db`, …) are popped afterwards so
    no later test reads a deleted tmpdir.
    """
    monkeypatch.setenv("GRABBY_DB_PATH", str(tmp_path / "grabby.db"))
    monkeypatch.setenv("GRABBY_CATALOG_PATH", str(tmp_path / "tracks.json"))
    monkeypatch.setenv("GRABBY_OUTPUT_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("GRABBY_TUNESTER_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("HUB_SLUG", raising=False)
    monkeypatch.delenv("GRABBY_PARTITION_SLUG", raising=False)
    sys.path.insert(0, GRABBY_DIR)
    saved = {name: sys.modules.get(name) for name in MODS}
    for name in MODS:
        sys.modules.pop(name, None)
    try:
        db = importlib.import_module("db")
        db.init_db()
        yield importlib.import_module("tunester"), db
    finally:
        sys.path.remove(GRABBY_DIR)
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


def write_catalog(tmp_path, *ids):
    """A catalog holding exactly `ids`, named the way the library names tracks."""
    (tmp_path / "tracks.json").write_text(json.dumps(
        [{"videoId": v, "author": f"Artist {v}", "title": f"Track {v}"}
         for v in ids], indent=2))


def seed_previous_process(tmp_path, db, tunester, rows):
    """Pretend a previous process wrote `rows` and then died.

    Writes the blob the way `_persist` writes it (full row shape, per vid), then
    wipes every in-memory trace - which is what a restart leaves behind.
    """
    write_catalog(tmp_path, *(vid for vid, _ in rows))
    tracks = []
    for vid, status in rows:
        row = tunester._blank_row(vid, f"Artist {vid} - Track {vid}")
        row["status"] = status
        row["message"] = f"persisted {status} message"
        tracks.append(row)
    db.set_config(tunester.RUN_STATE_KEY, json.dumps(
        {"updated_at": "2026-11-17T00:00:00+00:00", "tracks": tracks},
        ensure_ascii=False))
    tunester._progress.clear()
    tunester._run_task = None


def saved_rows(db, tunester):
    """The rows in the persisted blob, as the NEXT process would read them.

    Read through `tunester.RUN_STATE_KEY`: the config-row name is part of the
    contract, a silent rename would orphan the queue in every existing database.
    """
    raw = db.get_config(tunester.RUN_STATE_KEY)
    assert raw is not None, "the run state was never written"
    return {r["video_id"]: r for r in json.loads(raw)["tracks"]}


# ---------------------------------------------------------------------------
# what a restart does to the list
# ---------------------------------------------------------------------------

def test_with_nothing_saved_a_restart_still_shows_an_empty_panel(runmod, tmp_path):
    """The bug the owner reported, pinned so the fix cannot decay back to it.

    Nothing on disk -> `restore()` finds no batch, owes nothing, and
    `get_status()` (the body of `GET /tracks/tunester`) is empty.
    """
    tunester, db = runmod
    write_catalog(tmp_path, "v1")
    assert tunester.restore() == 0
    assert tunester.get_status() == {"active": False, "tracks": []}


def test_a_finished_batch_comes_back_as_a_list(runmod, tmp_path):
    """All-terminal rows are restored for READING: nothing owed, nothing running."""
    tunester, db = runmod
    seed_previous_process(tmp_path, db, tunester,
                          [("v1", "done"), ("v2", "fixup"), ("v3", "error")])
    assert tunester.restore() == 0
    st = tunester.get_status()
    assert not st["active"], "a finished batch must not look like a live run"
    got = {t["video_id"]: t for t in st["tracks"]}
    assert {v: t["status"] for v, t in got.items()} == {
        "v1": "done", "v2": "fixup", "v3": "error"}
    # Each row keeps what it carried: the panel says WHAT happened, and the
    # fixup's delivered name is the only place the new song title lives.
    assert got["v1"]["name"] == "Artist v1 - Track v1"
    assert got["v1"]["message"] == "persisted done message"



def test_an_interrupted_batch_resumes_exactly_its_unfinished_tracks(runmod, tmp_path):
    """`queued`/`running` are owed work; `done`/`fixup`/`error` are not.

    A `running` row goes back to `queued` - its tuner job died with this process,
    and re-submitting the same name is cheap (the tuner single-flights a song it
    is already fetching, and a finished one is a cache hit). The resumed run gets
    ONLY those ids, so a track that already finished is never tuned twice.
    """
    tunester, db = runmod
    seed_previous_process(tmp_path, db, tunester,
                          [("a", "running"), ("b", "queued"),
                           ("c", "done"), ("d", "fixup")])
    seen: list[list[str]] = []

    async def fake_batch(ids, catalog_map):
        seen.append(list(ids))

    async def scenario():
        tunester._run_batch = fake_batch
        owed = tunester.restore()
        await tunester._run_task
        return owed

    assert asyncio.run(scenario()) == 2
    assert seen == [["a", "b"]], "the resumed run is exactly the owed ids"
    st = tunester.get_status()
    assert not st["active"], "the resumed batch finished, so nothing is running"
    got = {t["video_id"]: t for t in st["tracks"]}
    assert got["a"]["status"] == "queued"
    assert "restart" in got["a"]["message"]
    assert got["b"]["status"] == "queued"
    assert got["c"]["status"] == "done" and got["d"]["status"] == "fixup"
    # The re-queue is on disk too: crash BEFORE the resumed run writes anything
    # and the panel still says `queued`, never the stale `running`.
    assert saved_rows(db, tunester)["a"]["status"] == "queued"


def test_a_resumed_run_refuses_a_second_batch(runmod, tmp_path):
    """`start()` keeps its single-flight guard across a resume.

    Otherwise a UI press after a restart `_progress.clear()`s the list the resume
    just restored and silently drops the tracks still owed work.
    """
    tunester, db = runmod
    seed_previous_process(tmp_path, db, tunester, [("a", "queued")])

    async def fake_batch(ids, catalog_map):
        await asyncio.sleep(30)

    async def scenario():
        tunester._run_batch = fake_batch
        assert tunester.restore() == 1
        with pytest.raises(RuntimeError, match="already in progress"):
            tunester.start(["a"])
        tunester._run_task.cancel()
        try:
            await tunester._run_task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_restore_does_nothing_twice(runmod, tmp_path):
    """A second `restore()` must not start a second run over the same list."""
    tunester, db = runmod
    seed_previous_process(tmp_path, db, tunester, [("a", "queued")])

    async def fake_batch(ids, catalog_map):
        await asyncio.sleep(30)

    async def scenario():
        tunester._run_batch = fake_batch
        first = tunester.restore()
        second = tunester.restore()
        runs = tunester._run_task
        runs.cancel()
        try:
            await runs
        except asyncio.CancelledError:
            pass
        return first, second

    first, second = asyncio.run(scenario())
    assert (first, second) == (1, 0), "the second call must not re-run the batch"


# ---------------------------------------------------------------------------
# when it is written, and what a clear does to it
# ---------------------------------------------------------------------------

def test_the_queue_is_on_disk_before_the_first_submit(runmod, tmp_path):
    """`start()` saves the whole queue up front, so a crash one second later
    still leaves the list to restore - and the work to redo."""
    tunester, db = runmod
    write_catalog(tmp_path, "a", "b")

    async def never_returns(ids, catalog_map):
        await asyncio.sleep(30)

    async def scenario():
        tunester._run_batch = never_returns
        tunester.start(["a", "b"])
        task = tunester._run_task
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    rows = saved_rows(db, tunester)
    assert {v: r["status"] for v, r in rows.items()} == {"a": "queued",
                                                         "b": "queued"}
    assert rows["a"]["name"] == "Artist a - Track a"


def test_only_status_changes_hit_the_database(runmod, tmp_path, monkeypatch):
    """Save on a status CHANGE, not on the 5 s log-tail rewrite.

    `_run_one` re-renders the tuner's current log line on every poll. Persisting
    that would rewrite the blob every 5 s per track for display-only text that
    the resumed run re-derives anyway.
    """
    tunester, db = runmod
    write_catalog(tmp_path, "v1")
    writes: list[str] = []

    def spy(key, value):
        assert key == tunester.RUN_STATE_KEY
        writes.append(value)

    monkeypatch.setattr(tunester, "set_config", spy)
    tunester._progress["v1"] = tunester._blank_row("v1", "Artist v1 - Track v1")
    tunester._persist()                      # the baseline start() performs
    baseline = len(writes)

    tunester._set("v1", "queued", "tuner: fetching foo")   # message only
    assert len(writes) == baseline, "a log-tail tick must not write the blob"
    tunester._set("v1", "queued", "tuner: fetching bar")   # still only text
    assert len(writes) == baseline
    tunester._set("v1", "running", "submitted to tunester")  # status change
    assert len(writes) == baseline + 1
    row = json.loads(writes[-1])["tracks"][0]
    assert (row["video_id"], row["status"]) == ("v1", "running")


def test_a_persist_failure_never_stops_the_run(runmod, tmp_path, monkeypatch):
    """Losing the panel is no reason to stop tuning: the save is logged, not fatal."""
    tunester, db = runmod
    write_catalog(tmp_path, "v1")

    def boom(key, value):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(tunester, "set_config", boom)
    tunester._set("v1", "queued", "queued")          # must not raise
    assert tunester._progress["v1"]["status"] == "queued"
    tunester._clear_saved()                          # must not raise either
    assert db.get_config(tunester.RUN_STATE_KEY) is None


def test_a_cleared_queue_stays_cleared_across_a_restart(runmod, tmp_path):
    """Clearing empties the SAVED blob, or the next start would resurrect it.

    "Clear Queue" cancels the batch AND empties the list. Clear only memory and
    a restart would restore the rows the operator just dismissed and re-run every
    one still marked `queued`.
    """
    tunester, db = runmod
    seed_previous_process(tmp_path, db, tunester,
                          [("a", "running"), ("b", "queued")])

    async def never_returns(session, base, vid, track):
        await asyncio.sleep(30)

    async def scenario():
        tunester._run_one = never_returns
        assert tunester.restore() == 2
        return await tunester.clear_run()

    res = asyncio.run(scenario())
    assert res["cancelled_run"] is True
    assert res["tracks"] == []
    assert saved_rows(db, tunester) == {}, "a cleared queue must not survive on disk"
    assert tunester.restore() == 0
    assert tunester.get_status() == {"active": False, "tracks": []}


# ---------------------------------------------------------------------------
# garbage on disk
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("blob, owed", [
    ("not json at all", 0),
    ("{}", 0),
    ('{"tracks": null}', 0),
    ('{"tracks": [{"video_id": ""}, {"video_id": "x", "status": "bogus"}]}', 1),
    ('[{"video_id": "x", "status": "queued"}]', 1),
], ids=["not-json", "no-key", "null-list", "junk-entries", "bare-list"])
def test_unusable_saved_state_restores_clean(runmod, tmp_path, blob, owed):
    """A torn or unexpected blob starts a sane queue instead of throwing.

    `restore()` runs in the lifespan: an exception there would take GrabbyTube's
    whole UI down over one malformed row - the worst possible outcome of a
    persistence feature. A recognized status survives; an unrecognized one is
    treated as still owed, so the worst case is a track getting tuned again.
    """
    tunester, db = runmod
    write_catalog(tmp_path, "x")
    db.set_config(tunester.RUN_STATE_KEY, blob)
    tunester._progress.clear()

    async def fake_batch(ids, catalog_map):
        pass

    async def scenario():
        tunester._run_batch = fake_batch
        got = tunester.restore()
        if tunester._run_task is not None:
            await tunester._run_task
        return got

    assert asyncio.run(scenario()) == owed
    if owed:
        assert tunester.get_status()["tracks"][0]["status"] == "queued"
    else:
        assert tunester.get_status()["tracks"] == []


def test_rows_missing_a_key_are_repaired_not_raised(runmod, tmp_path):
    """A partial row gets the full shape, so the UI can never read a missing key.

    `_blank_row` is the single source for the persisted, in-memory and
    `/tracks/tunester` shapes; a blob written by an older build (or hand-edited)
    must still yield every key the panel reads.
    """
    tunester, db = runmod
    write_catalog(tmp_path, "v1")
    db.set_config(tunester.RUN_STATE_KEY, json.dumps(
        {"tracks": [{"video_id": "v1", "status": "done"}]}))
    tunester._progress.clear()
    assert tunester.restore() == 0
    assert tunester.get_status()["tracks"][0] == {
        "video_id": "v1", "name": "", "status": "done", "message": "",
        "replaced": False, "artist": None, "title": None, "year": None}


# ---------------------------------------------------------------------------
# the resumed run against a library that changed underneath it
# ---------------------------------------------------------------------------

def test_a_track_deleted_while_the_batch_was_down_is_reported(runmod, tmp_path):
    """A resumed run names the problem instead of throwing `KeyError`.

    Deleting a track while the batch is down is a normal thing to do, and the
    resume passes a FRESH catalog read (so a renamed track is tuned under its
    current name, not a stale one). A vid that is simply gone becomes a readable
    error row - and the rest of the batch still runs.
    """
    tunester, db = runmod
    seed_previous_process(tmp_path, db, tunester,
                          [("gone", "queued"), ("kept", "queued")])
    write_catalog(tmp_path, "kept")          # `gone` was deleted while we were down
    ran: list[str] = []

    async def fake_one(session, base, vid, track):
        ran.append(vid)
        tunester._set(vid, "done", "tuned")

    async def scenario():
        tunester._run_one = fake_one
        tunester.restore()
        await tunester._run_task

    asyncio.run(scenario())
    assert ran == ["kept"]
    got = {t["video_id"]: t for t in tunester.get_status()["tracks"]}
    assert got["gone"]["status"] == "error"
    assert "no longer in the library" in got["gone"]["message"]
    assert got["kept"]["status"] == "done"
    assert saved_rows(db, tunester)["gone"]["status"] == "error"


def test_a_failed_index_rebuild_does_not_kill_the_run(runmod, tmp_path, monkeypatch):
    """The index sweep is the batch's `finally`; failing it must not abort the run.

    `update_index_file()` writes into DrivePod's cache directory - a shared mount
    that can be gone or read-only at exactly the wrong moment. The tracks are
    already tuned and saved by then, so a warning is the right response.
    """
    tunester, db = runmod
    seed_previous_process(tmp_path, db, tunester, [("a", "queued")])

    def boom():
        raise OSError("Read-only file system")

    async def fake_one(session, base, vid, track):
        tunester._set(vid, "done", "tuned")

    async def scenario():
        monkeypatch.setattr(tunester, "update_index_file", boom)
        tunester._run_one = fake_one
        tunester.restore()
        await tunester._run_task          # completes; the OSError stays inside
        return tunester.get_status()

    st = asyncio.run(scenario())
    assert not st["active"]
    assert st["tracks"][0]["status"] == "done"


def test_a_terminal_batch_still_heals_the_index(runmod, tmp_path):
    """A run that died between its last status write and its index rebuild heals.

    Every row terminal means the batch reached its end - but the process can die
    in the gap before `update_index_file()`, leaving DrivePod's index missing
    tracks whose MP3 and item JSON are already on disk. Restore's sweep is
    idempotent, so it is worth running whenever nothing is owed.
    """
    tunester, db = runmod
    seed_previous_process(tmp_path, db, tunester, [("a", "done")])
    cache = tmp_path / "cache"
    (cache / "a").mkdir(parents=True)
    (cache / "a" / "a.json").write_text(json.dumps(
        {"videoId": "a", "createdAt": "2026-11-17T00:00:00.000Z"}))
    assert not (cache / "grabby-index.json").exists()

    assert tunester.restore() == 0
    index = json.loads((cache / "grabby-index.json").read_text())
    assert [i["videoId"] for i in index] == ["a"], \
        "the restored sweep must publish the finished track to DrivePod"
