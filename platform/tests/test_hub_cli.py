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


# ------------------------------------------------------------------ vault hosts
def test_vault_of_names_the_host_partition_not_the_slug(hub_env, monkeypatch):
    """`hub vault start kid` means "get this ACCOUNT signed in", and the account
    has ONE browser program (vault-<vault>, run by the host partition).

    Resolving it to the typed slug is what the pre-dedup CLI did, and after the
    dedup that program does not exist for a member: supervisorctl answers
    "no such process" for a partition whose account is signed in elsewhere,
    which reads like a broken vault rather than a shared one.
    """
    hub, registry = hub_env
    run(hub, monkeypatch, ["partition", "add", "alpha", "--owner", "owner"])
    run(hub, monkeypatch, ["partition", "add", "kid", "--vault", "alpha",
                           "--owner", "owner"])
    reg = json.loads(registry.read_text())
    assert hub.vault_of(reg, "kid") == ("alpha", "alpha")
    assert hub.vault_of(reg, "alpha") == ("alpha", "alpha")
    # A vault id no partition is named after still resolves: the FIRST partition
    # naming it, in slug order, hosts it (gen-conf's rule, copied deliberately).
    run(hub, monkeypatch, ["partition", "set", "kid", "--vault", "sharedacct"])
    reg = json.loads(registry.read_text())
    assert hub.vault_of(reg, "kid") == ("sharedacct", "kid")
    assert hub.vault_of(reg, "alpha") == ("alpha", "alpha")
    # An unknown slug is its own vault, which is what the registry defaults to,
    # so supervisorctl's own "no such process" stays the answer.
    assert hub.vault_of(reg, "ghost") == ("ghost", "ghost")


def test_partition_list_says_which_partition_holds_the_browser(hub_env,
                                                               monkeypatch,
                                                               capsys):
    """`hub vault start <slug>` follows whatever this line prints, so a shared
    vault has to be visible in `partition list` or the operator starts sign-in
    on a partition that has no chromium and waits for a browser that never
    comes."""
    hub, _ = hub_env
    run(hub, monkeypatch, ["partition", "add", "alpha", "--owner", "owner"])
    run(hub, monkeypatch, ["partition", "add", "kid", "--vault", "alpha",
                           "--owner", "owner"])
    assert run(hub, monkeypatch, ["partition", "list"]) == 0
    out = capsys.readouterr().out
    assert "vault=alpha (browser)" in out
    assert "vault=alpha (via alpha)" in out


def test_adding_to_a_shared_vault_says_no_sign_in_is_needed_there(hub_env,
                                                                  monkeypatch,
                                                                  capsys):
    """The line an added partition gets has to be about the shared browser, not
    the usual "the watchdog reloads" line: `hub vault start kid` on a member
    drives alpha's chromium, and nobody has to sign into a chromium that does
    not exist for it.
    """
    hub, registry = hub_env
    run(hub, monkeypatch, ["partition", "add", "alpha", "--owner", "owner"])
    assert run(hub, monkeypatch, ["partition", "add", "beta", "--owner", "owner"]) == 0
    out = capsys.readouterr().out
    assert "ONE browser" not in out              # beta is its own vault
    assert "watchdog reloads" in out
    assert run(hub, monkeypatch, ["partition", "add", "kid", "--vault", "alpha",
                                  "--owner", "owner"]) == 0
    assert spec_of(registry, "kid")["vault"] == "alpha"
    out = capsys.readouterr().out
    assert "it signs in as vault alpha with alpha" in out
    assert "ONE browser (on alpha)" in out


def test_removing_the_host_moves_the_vault_to_the_member(hub_env, monkeypatch,
                                                         capsys):
    """A vault id outlives the partition that gave it its name: its profile and
    jar under vaults/<id>/ have to belong to somebody supervision can start, and
    the operator has to be told which one, because `hub vault start` for the
    member now talks to a program that lives on that member."""
    hub, registry = hub_env
    run(hub, monkeypatch, ["partition", "add", "alpha", "--owner", "owner"])
    run(hub, monkeypatch, ["partition", "add", "kid", "--vault", "alpha",
                           "--owner", "owner"])
    assert run(hub, monkeypatch, ["partition", "rm", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "vault alpha moves to kid" in out
    reg = json.loads(registry.read_text())
    assert hub.vault_of(reg, "kid") == ("alpha", "kid")


def test_removing_the_last_partition_says_the_vault_is_unhosted(hub_env,
                                                                monkeypatch,
                                                                capsys):
    hub, _ = hub_env
    run(hub, monkeypatch, ["partition", "add", "alpha", "--owner", "owner"])
    assert run(hub, monkeypatch, ["partition", "rm", "alpha"]) == 0
    assert "no partition left names vault alpha" in capsys.readouterr().out


def test_repointing_a_vault_reports_both_ends(hub_env, monkeypatch, capsys):
    """`--vault` moves a partition between Google accounts. The message has to
    cover both sides: where the moved partition's sign-in now comes from, and
    where the vault it LEFT now runs, because that is the browser the operator
    has to start next time."""
    hub, registry = hub_env
    run(hub, monkeypatch, ["partition", "add", "alpha", "--owner", "owner"])
    run(hub, monkeypatch, ["partition", "add", "kid", "--vault", "alpha",
                           "--owner", "owner"])
    run(hub, monkeypatch, ["partition", "add", "beta", "--owner", "owner"])
    assert run(hub, monkeypatch, ["partition", "set", "alpha",
                                  "--vault", "beta"]) == 0
    out = capsys.readouterr().out
    assert "alpha now signs in as vault beta" in out
    assert "vault alpha moves to kid" in out
    assert spec_of(registry, "alpha")["vault"] == "beta"
