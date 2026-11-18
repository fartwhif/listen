r"""The answer store: what a tune ASKED for, cached beside what it DOWNLOADED.

`stack/tunester/lookups.py` is the third subtree of the shared tuner's cache.
The media stores (`sources/`, `deliveries/`) already existed; these hold the
small repeatable ANSWERS a run chains on its way to a file - MusicBrainz
recording queries, AcoustID fingerprint clusters, archive.org item metadata,
nzb.life / archive.org / yt-dlp listings, the multi-part NNTP pull of a
release's tracklist sidecar, and the LLM version verdicts. In the hub these
are worth more than on a standalone deploy: ONE tuner answers for every
partition, so a `GrabbyTube /tracks/tunester` batch that re-asks a hundred
songs re-asks the APIs of the whole box, not of one user.

The rules these tests lock, because breaking any one of them makes the store
lie rather than merely be slower:

- a TTL is honoured EXACTLY, and a dead entry is UNLINKED, not just ignored:
  `put` never overwrites a published blob, so an entry that cannot be honored
  (expired, written under another TTL, torn mid-publish) would block its own
  refresh for the rest of the store's life;
- `TUNESTER_LOOKUP_TTL_HOURS=0` / `TUNESTER_SEARCH_TTL_MINUTES=0` /
  `TUNESTER_LLM_TTL_HOURS=0` are OFF switches that read nothing, write nothing
  AND delete nothing - flipping one back must restore what it ignored;
- only a SUCCESSFUL fetch is published: a failure (unreachable API, `queued`
  AcoustID status, non-zero yt-dlp, an LLM reply with no content) must leave
  the store empty, or one bad moment condemns every later run;
- the identity of an answer is what decides the reply, not what happened to
  be in the request: the AcoustID `client` key and the LLM api key are absent
  from the keys (rotating one must not invalidate the verdicts it bought),
  while the LLM's BASE URL IS in it - a llama.cpp host aliases an unknown
  model id to whatever it has loaded, so the same id on another host is a
  different model (measured on this box's GPU peer);
- a text sidecar has NO expiry (a Usenet posting is immutable) and is keyed by
  its message-ids, so the same posting reached from another query still hits —
  and it is the SMALLEST qualifying sidecar that gets fetched, because the
  512 KiB cap exists to make a 120 MB hash-volume box cost the tracklist;
- a 404 model substitution persists the resolved id on the ENDPOINT, not just
  in that one body, so the verdict the real model produced is actually findable
  (a store written under an id the server rejected answers nothing);
- the answer blobs count against `TUNESTER_CACHE_MAX_MB` exactly like the
  media blobs, through the one existing `_cache_evict` walk.

Everything runs against the vendored tree with `urllib.request.urlopen`,
`subprocess.run` and `nntplib` replaced by recorders: no MusicBrainz, no
AcoustID, no archive.org, no nzb.life, no yt-dlp, no LLM, no NNTP.
"""
import importlib
import json
import os
import sys
import time
import types
import urllib.error
import zlib

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
# `stack/tunester` in the repo checkout, flattened to /srv/stack/tunester in
# the image; both are searched so the suite says the same thing in either.
for _cand in (os.path.join(BASE, "stack", "tunester"),
              os.path.join(BASE, "tunester")):
    if os.path.isdir(_cand):
        TUNE_DIR = _cand
        break
else:
    TUNE_DIR = os.path.join(BASE, "stack", "tunester")  # names it in skips

MODS = ("lookups", "pipeline", "sources", "llm")


@pytest.fixture(scope="module")
def tune():
    """The vendored tuner modules, off everyone else's import path after use."""
    sys.path.insert(0, TUNE_DIR)
    saved = {}
    try:
        for name in MODS:
            saved[name] = sys.modules.get(name)
        mods = {name: importlib.import_module(name) for name in MODS}
        mods["TUNE_DIR"] = TUNE_DIR
        yield mods
    finally:
        sys.path.remove(TUNE_DIR)
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch, tune):
    """A private cache root per test, agreed on by BOTH sides.

    `lookups.cache_root()` re-reads `TUNESTER_CACHE` per call, but
    `pipeline.CACHE_DEFAULT` froze the ambient value at import time, and
    `_cache_evict` walks THAT - so the env var alone would put the answers in
    one tree and the eviction walk in another. Both point at the tmpdir.
    """
    root = tmp_path / "cache"
    monkeypatch.setenv("TUNESTER_CACHE", str(root))
    for name in ("TUNESTER_LOOKUP_TTL_HOURS", "TUNESTER_SEARCH_TTL_MINUTES",
                 "TUNESTER_LLM_TTL_HOURS", "TUNESTER_ACOUSTID_KEY",
                 "TUNESTER_NZB_APIKEY", "TUNESTER_NZB_API", "TUNESTER_NZB_I",
                 "TUNESTER_LLM_PRIMARY_BASE_URL", "TUNESTER_LLM_PRIMARY_API_KEY",
                 "TUNESTER_LLM_PRIMARY_MODEL", "TUNESTER_LLM_MODEL",
                 "OPENAI_BASE_URL", "OPENAI_API_KEY", "TUNESTER_COOKIES"):
        monkeypatch.delenv(name, raising=False)
    # `_cache_evict` walks pipeline's frozen constant, not the env var.
    monkeypatch.setattr(tune["pipeline"], "CACHE_DEFAULT", root)
    return root


@pytest.fixture
def log():
    """The tuner's log sink, kept so a test can read what a call reported.

    A list that is also callable: the tuner calls it, the assertions read it.
    Always truthy - `LLM.chat` guards every line with `if self.log:`, so a
    FRESH (empty) log would silently swallow the very hit lines a test then
    looks for, and the failure would read as a store bug."""
    class Log(list):
        def __call__(self, line):
            self.append(line)
        def __bool__(self):
            return True
    return Log()


class Resp:
    """A completed HTTP response: bytes through the context manager, and
    through `json.load(fp)` (which only ever calls `.read`)."""

    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self):
        return self._body

    def close(self):
        """`urllib.error.HTTPError` inherits `tempfile._TemporaryFileWrapper`,
        so a test that hands one of these as an error body sees `fp.close()`
        at GC time: without this an UNRAISABLE AttributeError surfaces during
        pytest's own report formatting and aborts the whole run."""


