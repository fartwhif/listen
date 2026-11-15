"""Env + path isolation for the platform tests.

The platform modules freeze their paths and credentials into module globals at
import time (that is what a container start wants), so a test that points
`HUB_DATA_DIR`/`HUB_REGISTRY` at a tmpdir has to RELOAD the module after setting
the env, and must put the ambient env and the pre-test modules back afterwards.

`gen-conf`'s output directories are absolute constants (`/etc/supervisor/conf.d`,
…) for the same reason; tests that exercise `main()` repoint those attributes at
a tmpdir, and tests that only assert on the generated TEXT call the emitters
directly and need neither root nor a writable filesystem.

The nginx checks need the real snippet files at the literal path
`/etc/nginx/snippets` (that is what the generated `include` lines name). Run
them inside the image (`LISTEN_TEST_NGINX=1`); on a host without nginx they skip.
"""
import importlib
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.dirname(HERE)
if PLATFORM not in sys.path:
    sys.path.insert(0, PLATFORM)

# Names the platform modules read at import time. A test that sets a name not
# listed here leaks it into the next test.
OWNED = (
    "HUB_DATA_DIR", "HUB_REGISTRY", "HUB_DB", "HUB_PORT", "HUB_COOKIE",
    "HUB_RADIO_PASS", "HUB_AUTH_DEBUG", "HUB_OWNER_USER", "HUB_OWNER_PASSWORD",
    "HUB_SUP_SOCK", "HUB_RELOAD_INTERVAL", "HUB_VAULT_MAX_AGE",
    "HUB_VAULT_SCAN", "HUB_VAULT_IDLE", "HUB_VAULT_RETRY",
    "HUB_VAULT_RETRY_MAX", "HUB_VAULT_GRACE", "HUB_VAULT_MAX_RUN",
    "HUB_MIGRATE_SLUG", "HUB_MIGRATE_VAULT", "HUB_MIGRATE_PROGRESS_USER",
    "HUB_LEGACY_DIR", "HUB_DEFAULT_SLUG", "STACK_DIR", "SITE_DOMAIN",
    "MAX_CONCURRENT_YTDLP", "YTDGATE_SLOTS_DIR", "TUNESTER_BLOBS",
    "TUNESTER_COOKIES", "DRIVEPOD_BGUTIL_URL", "GRABBY_BGUTIL_URL",
    "WG_PRIVATE_KEY", "WG_PEER_PUBLIC_KEY", "WG_PEER_ENDPOINT",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Blank every platform env name. A developer's own `.env` must not decide
    what a test asserts: a stray HUB_RADIO_PASS makes an anonymous request pass,
    a stray SITE_DOMAIN changes a generated URL."""
    for name in OWNED:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def reload_modules():
    """Reload named platform modules after the env is set, then restore the
    pre-test module objects so no later test reads a deleted tmpdir."""
    saved: list[tuple[str, object]] = []

    def _reload(*names: str):
        mods = []
        for name in names:
            importlib.import_module(name)
            saved.append((name, sys.modules[name]))
            mods.append(importlib.reload(sys.modules[name]))
        return mods[0] if len(mods) == 1 else mods

    yield _reload

    for name, mod in reversed(saved):
        sys.modules[name] = mod
        importlib.reload(mod)


@pytest.fixture
def gen_conf_dirs(monkeypatch, tmp_path):
    """Repoint gen-conf's absolute output paths (and its SPA shell template) at
    the tmpdir so main() can be exercised without root. Returns the roots."""
    import importlib
    gen = importlib.import_module("gen-conf")
    etc = tmp_path / "etc"
    www = tmp_path / "var/www"
    data = tmp_path / "data"
    data.mkdir()
    (etc / "supervisor/conf.d").mkdir(parents=True, exist_ok=True)
    (etc / "nginx/stack-locations").mkdir(parents=True, exist_ok=True)
    (etc / "nginx/conf.d").mkdir(parents=True, exist_ok=True)
    hub_www, spa_www = www / "hub", www / "drivepod"
    hub_www.mkdir(parents=True, exist_ok=True)
    spa_www.mkdir(parents=True, exist_ok=True)
    shell = spa_www / "index.html.dist"
    shell.write_text('<!doctype html><html><head><base href="/">'
                     '<script src="main-HASH.js"></script></head>'
                     '<body></body></html>\n')
    # Every path gen-conf writes or reads is a module-level constant frozen
    # from the environment at import time, so patching the CONSTANTS (rather
    # than the env plus a reload) is what lets main() run unprivileged, and
    # keeps the patch alive across a reload a test might trigger itself.
    for attr, value in (("SUP_DIR", etc / "supervisor/conf.d"),
                        ("LOC_DIR", etc / "nginx/stack-locations"),
                        ("MAPS", etc / "nginx/conf.d/05-maps.conf"),
                        ("HUB_WWW", hub_www), ("SPA_WWW", spa_www),
                        ("DATA", data), ("STACK", tmp_path / "srv/stack"),
                        ("REGISTRY", data / "partitions.json")):
        monkeypatch.setattr(gen, attr, str(value))
    # The registry itself: the ENTRYPOINT seeds it (platform/seed-registry.py
    # copies partitions.default.json and never overwrites) and only then runs
    # gen-conf; gen-conf's own contract is that a missing registry means no
    # partitions (see test_load_registry_missing_file_seeds_nothing). So a test
    # that exercises main() has to seed first, exactly like the start order -
    # here by running the real seeder with HUB_REGISTRY pointed at the tmpdir.
    monkeypatch.setenv("HUB_REGISTRY", str(data / "partitions.json"))
    # The slug is an explicit argument: read from sys.argv, main() would seed a
    # partition named after the test file when it runs under pytest.
    seed = importlib.import_module("seed-registry")
    assert seed.main("owner") == 0, "seeder refused to write the default registry"
    return {"gen": gen, "etc": etc, "www": www, "hub": hub_www,
            "spa": spa_www, "data": data, "shell": shell,
            "registry": data / "partitions.json"}


nginx_required = pytest.mark.skipif(
    not os.environ.get("LISTEN_TEST_NGINX"),
    reason="set LISTEN_TEST_NGINX=1 to run the nginx checks (needs nginx + the "
           "stack snippets at /etc/nginx/snippets, i.e. run it inside the image)")
