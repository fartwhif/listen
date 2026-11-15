"""Registry -> supervision + nginx generation.

The generation IS the multi-tenancy: a slug becomes programs, ports, paths and
gated locations, and nothing downstream re-derives it. So the tests here assert
the properties that a mistake would break, rather than exact bytes:

  * every path a partition serves is gated (an ungated location is a partition
    boundary that is not enforced);
  * every upstream is a LITERAL port for the right slug (a wrong literal sends
    one tenant's traffic to another tenant's backend, and an interpolated `$var`
    upstream would only show up at request time);
  * the emitters render balanced, single braces (the `{`/`}` doubling bug ate a
    whole partition's nginx block silently, because nothing ran `nginx -t`);
  * the shell gets its base href and its identity script AFTER the closed
    `<base>` tag (an unclosed tag swallows the script and the app then has
    neither a mount nor a user).

The nginx proof (`nginx -t` over exactly what gen-conf writes, for one and for
two partitions) needs the real snippet files at `/etc/nginx/snippets`, so those
tests run inside the image and skip elsewhere (`conftest.nginx_required`).

Module attributes (not the environment) are what the tests repoint: every path
and credential gen-conf reads is frozen into a module global at import time.
"""
import importlib
import ipaddress
import json
import pathlib
import os
import urllib.parse
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
import pytest

from conftest import nginx_required

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

pytest.importorskip("importlib")
gen = importlib.import_module("gen-conf")

TWO = {
    "slugs": ["owner", "family"],
    "defaultSlug": "owner",
    "partitions": {
        "owner": {"mode": "single", "name": "Home", "owner": "owner",
                  "radio": True, "vault": "owner"},
        "family": {"mode": "shared", "name": "Family", "owner": "owner",
                   "members": ["kid", "car"], "vault": "owner",
                   "quota": {"concurrentDownloads": 1},
                   "settings": {"TUNESTER_TRIALS": "3"}},
    },
}


def registry(tmp_path, reg=TWO, monkeypatch=None):
    """Write a registry and point gen-conf at it (module attribute, so a test's
    reload games cannot undo it)."""
    path = tmp_path / "partitions.json"
    path.write_text(json.dumps(reg, indent=2) + "\n")
    if monkeypatch is not None:
        monkeypatch.setattr(gen, "REGISTRY", str(path))
    return path


@pytest.fixture
def two(tmp_path, monkeypatch):
    registry(tmp_path, TWO, monkeypatch)
    monkeypatch.setattr(gen, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(gen, "STACK", "/srv/stack")
    return gen.load_registry()


# ---------------------------------------------------------------- registry
def test_load_registry_fills_defaults_and_binds_a_vault(two):
    assert two["slugs"] == ["owner", "family"]
    assert two["front"] == "owner"
    owner, family = two["partitions"]["owner"], two["partitions"]["family"]
    # index is position, and every spec gets the derived fields the emitters
    # assume without checking.
    assert (owner["index"], family["index"]) == (0, 1)
    assert owner["vault_slug"] == "owner"
    # family names no vault of its own: it shares the front partition's.
    assert family["vault_slug"] == "owner"
    assert family["name"] == "Family" and family["mode"] == "shared"
    assert family["radio"] is True


def test_load_registry_drops_bad_slugs_and_unknown_partition_entries(
        tmp_path, monkeypatch):
    reg = {"slugs": ["owner", "Bad Slug", "x" * 40, "owner", "fam_2"],
           "defaultSlug": "Missing",
           "partitions": {"owner": {}, "orphan": {"vault": "owner"}}}
    registry(tmp_path, reg, monkeypatch)
    loaded = gen.load_registry()
    # A slug becomes a path segment, a supervisor program name and a regex, so
    # anything outside ^[a-z][a-z0-9_-]{0,31}$ cannot be allowed through.
    assert loaded["slugs"] == ["owner", "fam_2"]
    # defaultSlug named a slug that is not in the list: the front falls to the
    # first good one rather than to a partition with no proxies.
    assert loaded["front"] == "owner"
    assert "orphan" not in loaded["partitions"]


def test_load_registry_repairs_front_to_first_slug(tmp_path, monkeypatch):
    registry(tmp_path, {"slugs": ["b", "a"], "defaultSlug": "gone",
                        "partitions": {}}, monkeypatch)
    assert gen.load_registry()["front"] == "b"


def test_load_registry_bad_json_is_empty_not_fatal(tmp_path, monkeypatch,
                                                  capsys):
    path = tmp_path / "partitions.json"
    path.write_text('{"slugs": [')          # truncated by a crashed writer
    monkeypatch.setattr(gen, "REGISTRY", str(path))
    reg = gen.load_registry()
    assert reg == {"slugs": [], "front": None, "partitions": {}}
    assert "not valid JSON" in capsys.readouterr().err


def test_load_registry_caps_partitions(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gen, "MAX_PARTITIONS", 2)
    reg = {"slugs": ["a", "b", "c"], "defaultSlug": "a", "partitions": {}}
    registry(tmp_path, reg, monkeypatch)
    loaded = gen.load_registry()
    # The cap is the port map's: 3000+i would collide past it.
    assert loaded["slugs"] == ["a", "b"]
    assert "port map limit" in capsys.readouterr().err


def test_load_registry_missing_file_seeds_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "REGISTRY", str(tmp_path / "nope.json"))
    assert gen.load_registry()["slugs"] == []


def test_registry_default_follows_the_state_directory(clean_env, reload_modules):
    """The registry default is DERIVED from HUB_DATA_DIR, not a literal /data.

    compose relocates the state directory with HUB_DATA_DIR and only the
    entrypoint exports HUB_REGISTRY, so any other process running the generator
    with the container's environment - a `docker exec`, an operator's hand-run -
    had no way to be told where the registry is. The literal default made it
    read a file that does not exist, report "0 partition(s)" and atomically
    rewrite every generated target to match: the live gateway then 404'd every
    partition while partitions.json sat right there untouched.
    """
    os.environ["HUB_DATA_DIR"] = "/mnt/stack/listen-data"
    gen2 = reload_modules("gen-conf")
    assert gen2.REGISTRY == "/mnt/stack/listen-data/partitions.json"
    # An explicit HUB_REGISTRY still wins: that is the handle the watchdog
    # program is supervised with, and the handle these tests use.
    os.environ["HUB_REGISTRY"] = "/etc/stack/partitions.json"
    gen3 = reload_modules("gen-conf")
    assert gen3.REGISTRY == "/etc/stack/partitions.json"


def test_seeder_writes_where_the_generator_will_read(clean_env, monkeypatch,
                                                      tmp_path):
    """seed-registry's destination has to be the same derivation, or a box that
    relocates its state directory seeds a registry under /data that gen-conf -
    reading the relocated path - never sees, and comes up with zero partitions.
    Never overwriting is the other half: a re-run must not clobber edits."""
    seed = importlib.import_module("seed-registry")
    dest = tmp_path / "listen-data/partitions.json"
    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path / "listen-data"))
    assert seed.main("owner") == 0
    assert dest.exists()
    dest.write_text('{"slugs": ["keep"]}')
    assert seed.main("owner") == 0
    assert json.loads(dest.read_text())["slugs"] == ["keep"]
    # HUB_REGISTRY overrides, and is what the entrypoint exports in practice.
    explicit = tmp_path / "explicit.json"
    monkeypatch.setenv("HUB_REGISTRY", str(explicit))
    assert seed.main("owner") == 0
    assert explicit.exists()


def test_port_map_is_collision_free(two):
    seen = {}
    for i in range(gen.MAX_PARTITIONS):
        for name, port in gen.ports(i).items():
            seen.setdefault(port, []).append(f"{i}:{name}")
            assert 1 <= port <= 65535
    assert len(seen) == len(gen.ports(0)) * gen.MAX_PARTITIONS, \
        f"port collision: {[k for k, v in seen.items() if len(v) > 1]}"
    # The published ports belong to index 0 ONLY: gen-conf binds index 0's
    # grabbytube and broadcaster non-loopback, and compose publishes exactly
    # these numbers, so a later partition landing on one would be reachable
    # from outside under the wrong tenant.
    assert gen.ports(0)["grabby"] == 8101 and gen.ports(1)["grabby"] == 8112
    assert gen.ports(0)["radio"] == 8090 and gen.ports(1)["radio"] == 8201
    assert gen.ports(0)["drivepod"] == 3000 and gen.ports(9)["drivepod"] == 3009


# ------------------------------------------------------------ supervisor
def program_blocks(text):
    return {m.group(1): m.group(0)
            for m in re.finditer(r"^\[program:([^\]]+)\]\n(.*?)(?=^\[|\Z)",
                                 text, re.S | re.M)}


def env_of(block):
    """Supervisor's own parser, when the image has it; otherwise the module's
    inverse. The real parser matters because gen-conf quotes values that
    contain commas, and a wrong quoting means a program silently gets a
    truncated value."""
    line = [l for l in block.splitlines() if l.startswith("environment=")]
    assert len(line) == 1
    try:
        from supervisor.datatypes import dict_of_key_value_pairs
    except ImportError:
        import envfmt
        return envfmt.parse_env_line(line[0][len("environment="):])
    return dict_of_key_value_pairs(line[0][len("environment="):])


def test_supervisor_conf_programs_per_partition(two):
    blocks = program_blocks(gen.supervisor_conf(two))
    # ONE vault program for the whole registry: both partitions here name vault
    # "owner", and a vault id is a Google account, so `vault-family` must not
    # exist - two chromiums re-typing one password is the bug that dedup fixes.
    assert sorted(blocks) == sorted([
        "bgutil", "hubauth", "nginx", "streamfix", "tunester", "watchdog",
        "drivepod-owner", "grabbytube-owner", "frogrock-owner",
        "drivepod-family", "grabbytube-family", "frogrock-family",
        "vault-owner"])
    # radio False must remove the broadcaster, not just its port.
    two["partitions"]["family"]["radio"] = False
    assert "frogrock-family" not in program_blocks(gen.supervisor_conf(two))
    # One shared service each, whatever the partition count.
    for shared in ("bgutil", "tunester", "hubauth", "watchdog", "nginx",
                   "streamfix"):
        assert sum(1 for n in blocks if n == shared) == 1
    # streamfix only exists for a front partition with a broadcaster (its whole
    # job is fronting :8090 for DNAS).
    nofront = {"slugs": [], "front": None, "partitions": {}}
    assert "streamfix" not in program_blocks(gen.supervisor_conf(nofront))


def test_vault_programs_are_manual_start(two):
    blocks = program_blocks(gen.supervisor_conf(two))
    # Named after the VAULT, and one per distinct vault: the program drives one
    # Google account's browser, so two partitions sharing vault "owner" get one
    # program between them (see test_vault_programs_are_named_after_the_vault).
    for vault in gen.vault_hosts(two):
        b = blocks[f"vault-{vault}"]
        # autostart/autorestart false is the owner's invariant: no chromium
        # until a session is actually stale. The watchdog owns the life.
        assert "autostart=false" in b and "autorestart=false" in b
        # The host partition is the argument: vault-run.py derives the profile
        # dir and the fan-out targets from it, and only the host has a browser.
        assert f"python3 /srv/stack/platform/vault-run.py {vault}" in b
        assert f"stdout_logfile=/var/log/sup/vault-{vault}.log" in b
    assert "autostart=true" in blocks["drivepod-owner"]
    # QUIT, not TERM: a graceful nginx drains the listeners mid-stream.
    assert "stopsignal=QUIT" in blocks["nginx"]
    assert "stopsignal=TERM" in blocks["drivepod-owner"]


def test_tenant_env_is_the_same_for_every_program_of_a_slug(two):
    sup = gen.supervisor_conf(two)
    blocks = program_blocks(sup)
    tenant = env_of(blocks["drivepod-owner"])
    assert env_of(blocks["grabbytube-owner"]) == tenant
    radio = env_of(blocks["frogrock-owner"])
    # The broadcaster shares the tenant's paths but not its PORT or bind:
    # PORT is the radio port, and only index 0 listens non-loopback.
    assert radio["PORT"] == str(gen.ports(0)["radio"]) != tenant["PORT"]
    assert radio["HOST"] == "0.0.0.0" and tenant["HOST"] == "127.0.0.1"
    for key in ("DATA_DIR", "CACHE_DIR", "DATABASE_URL", "COOKIES_FILE",
                "GRABBY_DB_PATH", "HUB_SLUG", "HUB_URL_PREFIX"):
        assert radio[key] == tenant[key], key
    # ...and the overrides are the ONLY difference.
    assert {k for k, v in radio.items() if tenant.get(k) != v} == \
        {"PORT", "HOST"}


def test_tenant_env_values_a_supervisor_parser_accepts(two):
    # The program is grabbytube-FAMILY: every value below names family's home,
    # ports and shared vault, which the owner partition's env cannot have.
    # (It once read grabbytube-owner and asserted family's values on it, and so
    # pinned HUB_SLUG to the front partition - the one slug that must NOT be in
    # a non-front tenant's env, because the slug header and the paths are what
    # separate tenants.)
    tenant = env_of(program_blocks(gen.supervisor_conf(two))["grabbytube-family"])
    home = f"{gen.DATA}/users/family"
    assert tenant["HUB_SLUG"] == "family"
    assert tenant["HUB_URL_PREFIX"] == "/l/family"
    assert tenant["DATA_DIR"] == home
    assert tenant["DATABASE_URL"] == f"file:{home}/database.db"
    assert tenant["GRABBY_DB_PATH"] == f"{home}/grabbytube.db"
    assert tenant["GRABBY_CATALOG_PATH"] == f"{home}/library/grabby-tracks.json"
    assert tenant["COOKIES_FILE"] == f"{home}/cookies.txt"
    # The browser family's ports are per partition, and the profile is per
    # PARTITION even in a shared vault (two chromiums, one profile dir, and the
    # second dies on SingletonLock).
    assert tenant["CDP_PORT"] == str(gen.ports(1)["cdp"])
    assert tenant["VNC_PORT"] == str(gen.ports(1)["vnc"])
    assert tenant["VAULT_DIR"] == f"{gen.DATA}/vaults/owner"
    assert tenant["VAULT_PROFILE_DIR"] == \
        f"{gen.DATA}/vaults/owner/profiles/family"
    # Both downloaders go through the concurrency gate as ONE word: an app that
    # uses the value as argv[0] cannot be handed "a b".
    assert tenant["DRIVEPOD_YTDLP"] == tenant["GRABBY_YTDLP"] == \
        "/srv/stack/platform/ytdlgated"
    assert " " not in tenant["DRIVEPOD_YTDLP"]
    # Registry quota and settings reach the tenant as env.
    assert tenant["GRABBY_MAX_CONCURRENT"] == "1"
    assert tenant["TUNESTER_TRIALS"] == "3"