class HTTP:
    """`urllib.request.urlopen` as a scripted recorder: one canned reply per
    call, in order. An `Exception` entry raises (so a wrapper's own
    failure path is exercised); running out of replies fails the test loudly."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []          # (url, data, headers) as sent

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", None) or str(req)
        self.calls.append((url, getattr(req, "data", None),
                           dict(getattr(req, "headers", {}) or {})))
        if not self.replies:
            raise AssertionError(f"unexpected HTTP call #{len(self.calls)}: {url[:90]}")
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return Resp(item)

    @property
    def n(self):
        return len(self.calls)

    def body(self, i=0):
        """The form/JSON body of call `i`, decoded."""
        return self.calls[i][1].decode()


@pytest.fixture
def nosleep(monkeypatch):
    """`time.sleep` recorded, never spent (wrappers back off on 429s)."""
    spent = []
    monkeypatch.setattr(time, "sleep", spent.append)
    return spent


# ------------------------------------------------------------------ the store

def test_answers_land_in_the_media_cache_root(cache, tune, log):
    """One root, so one cap and one backup story: the answers sit beside the
    source blobs under `TUNESTER_CACHE`, in their own subtrees."""
    lk = tune["lookups"]
    assert lk.cache_root() == tune["pipeline"].CACHE_DEFAULT
    assert (lk.API_STORE, lk.TEXT_STORE, lk.LLM_STORE) == \
        ("lookups", "nzb-text", "llm")
    lk.put(lk.API_STORE, lk.key("mb", "u"), "reference", "u", b"{}", None, log)
    lk.put(lk.LLM_STORE, lk.key("m", "p"), "llm", "h", b"ok", 3600.0, log)
    assert (cache / "lookups").is_dir() and (cache / "llm").is_dir()
    assert (cache / "lookups" / f"{lk.key('mb', 'u')}.bin").read_bytes() == b"{}"


def test_put_then_get_round_trips_with_provenance(cache, tune, log):
    """The sidecar says WHERE an answer came from and how long it is good
    for, so the store can be inspected and an entry audited."""
    lk = tune["lookups"]
    entry = lk.key("ao", "https://archive.org/metadata/x")
    assert lk.put(lk.API_STORE, entry, "reference", "https://archive.org/x",
                  b'{"files": []}', 72 * 3600.0, log) is True
    got = lk.get(lk.API_STORE, entry, 72 * 3600.0)
    assert got == b'{"files": []}'
    meta = json.loads((cache / "lookups" / f"{entry}.json").read_text())
    assert meta["kind"] == "reference" and meta["ttl"] == 72 * 3600.0
    assert meta["source"].endswith("/x") and meta["bytes"] == 13
    assert time.time() - meta["saved"] < 60


def test_a_hit_refreshes_the_entry_so_lru_keeps_what_is_used(cache, tune,
                                                             log):
    lk = tune["lookups"]
    entry = lk.key("mb", "u1")
    lk.put(lk.API_STORE, entry, "reference", "u1", b"old", None, log)
    blob = cache / "lookups" / f"{entry}.bin"
    old = blob.stat().st_mtime - 10_000
    os.utime(blob, (old, old))
    assert lk.get(lk.API_STORE, entry, None) == b"old"
    assert blob.stat().st_mtime > old


def test_an_expired_answer_is_unlinked_not_just_ignored(cache, tune, log):
    """`put` never overwrites a published blob, so an expired entry that was
    merely skipped would block its own refresh forever, silently."""
    lk = tune["lookups"]
    entry = lk.key("mb", "u2")
    lk.put(lk.API_STORE, entry, "reference", "u2", b"day-old", 3600.0, log)
    side = cache / "lookups" / f"{entry}.json"
    meta = json.loads(side.read_text())
    meta["saved"] = time.time() - 2 * 3600.0        # older than its TTL
    side.write_text(json.dumps(meta))
    assert lk.get(lk.API_STORE, entry, 3600.0) is None
    assert not blob_pair(cache, entry)              # gone, not shadowed
    # ...and the refresh the stale entry used to block now lands:
    assert lk.put(lk.API_STORE, entry, "reference", "u2", b"fresh",
                  3600.0, log) is True
    assert lk.get(lk.API_STORE, entry, 3600.0) == b"fresh"


def test_an_answer_written_under_another_ttl_is_stale(cache, tune, log,
                                                      monkeypatch):
    """The TTL is part of what the answer MEANS: an entry kept for 72 h is
    not a 60-minute answer, so moving the knob refreshes instead of serving
    something the operator has already declared too old to trust."""
    lk = tune["lookups"]
    monkeypatch.setenv("TUNESTER_LOOKUP_TTL_HOURS", "72")
    entry = lk.key("mb", "u3")
    ttl = lk.lookup_ttl("reference")
    lk.put(lk.API_STORE, entry, "reference", "u3", b"72h", ttl, log)
    monkeypatch.setenv("TUNESTER_LOOKUP_TTL_HOURS", "6")
    assert lk.get(lk.API_STORE, entry, lk.lookup_ttl("reference")) is None
    assert not blob_pair(cache, entry)


def test_ttl_zero_reads_writes_and_deletes_nothing(cache, tune, log,
                                                   monkeypatch):
    """The kill switch has to be a SWITCH: an operator who flips it off to
    watch live API calls (or to keep a service's answers fresh) must get the
    cached answers back by flipping it on again, so the switch may never
    destroy the entries it is ignoring."""
    lk = tune["lookups"]
    monkeypatch.setenv("TUNESTER_LOOKUP_TTL_HOURS", "72")
    entry = lk.key("mb", "u4")
    lk.put(lk.API_STORE, entry, "reference", "u4", b"live", 72 * 3600.0, log)
    monkeypatch.setenv("TUNESTER_LOOKUP_TTL_HOURS", "0")
    ttl = lk.lookup_ttl("reference")
    assert ttl == 0.0
    assert lk.get(lk.API_STORE, entry, ttl) is None
    assert lk.put(lk.API_STORE, entry, "reference", "u4", b"ignored", ttl,
                  log) is False
    assert blob_pair(cache, entry)                  # untouched, not wiped
    monkeypatch.setenv("TUNESTER_LOOKUP_TTL_HOURS", "72")
    assert lk.get(lk.API_STORE, entry, lk.lookup_ttl("reference")) == b"live"


def test_a_torn_or_corrupt_pair_is_dropped_so_the_next_put_wins(cache, tune,
                                                                log):
    """A run killed between the blob publish and the sidecar publish, or a
    sidecar that has stopped being JSON, must cost one fetch - not become an
    entry that answers nothing forever."""
    lk = tune["lookups"]
    d = cache / "lookups"
    d.mkdir(parents=True)
    for blob_only, bad_side in ((True, False), (False, True)):
        entry = lk.key("mb", f"torn-{blob_only}")
        (d / f"{entry}.bin").write_bytes(b"half")
        (d / f"{entry}.json").write_text(
            "" if blob_only else "{not json")
        assert lk.get(lk.API_STORE, entry, None) is None
        assert not blob_pair(cache, entry)
    assert lk.put(lk.API_STORE, lk.key("mb", "after"), "reference", "a",
                  b"ok", None, log) is True


def test_keys_separate_parts_that_concatenate_the_same_way(tune):
    lk = tune["lookups"]
    assert lk.key("ab", "c") != lk.key("a", "bc")
    assert lk.key("a", "b") == lk.key("a", "b")


def test_enabled_scope_switches_the_store_off_without_deleting_it(cache,
                                                                  tune, log):
    lk = tune["lookups"]
    entry = lk.key("mb", "scoped")
    lk.put(lk.API_STORE, entry, "reference", "s", b"kept", None, log)
    with lk.enabled_scope(False):
        assert lk.get(lk.API_STORE, entry, None) is None
        assert lk.put(lk.API_STORE, entry, "reference", "s", b"new", None,
                      log) is False
        assert lk.lookup_ttl("reference") == 0.0
        assert lk.lookup_ttl("search") == 0.0
        assert lk.lookup_ttl("text") == 0.0
        assert lk.lookup_ttl("llm") == 0.0
        with pytest.raises(RuntimeError):
            raise RuntimeError("a run that died inside the scope")
    assert lk.get(lk.API_STORE, entry, None) == b"kept"


def test_cached_publishes_only_a_successful_fetch(cache, tune, log):
    """`fetch` returns None for every kind of failure; nothing may be stored."""
    lk = tune["lookups"]
    parts = ("mb", "http://x/one")
    assert lk.cached("reference", parts, lambda: None, log, source="x") == \
        (False, None)
    assert not blob_pair(cache, lk.key(*parts))
    hit, got = lk.cached("reference", parts, lambda: b"answer", log, source="x")
    assert (hit, got) == (False, b"answer")
    hit, got = lk.cached("reference", parts, lambda: b"never-called", log,
                         source="x")
    assert (hit, got) == (True, b"answer")
    assert "lookup cache hit (reference)" in " ".join(log)


def test_drop_key_forgets_the_answer_that_a_wrapper_could_not_parse(cache,
                                                                    tune, log):
    lk = tune["lookups"]
    parts = ("ao", "http://x/two")
    lk.cached("reference", parts, lambda: b"garbage-not-json", log, source="x")
    assert lk.get(lk.API_STORE, lk.key(*parts), lk.lookup_ttl("reference")) \
        == b"garbage-not-json"
    lk.drop_key(lk.API_STORE, parts)
    assert lk.get(lk.API_STORE, lk.key(*parts), lk.lookup_ttl("reference")) \
        is None
    n = 0
    def fetch():
        nonlocal n
        n += 1
        return b'{"ok": 1}'
    assert lk.cached("reference", parts, fetch, log)[1] == b'{"ok": 1}'
    assert n == 1


def blob_pair(cache, entry):
    """True when an entry's blob+sidecar still exist."""
    d = cache
    return any((sub / f"{entry}.bin").exists() or (sub / f"{entry}.json").exists()
               for sub in (d / "lookups", d / "nzb-text", d / "llm"))




# ------------------------------------------------------------- MusicBrainz

def test_a_repeat_musicbrainz_query_never_reaches_the_api(cache, tune, log,
                                                          nosleep, monkeypatch):
    """The hub's re-tune path IS this call pattern: a GrabbyTube batch tunes
    the same songs again, and every derivation (studio_year, album, cd_year,
    the duration anchor, per-candidate recording queries) re-asks identical
    bytes. A live entry short-circuits BEFORE the 1 req/s spacing gate, so
    the hit costs neither a request nor the gate's sleep."""
    lk, pipe = tune["lookups"], tune["pipeline"]
    monkeypatch.setattr(pipe.urllib.request, "urlopen",
                        http := HTTP(b'{"a": 1}', b'{"a": 2}'))
    monkeypatch.setattr(pipe, "_MB_LAST", [time.time()])  # a miss WOULD space
    path = "/recording?query=offspring+self+esteem&fmt=json"
    first = pipe._mb_get(path, log)
    assert first == {"a": 1}
    spaced = len(nosleep)                          # the gate paid on the miss
    assert spaced >= 1 and http.n == 1
    assert pipe._mb_get(path, log) == {"a": 1}     # cached, not re-asked
    assert http.n == 1 and len(nosleep) == spaced  # no request, no spacing
    entry = lk.key("mb", pipe.MB_BASE + path)
    meta = json.loads((cache / "lookups" / f"{entry}.json").read_text())
    assert meta["source"] == pipe.MB_BASE + path
    assert meta["ttl"] == lk.lookup_ttl("reference")


def test_an_unreachable_musicbrainz_publishes_nothing(cache, tune, log,
                                                       nosleep, monkeypatch):
    """`returned None` must never be stored: one 502 or DNS failure would
    otherwise read as "MusicBrainz has no such recording" for the whole TTL
    and silently starve every duration anchor downstream."""
    lk, pipe = tune["lookups"], tune["pipeline"]
    monkeypatch.setattr(pipe.urllib.request, "urlopen",
                        HTTP(urllib.error.URLError("connection refused"),
                             b'{"a": 3}'))
    path = "/release?query=smash"
    assert pipe._mb_get(path, log) is None
    assert not blob_pair(cache, lk.key("mb", pipe.MB_BASE + path))
    assert pipe._mb_get(path, log) == {"a": 3}     # the failure healed itself


def test_a_non_json_musicbrainz_reply_forgets_its_own_entry(cache, tune, log,
                                                            nosleep, monkeypatch):
    """A body that will not parse is a damaged entry as often as a broken
    server, and trusting it forever would be worse than one more fetch."""
    lk, pipe = tune["lookups"], tune["pipeline"]
    monkeypatch.setattr(pipe.urllib.request, "urlopen",
                        HTTP(b"<html>rate limited</html>", b'{"a": 4}'))
    path = "/recording?query=x"
    assert pipe._mb_get(path, log) is None
    assert not blob_pair(cache, lk.key("mb", pipe.MB_BASE + path))
    assert "reply was not JSON" in " ".join(log)
    assert pipe._mb_get(path, log) == {"a": 4}


def test_lookup_ttl_zero_sends_every_musicbrainz_call_live(cache, tune, log,
                                                           nosleep, monkeypatch):
    lookups, pipe = tune["lookups"], tune["pipeline"]
    monkeypatch.setenv("TUNESTER_LOOKUP_TTL_HOURS", "0")
    monkeypatch.setattr(pipe.urllib.request, "urlopen",
                        http := HTTP(b'{"a": 1}', b'{"a": 1}'))
    path = "/recording?query=kill+switch"
    assert pipe._mb_get(path, log) == {"a": 1}
    assert pipe._mb_get(path, log) == {"a": 1}
    assert http.n == 2                             # both calls went out
    assert not blob_pair(cache, lookups.key("mb", pipe.MB_BASE + path))




# ------------------------------------------------------------------ AcoustID

def acoustid(tune, monkeypatch, *replies):
    """Wire the fingerprint API: recorder + a key (the gate never runs without
    one), so a test sees exactly what a lookup posts and stores."""
    pipe = tune["pipeline"]
    monkeypatch.setattr(pipe, "ACOUSTID_KEY", "k1")
    monkeypatch.setattr(pipe.urllib.request, "urlopen",
                        http := HTTP(*replies))
    return http


def ok(results):
    return json.dumps({"status": "ok", "results": results}).encode()


def test_an_acoustid_lookup_posts_the_fingerprint_form_and_caches_it(
        cache, tune, log, nosleep, monkeypatch):
    """The regression this locks: the form body is built OUTSIDE the retry
    closure (the stored identity omits `client` on purpose), and wrapping the
    request in a closure once moved the only `body =` assignment into a path
    that raised UnboundLocalError - inside `except Exception`, so EVERY
    lookup answered "the service never responded", the fingerprint gate read
    that as "no verdict", and every file passed unjudged."""
    lk, pipe = tune["lookups"], tune["pipeline"]
    http = acoustid(tune, monkeypatch,
                    ok([{"score": 0.99, "recordings": [{"title": "Studio"}]}]))
    fp = "AQADtkbYjZcS" * 4
    got = pipe._acoustid_lookup(fp, 254, log)
    assert got == [{"score": 0.99, "recordings": [{"title": "Studio"}]}]
    assert http.n == 1
    sent = dict(p.split("=", 1) for p in http.body().split("&"))
    assert sent["client"] == "k1" and sent["fingerprint"] == fp
    assert sent["duration"] == "254" and sent["meta"] == "recordings"
    assert log == []               # a fresh lookup has nothing to report

    # The second call is the same QUESTION, so it costs nothing - and an
    # empty result set is a real verdict ("nobody submitted these bytes"),
    # not a miss to re-ask.
    assert pipe._acoustid_lookup(fp, 254, log) == \
        [{"score": 0.99, "recordings": [{"title": "Studio"}]}]
    assert http.n == 1 and nosleep == []
    entry = lk.key("acoustid", pipe.ACOUSTID_HOST, fp, "254")
    meta = json.loads((cache / "lookups" / f"{entry}.json").read_text())
    assert meta["ttl"] == lk.lookup_ttl("reference")


def test_a_rotated_acoustid_key_reuses_the_answers_it_did_not_buy(
        cache, tune, log, nosleep, monkeypatch):
    """`client` spends quota, it does not change the answer: a stored verdict
    must still serve after the key changes, or a rotation silently re-pays
    every fingerprint the box has ever checked."""
    lk, pipe = tune["lookups"], tune["pipeline"]
    fp = "AQADtX8si" * 5
    http = acoustid(tune, monkeypatch, ok([{"score": 1.0}]))
    assert pipe._acoustid_lookup(fp, 215, log) == [{"score": 1.0}]
    monkeypatch.setattr(pipe, "ACOUSTID_KEY", "a-brand-new-key")
    assert pipe._acoustid_lookup(fp, 215, log) == [{"score": 1.0}]
    assert http.n == 1


def test_a_queued_acoustid_never_becomes_a_stored_verdict(cache, tune, log,
                                                          nosleep, monkeypatch):
    """`queued` is AcoustID's under-load answer. Reading it as "no results"
    condemns a good file; freezing it in the store condemns every later run,
    so four queued retries publish NOTHING and the next call starts clean."""
    lk, pipe = tune["lookups"], tune["pipeline"]
    queued = json.dumps({"status": "queued", "results": []}).encode()
    fp = "AQADsclGj" * 4
    http = acoustid(tune, monkeypatch, *[queued] * 4, ok([{"score": 0.9}]))
    assert pipe._acoustid_lookup(fp, 227, log) is None
    entry = lk.key("acoustid", pipe.ACOUSTID_HOST, fp, "227")
    assert not blob_pair(cache, entry)
    assert http.n == 4 and len(nosleep) == 4
    # The service is answering now: the verdict lands, and lands ONCE.
    assert pipe._acoustid_lookup(fp, 227, log) == [{"score": 0.9}]
    assert http.n == 5
    assert lk.get(lk.API_STORE, entry, lk.lookup_ttl("reference")) is not None


def test_an_error_status_acoustid_answer_publishes_nothing(cache, tune, log,
                                                           nosleep, monkeypatch):
    """A bad key is a SERVICE failure (None = "we do not know"), never a
    content verdict against the file, and it must not be remembered."""
    lk, pipe = tune["lookups"], tune["pipeline"]
    fp = "AQADmvbYc" * 4
    http = acoustid(tune, monkeypatch,
                    json.dumps({"status": "error", "error": {"code": 1}}).encode(),
                    ok([{"score": 0.5}]))
    assert pipe._acoustid_lookup(fp, 200, log) is None
    assert not blob_pair(cache, lk.key("acoustid", pipe.ACOUSTID_HOST, fp, "200"))
    assert http.n == 1


def test_an_unparseable_acoustid_reply_refetches_instead_of_stalling(
        cache, tune, log, nosleep, monkeypatch):
    """A stored body that no longer parses is dropped and reported, so one
    torn entry costs a fetch, not the whole fingerprint gate."""
    lk, pipe = tune["lookups"], tune["pipeline"]
    fp = "AQADc2Vn" * 4
    entry = lk.key("acoustid", pipe.ACOUSTID_HOST, fp, "234")
    d = cache / "lookups"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{entry}.bin").write_bytes(b"<html>proxy intercepted us</html>")
    (d / f"{entry}.json").write_text(json.dumps(
        {"kind": "reference", "source": "api", "ttl": lk.lookup_ttl("reference"),
         "saved": time.time(), "bytes": 30}))
    http = acoustid(tune, monkeypatch, ok([{"score": 0.98}]),
                    ok([{"score": 0.42}]))
    # The corrupt hit costs THIS call the verdict (None = "we do not know",
    # which never condemns a file) and unlinks the entry on the way out.
    assert pipe._acoustid_lookup(fp, 234, log) is None
    assert "cached reply was not JSON" in " ".join(log)
    assert http.n == 0
    assert pipe._acoustid_lookup(fp, 234, log) == [{"score": 0.98}]
    assert http.n == 1                     # healed by the refetch
    assert pipe._acoustid_lookup(fp, 234, log) == [{"score": 0.98}]
    assert http.n == 1


# ----------------------------------------------------------------- listings

def item(n: int) -> str:
    """One Newznab `<item>` with the shape `_nzb_query` parses (title, a
    `details/<hash>` link the release URL is read from, a pubDate)."""
    return (f"<item><title>Rip {n}: Artist - Album</title>"
            f"<link>https://nzb.life/release/details/{n:08x}</link>"
            f"<pubDate>Tue, 18 Nov 2026 00:00:0{n % 10} +0000</pubDate></item>")


def full_page() -> str:
    """100 items: nzb.life's page cap, so the caller keeps paging."""
    return "<feed>" + "".join(item(i) for i in range(1, 101)) + "</feed>"


class Fetches:
    """`sources._nzb_get` as a recorder: one canned reply (or raised error)
    per call, and the URLs it was asked for."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.urls = []

    def __call__(self, url, timeout, tries=4):
        self.urls.append(url)
        if not self.replies:
            raise AssertionError(f"unexpected nzb.life call #{len(self.urls)}")
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item.encode() if isinstance(item, str) else item

    @property
    def n(self):
        return len(self.urls)


def test_an_nzb_life_page_is_asked_for_once(cache, tune, log, monkeypatch):
    """A compile scan re-runs the same artist browse on every tune that gets
    to that layer, and nzb.life's WAF rate-limits hard - the paging browse is
    the one listing a re-tune re-asks verbatim."""
    src = tune["sources"]
    monkeypatch.setattr(src, "_nzb_get", get := Fetches(full_page(), item(1)))
    first = src._nzb_query("the crystal method", pages=2, log=log)
    assert len(first) == 101                      # paged past the 100 cap
    assert get.n == 2 and "offset=100" in get.urls[1]
    again = src._nzb_query("the crystal method", pages=2, log=log)
    assert [c.url for c in again] == [c.url for c in first]
    assert get.n == 2                             # both pages came from the store
    assert "nzb.life search failed" not in " ".join(log)


def test_an_nzb_life_query_text_shares_and_splits_its_own_keys(
        cache, tune, log, monkeypatch):
    """Apostrophes are removed for the API (both nzb.life and archive.org
    tokenize on them, which drowns `Dead Man's Party` in noise), and the
    stored identity is the URL AS SENT - so `Artist's` and `Artists` are the
    same answer (one spend, not two) while a yeared query is a different one.
    The year is a real relevance hint (scene titles carry the pressing year),
    so collapsing those two would tune every yeared search off the bare list."""
    src = tune["sources"]
    get = Fetches(item(1), item(2))
    monkeypatch.setattr(src, "_nzb_get", get)
    src._nzb_query("dead man's party", log=log)
    src._nzb_query("dead mans party", log=log)
    assert get.n == 1, "the same query as sent must not be asked twice"
    assert "1997" not in get.urls[0]
    src._nzb_query("dead mans party 1997", log=log)
    assert get.n == 2 and "1997" in get.urls[1]


def test_a_blocked_nzb_life_page_publishes_no_emptiness(cache, tune, log,
                                                        monkeypatch):
    """A 429/WAF block is the index NOT answering. Freezing its empty body
    as "no releases exist" would hide every real rip for the whole TTL."""
    src = tune["sources"]
    monkeypatch.setattr(src, "_nzb_get",
                        get := Fetches(RuntimeError("Already Downloaded"),
                                       item(7)))
    assert src._nzb_query("artist song", log=log) == []
    assert "nzb.life search failed" in " ".join(log)
    assert src._nzb_query("artist song", log=log)[0].title == "Rip 7: Artist - Album"
    assert get.n == 2


def test_archive_org_metadata_outlives_its_own_search_page(cache, tune, log,
                                                           monkeypatch):
    """The metadata call is the expensive half of an archive search (one GET
    per candidate item) and an item's file list barely moves; the search
    page IS a listing. Different kinds, so `TUNESTER_SEARCH_TTL_MINUTES=0`
    takes the listings live WITHOUT re-paying metadata."""
    src = tune["sources"]
    monkeypatch.setenv("TUNESTER_SEARCH_TTL_MINUTES", "0")
    monkeypatch.setattr(src.urllib.request, "urlopen",
                        http := HTTP(b'{"files": [1,2,3]}', b'{"matches": []}',
                                     b'{"matches": [9]}'))
    meta = "https://archive.org/metadata/boingo-alive"
    assert src._cached_json(meta, "reference", log) == {"files": [1, 2, 3]}
    assert src._cached_json(meta, "reference", log) == {"files": [1, 2, 3]}
    assert http.n == 1
    listing = "https://archive.org/advancedsearch?q=boingo"
    assert src._cached_json(listing, "search", log) == {"matches": []}
    assert http.n == 2
    assert src._cached_json(listing, "search", log) == {"matches": [9]}
    assert http.n == 3                            # listings stay live at TTL 0


def test_an_unparseable_archive_org_reply_forgets_its_own_entry(cache, tune,
                                                                 log, monkeypatch):
    """archive.org answers a rate-limit with an HTML page and HTTP 200, so a
    200-that-is-not-JSON is published by the fetch and rejected by the
    parser: the entry has to be dropped, or the same HTML page reads as
    "this item has no files" for the whole TTL."""
    lk, src = tune["lookups"], tune["sources"]
    monkeypatch.setattr(src.urllib.request, "urlopen",
                        http := HTTP(b"<html>429 slow down</html>",
                                     b'{"files": ["a"]}'))
    url = "https://archive.org/metadata/rate-limited-item"
    assert src._cached_json(url, "reference", log) is None
    assert "reply was not JSON" in " ".join(log)
    assert not blob_pair(cache, lk.key("ao", url))
    assert src._cached_json(url, "reference", log) == {"files": ["a"]}
    assert http.n == 2


# ---------------------------------------------------------------------- model

def chat(tune, monkeypatch, log, *replies, api_key="k1", model="m-a",
         base="https://llm.test/v1"):
    """`LLM.chat` wired to canned replies: returns (endpoint, recorder, ask).

    The backend is passed in explicitly so a test never depends on which
    `.env` the module happened to read, and `ask()` defaults to the same
    (system, user, max_tokens) on every call."""
    lm = tune["llm"]
    http = HTTP(*replies)
    monkeypatch.setattr(lm.urllib.request, "urlopen", http)
    ep = lm.LLMEndpoint("primary", base, api_key, model)
    return ep, http, lambda system="SYS", user="USR", max_tokens=1500: \
        lm.LLM(log=log, endpoints=[ep]).chat(system, user, max_tokens)


def reply(text):
    return json.dumps(
        {"choices": [{"message": {"content": text}}]}).encode()


def http_error(code, detail):
    return urllib.error.HTTPError("https://llm.test/v1/chat/completions",
                                  code, "err", {}, Resp(detail.encode()))


def test_a_repeat_verdict_on_the_same_prompt_never_reaches_the_gpu(
        cache, tune, log, nosleep, monkeypatch):
    """`temperature 0` plus a fixed prompt makes the QC gate a pure function,
    and a library re-tune asks the SAME question about the SAME candidate.
    The store holds the reply TEXT, so a hit costs no GPU time at all."""
    lk = tune["lookups"]
    ep, http, ask = chat(tune, monkeypatch, log, reply("STUDIO"),
                         reply("should not be spent"))
    assert ask() == "STUDIO"
    assert ask() == "STUDIO"
    assert http.n == 1, "the second identical verdict re-paid the model"
    body = json.loads(http.body())
    assert body["temperature"] == 0.0 and body["stream"] is False
    entry = lk.key(ep.base_url, "m-a", "1500", "SYS", "USR")
    assert (cache / "llm" / f"{entry}.bin").read_bytes() == b"STUDIO"
    meta = json.loads((cache / "llm" / f"{entry}.json").read_text())
    assert meta["kind"] == "llm" and meta["ttl"] == lk.lookup_ttl("llm")
    assert "lookup cache hit (llm)" in " ".join(log)


def test_a_stored_verdict_answers_even_with_the_gpu_peer_down(
        cache, tune, log, nosleep, monkeypatch):
    """The answer outlives the box that produced it: a weekend re-tune does
    not care whether the inference host is up."""
    lk, lm = tune["lookups"], tune["llm"]
    entry = lk.key("https://llm.test/v1", "m-a", "1500", "SYS", "USR")
    lk.put(lk.LLM_STORE, entry, "llm", "https://llm.test/v1", b"CACHED",
           lk.lookup_ttl("llm"), log)
    http = HTTP(urllib.error.URLError(ConnectionRefusedError()))
    monkeypatch.setattr(lm.urllib.request, "urlopen", http)
    ep = lm.LLMEndpoint("primary", "https://llm.test/v1", "k1", "m-a")
    assert lm.LLM(log=log, endpoints=[ep]).chat("SYS", "USR") == "CACHED"
    assert http.n == 0


def test_a_different_question_or_model_is_its_own_answer(cache, tune, log,
                                                        nosleep, monkeypatch):
    """Every part of the reply's identity is in the key, and the HOST is one
    of them: this box's GPU peer answers a model id it does not serve by
    ALIASING it, so the same id on another base URL is a different model.
    The api key is deliberately not one - it authorizes, it does not answer."""
    lk = tune["lookups"]
    ep, http, ask = chat(tune, monkeypatch, log,
                         reply("A"), reply("B"), reply("C"), reply("D"))
    assert ask() == "A"                                 # SYS / USR
    assert ask(user="OTHER") == "B"                      # different prompt
    assert ask(max_tokens=900) == "C"                    # different sampling
    assert ask(system="OTH") == "D"                      # a different SYSTEM
    assert http.n == 4                                   # prompt is its own key
    assert lk.get(lk.LLM_STORE, lk.key(ep.base_url, "m-a", "1500", "SYS",
                                       "OTHER"), lk.lookup_ttl("llm")) == b"B"
    ep2, http2, ask2 = chat(tune, monkeypatch, log, reply("E"), api_key="k2")
    assert ask2() == "A" == ask2()                        # rotated key: a hit
    assert http2.n == 0, "a rotated key re-bought a verdict it already had"
    assert "lookup cache hit (llm)" in " ".join(log), \
        "a hit that reports nothing is indistinguishable from a fresh spend"
    ep3, http3, ask3 = chat(tune, monkeypatch, log, reply("F"),
                            base="https://other.test/v1")
    assert ask3() == "F"                                 # other host: fresh spend
    assert http3.n == 1


def test_a_model_id_the_server_aliases_gets_its_own_entry(
        cache, tune, log, nosleep, monkeypatch):
    """A 404 on the configured id is answered with the server's real id. The
    re-derived key means the stored entry never claims to be an answer from
    a model that never produced it."""
    lk, lm = tune["lookups"], tune["llm"]
    ep, http, ask = chat(tune, monkeypatch, log,
                         http_error(404, '{"error":"model not found"}'),
                         json.dumps({"data": [{"id": "qwen-real"}]}).encode(),
                         reply("STUDIO"))
    assert ask() == "STUDIO"
    assert http.n == 3                       # 404, /models, the real chat
    real = lk.key(ep.base_url, "qwen-real", "1500", "SYS", "USR")
    wrong = lk.key(ep.base_url, "m-a", "1500", "SYS", "USR")
    assert (cache / "llm" / f"{real}.bin").read_bytes() == b"STUDIO"
    assert not (cache / "llm" / f"{wrong}.bin").exists()
    assert ep.model == "qwen-real"
    assert ask() == "STUDIO" and http.n == 3 # the RESOLVED id now hits


def test_a_cold_start_or_a_dead_backend_publishes_no_verdict(
        cache, tune, log, nosleep, monkeypatch):
    """A 503 "loading model" is waited out; an empty reply is a failure.
    Neither may be stored, or one bad moment would answer every later run
    with bytes no model ever chose."""
    lk, lm = tune["lookups"], tune["llm"]
    ep, http, ask = chat(tune, monkeypatch, log,
                         http_error(503, "model is loading"), reply("OK"))
    assert ask() == "OK"
    assert http.n == 2 and nosleep == [5]     # retried, not failed over
    # An empty reply is a FAILURE, not a late answer: no `content` and no
    # `reasoning_content` raises inside the call, and raises WITHOUT
    # publishing - so the next run re-asks instead of inheriting bytes no
    # model ever chose. (A 503 above retried; this is the other half.)
    ep, http, ask = chat(tune, monkeypatch, log,
                         json.dumps({"choices": [{"message": {}}]}).encode())
    with pytest.raises(lm.LLMError):
        ask(user="DIFFERENT")                # its own key: no scenario-1 hit
    assert http.n == 1                         # one POST, no retry, no store
    assert len([p for p in (cache / "llm").glob("*.bin")]) == 1  # scenario 1's


def test_llm_ttl_zero_sends_every_verdict_live(cache, tune, log, nosleep,
                                               monkeypatch):
    lk = tune["lookups"]
    monkeypatch.setenv("TUNESTER_LLM_TTL_HOURS", "0")
    ep, http, ask = chat(tune, monkeypatch, log, reply("A"), reply("B"))
    assert ask() == "A"
    assert ask() == "B"
    assert http.n == 2
    assert not list((cache / "llm").glob("*.bin"))
    assert lk.lookup_ttl("llm") == 0


# ----------------------------------------------------------------- nntp text

def yenc(payload: bytes) -> list[bytes]:
    """One raw-8bit yEnc article body as NNTP hands it to `conn.body().lines`:
    the =ybegin banner, the escaped+shifted bytes, the =yend trailer with the
    part CRC `_yenc_part` checks."""
    out = bytearray()
    for b in payload:
        v = (b + 0x2A) & 0xFF
        if v in (0, 9, 10, 13, 61):
            out += bytes((0x3D, (v + 0x40) & 0xFF))
        else:
            out.append(v)
    return ([b"=ybegin line=128 size=%d name=tracklist.txt" % len(payload)]
            + [out[i:i + 128] for i in range(0, len(out), 128)]
            + [b"=yend size=%d pcrc32=%08x"
               % (len(payload), zlib.crc32(payload))])


class NNTP:
    """`nntplib.NNTP_SSL` as a recorder: every connection, group select and
    article body is counted, so a test can see the round-trip it refused to
    pay twice. An article body of `None` is a part fetch failure."""

    def __init__(self, articles, fail=()):
        self.articles = articles      # msgid -> payload bytes
        self.fail = set(fail)
        self.conns: list[tuple] = []
        self.groups: list[str] = []
        self.bodies: list[str] = []

    def __call__(self, host, port, user=None, password=None, timeout=None):
        self.conns.append((host, port, user, password))
        return self

    def group(self, grp):
        self.groups.append(grp)

    def body(self, mid):
        self.bodies.append(mid)             # recorded EXACTLY as fetched
        # The tuner asks for the article as `<msgid>`, which is what a real
        # server wants, so the lookup strips the brackets nntplib does not
        # add itself. A fake that compared the raw argument against the
        # declared ids missed EVERY article: each read "failed", no
        # tracklist was ever returned, and nothing was ever published.
        key = mid.strip("<>")
        if key not in self.articles:
            raise OSError("no such article")
        return None, types.SimpleNamespace(
            lines=yenc(self.articles[key]))

    def quit(self):
        pass


def nntp(tune, monkeypatch, articles, fail=()):
    """Install a fake `nntplib` (absent from this Python) that answers only
    the message-ids a test declares."""
    mod = types.ModuleType("nntplib")
    rec = NNTP(articles, fail)
    mod.NNTP_SSL = rec
    monkeypatch.setitem(sys.modules, "nntplib", mod)
    return rec


TRACKS = (b"1 - Oingo Boingo - Home Alone\r\n"
          b"2 - Oingo Boingo - Goodbye Goodbye\r\n")


def sidecar_files(msgids, ext="txt", total=90, segs=None):
    """The `(subject, group, [(msgid, segment BYTES)])` shape
    `_nzb_release_files` produces, with one small text sidecar - the only
    thing a 120 MB hash-volume box costs a compilation scan. Segment sizes
    are the per-article byte counts the NZB carries, because the sidecar cap
    (`NZB_SIDE_MAX`) SUMS them: a test that fills the slot with anything
    else is testing a shape the release never has."""
    subj = f"[PRiVATE]-[EnCrYpTnZb]-[29-oingo_boingo-goodbye-goodbye-marr.{ext}]"
    if segs is None:
        segs = [(m, max(1, total // len(msgids))) for m in msgids]
    return [(subj, "alt.binaries.music.edm", segs)]


def test_a_release_tracklist_is_read_off_nntp_once_and_never_expires(
        cache, tune, log, monkeypatch):
    """Usenet articles are immutable once posted, so a release's tracklist
    has NO expiry: the multi-part NNTP round-trip a compilation scan pays
    per candidate is paid once per release forever - even after every TTL
    knob has aged out and the reference store has been emptied."""
    lk, pipe = tune["lookups"], tune["pipeline"]
    n = nntp(tune, monkeypatch, {"aa@grp": TRACKS})
    files = sidecar_files(["aa@grp"])
    first = pipe._nzb_sidecar_text(files, log, "https://api.nzb.life/release/details/aaa")
    assert b"Goodbye Goodbye" in (first or "").encode()
    assert n.conns and n.bodies == ["<aa@grp>"]
    entry = lk.key("nzbtext", "alt.binaries.music.edm", "aa@grp")
    meta = json.loads((cache / "nzb-text" / f"{entry}.json").read_text())
    assert meta["ttl"] is None, "an immutable posting must not carry an expiry"
    assert meta["source"] == "https://api.nzb.life/release/details/aaa"
    monkeypatch.setenv("TUNESTER_LOOKUP_TTL_HOURS", "1")  # reference store ages
    again = pipe._nzb_sidecar_text(files, log, "https://api.nzb.life/release/details/aaa")
    assert again == first
    assert n.bodies == ["<aa@grp>"], "the tracklist was fetched from NNTP twice"
    assert "reading tracklist" in " ".join(log)
    assert "lookup cache hit (text)" in " ".join(log)
    # Age the reference store out entirely; the text entry still answers.
    monkeypatch.setenv("TUNESTER_LOOKUP_TTL_HOURS", "72")
    assert pipe._nzb_sidecar_text(files, log) == first


def test_the_same_posting_reached_from_another_release_is_the_same_answer(
        cache, tune, log, monkeypatch):
    """The identity is the GROUP plus the exact message-ids, NOT the nzb.life
    release id: the same box posted once, and a comp scan and an album pluck
    reach it through different release pages."""
    lk, pipe = tune["lookups"], tune["pipeline"]
    n = nntp(tune, monkeypatch, {"aa@grp": TRACKS, "bb@grp": b"other\r\n"})
    files = sidecar_files(["aa@grp"])
    pipe._nzb_sidecar_text(files, log, "https://api.nzb.life/release/details/aaa")
    got = pipe._nzb_sidecar_text(files, log, "https://api.nzb.life/release/details/bbb")
    assert b"Goodbye Goodbye" in got.encode() and n.bodies == ["<aa@grp>"]
    # A different posting IS a different answer, even for the same subject.
    pipe._nzb_sidecar_text(sidecar_files(["bb@grp"]), log, "https://x/details/c")
    assert n.bodies == ["<aa@grp>", "<bb@grp>"]
    assert lk.lookup_ttl("text") is None


def test_a_part_that_fails_to_fetch_publishes_no_tracklist(
        cache, tune, log, monkeypatch):
    """A torn multi-part read must not be stored: freezing half a tracklist
    would decide every later scan of this release, forever (no expiry)."""
    lk, pipe = tune["lookups"], tune["pipeline"]
    n = nntp(tune, monkeypatch, {"aa@grp": TRACKS})
    # ONE file posted in two parts, the second article gone from the server
    files = sidecar_files([], segs=[("aa@grp", 45), ("missing@grp", 45)])
    assert pipe._nzb_sidecar_text(files, log, "https://x/details/a") is None
    assert "sidecar part fetch failed" in " ".join(log)
    assert not list((cache / "nzb-text").glob("*.bin"))
    assert pipe._nzb_sidecar_text(files, log, "https://x/details/a") is None
    assert len(n.bodies) == 4, "the failed read was cached as emptiness"


def test_lookup_ttl_zero_sends_the_tracklist_reads_live(
        cache, tune, log, monkeypatch):
    """`TUNESTER_LOOKUP_TTL_HOURS=0` is ONE switch for the whole store: the
    no-expiry text subtree goes live with the API answers it shares it with."""
    pipe = tune["pipeline"]
    monkeypatch.setenv("TUNESTER_LOOKUP_TTL_HOURS", "0")
    n = nntp(tune, monkeypatch, {"aa@grp": TRACKS})
    files = sidecar_files(["aa@grp"])
    pipe._nzb_sidecar_text(files, log)
    pipe._nzb_sidecar_text(files, log)
    assert n.bodies == ["<aa@grp>", "<aa@grp>"]
    assert not list((cache / "nzb-text").glob("*.bin"))


def test_a_release_with_no_small_text_sidecar_never_opens_nntp(
        cache, tune, log, monkeypatch):
    """Only a small text file earns a sidecar read: a 120 MB `.wav` must not
    be pulled for its header, and a `.jpg` is not a tracklist."""
    pipe = tune["pipeline"]
    n = nntp(tune, monkeypatch, {"aa@grp": TRACKS})
    assert pipe._nzb_sidecar_text(sidecar_files(["aa@grp"], ext="jpg"),
                                  log) is None
    # past NZB_SIDE_MAX (512 KB): a sidecar this big is not a tracklist
    big = sidecar_files([], segs=[("aa@grp", 4_000_000)])
    assert pipe._nzb_sidecar_text(big, log) is None
    assert n.conns == [] and n.bodies == []


def test_the_smallest_sidecar_wins_when_a_release_carries_two(
        cache, tune, log, monkeypatch):
    """The pick IS the size cap's point: a 200 KB `.nfo` beside a 900 B
    `.m3u` must cost the `.m3u`, whichever order the manifest lists them.
    Building the ranking tuple and never consulting it pulled the LAST
    qualifying sidecar instead - which also changes what gets cached, so a
    release can end up stored under the wrong posting's key."""
    pipe = tune["pipeline"]
    n = nntp(tune, monkeypatch, {"nfo@grp": b"INFO\r\n", "m3u@grp": TRACKS})
    files = [sidecar_files(["nfo@grp"], ext="nfo", total=200_000)[0],
             sidecar_files(["m3u@grp"], ext="m3u")[0]]
    got = pipe._nzb_sidecar_text(files, log, "https://x/details/a")
    assert got and "Home Alone" in got
    assert n.bodies == ["<m3u@grp>"]
    assert ".m3u" in " ".join(log)              # the log names the file pulled
    # The reversed manifest picks the SAME sidecar (and the same cache key).
    again = pipe._nzb_sidecar_text(list(reversed(files)), log,
                                   "https://x/details/a")
    assert again == got and n.bodies == ["<m3u@grp>"]


# ------------------------------------------------------------------- together

def test_the_answer_stores_count_against_the_same_cap_as_the_audio(
        cache, tune, log, monkeypatch):
    """One root, one LRU: `_cache_evict` walks every `*.bin` under the cache,
    so the KB-sized answers are dropped oldest-first with the GB-sized
    blobs instead of growing past `TUNESTER_CACHE_MAX_MB` unnoticed."""
    lk, pipe = tune["lookups"], tune["pipeline"]
    # 100+2 (answers) + 2000+2 (media) = 2104 against a 2050 cap: evicting
    # the LRU pair (102) lands exactly under it, so the test can see WHICH
    # subtree the walk chose, not merely that something moved.
    monkeypatch.setattr(pipe, "CACHE_MAX_BYTES", 2050)
    old = cache / "llm"
    old.mkdir(parents=True)
    (old / "answer.bin").write_bytes(b"x" * 100)
    (old / "answer.json").write_text("{}")
    media = cache / "sources"
    media.mkdir(parents=True)
    (media / "album.bin").write_bytes(b"y" * 2000)
    (media / "album.json").write_text("{}")
    for p in old.iterdir():              # the ANSWERS are the LRU here
        os.utime(p, (1000, 1000))
    for p in media.iterdir():            # the audio was used more recently
        os.utime(p, (2000, 2000))
    pipe._cache_evict(log)
    assert (media / "album.bin").exists()
    assert not list(old.glob("*")), "the cap evicted the newer entry first"
    assert "evicted" in " ".join(log)
