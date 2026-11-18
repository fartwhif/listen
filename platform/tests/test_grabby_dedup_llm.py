"""The duplicate hunt asks the endpoint it was pointed at.

`find_duplicates` (vendored GrabbyTube `db.py`) is the only LLM call GrabbyTube
makes: `GET /tracks/duplicates` posts the cleaned title list and parses the reply
back into duplicate groups. Two ways that call can quietly stop meaning what the
operator configured:

- A hardcoded model id. Pointing `GRABBY_LLM_BASE_URL` at a server serving a
  DIFFERENT model can still work when that server aliases unknown ids - this
  box's does: `qwen3.6-27b` against a llama.cpp instance serving exactly one
  model answers, naming the model it actually ran - so the feature looks fine
  while every request lies about which model judged the library. The fix is
  `settings.llm_model`, read from `GRABBY_LLM_MODEL`; the env NAME is what
  compose writes, so a name that failed to reach the setting would leave every
  request on the default id. The base URL and the bearer key are the same
  deployment-supplied values and must show in the request for the same reason.
- A reply with no `content`. A reasoning server that runs out of budget
  mid-thought puts the answer in `reasoning_content` only; the parse has to fall
  back to it or a good answer reports "no parseable JSON".

`aiohttp` is replaced by a recorder, so these assertions are about the request
that leaves the process and the reply that comes back: no endpoint, no network.
Needs the image's dependencies (mutagen, yt_dlp, pydantic-settings, aiohttp), so
it skips without them like the other vendored-GrabbyTube modules.
"""
import asyncio
import contextlib
import importlib
import json
import os
import sys
import types

import pytest

pytest.importorskip("mutagen")
pytest.importorskip("yt_dlp")
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

MODS = ("config", "db")


class _Response:
    status = 200

    def __init__(self, reply):
        self._reply = reply

    async def json(self):
        return self._reply


class _Post:
    def __init__(self, session, url, headers, body):
        self.session = session
        self.url = url
        self.headers = dict(headers or {})
        self.body = body

    async def __aenter__(self):
        self.session.calls.append({"url": self.url, "headers": self.headers,
                                   "body": self.body})
        return _Response(self.session.reply())

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Fake aiohttp session: `post` records the request, `reply()` supplies the
    body, so a test decides the reply while wiring the environment."""

    def __init__(self, calls, reply):
        self.calls = calls
        self.reply = reply

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None, timeout=None):
        return _Post(self, url, headers, json)


@contextlib.contextmanager
def grabby_db(monkeypatch, tmp_path, calls, reply, **extra_env):
    """Yield (db, catalog_path) with the vendored `db` imported fresh under
    `extra_env` and a recording aiohttp installed.

    Fresh per use because `config` freezes its settings object at import and
    `db` freezes CATALOG_PATH from it - which is what a process start is. The
    directory stays on `sys.path` for the whole block because `find_duplicates`
    imports its own siblings (`from config import settings`) at CALL time; the
    bare module names and `aiohttp` are restored on the way out so no later test
    reads a deleted tmpdir or keeps the fake transport.
    """
    catalog = tmp_path / "tracks.json"
    env = {"GRABBY_CATALOG_PATH": str(catalog),
           "GRABBY_DB_PATH": str(tmp_path / "grabby.db"),
           "GRABBY_OUTPUT_DIR": str(tmp_path / "cache"),
           "GRABBY_LLM_BASE_URL": "https://llm.test/v1/",
           "GRABBY_LLM_API_KEY": "sk-test-key",
           "GRABBY_LLM_MODEL": "model-under-test"}
    env.update(extra_env)
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    saved = {name: sys.modules.get(name) for name in MODS + ("aiohttp",)}
    sys.path.insert(0, GRABBY_DIR)
    sys.modules["aiohttp"] = types.SimpleNamespace(
        ClientSession=lambda *a, **k: _Session(calls, reply),
        ClientTimeout=lambda **k: k)
    for name in MODS:
        sys.modules.pop(name, None)
    try:
        yield importlib.import_module("db"), catalog
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        sys.path.remove(GRABBY_DIR)


CATALOG = (
    {"videoId": "aaa1", "author": "Artist X", "title": "Skill Issue",
     "duration": 180.0},
    {"videoId": "aaa2", "author": "Artist X",
     "title": "Skill Issue (Official Video)", "duration": 186.0},
    {"videoId": "bbb1", "author": "Artist Y", "title": "Other Song",
     "duration": 200.0},
)
LLM_JSON = json.dumps({"duplicates": [{
    "track": "Artist X - Skill Issue",
    "occurrences": [{"index": 1}, {"index": 2}]}]})
EMPTY_REPLY = {"choices": [{"message": {"content": "{}"}}]}


def hunt(monkeypatch, tmp_path, reply, **extra_env):
    """Run the real `find_duplicates` over `CATALOG` with a recording
    transport; returns (groups, calls)."""
    calls = []
    with grabby_db(monkeypatch, tmp_path, calls, lambda: reply,
                   **extra_env) as (db, catalog):
        catalog.write_text(json.dumps(list(CATALOG), indent=2))
        groups = asyncio.run(db.find_duplicates())
    return groups, calls


def test_request_carries_the_configured_endpoint_key_and_model(
        monkeypatch, tmp_path):
    """The base URL, the bearer key and the model id in the ONE POST are the
    deployment's, not baked-in constants."""
    _, calls = hunt(monkeypatch, tmp_path, EMPTY_REPLY)
    assert len(calls) == 1, "duplicate detection must make exactly one request"
    call = calls[0]
    assert call["url"] == "https://llm.test/v1/chat/completions", \
        "the configured base URL (trailing slash trimmed) + /chat/completions"
    assert call["headers"].get("Authorization") == "Bearer sk-test-key"
    assert call["body"]["model"] == "model-under-test", \
        "GRABBY_LLM_MODEL must be what the endpoint is asked to run"