def test_tenant_env_ignores_non_env_setting_names(two, monkeypatch):
    two["partitions"]["family"]["settings"] = {"lowercase": "x", "9BAD": "y",
                                               "GOOD_ONE": "z"}
    tenant = env_of(program_blocks(gen.supervisor_conf(two))["grabbytube-family"])
    assert tenant["GOOD_ONE"] == "z"
    assert "lowercase" not in tenant and "9BAD" not in tenant


def test_pool_env_lists_every_partition_once(two):
    pool = env_of(program_blocks(gen.supervisor_conf(two))["tunester"])
    assert pool["TUNESTER_COOKIES"] == ",".join(
        f"{gen.DATA}/users/{s}/cookies.txt" for s in two["slugs"])
    # The pool writes the blob store; unset blobs would mean per-partition
    # downloads of the same song.
    assert pool["TUNESTER_BLOBS"] == f"{gen.DATA}/pool/blobs"
    assert pool["TUNESTER_OUTDIR"] == f"{gen.DATA}/pool/out"
    assert pool["TUNESTER_YTDLP"].endswith("ytdlgated")
    # Published for tunester.<domain>, so non-loopback by contract.
    assert pool["TUNESTER_WEB_HOST"] == "0.0.0.0"
    assert pool["TUNESTER_WEB_PORT"] == "8102"
    # The shared cap must match the tenants' or the bucket has two sizes.
    tenant = env_of(program_blocks(gen.supervisor_conf(two))["grabbytube-owner"])
    assert pool["MAX_CONCURRENT_YTDLP"] == tenant["MAX_CONCURRENT_YTDLP"]
    assert pool["YTDGATE_SLOTS_DIR"] == tenant["YTDGATE_SLOTS_DIR"]


def test_gate_and_watchdog_env_name_the_paths_they_read(two):
    blocks = program_blocks(gen.supervisor_conf(two))
    gate = env_of(blocks["hubauth"])
    watch = env_of(blocks["watchdog"])
    # The gate answers "is <slug> real" from the registry, so it has to be
    # pointed at the SAME file gen-conf supervises from.
    assert gate["HUB_REGISTRY"] == gen.REGISTRY
    assert watch["HUB_REGISTRY"] == gen.REGISTRY
    assert gate["HUB_DB"] == watch["HUB_DB"] == f"{gen.DATA}/hub.db"
    assert gate["HUB_PORT"] == "8099"
    assert watch["HUB_DATA_DIR"] == gen.DATA


def test_credentials_are_forwarded_only_when_set(two, monkeypatch):
    monkeypatch.setattr(gen, "SITE_DOMAIN", "listen.example.com")
    monkeypatch.setenv("TUNESTER_ACOUSTID_KEY", "abc123")
    monkeypatch.delenv("GOOGLE_EMAIL", raising=False)
    monkeypatch.setenv("HUB_RADIO_PASS", "broadcastpass")
    blocks = program_blocks(gen.supervisor_conf(two))
    assert env_of(blocks["tunester"])["TUNESTER_ACOUSTID_KEY"] == "abc123"
    assert env_of(blocks["hubauth"])["HUB_RADIO_PASS"] == "broadcastpass"
    # An unset credential stays ABSENT, so the service keeps its own default
    # (an empty string would make `if os.environ.get(X)` truthy and change
    # behaviour the standalone deploy did not have).
    assert "GOOGLE_EMAIL" not in env_of(blocks["vault-owner"])


def test_vault_env_gets_push_targets_and_door(two, monkeypatch):
    monkeypatch.setattr(gen, "SITE_DOMAIN", "listen.example.com")
    blocks = program_blocks(gen.supervisor_conf(two))
    vault = env_of(blocks["vault-owner"])
    tenant = env_of(blocks["drivepod-owner"])
    # The exporter pushes its refreshed jar to EVERY partition sharing this
    # vault, or the second partition keeps using the dead cookies.
    assert vault["DRIVEPOD_PUSH_URLS"] == ",".join(
        f"http://127.0.0.1:{gen.ports(s['index'])['drivepod']}"
        for s in two["partitions"].values())
    assert vault["GRABBY_PUSH_URLS"].count("http://") == 2
    door = vault["VNC_URL"]
    # The lite page builds ws://host/ + path, so the slug MUST be in `path`:
    # a bare `?path=websockify` (the page's own default) aims at the portal
    # root, where no partition is implied and nothing is gated.
    assert door == ("http://listen.example.com/l/owner/vnc/vnc_lite.html"
                    "?path=l/owner/vnc/websockify")
    assert not door.split("?", 1)[1].split("=", 1)[1].startswith("/")
    # A door URL is only useful with a domain; unset leaves the exporter's
    # bare "noVNC" hint rather than a invented hostname.
    monkeypatch.setattr(gen, "SITE_DOMAIN", "")
    assert "VNC_URL" in env_of(program_blocks(
        gen.supervisor_conf(two))["vault-owner"])
    assert gen.vnc_door("owner") == ""
    # The family keeps the tenant's own browser ports and paths.
    for key in ("CDP_PORT", "VNC_PORT", "WEBSOCKIFY_PORT", "HOME",
                "VAULT_PROFILE_DIR", "COOKIES_FILE"):
        assert vault[key] == tenant[key], key


def _with_index(reg: dict) -> dict:
    """load_registry's per-spec fill-in (index, vault_slug, defaults) for a
    registry a test builds by hand instead of reading from a file."""
    out = {"slugs": list(reg["slugs"]), "front": reg.get("defaultSlug"),
           "partitions": {}}
    for i, slug in enumerate(out["slugs"]):
        spec = dict(reg["partitions"][slug])
        spec["index"] = i
        spec.setdefault("mode", "single")
        spec.setdefault("name", slug.capitalize())
        spec.setdefault("owner", "owner")
        spec.setdefault("radio", True)
        spec["vault_slug"] = spec.get("vault") or slug
        spec.setdefault("quota", {})
        spec.setdefault("settings", {})
        out["partitions"][slug] = spec
    return out


def test_vault_programs_are_named_after_the_vault_not_the_partition(two):
    """The dedup itself: a vault id is a GOOGLE ACCOUNT, so two partitions
    naming it get ONE browser family, and it is named for the vault.

    Named after the partition (what this generator used to do), removing the
    host - or renaming which partition hosts it - orphans vaults/<vault>/: the
    program vanishes with the name it carried while the profile and jar stay on
    disk for an account the survivors still sign in as.
    """
    blocks = program_blocks(gen.supervisor_conf(two))
    assert blocks.get("vault-family") is None
    # Take the naming partition away: the vault keeps its name and its program,
    # and the surviving member that still names it becomes the host (the
    # argument to vault-run.py), because the profile has to belong to somebody
    # supervision can start.
    reg = {"slugs": ["family"], "defaultSlug": "family",
           "partitions": {"family": dict(two["partitions"]["family"],
                                         vault="owner")}}
    blocks2 = program_blocks(gen.supervisor_conf(_with_index(reg)))
    assert "vault-owner" in blocks2 and "vault-family" not in blocks2
    assert "python3 /srv/stack/platform/vault-run.py family" \
        in blocks2["vault-owner"]
    assert gen.vault_hosts(two) == {"owner": "owner"}


def test_two_accounts_get_two_vault_programs(two):
    """The other half: the dedup is per VAULT, not global. Two Google accounts
    in one box are two browsers, each with its own program, ports and profile -
    sharing is one browser per ACCOUNT, not one browser per box."""
    reg = json.loads(json.dumps(two))
    reg["partitions"]["family"]["vault"] = "kidacct"
    reg = _with_index(reg)
    blocks = program_blocks(gen.supervisor_conf(reg))
    assert sorted(n for n in blocks if n.startswith("vault-")) == [
        "vault-kidacct", "vault-owner"]          # the two vault ids
    assert "vault-run.py family" in blocks["vault-kidacct"]
    # Each family dials its OWN browser ports: two chromiums on one host cannot
    # share an X display, a VNC port, a websockify or a CDP port.
    for name, slug in (("vault-owner", "owner"), ("vault-kidacct", "family")):
        e = env_of(blocks[name])
        p = gen.ports(reg["partitions"][slug]["index"])
        assert e["VNC_PORT"] == str(p["vnc"]) and e["CDP_PORT"] == str(p["cdp"])
        assert e["WEBSOCKIFY_PORT"] == str(p["ws"])
        assert e["VAULT_SLUG"] == reg["partitions"][slug]["vault_slug"]
        assert e["VAULT_DIR"] == f"{gen.DATA}/vaults/{e['VAULT_SLUG']}"
        # Two chromiums, one profile dir each: sharing the directory is a
        # SingletonLock fight and "profile is already in use".
        assert e["VAULT_PROFILE_DIR"] == \
            f"{gen.DATA}/vaults/{e['VAULT_SLUG']}/profiles/{slug}"
    assert gen.vault_hosts(reg) == {"owner": "owner", "kidacct": "family"}


def test_two_accounts_get_their_own_credentials(two, monkeypatch):
    """`settings.GOOGLE_EMAIL` IS the account a vault signs in as, so it has to
    reach `vault-<id>` and not the container's single `.env` account. With the
    precedence reversed every browser authenticates the `.env` account while each
    partition's apps read a DIFFERENT jar - a wrong-session box that still looks
    healthy from the outside (handshake up, jar fresh, just somebody else's).

    `.env` stays the fallback, which is the one-account-per-box shape that
    test_credentials_are_forwarded_only_when_set covers.
    """
    monkeypatch.setenv("GOOGLE_EMAIL", "env-account@example.com")
    monkeypatch.setenv("GOOGLE_PASSWORD", "envpass")
    reg = json.loads(json.dumps(two))
    reg["partitions"]["owner"]["settings"] = {
        "GOOGLE_EMAIL": "home@example.com", "GOOGLE_PASSWORD": "homepass"}
    reg["partitions"]["family"]["vault"] = "kidacct"
    reg["partitions"]["family"]["settings"] = {"GOOGLE_EMAIL": "kid@example.com"}
    blocks = program_blocks(gen.supervisor_conf(_with_index(reg)))
    assert env_of(blocks["vault-owner"])["GOOGLE_EMAIL"] == "home@example.com"
    assert env_of(blocks["vault-owner"])["GOOGLE_PASSWORD"] == "homepass"
    assert env_of(blocks["vault-kidacct"])["GOOGLE_EMAIL"] == "kid@example.com"
    # A name settings does NOT carry still falls back to `.env`: a second account
    # that shares the box's password must not lose it.
    assert env_of(blocks["vault-kidacct"])["GOOGLE_PASSWORD"] == "envpass"


def test_vault_env_fans_writes_out_to_every_member(two):
    """The other half of one-browser-per-vault: a member's apps read their OWN
    COOKIES_FILE (that is what separates tenants), so the host's exporter has
    to write there too, or a member's jar ages out with no browser of its own.

    The HOST's own paths are not in the lists: it keeps writing them the way it
    always did, and listing them twice would give one process two writers of
    the same file.
    """
    v = env_of(program_blocks(gen.supervisor_conf(two))["vault-owner"])
    data = gen.DATA
    assert v["COOKIE_FANOUT_FILES"] == f"{data}/users/family/cookies.txt"
    assert v["COOKIE_SESSION_STATE_FANOUT"] == \
        f"{data}/users/family/session-state.json"
    assert v["COOKIES_FILE"] not in v["COOKIE_FANOUT_FILES"].split(",")
    assert v["COOKIE_SESSION_STATE"] not in \
        v["COOKIE_SESSION_STATE_FANOUT"].split(",")
    # companion_paths is what renders these; it must agree with the env exactly.
    assert gen.companion_paths(two, "owner") == {
        "cookies": [f"{data}/users/family/cookies.txt"],
        "state": [f"{data}/users/family/session-state.json"]}
    # A vault with no members gets the keys OMITTED, not empty: the exporter's
    # standalone deploy has no companions, and an empty quoted value in a
    # generated file reads like a path that went missing.
    solo = _with_index({"slugs": ["owner"], "defaultSlug": "owner",
                        "partitions": {"owner": {"vault": "owner"}}})
    solo_env = env_of(program_blocks(gen.supervisor_conf(solo))["vault-owner"])
    assert "COOKIE_FANOUT_FILES" not in solo_env
    assert "COOKIE_SESSION_STATE_FANOUT" not in solo_env
    assert gen.companion_paths(solo, "owner") == {"cookies": [], "state": []}
    # Members of ANOTHER vault are not this vault's companions.
    reg = json.loads(json.dumps(two))
    reg["partitions"]["family"]["vault"] = "kidacct"
    assert gen.companion_paths(_with_index(reg), "owner") == {
        "cookies": [], "state": []}


