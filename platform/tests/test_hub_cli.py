"""`hub partition` setting spellings.

Settings are the escape hatch for anything the registry has no flag for, so the
one thing that must be true is that a typed KEY=VALUE lands in partitions.json
and nothing else reports success. Both actions used to read only one of the two
spellings argparse offers — `add` read `--set` only, `set` read positionals
only — while both printed "settings updated"/added, so `hub partition set s
--set K=v` looked applied and changed nothing. These tests pin: either spelling
works on either action, an empty `set` is an error rather than a silent no-op,
and a refused setting leaves the registry byte-identical.
"""
import json
import sys

import pytest


@pytest.fixture
def hub_env(monkeypatch, tmp_path, clean_env, reload_modules):
    """hub pointed at a tmpdir registry + identity DB, as a module import."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("HUB_DATA_DIR", str(data))
    monkeypatch.setenv("HUB_OWNER_PASSWORD", "testpassw0rd")
    # hubdb too: `partition add --owner` writes a membership through it, and
    # its DB path freezes at import time like every other platform module.
    hub = reload_modules("hubdb", "hub")[1]
    return hub, data / "partitions.json"

def run(hub, monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["hub"] + argv)
    return hub.main()


def spec_of(path, slug: str) -> dict:
    reg = json.loads(path.read_text())
    return reg["partitions"][slug]


def test_set_accepts_dash_set(hub_env, monkeypatch):
    """The reported bug: `set --set K=v` printed success and saved nothing."""
    hub, registry = hub_env
    assert run(hub, monkeypatch, ["partition", "add", "alpha", "--owner", "owner"]) == 0
    rc = run(hub, monkeypatch,
             ["partition", "set", "alpha", "--set", "GRABBY_YTDLP=/tmp/x/ytdl"])
    assert rc == 0
    assert spec_of(registry, "alpha")["settings"] == {
        "GRABBY_YTDLP": "/tmp/x/ytdl"}


def test_set_accepts_positionals(hub_env, monkeypatch):
    hub, registry = hub_env
    run(hub, monkeypatch, ["partition", "add", "alpha", "--owner", "owner"])
    rc = run(hub, monkeypatch, ["partition", "set", "alpha", "GRABBY_PORT=8113"])
    assert rc == 0
    assert spec_of(registry, "alpha")["settings"] == {"GRABBY_PORT": "8113"}


def test_add_accepts_positionals(hub_env, monkeypatch):
    """The symmetric hole: `add` used to ignore positional KEY=VALUE.

    argparse binds the contiguous positional run to (action, slug, kv) before
    flags, so the values must be typed before an option — `add beta K=v --owner
    owner` works, `add beta --owner owner K=v` is an argparse error (an intake
    quirk, not a silent drop: the CLI refuses loudly).
    """
    hub, registry = hub_env
    rc = run(hub, monkeypatch,
             ["partition", "add", "beta", "TUNESTER_X=1", "--owner", "owner"])
    assert rc == 0
    assert spec_of(registry, "beta")["settings"] == {"TUNESTER_X": "1"}


def test_both_spellings_merge(hub_env, monkeypatch):
    hub, registry = hub_env
    run(hub, monkeypatch, ["partition", "add", "alpha", "--owner", "owner"])
    rc = run(hub, monkeypatch,
             ["partition", "set", "alpha", "A=1", "--set", "B=2"])
    assert rc == 0
    assert spec_of(registry, "alpha")["settings"] == {"A": "1", "B": "2"}


def test_set_without_values_is_an_error(hub_env, monkeypatch):
    """`set` alone printed "settings updated" for a change that did not happen."""
    hub, registry = hub_env
    run(hub, monkeypatch,
        ["partition", "add", "alpha", "--owner", "owner", "--set", "A=1"])
    before = registry.read_text()
    assert run(hub, monkeypatch, ["partition", "set", "alpha"]) == 1
    assert registry.read_text() == before
    assert spec_of(registry, "alpha")["settings"] == {"A": "1"}


def test_refused_setting_leaves_registry_untouched(hub_env, monkeypatch):
    """apply_settings validates names/values before anything is saved."""
    hub, registry = hub_env
    run(hub, monkeypatch,
        ["partition", "add", "alpha", "--owner", "owner", "--set", "A=1"])
    before = registry.read_text()
    assert run(hub, monkeypatch,
               ["partition", "set", "alpha", "--set", "lower=1"]) == 1
    assert registry.read_text() == before


def test_missing_slug_still_fails(hub_env, monkeypatch):
    hub, _ = hub_env
    assert run(hub, monkeypatch,
               ["partition", "set", "ghost", "--set", "A=1"]) == 1