def test_model_default_is_the_upstream_id_without_the_env(monkeypatch, tmp_path):
    """Unset `GRABBY_LLM_MODEL` = the id the feature was written against, so a
    standalone container keeps sending its exact previous request body."""
    _, calls = hunt(monkeypatch, tmp_path, EMPTY_REPLY, GRABBY_LLM_MODEL=None)
    assert calls[0]["body"]["model"] == "qwen3.6-27b"


def test_prompt_carries_every_library_track(monkeypatch, tmp_path):
    """The judgement input: numbered cleaned titles with artist and length. A
    dropped or renumbered line mis-attributes every group the model reports,
    because occurrences are resolved by index into the catalog."""
    _, calls = hunt(monkeypatch, tmp_path, EMPTY_REPLY)
    user = calls[0]["body"]["messages"][1]["content"]
    assert '"Skill Issue" | Artist X | 3.0m' in user
    assert '"Skill Issue (Official Video)" | Artist X' not in user, \
        "wrapper junk must be cleaned before asking"
    assert '"Other Song" | Artist Y | 3.3m' in user
    assert user.index("1. ") < user.index("2. ") < user.index("3. ")


@pytest.mark.parametrize("shape", ["content", "reasoning_content"])
def test_reply_parses_from_content_or_reasoning(monkeypatch, tmp_path, shape):
    """A reasoning model out of token budget answers with `content: null` and
    the JSON inside `reasoning_content`; both shapes must yield the group."""
    payload = {"choices": [{"message": (
        {"content": LLM_JSON} if shape == "content"
        else {"content": None, "reasoning_content": LLM_JSON})}]}
    groups, _ = hunt(monkeypatch, tmp_path, payload)
    assert [g["key"] for g in groups] == ["Artist X - Skill Issue"], shape
    assert [t["videoId"] for t in groups[0]["tracks"]] == ["aaa1", "aaa2"]


@pytest.mark.parametrize("missing", ["GRABBY_LLM_API_KEY", "GRABBY_LLM_BASE_URL"])
def test_gate_off_without_credentials_sends_nothing(monkeypatch, tmp_path,
                                                    missing):
    """No key or no URL = no request at all: an anonymous POST to a server that
    ignores auth would report groups nobody configured an LLM to produce."""
    groups, calls = hunt(monkeypatch, tmp_path, {"choices": [
        {"message": {"content": LLM_JSON}}]}, **{missing: None})
    assert groups == []
    assert calls == [], "the gate must not contact the endpoint"


def test_library_below_two_tracks_never_asks(monkeypatch, tmp_path):
    """One track (or none) has no duplicate to find; the feature used to spend a
    full completion on it."""
    for catalog in ([CATALOG[0]], []):
        calls = []
        with grabby_db(monkeypatch, tmp_path, calls,
                       lambda: {"choices": [{"message": {"content": LLM_JSON}}]}) \
                as (db, path):
            path.write_text(json.dumps(catalog, indent=2))
            assert asyncio.run(db.find_duplicates()) == []
        assert calls == [], f"catalog of {len(catalog)} track(s) must not ask"