def test_a_member_door_proxies_the_vault_hosts_websocket(two):
    """No chromium runs for a member, so its noVNC door has to reach the HOST's
    websockify (owner's ws port), while the mount and the gate stay the member's
    own: one shared browser, one door per partition.

    Proxying a member's door to the member's own (now non-existent) websockify
    is the failure mode the dedup introduces, and it fails only when the user
    actually opens the door - after the sign-in worked.
    """
    body = gen.nginx_locations(two)
    doors = {n: b for n, b in locations(body) if n and n.endswith("/vnc/websockify")}
    assert sorted(doors) == ["/l/family/vnc/websockify", "/l/owner/vnc/websockify",
                             "/vnc/websockify"]
    host_ws = gen.ports(two["partitions"]["owner"]["index"])["ws"]
    member_ws = gen.ports(two["partitions"]["family"]["index"])["ws"]
    for name in ("/l/family/vnc/websockify", "/vnc/websockify"):
        m = re.search(r"proxy_pass http://127\.0\.0\.1:(\d+)", doors[name])
        assert int(m.group(1)) == host_ws, (name, m.group(0))
    m = re.search(r"proxy_pass http://127\.0\.0\.1:(\d+)",
                  doors["/l/owner/vnc/websockify"])
    assert int(m.group(1)) == host_ws != member_ws
    # The gate is still the MEMBER's: the door serves whoever is signed in to
    # that partition, not whoever hosts the browser.
    assert "auth_request /_gate/family;" in doors["/l/family/vnc/websockify"]
    # Only the door follows the vault: the member's api and grabby mounts stay
    # on the member's own ports.
    locs = dict(locations(body))
    assert f"http://127.0.0.1:{gen.ports(1)['drivepod']}" in locs["/l/family/api/"]
    assert f"http://127.0.0.1:{gen.ports(1)['grabby']}" in \
        locs["/l/family/grabby/"]


def test_picker_names_the_vault_and_says_who_holds_the_browser(two):
    """Two partitions signing in as the SAME Google account is a different
    choice from two accounts, and the picker is the only place a user makes it:
    the card names the vault and says which partition's browser a sign-in runs
    in, because starting it there serves the other card too."""
    picker = gen.picker_html(two)
    assert "vault owner" in picker
    assert "sign-in host for 1 other" in picker
    assert "signs in via /owner" in picker
    # Two accounts: no card claims anything about the other one.
    reg = json.loads(json.dumps(two))
    reg["partitions"]["family"]["vault"] = "kidacct"
    sep = gen.picker_html(_with_index(reg))
    assert "sign-in host" not in sep and "signs in via" not in sep
    assert "vault owner" in sep and "vault kidacct" in sep


# ----------------------------------------------------------------- nginx
LOC_RE = re.compile(
    r"^\s*(location)\s+(?P<kind>=|~\*|\^~|~|@)?\s*(?P<name>\S+)?\s*\{(?P<body>.*?^\})\n",
    re.S | re.M)


def locations(body: str):
    """(name, block_text) for every location in a generated file.

    A quoted regex pattern is ONE token, so its name comes back without the
    surrounding quotes: `^/l/family/api/auth/`, not `"^/l/family/api/auth/"`.
    Leaving the quotes in means every comparison against a name is comparing a
    string that starts with `"` - and a `re.match("^/l/")` on it silently reports
    the block as prefix-less, which is how the radio blocks got judged against
    the FRONT partition's ports."""
    out = []
    for m in LOC_RE.finditer(body):
        name = m.group("name") or m.group("body")
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        out.append((name, m.group(0)))
    return out


def test_locations_emits_both_prefixes_and_front_aliases(two):
    body = gen.nginx_locations(two)
    names = [n for n, _ in locations(body)]
    for slug in ("owner", "family"):
        # The mount itself (an exact location that redirects to `prefix/`) and
        # the SPA-shell fallback block under it.
        assert f"/l/{slug}" in names and f"/l/{slug}/" in names
        for suffix in ("/api/", "/cache/", "/library/", "/radio/",
                       "/grabby/", "/vnc/", "/vnc/websockify"):
            assert f"/l/{slug}{suffix}" in names, suffix
        # The two things under a partition that are REGEXES rather than prefixes
        # are named by their pattern (see locations()): the mounted login surface
        # and the shared pool's job surface.
        assert f"^/l/{slug}/api/auth/" in names
        assert f"^/l/{slug}/api/(?:(?:jobs|clear|health)(?:/.*)?)$" in names
    # The front partition ALSO answers at root, unchanged, for the callers that
    # predate the gateway (VPS server_names, DNAS, the vault's callbacks).
    for bare in ("/api/", "/cache/", "/library/", "/radio/", "/grabby/",
                 "/vnc/"):
        assert bare in names
    # A non-front partition never gets bare aliases: `/api/` for two partitions
    # would be a duplicate location and nginx -t would fail.
    assert "/l/family" in names

def test_prefix_modifiers_are_plain_exactly_where_a_regex_sibling_must_win(two):
    """`^~` means "this prefix is the longest match, do not evaluate regexes", so
    on a prefix that has a SIBLING regex which must win for some URI, the
    modifier has to be ABSENT. Both `/api/` and `/radio/` have one here - the
    shared pool's job surface and the broadcast stream - and with `^~` the stream
    would have been served the player page and `/l/<slug>/api/jobs` would have
    been drivepod's 404. Every other prefix keeps `^~` because nothing beside it
    needs to beat it: the dotted-progress regex is NESTED inside `/cache/` (a
    nested location is searched only after its parent prefix wins), and
    `/library/`, `/grabby/` and the SPA shell prefix gain the protection - an
    Angular route like `/l/<slug>/library` must not be re-routed by a sibling
    regex. `/vnc/` is the one name that appears TWICE on purpose: an EXACT
    location answers the door itself (a prefix location with a directory
    candidate dies in nginx's directory-index phase and bounces a signed-in
    member to /login) and the `^~` prefix serves the noVNC tree under it.
    """
    kinds: dict[str, set] = {}
    for m in LOC_RE.finditer(gen.nginx_locations(two)):
        name = m.group("name") or ""
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        kinds.setdefault(name, set()).add((m.group("kind") or "").strip())
    for pre in ("/l/owner", "/l/family", ""):            # "" = front's bare form
        for suffix in ("/api/", "/radio/"):
            assert kinds[f"{pre}{suffix}"] == {""}, f"{pre}{suffix} must be plain"
    for suffix in ("/cache/", "/library/", "/grabby/", "/"):
        assert kinds[f"/l/owner{suffix}"] == {"^~"}, suffix
    assert kinds["/l/owner/vnc/"] == {"=", "^~"}
    assert kinds["/vnc/"] == {"=", "^~"}
    # The bare aliases come from the FRONT partition only: a second partition
    # emitting `/api/` would be a duplicate location and nginx -t fails outright.
    for bare in ("/api/", "/cache/", "/library/", "/radio/", "/grabby/",
                 "/vnc/"):
        assert list(kinds).count(bare) == 1, bare
    assert "/" not in kinds          # `/` is the picker's, not a partition's


def test_every_partition_path_is_gated(two):
    """The partition boundary. An ungated proxy or media location is a tenant
    reachable without a session."""
    for slug in ("owner", "family"):
        body = gen._locations(slug, f"/l/{slug}", gen.ports(
            two["partitions"][slug]["index"]), two["partitions"][slug])
        for name, block in locations(body):
            serves = ("proxy_pass" in block or "alias " in block
                      or "root " in block)
            redirects = "return 301" in block or "return 302" in block
            if not (serves or redirects):
                continue
            if redirects:
                continue
            if (name or "").endswith("/api/auth/"):
                # Public by nature: a human WITHOUT a session has to be able to
                # log in, so the auth surface cannot be behind the gate. That
                # is the same decision gateway.conf makes for the bare
                # /api/auth/<verb> locations, and hubauth is what authenticates.
                continue
            assert f"auth_request /_gate/{slug};" in block, \
                f"ungated location {name!r}:\n{block}"
        # A page navigation gets the login redirect, a media/api request gets
        # JSON; using the wrong one is how a SPA poll gets an HTML body.
        gated_login = [n for n, b in locations(body)
                       if "error_page 401 403 = /_login-redir" in b]
        gated_json = [n for n, b in locations(body)
                      if "error_page 401 403 = @noauth" in b]
        assert any(n and n.endswith("/index.html") or n.endswith("/")
                   for n in gated_login)
        assert any(n and n.endswith("/api/") for n in gated_json)
        assert any(n and n.endswith("/cache/") for n in gated_json)


def test_locations_use_literal_ports_for_the_right_partition(two):
    body = gen.nginx_locations(two)
    hosts = gen.vault_hosts(two)
    for name, block in locations(body):
        m = re.search(r"proxy_pass http://127\.0\.0\.1:(\d+)(/?)", block)
        if not m:
            continue
        assert "$" not in m.group(0), f"interpolated upstream in {name}"
        port = int(m.group(1))
        # A regex location's name is its PATTERN, which starts with `^`; both
        # shapes carry the partition's slug right after it.
        mslug = re.match(r"^\^?/l/([a-z0-9_-]+)", name or "")
        if port in (8099, 8102):
            continue                      # hubauth and the shared pool
        slug = mslug.group(1) if mslug else two["front"]
        if "/vnc/" in (name or ""):
            # The door follows the VAULT, not the mount: a member's noVNC has to
            # reach the ONE browser its Google account owns, and a member that
            # shares a vault has no websockify of its own to reach.
            slug = hosts[two["partitions"][slug]["vault_slug"]]
        expected = set(gen.ports(two["partitions"][slug]["index"]).values())
        assert port in expected, f"{name}: port {port} is not {slug}'s"


def test_locations_declare_slug_and_user_headers_where_the_app_needs_them(two):
    body = gen.nginx_locations(two)
    for name, block in locations(body):
        if not name or not name.endswith(("/api/", "/grabby/", "/radio/")):
            continue
        if "proxy_pass" not in block:
            continue
        assert 'proxy_set_header X-Partition-Slug "' in block, name
    # The api block OVERWRITES a client-sent X-User from the gate's answer:
    # otherwise any caller could pick whose progress file it overwrites.
    api = dict(locations(body))["/l/family/api/"]
    assert "auth_request_set $gate_user $upstream_http_x_user;" in api
    assert "proxy_set_header X-User $gate_user;" in api
    # The pool job surface keeps its /api prefix and adds the slug so the pool
    # can attribute a job to a tenant.
    jobs = next(b for n, b in locations(body) if "jobs" in (n or ""))
    assert "X-Partition-Slug" in jobs and "/api/$1" in jobs


def test_generated_regexes_quote_their_braces(two):
    """nginx's tokenizer ends a location pattern at the first `{` outside a
    string: unquoted, `{1,40}` is the block opener and nginx -t dies with
    "missing closing parenthesis"."""
    body = gen.nginx_locations(two)
    # Only regex locations can be quoted, so that is what gets checked; a
    # bare-modifier prefix never carries a brace quantifier to quote about.
    for m in re.finditer(r"location (~\*?|~)\s+(\S+)", body):
        assert m.group(2).startswith('"'), f"unquoted regex: {m.group(0)}"
    # No regex location may be unquoted, and every quoted pattern must be
    # brace-balanced inside its quotes.
    for m in re.finditer(r'location ~\*? "[^"]*"', body):
        pat = m.group(0).split('"', 1)[1].rstrip('"')
        assert pat.count("{") == pat.count("}"), pat


def test_generated_nginx_has_no_doubled_braces(two):
    """Regression: the plain-string pieces of a mixed concatenation are NOT
    format-processed, so a `{{` written there reaches the file as two braces -
    nginx then opened a block and closed nothing, and no test was running
    `nginx -t` to notice. Every emitter here must render single braces only."""
    rendered = {"locations": gen.nginx_locations(two),
                "maps": gen.nginx_maps(two),
                "picker": gen.picker_html(two),
                "supervisor": gen.supervisor_conf(two)}
    rendered.update({f"auth:{n}": b for n, b in gen.auth_pages(two)})
    for slug in two["slugs"]:
        shells = dict(gen.spa_shells({**two, **{"slugs": [slug]}}))
        assert shells, "no shell for a registered slug"
        rendered[f"shell:{slug}"] = list(shells.values())[0]
    for name, body in rendered.items():
        assert "{{" not in body and "}}" not in body, name
        assert body.count("{") == body.count("}"), \
            f"unbalanced braces in {name}: {body.count('{')} vs {body.count('}')}"


def test_state_json_gets_no_cache_and_media_gets_long_cache(two):
    body = gen.nginx_locations(two)
    blocks = dict(locations(body))
    state = ("/l/family/cache/index.json", "/l/family/cache/status.json",
             "/l/family/cache/grabby-played.json",
             "/l/family/cache/completed.json", "/l/family/cache/progress.json")
    for name in state:
        # no-cache, not no-store: these files churn, but the SPA polls them on a
        # timer and the block sets `etag on`, so a revalidation costs 0 bytes.
        assert 'Cache-Control "no-cache"' in blocks[name], name
    # The api prefix takes its no-store from proxy-svc.conf and declares no
    # Cache-Control of its own. Two Cache-Control headers on one response is how
    # a media block's day of cache gets cancelled: a browser unions no-store with
    # anything else as "do not cache".
    api_block = blocks["/l/family/api/"]
    assert "include /etc/nginx/snippets/proxy-svc.conf;" in api_block
    assert "Cache-Control" not in api_block, api_block
    assert 'Cache-Control "no-store"' in \
        open(f"{REPO}/nginx/snippets/proxy-svc.conf").read()
    # The mp3/webp tree is written once and renamed into place, so its ETag is
    # exact and a day of cache costs nothing.
    media = blocks["/l/family/cache/"]
    assert "media-static.conf" in media
    snippet = open(f"{REPO}/nginx/snippets/media-static.conf").read()
    assert 'max-age=86400' in snippet


def test_progress_is_per_member_with_a_partition_fallback(two):
    body = gen.nginx_locations(two)
    blocks = dict(locations(body))
    exact = blocks["/l/family/cache/progress.json"]
    # try_files, not alias: an alias built from an unset $gate_user gives
    # `progress-.json` and 404s a legitimate member.
    assert "alias" not in exact
    assert "progress-$gate_user.json" in exact
    assert "try_files" in exact and "/users/family/cache/progress.json" in exact
    dotted = blocks["/l/family/cache/"]
    assert "progress\\.(?<puser>" in dotted
    # ^~ on the media prefix means a SIBLING regex could never win for these
    # URIs, so the dotted form has to be nested inside it.
    assert dotted.rstrip().endswith("}")
    assert dotted.count("location") == 2


