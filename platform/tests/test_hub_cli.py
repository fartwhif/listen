"""`hub partition` setting spellings.

Settings are the escape hatch for anything the registry has no flag for, so the
one thing that must be true is that a typed KEY=VALUE lands in partitions.json
and nothing else reports success. Both actions used to read only one of the two
spellings argparse offers — `add` read `--set` only, `set` read positionals
only — while both printed "settings updated"/added, so `hub partition set s
--set K=v` looked applied and changed nothing. These tests pin: either spelling
works on either action, an empty `set` is an error rather than a silent no-op,
and a refused setting leaves the registry byte-identical.

Also here: `hub reload`, the one verb that APPLIES config. Two things have to
stay true about it - it finds `gen-conf.py` when `hub` itself was reached through
the image's `/usr/local/bin/hub` symlink, and it reloads nginx after validating
the config it just generated (see the reload tests).
"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys

import pytest

PLATFORM = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN_CONF = os.path.join(PLATFORM, "gen-conf.py")


class FakeRun:
    """`subprocess.run` stand-in: records every argv, returncodes by program.

    `reload` talks to three programs (gen-conf, nginx, supervisorctl) and its
    contract is about their ORDER and which ones run at all, which is exactly
    what a real subprocess cannot show on a host with no nginx and no
    supervisor to talk to.
    """

    def __init__(self, **codes):
        self.calls = []
        self.codes = codes

    def run(self, argv, **kw):
        self.calls.append(list(argv))
        code = self.codes.get(self._prog(argv), 0)
        return subprocess.CompletedProcess(argv, code, "" if code else "ok",
                                           "nginx: configuration file "
                                           "test failed" if code else "")

    @staticmethod
    def _prog(argv):
        """Name a call by the program it drives, not by argv[0] - gen-conf runs
        as `[sys.executable, …/gen-conf.py]`, so argv[0] is just python."""
        if any(str(a).endswith("gen-conf.py") for a in argv):
            return "gen-conf"
        return argv[0]


def fake_nginx(hub, monkeypatch):
    """Point hub's subprocess at a recorder, with nginx's `-t` succeeding."""
    fake = FakeRun(**{"gen-conf.py": 0, "nginx": 0, "supervisorctl": 0})
    monkeypatch.setattr(hub, "subprocess", fake)
    return fake


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


# ---------------------------------------------------------------------- reload
def load_hub_via(path, name: str):
    """Import hub.py as if it LIVED at `path` (a symlink in the tests).

    The explicit loader is not ceremony: `spec_from_file_location` alone returns
    None for `/usr/local/bin/hub`, which has no `.py` to choose a loader from,
    and that extensionless name is exactly what the image puts on PATH. The
    loader has to reach the spec's `origin`, or `has_location` stays false and
    the module gets no `__file__` - the very value under test.
    """
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_reload_finds_gen_conf_when_hub_runs_through_its_symlink(tmp_path,
                                                                 monkeypatch,
                                                                 clean_env):
    """The image puts hub on PATH as the symlink `/usr/local/bin/hub`, and that
    is how every `docker compose exec listen hub …` reaches it.

    `__file__` is then the LINK, not the file: the imports still work, because
    Python resolves the script path for `sys.path[0]`, but `dirname(abspath(
    __file__))` is /usr/local/bin - and `reload` ran gen-conf from there, so the
    one verb that applies config died with `can't open file
    /usr/local/bin/gen-conf.py: No such file or directory` while everything else
    about the CLI worked. Sibling lookups have to go through the resolved path.
    """
    link = tmp_path / "bin/hub"
    link.parent.mkdir()
    link.symlink_to(os.path.join(PLATFORM, "hub.py"))
    hub = load_hub_via(str(link), "hub_via_symlink")
    # The trap itself, stated as an assertion: this module believes it lives in
    # a directory that has no gen-conf.py in it.
    assert os.path.dirname(os.path.abspath(hub.__file__)) == str(link.parent)
    assert not os.path.exists(os.path.join(str(link.parent), "gen-conf.py"))
    fake = FakeRun()
    monkeypatch.setattr(hub, "subprocess", fake)
    monkeypatch.setattr(sys, "argv", ["hub", "reload"])
    assert hub.main() == 0
    target = fake.calls[0][1]
    assert target == GEN_CONF
    assert os.path.isfile(target), f"reload tried to run {target}"


def test_reload_validates_the_gateway_before_reloading_it(hub_env, monkeypatch):
    """`nginx -s reload` IS part of reload, and only after `nginx -t` passes.

    Regenerate + supervisor alone gives a new partition its backends while the
    gateway still serves the OLD location set - live programs, no proxy, 404 at
    the mount - and the watchdog does the same three steps 10 s later, so the
    impatient version must not be a different, partial procedure.
    """
    hub, _ = hub_env
    fake = fake_nginx(hub, monkeypatch)
    assert run(hub, monkeypatch, ["reload"]) == 0
    progs = [FakeRun._prog(c) for c in fake.calls]
    assert progs == ["gen-conf", "nginx", "nginx", "supervisorctl",
                     "supervisorctl"]
    assert fake.calls[1][1:] == ["-t"]
    assert fake.calls[2][1:] == ["-s", "reload"]
    assert [c[-1] for c in fake.calls[3:]] == ["reread", "update"]


def test_bad_generated_config_never_reaches_the_gateway(hub_env, monkeypatch,
                                                        capsys):
    """A partitions.json edit gen-conf can render but nginx rejects must not
    replace a working gateway - the failure mode the entrypoint guards against,
    which `hub reload` used to bypass by never validating at all.

    The supervisor half is still applied (gen-conf wrote valid program files and
    supervisor judges those itself), and the command reports failure.
    """
    hub, _ = hub_env
    fake = FakeRun(**{"nginx": 1})
    monkeypatch.setattr(hub, "subprocess", fake)
    assert run(hub, monkeypatch, ["reload"]) == 1
    assert ["-s", "reload"] not in [c[1:] for c in fake.calls]
    assert [c[-1] for c in fake.calls if FakeRun._prog(c) == "supervisorctl"] \
        == ["reread", "update"]
    err = capsys.readouterr().err
    assert "FAILED nginx -t" in err and "keeps the previous config" in err


def test_generator_failure_applies_nothing(hub_env, monkeypatch):
    """If gen-conf refused to write, there is no new config to validate or
    apply: neither nginx nor supervisor is touched, and its exit code is the
    answer."""
    hub, _ = hub_env
    fake = FakeRun(**{"gen-conf": 3})
    monkeypatch.setattr(hub, "subprocess", fake)
    assert run(hub, monkeypatch, ["reload"]) == 3
    assert [FakeRun._prog(c) for c in fake.calls] == ["gen-conf"]