def test_radio_mount_is_stripped_for_both_shapes(two):
    """The broadcaster's own routes are root-level; the gateway mounts them and
    has to take the mount back off. A regex location with a URI-less proxy_pass
    forwards the WHOLE matched path, so it needs an explicit rewrite."""
    body = gen.nginx_locations(two)
    blocks = dict(locations(body))
    stream = next(b for n, b in locations(body)
                  if n.endswith("/radio/stream(?:\\.mp3)?"))
    assert "rewrite ^/l/family/radio/" in body
    prefixed = blocks["/l/family/radio/"]
    assert prefixed[prefixed.index("proxy_pass"):].startswith(
        "proxy_pass http://127.0.0.1:8201/")
    assert "/;" in prefixed[prefixed.index("proxy_pass"):].split("\n")[0]
    # A bare mount with no trailing slash matches neither rule and must
    # redirect, not 404.
    assert "return 301 /l/family/radio/;" in blocks["/l/family/radio"]
    # The stream is the public broadcast surface: chunkless 1.0 framing.
    assert "proxy-stream.conf" in stream


def test_spa_shell_shows_the_mount_and_the_user(tmp_path, two, monkeypatch):
    spa = tmp_path / "www"
    spa.mkdir()
    (spa / "index.html.dist").write_text(
        '<!doctype html><html lang=en><head>'
        '<base href="/">'
        '<script src="main-ABCD1234.js"></script>'
        '<link rel="stylesheet" href="styles-XY.css">'
        '<link rel="icon" href="favicon.ico">'
        '<link rel="modulepreload" href="./chunk-9.js">'
        '</head><body><app-root></app-root></body></html>\n')
    monkeypatch.setattr(gen, "SPA_WWW", str(spa))
    shells = dict(gen.spa_shells(two))
    assert sorted(shells) == [f"{spa}/index.html", f"{spa}/l/family/index.html",
                              f"{spa}/l/owner/index.html"]
    shell = shells[f"{spa}/l/family/index.html"]
    assert '<base href="/l/family/">' in shell
    # The identity script has to come AFTER the closed <base> tag: text inside
    # an unclosed start tag is swallowed by the parser and the script never
    # runs - which is how this file once shipped neither base href nor user.
    base_end = shell.index(">", shell.index("<base"))
    script = shell.index("window.__HUB__")
    assert script > base_end
    assert shell.count("<base") == 1              # replaced, never appended
    assert "<base" not in shell[base_end:script]  # and it is the closed tag
    assert '"slug":"family"' in shell and '"user":"__HUB_USER__"' in shell
    # Relative bundle refs must become root-absolute: the shell lives under
    # /l/<slug>/ but the hashed bundles are portal-root locations, so a
    # relative ref would fetch HTML where JavaScript was expected.
    assert 'src="/main-ABCD1234.js"' in shell
    assert 'href="/styles-XY.css"' in shell
    assert 'href="/favicon.ico"' in shell
    assert 'href="/./chunk-9.js"' in shell
    # The front partition gets a root shell too, with base "/".
    root = shells[f"{spa}/index.html"]
    assert '<base href="/">' in root and '"slug":"owner"' in root
    # No template: shells appear on the next reload rather than breaking
    # generation.
    monkeypatch.setattr(gen, "SPA_WWW", str(tmp_path / "absent"))
    assert gen.spa_shells(two) == []


def test_absolutize_refs_leaves_absolute_and_attribute_values_alone():
    html = ('<a href="https://x/y">x</a><link href="data:,a">'
            '<img src="/keep/me.png"><a href="#frag">'
            '<link media="print" onload="this.href=\'x\'" href="late.css">')
    out = gen.absolutize_refs(html)
    assert 'href="/late.css"' in out
    assert "https://x/y" in out and "data:,a" in out and "#frag" in out
    assert 'src="/keep/me.png"' in out and 'media="print"' in out
    assert "this.href='x'" in out


def test_picker_and_auth_pages_agree_with_the_registry(two):
    picker = gen.picker_html(two)
    cards = re.findall(r'<a class="card" href="/l/([a-z0-9_-]+)/">', picker)
    assert cards == two["slugs"]
    assert "shared &middot; 2 members" in picker      # family: kid + car
    assert "personal &middot; 1 member" in picker     # owner alone
    pages = dict(gen.auth_pages(two))
    for name in ("login.html", "pair.html", "logout.html"):
        body = pages[f"{gen.HUB_WWW}/{name}"] if False else next(
            b for p, b in gen.auth_pages(two) if p.endswith(name))
        assert '<link rel=stylesheet href=/hub.css>' in body
    login = next(b for p, b in gen.auth_pages(two) if p.endswith("login.html"))
    options = re.findall(r'<option value="([a-z0-9_-]+)">', login)
    assert options == two["slugs"]
    pair = next(b for p, b in gen.auth_pages(two) if p.endswith("pair.html"))
    # The pairing hint has to name the front slug the operator types at the
    # CLI, not a placeholder.
    assert "hub code owner" in pair
    assert "%(options)s" not in login and "%(front)s" not in pair


def test_auth_pages_carry_the_script_and_post_the_right_way(two):
    """Every door page loads hub.js AND declares the no-JS fallback POST.

    The pages carry `data-action=login`, and hub.js is what turns that into a
    fetch. Without a `<script>` tag it is an ordinary HTML form, which submits
    GET to its own URL: clicking Log in rewrote the address bar to
    `/login?slug=owner&username=owner&password=<secret>` and redisplayed the
    empty door (live report 2026-11-15 - the gateway access log recorded the
    password in cleartext and a fresh 200 `/login` per "attempt"), so it looked
    like nothing happened and the secret landed in history and in the log.

    Both halves matter: the script is what makes the scripted path work, and
    `method=post action=/api/auth/<verb>` means a browser that does not run
    hub.js (scripting disabled, a car head-unit that blocks it, a stale page
    whose script request failed) still sends credentials as a form body instead
    of a query string.
    """
    for verb in ("login", "pair", "logout"):
        body = next(b for p, b in gen.auth_pages(two)
                    if p.endswith(f"{verb}.html"))
        assert '<script src=/hub.js defer></script>' in body, verb
        assert (f'<form data-action={verb} method=post '
                f'action=/api/auth/{verb}>') in body, verb
        for tag in re.findall(r"<form\b[^>]*>", body):
            assert "method=post" in tag, (verb, tag)
        assert body.index("</head>") < body.index("<body"), verb
    # The picker has no form, so it needs no script; it must still offer doors.
    picker = gen.picker_html(two)
    assert "href=/login" in picker and "href=/pair" in picker

    # The asset it points at is real and shipped, and the gateway serves it
    # ungated (a 404 here is how the scripted path would silently die).
    script = pathlib.Path(REPO) / "hub" / "hub.js"
    assert script.exists() and "api/auth/login" in script.read_text()
    conf = open(f"{REPO}/nginx/gateway.conf").read()
    assert 'location = /hub.js' in conf

def test_gateway_exposes_the_portal_and_deny_shape():
    """Static-file contract the generated blocks depend on: `@noauth` is the
    401 every generated media/api location names, and the gate plumbing has to
    hand the gate the parent's URI so ?pass= can be judged by path."""
    conf = open(f"{REPO}/nginx/gateway.conf").read()
    assert """return 401 '{"error":"unauthorized","login":"/login"}'""" in conf
    assert conf.count("location @noauth") == 1
    assert "location = /_login-redir" in conf and "next=$request_uri" in conf
    assert "proxy_set_header X-Gate-Uri $request_uri;" in conf
    assert "proxy_set_header Cookie $http_cookie;" in conf
    assert 'include /etc/nginx/stack-locations/*.conf;' in conf
    # The server block MUST pin the redirect form: without `absolute_redirect off`
    # this image's nginx (1.22) rewrites every generated 301/302 into
    # `http://<Host>/...`, locking the scheme to http behind the VPS's TLS.
    assert re.search(r"^\s*absolute_redirect\s+off;", conf, re.M), \
        "gateway.conf must set `absolute_redirect off`"
    # No resolver and no variables in the gate target: every upstream is a
    # literal from gen-conf, so a missing name cannot fail at request time.
    # Only DIRECTIVES count here: the file explains in a comment why no resolver
    # is configured, and asserting on the whole text would fail on its own prose.
    directives = "\n".join(l for l in conf.splitlines()
                           if not l.strip().startswith("#"))
    assert "resolver" not in directives
    # Every gate target is a literal from gen-conf: no variable, no name to
    # resolve, so a missing service name cannot fail at request time.
    assert "proxy_pass http://127.0.0.1:8099/gate/$gs" in directives
    assert conf.count('"^/_gate/(?<gs>') == 1


def test_maps_file_records_the_radio_pass_decision(two, monkeypatch):
    monkeypatch.setattr(gen, "DATA", two["partitions"]["owner"]["vault"])
    maps = gen.nginx_maps(two)
    assert maps.startswith("# generated by platform/gen-conf.py")
    assert maps.count("{") == maps.count("}")
    assert "unset" in maps
    monkeypatch.setenv("RADIO_PASS", "letmein")
    assert "set" in gen.nginx_maps(two).splitlines()[-1]


# ------------------------------------------------------------------ main
def test_main_writes_every_target_atomically(gen_conf_dirs, monkeypatch):
    roots = gen_conf_dirs
    monkeypatch.setenv("SITE_DOMAIN", "listen.example.com")
    reg = json_load(roots["registry"])
    assert reg["defaultSlug"] == "owner"
    rc = roots["gen"].main()
    assert rc == 0
    sup = (roots["etc"] / "supervisor/conf.d/stack-programs.conf").read_text()
    loc = (roots["etc"] / "nginx/stack-locations/10-locations.conf").read_text()
    maps = (roots["etc"] / "nginx/conf.d/05-maps.conf").read_text()
    assert "[program:drivepod-owner]" in sup and "[program:tunester]" in sup
    assert "location /l/owner/api/" in loc
    assert "location /api/" in loc               # front aliases
    assert (roots["hub"] / "index.html").exists()
    assert (roots["hub"] / "login.html").exists()
    assert (roots["spa"] / "l/owner/index.html").read_text(
    ).count("base href") == 1
    # One shell per REGISTERED slug and no others: the seeded registry holds just
    # owner, so no family shell exists yet here - and `l/family/index.html` after
    # a partition add (the next test) is what proves main() creates the directory
    # the image ships no trace of (/var/www/drivepod/l/<slug>).
    assert (roots["spa"] / "l/owner").is_dir()
    assert not (roots["spa"] / "l/family").exists()
    for sub in ("cache", "library", "tmp", "logs"):
        assert (roots["data"] / "users/owner" / sub).is_dir()
    assert (roots["data"] / "vaults/owner").is_dir()
    # Atomic: no .tmp file survives a successful run.
    leftovers = [str(p.relative_to(roots["etc"])) for p in
                 roots["etc"].rglob("*.tmp")]
    assert leftovers == []
    before = {p: p.read_bytes() for p in (roots["etc"] / "nginx/stack-locations").glob("*")}
    assert roots["gen"].main() == 0
    assert {p: p.read_bytes() for p in
            (roots["etc"] / "nginx/stack-locations").glob("*")} == before
    assert maps == (roots["etc"] / "nginx/conf.d/05-maps.conf").read_text()


def test_main_survives_a_partition_add(gen_conf_dirs):
    roots = gen_conf_dirs
    assert roots["gen"].main() == 0
    body = (roots["etc"] / "nginx/stack-locations/10-locations.conf").read_text()
    assert "/l/family" not in body
    # The default registry the fixture seeds is front=owner only; add family.
    reg = json.loads(roots["registry"].read_text())
    reg["slugs"].append("family")
    # `vault: "owner"` is the point of this registry: family signs in as the
    # front partition's Google account, so it gets no browser of its own.
    reg["partitions"]["family"] = {"mode": "shared", "name": "Family",
                                   "owner": "owner", "vault": "owner"}
    roots["registry"].write_text(json.dumps(reg, indent=2))
    assert roots["gen"].main() == 0
    body = (roots["etc"] / "nginx/stack-locations/10-locations.conf").read_text()
    assert "location /l/family/api/" in body
    sup = (roots["etc"] / "supervisor/conf.d/stack-programs.conf").read_text()
    # family names vault "owner", the same account the front partition signs in
    # as, so it gets the app programs but NO vault program of its own: the one
    # vault-owner family already serves it (see the dedup tests below).
    assert ("[program:drivepod-family]" in sup
            and "[program:frogrock-family]" in sup)
    assert "[program:vault-family]" not in sup
    assert (roots["spa"] / "l/family/index.html").read_text().count(
        '<base href="/l/family/">') == 1


def json_load(path):
    return json.loads(path.read_text())


# ------------------------------------------------- supervisor parsing proof
def test_environment_strings_parse_with_supervisors_own_parser(two):
    """gen-conf quotes every value because supervisor's parser is shlex-based
    and a comma separates pairs - TUNESTER_COOKIES and the push URL lists are
    comma-separated VALUES. Verified against the shipped supervisor, not
    against gen-conf's own inverse."""
    pytest.importorskip("supervisor.datatypes")
    from supervisor.datatypes import dict_of_key_value_pairs
    sup = gen.supervisor_conf(two)
    for line in sup.splitlines():
        if line.startswith("environment="):
            vals = dict_of_key_value_pairs(line[len("environment="):])
            assert vals, line
            for k, v in vals.items():
                assert k == k.upper() and not v.startswith('"'), k
                assert '"' not in v, f"{k}={v!r} would break the parser"
    pool = dict_of_key_value_pairs(
        [l for l in sup.splitlines()
         if l.startswith("environment=") and "TUNESTER_COOKIES" in l][0]
        [len("environment="):])
    assert pool["TUNESTER_COOKIES"].count(",") == len(two["slugs"]) - 1


def test_generated_supervisor_file_is_valid_to_supervisor(two, tmp_path):
    """Parse the generated file with supervisor's own reader, on the path
    `supervisord` itself takes at start: `ServerOptions.read_config` over the
    real platform base conf plus an `[include]` of the generated programs. That
    means section kinds, every option's datatype and the priority values are
    supervisor's verdict rather than this test's approximation - a `[group:]`
    where a `[program:]` belongs, or a `startsecs` that is not an integer, fails
    here and nowhere else. The base conf's absolute log/pid/socket paths are
    rewritten into tmp_path so supervisor's existing-directory checks pass
    unprivileged.
    """
    options = pytest.importorskip("supervisor.options")
    body = gen.supervisor_conf(two)
    conf_dir, logs, run = tmp_path / "conf.d", tmp_path / "logs", tmp_path / "run"
    for d in (conf_dir, logs, run):
        d.mkdir()
    (conf_dir / "programs.conf").write_text(body)
    text = open(f"{REPO}/platform/supervisord.conf").read()
    text = re.sub(r"^logfile=.*$", f"logfile={logs}/supervisord.log",
                  text, flags=re.M)
    text = re.sub(r"^childlogdir=.*$", f"childlogdir={logs}", text, flags=re.M)
    text = re.sub(r"^pidfile=.*$", f"pidfile={run}/supervisord.pid",
                  text, flags=re.M)
    text = re.sub(r"^file=/var/run/supervisor.sock$", f"file={run}/supervisor.sock",
                  text, flags=re.M)
    text += f"\n[include]\nfiles={conf_dir}/*.conf\n"
    base = tmp_path / "supervisord.conf"
    base.write_text(text)
    so = options.ServerOptions()
    so.configfile = str(base)
    so.here = str(tmp_path)
    with open(base) as fh:
        so.read_config(fh)
    # Zero criticals AND zero warnings. In the shipped supervisor parse_infos
    # holds plain strings and the "Included extra file" note lands THERE, not in
    # parse_warnings, so the include note is the only thing that may appear.
    assert so.parse_criticals == [] and so.parse_warnings == []
    assert all(i.startswith("Included extra file") for i in so.parse_infos)
    procs = {p.name: p.priority
             for g in so.configroot.supervisord.process_group_configs
             for p in g.process_configs}
    assert sorted(procs) == sorted(program_blocks(body))
    # Priority order encodes the real dependencies: gate before proxy.
    assert procs["hubauth"] < procs["nginx"]
    assert procs["bgutil"] < procs["tunester"]


# ------------------------------------------------- nginx validation (in-image)
ONE = {"slugs": ["owner"], "defaultSlug": "owner",
       "partitions": {"owner": {"mode": "single", "name": "Home",
                                "owner": "owner", "radio": True,
                                "vault": "owner"}}}


def _nginx_tree(tmp_path, loc_body, maps_body):
    """Lay the generated files out the way the image does and return the master
    conf to validate them with.

    `nginx -t` is the only parser here that will not forgive a mistake: it is
    what the ENTRYPOINT runs before supervisord, and the production layout is
    conf.d/05-maps + conf.d/10-platform + conf.d/20-gateway + the generated
    stack-locations. The single rewrite is the gateway's `include` line, so the
    generated file is read from the tmpdir instead of the live /etc path - a
    test run inside a live container must not leave tmpdir paths where the next
    `nginx -s reload` would read them.
    """
    confd, locdir = tmp_path / "conf.d", tmp_path / "locations"
    confd.mkdir(parents=True)
    locdir.mkdir(parents=True)
    (confd / "05-maps.conf").write_text(maps_body)
    gateway = open(f"{REPO}/nginx/gateway.conf").read()
    target = f"include {locdir}/*.conf;"
    assert "include /etc/nginx/stack-locations/*.conf;" in gateway, \
        "the gateway no longer includes the generated locations at all"
    gateway = gateway.replace("include /etc/nginx/stack-locations/*.conf;",
                              target)
    (confd / "20-gateway.conf").write_text(gateway)
    (confd / "10-platform.conf").write_text(
        open(f"{REPO}/nginx/platform.conf").read())
    (locdir / "10-locations.conf").write_text(loc_body)
    conf = tmp_path / "nginx.conf"
    conf.write_text(
        f"pid {tmp_path}/nginx.pid;\n"
        f"error_log {tmp_path}/error.log warn;\n"
        "events { worker_connections 128; }\n"
        "http {\n"
        "  include /etc/nginx/mime.types;\n"
        "  default_type application/octet-stream;\n"
        f"  access_log {tmp_path}/access.log;\n"
        f"  include {confd}/*.conf;\n"
        "}\n")
    return conf


def _run_nginx(conf):
    return subprocess.run(["nginx", "-t", "-c", str(conf)],
                          capture_output=True, text=True)


# ---------------------------------------------------------------- serving
# `nginx -t` validates syntax only. A directive can be perfectly legal and
# still make the block unservable - a `return` in SERVER context aborts every
# request in the rewrite phase, before any location is looked up - so these
# tests start a real nginx over exactly the tree the image assembles and ask
# it for the paths the gateway is supposed to answer.

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep 301/302 as the answer under test instead of following them: the
    redirects ARE the contract (bare slug -> slash, gated page -> login door)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _world_readable(root):
    """Open the harness tree the way nginx needs it, for two different reasons.

    The served files must be other-readable because the worker is not root (in
    the container it is `www-data` reading a root-owned `/data`), and every
    ANCESTOR must be other-EXECUTABLE because a directory that is 0700 denies a
    worker every path below it even when the file itself is 0644 - which is
    exactly what pytest's basetemp (`/tmp/pytest-of-<user>/pytest-N`, 0700) does
    to a harness tree: the master, running as root, reads everything and
    `nginx -t` says nothing, while the worker answers 404/500 and logs `stat()
    ... failed (13: Permission denied)`. Only `o+rx` is ever added: nothing here
    becomes writable by other, and `/` is left alone.
    """
    root = pathlib.Path(root)
    for d in (root, *root.parents):
        if str(d) == "/":
            continue
        os.chmod(d, os.stat(d).st_mode | 0o055)
    for here, dirs, files in os.walk(root):
        for name in dirs:
            p = os.path.join(here, name)
            os.chmod(p, os.stat(p).st_mode | 0o055)
        for name in files:
            p = os.path.join(here, name)
            os.chmod(p, os.stat(p).st_mode | 0o044)


def _relocate(text, serve_port, stub_port, hub_www, spa_www):
    """Move the gateway's listener, every upstream dial and the two www roots
    onto harness-local ports and directories.

    EVERY `127.0.0.1:<port>` becomes the stub's port - the hubauth dials
    (the gate's proxy_pass, the four /api/auth verbs and the generated
    per-partition api/auth proxy) because they belong to the stub, and every
    backend dial (drivepod, grabby, radio, tunester, websockify) because the
    harness runs none of them. Two independent reasons, both necessary: a dial
    to a port nothing listens on logs `connect() failed (111)` and answers 502,
    which would make the "error.log stayed empty" assertion unwinnable; and
    inside the running container those ports ARE live, so a backend left in
    place would answer for the harness and let a test pass against a service it
    never started. The stub answers any non-gate path with 200 JSON, so a
    gated-through request has somewhere to land.

    `:80` must move because the suite runs inside a container that already owns
    it, and the IPv6 twin is dropped rather than doubled onto one loopback
    address.

    The two `root`s the GATEWAY names are image paths that only gen-conf fills,
    at container start: left alone, `/hub.js` would be answered by the image's
    own baked `/var/www/hub`, so the test would pass for the wrong reason inside
    a container and fail anywhere else. Both are repointed at the tree this
    fixture writes, so a served static proves the location reaches the file
    gen-conf writes - not whatever the image happens to ship.
    """
    dialed = len(re.findall(r"127\.0\.0\.1:\d+", text))
    text = re.sub(r"127\.0\.0\.1:\d+", f"127.0.0.1:{stub_port}", text)
    assert dialed, "no 127.0.0.1:<port> upstream to relocate"
    text = text.replace("/var/www/hub", str(hub_www))
    text = text.replace("/var/www/drivepod", str(spa_www))
    text = re.sub(r"listen \[::\]:\d+[^\n]*\n", "", text)
    return re.sub(r"listen \d+ default_server;",
                  f"listen 127.0.0.1:{serve_port} default_server;", text)


def _stub_hubauth(port):
    """A hubauth stand-in: `GET /gate/<slug>` answers the gate's contract,
    everything else answers 200 JSON.

    `server.allow_gate` is read per request so a test can put identity on the
    wire mid-test without rebinding the port: the gate answers 200 + X-User +
    X-Partition-Slug when it is True (request goes through) and 401 when it is
    False (the location's own error_page decides what the client sees).
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _answer(self):
            gated = self.path.startswith("/gate")
            allow = getattr(self.server, "allow_gate", False)
            body = b"" if gated else b'{"ok":true,"user":"owner"}'
            self.send_response(200 if (allow or not gated) else 401)
            self.send_header("Content-Length", str(len(body)))
            if not gated:
                self.send_header("Content-Type", "application/json")
            elif allow:
                self.send_header("X-User", "owner")
                self.send_header("X-Partition-Slug", self.path.split("/")[2])
            self.end_headers()
            if body:
                self.wfile.write(body)

        do_GET = do_POST = do_HEAD = _answer

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", port), Handler)
    srv.allow_gate = False
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _fetch(port, path, headers=None, data=None):
    """(status, headers, body) for one harness request, redirects un-followed.

    `data` turns it into a POST; nothing else about the request changes, so a
    GET-era assertion and a body-carrying one read the same way."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers=headers or {}, data=data)
    try:
        with _OPENER.open(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _serve(tmp_path, monkeypatch, start_identity):
    """Two partitions, gen-conf rooted in tmp_path, and a real nginx serving it.

    `start_identity(port)` supplies whoever answers the gate on the harness's
    stub port - the stand-in for most tests (fast, and lets a test flip
    allow/deny mid-run) or the REAL platform/hubauth.py for the credential-
    format tests, which a stub that ignores the query cannot fail.

    gen-conf's module constants (DATA/HUB_WWW/SPA_WWW/STACK) are repointed
    BEFORE the emitters run, so every `root`/`alias` the generated file names
    is a tmp path: the harness cannot read or write a live partition's files,
    and it cannot talk to a live backend either (the tree only answers paths a
    location serves itself). The tree is chmod'd world-readable because nginx
    workers drop root, and a pytest tmpdir is 0700.
    """
    data, www = tmp_path / "data", tmp_path / "www"
    hub_www, spa_www = www / "hub", www / "drivepod"
    (data / "users").mkdir(parents=True)
    hub_www.mkdir(parents=True)
    spa_www.mkdir(parents=True)
    novnc = tmp_path / "novnc"
    (novnc / "core").mkdir(parents=True)
    # The two forms the door serves: the lite page, whose stock default ws path
    # must come out partition-specific, and one module under it, which must NOT
    # be rewritten (its own `ws://host/websockify` fallback contains the same
    # bare string).
    (novnc / "vnc_lite.html").write_text(
        "<!doctype html><title>noVNC</title>\n"
        "<script>const path = readQueryVariable('path', 'websockify');\n"
        "url += '/' + path;</script>\n")
    (novnc / "core/rfb.js").write_text(
        "this._socket.open('ws://' + host + '/websockify');\n")
    for attr, value in (("DATA", data), ("HUB_WWW", hub_www),
                        ("SPA_WWW", spa_www), ("NOVNC_DIR", novnc),
                        ("STACK", tmp_path / "stack")):
        monkeypatch.setattr(gen, attr, str(value))
    registry(tmp_path, TWO, monkeypatch)
    reg = gen.load_registry()

    serve, stub = _free_port(), _free_port()
    confd, locdir = tmp_path / "conf.d", tmp_path / "locations"
    confd.mkdir()
    locdir.mkdir()
    (confd / "05-maps.conf").write_text(gen.nginx_maps(reg))
    (confd / "10-platform.conf").write_text(
        open(f"{REPO}/nginx/platform.conf").read())
    gateway = open(f"{REPO}/nginx/gateway.conf").read()
    assert "include /etc/nginx/stack-locations/*.conf;" in gateway, \
        "the gateway no longer includes the generated locations at all"
    gateway = _relocate(
        gateway.replace("include /etc/nginx/stack-locations/*.conf;",
                        f"include {locdir}/*.conf;"), serve, stub,
        hub_www, spa_www)
    assert f"listen 127.0.0.1:{serve} default_server;" in gateway, \
        "the gateway has no `listen <port> default_server;` to rebind"
    (confd / "20-gateway.conf").write_text(gateway)
    (locdir / "10-locations.conf").write_text(
        _relocate(gen.nginx_locations(reg), serve, stub, hub_www, spa_www))
    conf = tmp_path / "nginx.conf"
    conf.write_text(
        f"pid {tmp_path}/nginx.pid;\n"
        f"error_log {tmp_path}/error.log warn;\n"
        "events { worker_connections 128; }\n"
        "http {\n"
        "  include /etc/nginx/mime.types;\n"
        "  default_type application/octet-stream;\n"
        f"  access_log {tmp_path}/access.log;\n"
        f"  include {confd}/*.conf;\n"
        "}\n")

    # the files the gateway's static locations read
    (hub_www / "hub.js").write_text("// hub\n")
    (hub_www / "hub.css").write_text(":root{color:red}\n")
    (hub_www / "robots.txt").write_text("User-agent: *\n")
    for path, body in gen.auth_pages(reg):
        open(path, "w").write(body)
    (hub_www / "index.html").write_text(gen.picker_html(reg))

    _world_readable(tmp_path)

    srv = start_identity(stub)
    nginx = subprocess.Popen(["nginx", "-c", str(conf)],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    deadline = time.time() + 15

    def stop():
        nginx.terminate()
        nginx.wait(timeout=15)
        srv.shutdown()

    try:
        while time.time() < deadline:
            try:
                _fetch(serve, "/health")
                break
            except OSError:
                if nginx.poll() is not None:
                    log = (tmp_path / "error.log")
                    stop()
                    raise AssertionError(
                        "harness nginx would not start: "
                        + (log.read_text() if log.exists() else "no error.log"))
                time.sleep(0.1)
        else:
            stop()
            raise AssertionError("harness nginx never answered on its port")
        yield {"reg": reg, "port": serve, "stub_port": stub, "srv": srv,
               "data": data, "www": www, "tmp": tmp_path, "conf": conf}
    finally:
        stop()


@pytest.fixture
def served_two(tmp_path, monkeypatch):
    yield from _serve(tmp_path, monkeypatch, _stub_hubauth)


@pytest.fixture
def served_ident(tmp_path, monkeypatch, reload_modules):
    """The same harness, but the gate is the REAL platform/hubauth.py.

    Which credential FORM a caller may use - cookie, bearer header, `?key=`,
    `?pass=` - is decided by hubauth from the request the GATEWAY forwards, and
    `_stub_hubauth` ignores everything but the path. A query-carrying form is
    therefore only proved end to end here, with the generated locations, the
    gateway's gate plumbing and the real identity server all on the wire.

    The identity server is started on the harness's stub port, which is what
    every relocated dial points at, and hubauth is reloaded after the env is
    set because it freezes DB path, registry path and the radio pass at import
    (conftest.reload_modules puts the real modules back afterwards).

    Because the harness runs no apps, hubauth is ALSO the stand-in upstream for
    every api/radio/jobs dial, and it answers a path it does not implement with
    its own 404 `{"error":"not-found"}`. That is what makes a gate decision
    readable: 404 `not-found` = the gate let the request through and the
    upstream has no route, while 401 with @noauth's `unauthorized` body (or a
    302 to the login door) = the gate refused it. A test that asserted 200 on a
    gated api path here would be asserting that hubauth serves drivepod's API.

    Identity seeded: `owner` is admin in both partitions, `kid` and `car` are
    members of `family` only. Three sessions - one per member, each bound to
    the partition it was minted for - plus owner's api key and kid's, so a test
    can tell "wrong partition" from "not a member of that partition" from "no
    such credential".
    """
    import threading
    from http.server import ThreadingHTTPServer

    data = tmp_path / "data"
    creds = {}
    monkeypatch.setenv("HUB_DB", str(data / "hub.db"))
    monkeypatch.setenv("HUB_REGISTRY", str(tmp_path / "partitions.json"))
    monkeypatch.setenv("HUB_RADIO_PASS", "streampass")
    hubdb_m, hubauth_m = reload_modules("hubdb", "hubauth")

    def start(port):
        conn = hubdb_m.init()
        hubdb_m.add_user(conn, "owner", "ownerpass")
        hubdb_m.add_user(conn, "kid", "kidpassword")
        hubdb_m.add_user(conn, "car", "carpassword")
        for slug in ("owner", "family"):
            hubdb_m.set_membership(conn, slug, "owner", "admin")
        for name in ("kid", "car"):
            hubdb_m.set_membership(conn, "family", name, "member")
        creds["key"] = hubdb_m.regen_api_key(conn, "owner")
        creds["kid_key"] = hubdb_m.regen_api_key(conn, "kid")
        creds["owner_sess"] = hubdb_m.mint_session(conn, "owner", "owner", "pc")
        creds["kid_sess"] = hubdb_m.mint_session(conn, "family", "kid", "phone")
        creds["car_sess"] = hubdb_m.mint_session(conn, "family", "car", "car")
        conn.close()
        srv = ThreadingHTTPServer(("127.0.0.1", port), hubauth_m.Hub)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    gen_ = _serve(tmp_path, monkeypatch, start)
    env = next(gen_)
    env.update(creds, radio_pass="streampass")
    try:
        yield env
    finally:
        next(gen_, None)

def _media_tree(env, slug="owner"):
    """One SPA shell, one cache index, one progress pair and one mp3 for the
    partition whose bare aliases the front is, so the locations under test have
    real files to answer with."""
    home = env["data"] / "users" / slug
    (home / "cache").mkdir(parents=True)
    (home / "library").mkdir(parents=True)
    for part in env["reg"]["slugs"]:
        shell = env["www"] / "drivepod/l" / part
        shell.mkdir(parents=True, exist_ok=True)
        (shell / "index.html").write_text(
            f'<!doctype html><html><head><base href="/l/{part}/">'
            "</head><body>who=__HUB_USER__</body></html>\n")
        os.chmod(shell, 0o755)
    (home / "cache/index.json").write_text('{"files":[]}\n')
    # DASHED member file for the bare form (progress.json -> progress-<X-User>
    # .json) and one for the dotted form too: the dotted URI `progress.kid.json`
    # captures `kid` and resolves to progress-KID.json, because the file on disk
    # is dashed and only the URL alias is dotted.
    (home / "cache/progress-owner.json").write_text('{"pos":11}\n')
    (home / "cache/progress-kid.json").write_text('{"pos":22}\n')
    (home / "library/Artist - Song.mp3").write_bytes(b"ID3\x03\x00\x00"
                                                     + b"\x00" * 300)
    _world_readable(env["tmp"])
    return home

def _both_partition_media(env):
    """Real files in BOTH homes, distinct per partition, so a 200 also proves
    WHICH home the gate selected - plus one progress file per family member.

    The bare `progress.json` (pos 55) is the pre-hub file the try_files
    fallback would serve if the gate named nobody: giving it a position no
    member file carries is what turns "the identity decided the filename" into
    a real assertion instead of a file that merely happens to be there.
    """
    front = _media_tree(env)
    fam = _media_tree(env, "family")
    for name, pos in (("progress-owner", 99), ("progress-kid", 88),
                      ("progress-car", 77)):
        (fam / "cache" / f"{name}.json").write_text(f'{{"pos":{pos}}}\n')
    for home in (front, fam):
        (home / "cache/progress.json").write_text('{"pos":55}\n')
    _world_readable(env["tmp"])
    return front, fam

@nginx_required
def test_served_gate_accepts_every_documented_credential_form(served_ident):
    """The credential FORMS the README promises, over the wire.

    `_stub_hubauth` decides allow/deny from one boolean, so it cannot fail a
    wrong credential FORM. `?key=` used to die at the gate for exactly that
    reason: the `auth_request` subrequest nginx sends has an EMPTY `$args` of
    its own - measured against nginx 1.22 for the exact-, regex- and no-URI-form
    `proxy_pass` idioms alike, its request line is `/gate/<slug>` with no `?...`
    at all - so the caller's query reaches hubauth ONLY through the gateway's
    `X-Gate-Uri` header. A regression there reads exactly like "every partition
    is broken", from the inside, with nothing in `nginx -t` to say why.

    Reading the answers needs the harness's stand-in upstream in mind: the
    harness runs no apps, so every api/radio/jobs dial points at hubauth too,
    and hubauth answers a path it does not implement with its OWN 404
    `{"error":"not-found"}`. `not-found` therefore means "the gate let this
    through and the upstream has no route", while 401 with @noauth's
    `unauthorized` body - or a 302 to the login door - means the gate refused
    it. Paths nginx serves from disk answer with real bytes, and WHICH file it
    answers with depends on who the gate said the caller was.
    """
    env = served_ident
    port, key, kid_key = env["port"], env["key"], env["kid_key"]
    _both_partition_media(env)
    owner_cookie = {"Cookie": f"listen_sess={env['owner_sess']}"}
    kid_cookie = {"Cookie": f"listen_sess={env['kid_sess']}"}
    NOAUTH = b"unauthorized"          # the gateway's own refusal body

    def call(path, form, who="owner"):
        """One request, in one credential FORM, as one identity. The query
        form has to join the path's own query with `&` - and that is part of
        the contract, because a gate that split `?slug=owner&key=…` anywhere
        else than at the first `&` would read the slug as `owner&key=…` and
        answer unknown-partition."""
        token, cookie = (key, owner_cookie) if who == "owner" else \
            (kid_key, kid_cookie)
        if form == "query":
            return _fetch(port, path + ("&key=" if "?" in path else "?key=")
                          + token)
        if form == "bearer":
            return _fetch(port, path, {"Authorization": f"Bearer {token}"})
        return _fetch(port, path, cookie)

    def reached_upstream(status, body):
        """The gate passed and the stand-in upstream answered: hubauth's 404,
        NOT the gateway's 401 and not nginx's no-location-matched page."""
        return status == 404 and b"not-found" in body

    # ---- nothing offered ------------------------------------------------
    status, hdr, body = _fetch(port, "/api/cache/status")
    assert status == 401 and NOAUTH in body, body[:120]
    assert "Bearer" in hdr.get("WWW-Authenticate", ""), hdr
    status, _, body = _fetch(port, "/library/Artist%20-%20Song.mp3")
    assert status == 401 and NOAUTH in body, status
    status, hdr, _ = _fetch(port, "/l/family/")
    parts = urllib.parse.urlsplit(hdr.get("Location", ""))
    assert status == 302 and f"{parts.path}?{parts.query}" == \
        "/login?next=/l/family/", hdr

    # ---- the three forms, same identity through each --------------------
    # `?key=` is the form a car player or podcast app has to use (it cannot put
    # a header on a stream URL) and the one the gate used to ignore.
    # /api/auth/me is ungated and answered by hubauth itself, so the identity
    # arrives in a body instead of in the header the proxy would overwrite.
    EXPECT = {"slug": "owner", "username": "owner", "role": "admin"}
    for form in ("bearer", "cookie", "query"):
        status, _, body = call("/api/auth/me?slug=owner", form)
        assert status == 200, (form, status, body[:120])
        assert json.loads(body) == EXPECT, (form, body)
        # ...and through the mounted form of the same auth mount, whose rewrite
        # has to take the mount off the PATH while leaving the query - slug
        # AND credential - alone, or the answer would change with the form.
        status, _, body = call("/l/owner/api/auth/me?slug=owner", form)
        assert status == 200 and json.loads(body) == EXPECT, (form, status, body)
        # The media tree answers with a FILE, and which file is decided by the
        # identity the gate asserted: front partition, owner's own progress.
        status, _, body = call("/cache/progress.json", form)
        assert status == 200 and b'"pos":11' in body, (form, status, body)
        status, _, body = call("/library/Artist%20-%20Song.mp3", form)
        assert status == 200 and body.startswith(b"ID3"), (form, status, body[:8])
        # A gated api call reaches its backend.
        status, _, body = call("/api/cache/status", form)
        assert reached_upstream(status, body), (form, status, body[:120])
    # The same four paths, kid's identity, kid's partition: every form carries
    # an identity, not just a permission, so a mounted partition's media and
    # api all answer as kid.
    for form in ("bearer", "cookie", "query"):
        status, _, body = call("/l/family/api/auth/me?slug=family", form, "kid")
        assert status == 200 and json.loads(body) == {
            "slug": "family", "username": "kid", "role": "member"}, \
            (form, status, body[:120])
        status, _, body = call("/l/family/cache/progress.json", form, "kid")
        assert status == 200 and b'"pos":88' in body, (form, status, body[:120])
        status, _, body = call("/l/family/library/Artist%20-%20Song.mp3",
                               form, "kid")
        assert status == 200 and body.startswith(b"ID3"), (form, status, body[:8])
        status, _, body = call("/l/family/api/cache/status", form, "kid")
        assert reached_upstream(status, body), (form, status, body[:120])

    # ---- the one asymmetry: a session is bound, an api key is not ---------
    # owner is admin in BOTH partitions, so owner's key opens family's media and
    # lands on family's copy of OWNER's file (pos 99, not the front's 11 and not
    # kid's 88). owner's session does not open family at all - and that is the
    # boundary the README promises, not an oversight: a `/l/family/…` location
    # proxies to family's apps, so the session has to be family's. Substituting
    # the session's own slug instead of refusing would let a front session walk
    # into family while every membership check still passed.
    for form in ("bearer", "query"):
        status, _, body = call("/l/family/cache/progress.json", form)
        assert status == 200 and b'"pos":99' in body, (form, status, body[:120])
    for path, form in (("/l/family/cache/progress.json", "cookie"),
                       ("/l/family/api/cache/status", "cookie"),
                       ("/l/family/library/Artist%20-%20Song.mp3", "cookie")):
        status, _, body = call(path, form)
        assert status == 401 and NOAUTH in body, (path, status, body[:120])
    # kid is in family only, so the front stays shut for kid in every form -
    # a session because it is bound elsewhere, a key because kid is no member.
    for path in ("/cache/progress.json", "/l/owner/cache/progress.json",
                 "/l/owner/api/cache/status"):
        for form in ("bearer", "cookie", "query"):
            status, _, body = call(path, form, "kid")
            assert status == 401 and NOAUTH in body, (path, form, body[:120])
    # The ungated portal endpoint says the same thing in its own words, naming
    # the reason the gateway flattens into one body: hubauth answers /api/auth/*
    # directly, so `not-a-member` survives to the client.
    status, _, body = call("/api/auth/me?slug=owner", "query", "kid")
    assert status == 401 and b"not-a-member" in body, (status, body)
    status, _, body = _fetch(port, "/api/cache/status?key=lk_nosuchkey")
    assert status == 401 and NOAUTH in body, (status, body[:120])

    # ---- ?pass= is the stream allowance, and the stream ONLY -------------
    stream = "?pass=" + env["radio_pass"]
    for path in ("/l/owner/radio/stream.mp3", "/l/owner/radio/stream",
                 "/radio/stream.mp3"):
        status, _, body = _fetch(port, path + stream)
        assert reached_upstream(status, body), (path, status, body[:120])
    # Nowhere else. The pass names no member, so the GATE scopes it to the
    # stream path (hubauth.gate_path) rather than a nginx map that two files
    # would have to keep in step.
    for path in ("/api/cache/status", "/library/Artist%20-%20Song.mp3",
                 "/cache/progress.json", "/l/family/api/cache/status",
                 "/l/family/cache/progress.json"):
        status, _, body = _fetch(port, path + stream)
        assert status == 401 and NOAUTH in body, ("?pass= unlocked " + path)
    status, _, body = _fetch(port, "/l/owner/radio/stream.mp3?pass=wrong")
    assert status == 401 and NOAUTH in body, "a wrong pass got in"
    # a member reaches the stream with their own credential, pass or not
    status, _, body = call("/l/family/radio/stream.mp3", "cookie", "kid")
    assert reached_upstream(status, body), (status, body[:120])
    # and the pass and a real credential on one URL resolve to the MEMBER,
    # because a token outranks the pass in identify(): no anonymous 55.
    status, _, body = call("/l/family/radio/stream.mp3?pass=" + env["radio_pass"],
                           "query", "kid")
    assert status != 401, (status, body[:120])

    # ---- the gate's own reasons, asked the way nginx asks ----------------
    # A refused request leaves the client with @noauth's fixed body, so the
    # reason exists only at hubauth: its own port, the same /gate/<slug> nginx
    # calls, and the URI it was asked about in the header the gateway sets.
    stub = env["stub_port"]
    for path, headers, want in (
            (f"/gate/owner?key=lk_nosuchkey", {}, "bad-key"),
            ("/gate/owner", {"X-Gate-Uri": "/l/owner/api/x"}, "no-credentials"),
            ("/gate/ghostslug", {"X-Gate-Uri": "/l/ghostslug/api/x"},
             "unknown-partition"),
            (f"/gate/owner?key={kid_key}", {}, "not-a-member"),
            ("/gate/family", {"X-Gate-Uri": "/l/family/api/x",
                              "Cookie": f"listen_sess={env['owner_sess']}"},
             "wrong-partition"),
            ("/gate/family", {"X-Gate-Uri": "/l/family/api/x",
                              # session-shaped (hubdb.new_token is urlsafe(32),
                              # so NOT the lk_ prefix that names an api key)
                              "Cookie": "listen_sess=" + "n" * 43},
             "bad-session")):
        status, _, body = _fetch(stub, path, headers)
        assert status == 401 and json.loads(body)["error"] == want, (path, body)
    status, hdr, body = _fetch(stub, "/gate/family",
                               {"X-Gate-Uri": f"/l/family/x?key={key}"})
    assert status == 200 and json.loads(body)["username"] == "owner", body
    # The header, not the body, is what nginx copies into the upstream request
    # (auth_request_set), so it has to be there on the allow.
    assert hdr.get("X-User") == "owner" and hdr.get("X-Partition-Slug") == \
        "family", hdr
    # Precedence, measured at the gate itself: a bearer token outranks a
    # cookie (a headless client never depends on cookie policy) and a cookie
    # outranks `?key=` (a stale query string in a shared URL cannot downgrade
    # the browser that is actually signed in). Two DIFFERENT identities per
    # case, so the answer names which token won instead of merely showing 200.
    status, _, body = _fetch(stub, "/gate/family",
                             {"X-Gate-Uri": "/l/family/x",
                              "Authorization": f"Bearer {kid_key}",
                              "Cookie": f"listen_sess={env['car_sess']}"})
    assert status == 200 and json.loads(body)["username"] == "kid", body
    status, _, body = _fetch(stub, "/gate/family",
                             {"X-Gate-Uri": f"/l/family/x?key={key}",
                              "Cookie": f"listen_sess={env['kid_sess']}"})
    assert status == 200 and json.loads(body)["username"] == "kid", body

    log = (env["tmp"] / "error.log").read_text()
    assert not log.strip(), log

@nginx_required
def test_served_auth_endpoints_read_both_body_encodings(served_ident):
    """`/login` `/pair` `/codes` parse a FORM body as well as a JSON one.

    hub.js posts JSON, so nothing in the browser path can notice a broken form
    branch - and the branch was broken: `{k: v[0] for k, v in parse_qs(raw)}`
    iterates a dict's KEYS, so `for k, v` unpacked the key STRING, a four-char
    name like `code` raised "too many values to unpack", and the blanket
    `except` returned an empty body. Every form-encoded POST then answered as
    if the fields had never been sent: login and pairing both said "pick a
    partition" with the slug present. A JS-less client is exactly who posts
    forms - a car-stereo UI, `curl --data-urlencode`, a pairing script - and the
    gateway forwards those bodies untouched, so it failed identically direct to
    hubauth and through :80.

    Asserting the FIELDS is the point, not the status: a body that parsed to
    `{}` would still 200 on some paths with the wrong partition, so every case
    checks the value the field carries (which partition was chosen, which
    member the code named) and the pairing cookie is then used to read that
    member's own progress file.
    """
    env = served_ident
    stub, port, key = env["stub_port"], env["port"], env["key"]
    _both_partition_media(env)
    FORM = "application/x-www-form-urlencoded"

    def post(where, path, enc, fields, headers=None):
        """One POST of the same fields in one encoding, to hubauth's port."""
        body = (urllib.parse.urlencode(fields).encode() if enc == "form"
                else json.dumps(fields).encode())
        hdr = dict(headers or {})
        hdr["Content-Type"] = FORM if enc == "form" else "application/json"
        return _fetch(where, path, headers=hdr, data=body)

    def session_of(set_cookie):
        """The token `Set-Cookie` just granted, for use as a Cookie header."""
        assert set_cookie.startswith("listen_sess="), set_cookie
        return {"Cookie": "listen_sess=" +
                set_cookie[len("listen_sess="):].split(";")[0]}

    # ---- login: same answer from both encodings -----------------------
    # `next` is a third field, so this also proves multi-key bodies survive
    # (`for k, v in parse_qs(...)` would have unpacked `next` too, at 4 chars).
    for enc in ("form", "json"):
        status, hdr, body = post(stub, "/login", enc, {
            "slug": "owner", "username": "owner", "password": "ownerpass",
            "next": "/l/owner/cache/index.json"})
        assert status == 200, (enc, status, body[:160])
        data = json.loads(body)
        assert data["slug"] == "owner" and data["username"] == "owner", (enc, body)
        assert data["redirect"] == "/l/owner/cache/index.json", (enc, data)
        assert "listen_sess=" in hdr.get("Set-Cookie", ""), (enc, hdr)
    # The slug field is what picked the partition - owner is admin in BOTH, so a
    # body that lost `slug` could not be told apart from one that chose one. Ask
    # for a partition kid is NOT in and the refusal names the membership check,
    # which only runs once the slug arrived.
    status, _, body = post(stub, "/login", "form", {
        "slug": "ghostslug", "username": "owner", "password": "ownerpass"})
    assert status == 400 and b"pick a partition" in body, (status, body)
    status, _, body = post(stub, "/login", "form", {
        "slug": "owner", "username": "kid", "password": "kidpassword"})
    assert status == 403 and b"not a member" in body, (status, body)
    status, _, body = post(stub, "/login", "form", {
        "slug": "owner", "username": "owner", "password": "wrongpassword"})
    assert status == 401 and b"wrong username or password" in body, (status, body)

    # ---- a browser form post gets its redirect AS a redirect ------------
    # The no-JS fallback sends Accept: text/html with a form body. 200 +
    # {"redirect": ...} strands that caller on a JSON page it cannot follow,
    # and hub.js is distinguishable because it names application/json in its
    # Accept. So: text/html-only + form body -> 302 with Location AND the
    # session cookie on the same response; anything else -> the JSON it had.
    BROWSER = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,*/*;q=0.8")
    status, hdr, body = post(stub, "/login", "form", {
        "slug": "family", "username": "kid", "password": "kidpassword",
        "next": "/l/family/cache/index.json"}, {"Accept": BROWSER})
    assert status == 302, (status, body[:160])
    assert hdr.get("Location") == "/l/family/cache/index.json", hdr
    status, _, body = _fetch(stub, "/me",
                             headers={**session_of(hdr["Set-Cookie"])})
    me = json.loads(body)
    assert status == 200 and (me["slug"], me["username"]) == ("family", "kid"), body

    # `Accept: */*` - curl, a podcatcher, a script with no Accept at all -
    # keeps its JSON body; the 302 is for HTML callers only.
    status, _, body = post(stub, "/login", "form", {
        "slug": "family", "username": "kid", "password": "kidpassword"},
        {"Accept": "*/*"})
    assert status == 200 and json.loads(body)["redirect"] == "/l/family/", body
    # hub.js itself (JSON in Accept) is unaffected in either direction.
    status, _, body = post(stub, "/login", "json", {
        "slug": "family", "username": "kid", "password": "kidpassword"},
        {"Accept": "application/json"})
    assert status == 200 and json.loads(body)["redirect"] == "/l/family/", body

    # A browser post that FAILS stays JSON: the inline error is rendered by
    # hub.js, and a 302 back to /login would drop the reason on the floor.
    status, _, body = post(stub, "/login", "form", {
        "slug": "family", "username": "kid", "password": "wrongpassword"},
        {"Accept": BROWSER})
    assert status == 401 and b"wrong username or password" in body, (status, body)

    # ---- codes + pairing: both fields, both shapes --------------------
    # `/codes`' `member` decides the IDENTITY the code will sign a device in as
    # (the pairing itself reads it off the code, not off the pair body), so a
    # code minted `member=car` and redeemed as `car` proves BOTH bodies were
    # parsed. owner's api key is the credential the README gives an admin script,
    # and an api key names no partition, so `slug` is load-bearing too: a lost
    # body answered `name a partition` (400) instead of minting.
    def mint(enc, member=None):
        fields = {"slug": "family"}
        if member:
            fields["member"] = member
        status, _, body = post(stub, "/codes", enc, fields,
                               {"Authorization": f"Bearer {key}"})
        assert status == 200, (enc, status, body[:160])
        data = json.loads(body)
        assert data["slug"] == "family", (enc, body)
        # the echoed member is the parsed field verbatim; ABSENT means the code
        # signs in as a member of its own (hubauth reports that as null).
        return data["code"], data["member"]

    code_car, named = mint("form", "car")
    assert named == "car", named
    code_auto, unnamed = mint("json")
    assert unnamed is None, unnamed

    status, hdr, body = post(stub, "/pair", "form",
                             {"slug": "family", "code": code_car})
    assert status == 200, (status, body[:160])
    car = json.loads(body)
    assert car["username"] == "car" and car["slug"] == "family", body
    assert car["redirect"] == "/l/family/", body
    car_cookie = session_of(hdr["Set-Cookie"])
    # The pairing is real, not a 200: the minted cookie opens family's media and
    # lands on CAR's own position file, so the identity the mint body named is
    # the identity the gate asserts - and the front stays shut for it.
    status, _, body = _fetch(port, "/l/family/cache/progress.json", car_cookie)
    assert status == 200 and b'"pos":77' in body, (status, body[:120])
    status, _, body = _fetch(port, "/cache/progress.json", car_cookie)
    assert status == 401, (status, body[:120])

    # The unnamed code takes a member of its own (`pair-` + hex), which is the
    # second half of the multi-device promise - a second car in ONE partition
    # gets its own name, not somebody else's position file.
    status, _, body = post(stub, "/pair", "json", {"slug": "family",
                                                   "code": code_auto})
    assert status == 200, (status, body[:160])
    auto = json.loads(body)
    assert auto["username"].startswith("pair-") and auto["slug"] == "family", body

    # A wrong code is refused in hubauth's own words, not as a missing field:
    # `enter the 8-character code` (400) is what an unread body produced, so the
    # 401 below is the proof the code field arrived.
    status, _, body = post(stub, "/pair", "form", {"slug": "family",
                                                   "code": "ZZZZZZZZ"})
    assert status == 401 and b"code not recognised" in body, (status, body)
    # A code is single-use, whichever encoding redeemed it: `already used` is a
    # different sentence from `not recognised`, and only a redeemed code earns it.
    status, _, body = post(stub, "/pair", "form", {"slug": "family",
                                                   "code": code_car})
    assert status == 401 and b"already used" in body, (status, body)
    # and the slug has to be the code's own partition - family's code typed at
    # the front is refused, which only happens once the slug field arrived.
    status, _, body = post(stub, "/pair", "form", {"slug": "owner",
                                                   "code": code_auto})
    assert status == 401 and b"code not recognised" in body, (status, body)

    # ---- through the gateway, where a car actually posts --------------
    # The gateway maps `/api/auth/<verb>` onto hubauth's short paths with a
    # body-preserving `proxy_pass`, and the mounted `/l/<slug>/api/auth/<verb>`
    # rewrite strips the prefix while leaving the body alone. Either could eat
    # or replace it (a rewrite that dropped the request body is legal config)
    # and hubauth would then answer "pick a partition" - which is a 400, so
    # these assertions must be the values, not the status.
    def mint_via_gateway(slug="family", member="car"):
        """Mint over :80, form-encoded. `member` rides along so the mint body is
        proved through the gateway too, and the pairing that redeems the code can
        be identified by name instead of by a generated `pair-` name."""
        status, _, body = _fetch(port, "/api/auth/codes",
                                 {"Authorization": f"Bearer {key}",
                                  "Content-Type": FORM},
                                 data=urllib.parse.urlencode(
                                     {"slug": slug, "member": member}).encode())
        assert status == 200, (status, body[:160])
        data = json.loads(body)
        assert data["slug"] == slug and data["member"] == member, body
        return data["code"]
    code = mint_via_gateway()
    # A car types only the partition and the code; the identity rides on the
    # minted code, so no `member` field in the pair body can account for it.
    status, hdr, body = _fetch(port, "/api/auth/pair",
                               {"Content-Type": FORM},
                               data=urllib.parse.urlencode(
                                   {"slug": "family", "code": code}).encode())
    assert status == 200, (status, body[:160])
    data = json.loads(body)
    assert data["username"] == "car" and data["slug"] == "family", body
    car2 = session_of(hdr["Set-Cookie"])
    status, _, body = _fetch(port, "/l/family/cache/progress.car.json", car2)
    assert status == 200 and b'"pos":77' in body, (status, body[:120])
    # the same POST through the partition mount of the same verb, whose regex
    # location has to hand hubauth `/pair` with the body intact
    code = mint_via_gateway()
    status, _, body = _fetch(port, "/l/family/api/auth/pair",
                             {"Content-Type": FORM},
                             data=urllib.parse.urlencode(
                                 {"slug": "family", "code": code}).encode())
    assert status == 200 and b'"username": "car"' in body, (status, body[:160])
    # a login through the door, form-encoded, with the bounce target echoed
    # back the way hub.js echoes it: the landing page is body data too.
    status, hdr, body = _fetch(port, "/api/auth/login", {"Content-Type": FORM},
                               data=urllib.parse.urlencode({
                                   "slug": "family", "username": "kid",
                                   "password": "kidpassword",
                                   "next": "/l/family/cache/progress.json"}).encode())
    assert status == 200, (status, body[:160])
    assert json.loads(body)["redirect"] == "/l/family/cache/progress.json", body
    kid_cookie = session_of(hdr["Set-Cookie"])
    status, _, body = _fetch(port, "/l/family/cache/progress.json", kid_cookie)
    assert status == 200 and b'"pos":88' in body, (status, body[:120])

    log = (env["tmp"] / "error.log").read_text()
    assert not log.strip(), log


@nginx_required
def test_served_gate_names_the_member_so_devices_keep_separate_positions(
        served_ident):
    """The multi-device guarantee end to end: `progress.json` is the CALLER's
    file, named by the identity the gate asserted - two listeners in one
    partition keep separate positions on one URL, an api key acts as its own
    owner, a member name the partition has no file for is a 404 rather than
    somebody else's position, and no amount of filename guessing crosses a
    partition."""
    env = served_ident
    port, key, kid_key = env["port"], env["key"], env["kid_key"]
    _both_partition_media(env)
    kid_cookie = {"Cookie": f"listen_sess={env['kid_sess']}"}
    car_cookie = {"Cookie": f"listen_sess={env['car_sess']}"}

    # One URL, two devices, two files - and never pos 55, the partition's own
    # progress.json, which only the no-identity fallback under that name could
    # serve (and which the gateway never reaches, because the gate demands a
    # credential first).
    for headers, query, want in ((kid_cookie, "", 88), (car_cookie, "", 77),
                                 (None, f"?key={kid_key}", 88),
                                 (None, f"?key={key}", 99)):
        status, _, body = _fetch(port, "/l/family/cache/progress.json" + query,
                                 headers)
        assert status == 200 and f'"pos":{want}'.encode() in body, \
            (query, status, body)
    # The dotted form names a member explicitly. Inside one shared partition
    # that is the point of it - a household script reads the car's position -
    # so it resolves for any member of THAT partition.
    status, _, body = _fetch(port, "/l/family/cache/progress.car.json", kid_cookie)
    assert status == 200 and b'"pos":77' in body, (status, body)
    # A name with no file behind it stays a 404 (nginx's own page, not the
    # upstream's JSON) instead of falling through to someone else's position.
    status, _, body = _fetch(port, "/l/family/cache/progress.nobody.json",
                             kid_cookie)
    assert status == 404 and b"not-found" not in body, (status, body)
    # Guessing across a partition is the gate's decision, not the filename's:
    # kid asking OWNER's URL for kid's own file is refused before nginx
    # consults a single try_files candidate.
    for path in ("/l/owner/cache/progress.kid.json", "/cache/progress.kid.json",
                 "/l/owner/cache/progress.json"):
        status, _, body = _fetch(port, path, kid_cookie)
        assert status == 401 and b"unauthorized" in body, (path, status, body)
    log = (env["tmp"] / "error.log").read_text()
    assert not log.strip(), log


@nginx_required
def test_served_gateway_answers_its_own_routes(served_two):
    """The check `nginx -t` cannot make: a `return 404` in SERVER context is
    legal syntax and a dead gateway.

    nginx runs a `return` written at server level in the rewrite phase, before
    it looks up any location, so EVERY location in that server is unreachable
    - the exact `= /health`, the picker's `= /`, every generated partition
    prefix - while the server's own `add_header ... always` still stamps the
    404 it produced. The running container showed exactly that: 404 from
    /health, /login, /hub.js and /l/owner/, an empty error.log, and the
    gateway's marker header on all of them. No syntax check can see this; only
    asking a live nginx for a path the block must answer finds it.
    """
    port = served_two["port"]
    for path, expected in (("/health", 200), ("/", 200), ("/login", 200),
                           ("/pair", 200), ("/logout", 200), ("/hub.js", 200),
                           ("/hub.css", 200), ("/robots.txt", 200),
                           ("/api/auth/me", 200), ("/l/owner", 301),
                           ("/l/family", 301), ("/l/owner/", 302),
                           ("/l/family/", 302), ("/l/nope/", 404)):
        status, hdr, body = _fetch(port, path)
        assert status == expected, (path, status, body[:120])
    # the picker is generated from the same registry that made the proxies
    status, _, body = _fetch(port, "/")
    assert b'href="/l/owner/"' in body and b'href="/l/family/"' in body, body
    # /health is the container's liveness body, not a page
    status, hdr, body = _fetch(port, "/health")
    assert b"listen-stack" in body, body
    assert "json" in hdr.get("Content-Type", ""), hdr
    # the 302s above are the login door, not a redirect to nowhere: the gate
    # refused, and the door carries the page it came from. The Location must be a
    # PATH: nginx 1.22's own default (`absolute_redirect on`) would answer
    # `http://<Host>/login?...`, which pins the scheme to http - nginx cannot see
    # the VPS's TLS termination - and lets a client-chosen Host header decide where
    # a login bounce lands. gateway.conf turns that default off for this reason.
    status, hdr, _ = _fetch(port, "/l/family/")
    assert hdr.get("Location") == "/login?next=/l/family/", hdr
    # and the same for the 301s, which a browser caches: a scheme-locked absolute
    # 301 here would be remembered across an http -> https change.
    status, hdr, _ = _fetch(port, "/l/family")
    assert hdr.get("Location") == "/l/family/", hdr
    # an unknown slug is a 404 because NO location matches it - the contract
    # the gateway header documents, and the only case the block's tail answers
    status, hdr, body = _fetch(port, "/l/nope/")
    assert status == 404 and "json" not in hdr.get("Content-Type", ""), body


@nginx_required
def test_served_locations_keep_their_contract(served_two):
    """What a location that MATCHED must still do: challenge a gated api with
    401 + Bearer, inject the gate's X-User into the gated shell, keep the
    churning state files no-cache and the content-permanent mp3 304-able."""
    env = served_two
    port = env["port"]
    _media_tree(env)

    # front bare api surface: the JSON challenge, because this is a fetch
    status, hdr, body = _fetch(port, "/api/cache/status")
    assert status == 401 and b"unauthorized" in body, (status, body[:120])
    assert "Bearer" in hdr.get("WWW-Authenticate", ""), hdr

    env["srv"].allow_gate = True
    status, hdr, body = _fetch(port, "/l/family/")
    assert status == 200, (status, body[:120])
    assert "no-cache" in hdr.get("Cache-Control", ""), hdr
    assert body.count(b"who=owner") == 1, body        # sub_filter replaced once

    status, hdr, body = _fetch(port, "/cache/index.json")
    assert status == 200 and "no-cache" in hdr.get("Cache-Control", ""), hdr

    # the multi-device guarantee: bare progress.json resolves to the CALLER's
    # file, named by the identity the gate asserted (never by a client header)
    status, _, body = _fetch(port, "/cache/progress.json")
    assert status == 200 and b'"pos":11' in body, (status, body)
    status, _, body = _fetch(port, "/cache/progress.kid.json")
    assert status == 200 and b'"pos":22' in body, (status, body)

    status, hdr, _ = _fetch(port, "/library/Artist%20-%20Song.mp3")
    assert status == 200, (status, hdr)
    assert "max-age=86400" in hdr.get("Cache-Control", ""), hdr
    etag = hdr.get("ETag")
    assert etag, "media tree served without an ETag"
    status, _, _ = _fetch(port, "/library/Artist%20-%20Song.mp3",
                          headers={"If-None-Match": etag})
    assert status == 304, status

@nginx_required
def test_served_vault_door_answers_itself_for_a_member(served_two):
    """The door has to answer with the noVNC page, not a redirect to the login
    form, for someone who IS signed in - and it has to answer with the page that
    points at ITS OWN partition's websocket.

    A `try_files $uri $uri/ …` prefix location cannot do this: for `/vnc/` the
    `$uri/` candidate is the aliased directory, and because the noVNC release has
    no index.html nginx fails in its directory-index phase with 403 "directory
    index forbidden", which the block's own error_page rewrites into the login
    redirect. That is a member with a valid session bounced to /login, and
    `nginx -t` says nothing about it (measured live in the container: the door
    302'd while `/l/owner/vnc/vnc_lite.html` beside it answered 200). Serving
    the page from an EXACT location with one FILE candidate skips that phase.

    The other half is the rewrite: rfb.js builds its websocket URL as
    scheme://host/ + path, so the page's stock default `websockify` aims at the
    portal root, where no partition is implied and the gate has no slug to
    check. sub_filter replaces the default with this partition's path - but ONLY
    in the door page, so rfb.js's own `/websockify` fallback (the same bare
    string) survives.
    """
    env = served_two
    port = env["port"]
    stock = "readQueryVariable('path', 'websockify')"

    # No session yet: the door is a page, so it bounces to the login form.
    status, hdr, _ = _fetch(port, "/l/owner/vnc/")
    assert status == 302, (status, hdr)
    assert "/login" in urllib.parse.urlsplit(hdr.get("Location", "")).path, hdr

    env["srv"].allow_gate = True
    for pre, mount in (("/l/owner", "l/owner/"), ("/l/family", "l/family/"),
                       ("", "")):                 # "" = front's bare door
        status, hdr, body = _fetch(port, f"{pre}/vnc/")
        assert status == 200, (pre, status, body[:120])
        assert b"<title>noVNC" in body, (pre, body[:120])
        assert stock.encode() not in body, (pre, "default ws path survived")
        want = ("readQueryVariable('path', '%svnc/websockify')"
                % mount).encode()
        assert want in body, (pre, body[body.find(b"path ="):][:120])
        # served from the file, uncompressed (sub_filter skips gzip'd bodies)
        assert hdr.get("Content-Encoding") is None, (pre, hdr)

    # The tree under the door is FILE-ONLY: a miss is a 404, never the lite
    # page. A door that answers a missing .js with HTML is how a typo'd module
    # import turns into "noVNC silently shows a black screen".
    for path in ("/l/owner/vnc/core/rfb.js", "/l/family/vnc/core/rfb.js",
                 "/vnc/core/rfb.js"):
        status, hdr, body = _fetch(port, path)
        assert status == 200, (path, status)
        assert b"/websockify" in body, (path, "rfb's own fallback rewritten")
    for path in ("/l/owner/vnc/zzz.js", "/l/owner/vnc/core/", "/vnc/zzz.js"):
        status, _, body = _fetch(port, path)
        assert status == 404, (path, status, body[:120])
        assert b"<title>noVNC" not in body, path

    # The bare door still redirects to the lite page WITH its query intact.
    status, hdr, _ = _fetch(port, "/l/owner/vnc?pass=x")
    assert status == 302, (status, hdr)
    parts = urllib.parse.urlsplit(hdr.get("Location", ""))
    assert (parts.path == "/l/owner/vnc/vnc_lite.html"
            and parts.query == "pass=x"), hdr

    log = (env["tmp"] / "error.log").read_text()
    assert not log.strip(), log


@nginx_required
def test_served_tree_produces_no_nginx_errors(served_two):
    """The silent-failure check: a wrong `alias`, a wrong `root`, a sub_filter
    target that is not there or a regex that does not compile all log a line
    while still answering *something*, so the sweep above is only trustworthy
    if the error log stayed empty across all of it."""
    env = served_two
    port = env["port"]
    _media_tree(env)
    env["srv"].allow_gate = True
    paths = ["/health", "/", "/login", "/pair", "/hub.js", "/hub.css",
             "/robots.txt", "/api/auth/me", "/l/owner", "/l/family",
             "/l/owner/", "/l/family/", "/l/nope/", "/api/cache/status",
             "/cache/index.json", "/cache/progress.json",
             "/cache/progress.kid.json", "/library/Artist%20-%20Song.mp3"]
    got = {path: _fetch(port, path)[0] for path in paths}
    log = (env["tmp"] / "error.log").read_text()
    dead = {p: s for p, s in got.items() if s == 500}
    assert not dead, (dead, log)
    text = (env["tmp"] / "error.log").read_text()
    assert not text.strip(), text


@nginx_required
@pytest.mark.parametrize("src", [ONE, TWO], ids=["one-partition", "two-partitions"])
def test_generated_config_passes_nginx_t(src, tmp_path, monkeypatch):
    """The check the entrypoint actually runs, on the registry shapes that can
    exist: gen-conf's own brace/matcher tests cannot tell an nginx idiom that
    parses from one that does not (the `^~` prefixes ate two sibling regex
    locations, and a nested `location` is legal in exactly one of those two
    places)."""
    monkeypatch.setattr(gen, "DATA", str(tmp_path / "data"))
    registry(tmp_path, src, monkeypatch)
    reg = gen.load_registry()
    proc = _run_nginx(_nginx_tree(tmp_path, gen.nginx_locations(reg),
                                  gen.nginx_maps(reg)))
    assert proc.returncode == 0, proc.stderr
    assert "warn" not in proc.stderr.lower(), proc.stderr


@nginx_required
def test_nginx_t_actually_rejects_the_generated_file(tmp_path, monkeypatch):
    """Negative control: a harness that always passes proves nothing. Delete one
    closing brace from the generated body and the same harness must fail while
    naming the generated file - which also proves the tree above is what nginx
    validated: a harness that only ever read the gateway would stay green."""
    monkeypatch.setattr(gen, "DATA", str(tmp_path / "data"))
    registry(tmp_path, TWO, monkeypatch)
    reg = gen.load_registry()
    good = gen.nginx_locations(reg)
    broken = good.replace("}\n", "", 1)
    assert broken != good, "the injected break did not land"
    proc = _run_nginx(_nginx_tree(tmp_path, broken, gen.nginx_maps(reg)))
    assert proc.returncode != 0, proc.stderr
    assert "10-locations.conf" in proc.stderr, proc.stderr


