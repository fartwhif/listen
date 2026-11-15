#!/usr/bin/env python3
"""Registry -> supervision + nginx, so adding a partition needs no rebuild.

Called by the entrypoint at start and by the watchdog whenever
partitions.json changes (supervisorctl reread/update + nginx -s reload).

Port map for slug index i (front partition = index 0 = defaultSlug):
    drivepod backend   3000+i     hub vault server  3100+i
    grabbytube         8101 (i=0) else 8111+i
    vault VNC          5901+i     vault websockify  8181+i
    vault VNC driver   8123+i     vault chromium CDP 9222+i
Fixed: gateway 80, hub/auth 8099, tunester pool 8102, bgutil 4416,
stream-fix shim 8091. Ten partitions max (documented in README).

Everything is reachable through the gateway; the non-loopback listeners that
exist are there for callers that bypass it by contract: the front broadcaster
on 8090 (VPS proxy frogrock.example.com -> peer:8090, and DNAS's registration),
the stream-fix shim on 8091 (DNAS relay target http://stream-fix:8091),
front grabbytube on 8101 (VPS proxy grabbytube.example.com, and its standalone
UI at that origin's root) and the shared tunester on 8102. Those are PUBLISHED
by docker-compose; unpublished ports are unreachable from the WireGuard peer
regardless of bind, so binds stay default and only index 0's grabbytube and
radio are non-loopback.

Vault programs are per VIRTUAL HOST (vault_hosts), not per partition: a vault id
names a GOOGLE ACCOUNT, and several partitions may name the same one. Only the
host partition gets a `vault-<vault>` program, because two chromiums signing in
as the same account fight over the session (each re-types the password, each
rotates the cookie jar the other is using). A member partition's browser ports
are allocated by the map above and stay idle; its noVNC door proxies to the
host's.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# The supervisor `environment=` format lives in platform/envfmt.py so that
# `hub partition set` can refuse an unrepresentable value at the CLI instead of
# letting supervisord reject the whole generated file later.
from envfmt import REASON, env_str, env_val, unsuitable

REGISTRY = os.environ.get("HUB_REGISTRY", "/data/partitions.json")
STACK = os.environ.get("STACK_DIR", "/srv/stack")
DATA = os.environ.get("HUB_DATA_DIR", "/data")
# The public domain every door URL and the picker are built from (see vnc_door).
# A constant for the same reason as every other path here: a test (and a
# partition add) must see the same value the emitters see, not re-read the
# environment mid-generation.
SITE_DOMAIN = os.environ.get("SITE_DOMAIN", "").strip().strip("/")
SUP_DIR = "/etc/supervisor/conf.d"
MAPS = "/etc/nginx/conf.d/05-maps.conf"
LOC_DIR = "/etc/nginx/stack-locations"
# Generated HTML lands here. Constants rather than inline literals: the picker,
# the auth doors and the SPA shells are the parts of generation a test can
# actually run (supervisor's and nginx's system dirs cannot be repointed from
# an unprivileged test, but these can).
HUB_WWW = "/var/www/hub"
SPA_WWW = "/var/www/drivepod"
# The stock Debian noVNC release, served read-only under every partition's door
# (see the vault-door block in _locations). A constant for the same reason as
# the two www roots: the serving tests repoint it at a tree they write, so a
# served door proves THIS generator's alias instead of whatever the image (or
# the host running pytest) happens to have installed at that path.
NOVNC_DIR = "/usr/share/novnc"
SLOTS_DIR = "/run/ytdl-slots"
# The single-executable form of the concurrency gate: platform/ytdlgated gates
# and then execs the real yt-dlp, so an app that builds a subprocess LIST can
# name it as argv[0] and an app that builds a SHELL string can put it first,
# with no app having to know a gate exists.
GATED_YTDLP = f"{STACK}/platform/ytdlgated"
SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# Names gen-conf forwards from the container's own environment to the platform
# services (see passthrough). They are the credentials and tuning knobs the six
# pre-hub containers each got from their own .env; in the merged stack they come
# from the compose env_file once. A name that is unset is simply absent, so the
# service keeps its own default - which is what makes an unconfigured box behave
# like today's (no AcoustID key -> gate skipped, no NNTP -> archive-only).
TUNESTER_KEYS = (
    # sources
    "TUNESTER_NZB_APIKEY", "TUNESTER_NZB_API", "TUNESTER_NZB_I",
    "TUNESTER_NZB_ARTIST_PAGES", "TUNESTER_NZB_COMP_CHECKS",
    "TUNESTER_NZB_MAX_ARCHIVE_MB",
    "TUNESTER_NNTP_HOST", "TUNESTER_NNTP_PORT", "TUNESTER_NNTP_USER",
    "TUNESTER_NNTP_PASS",
    # content gate
    "TUNESTER_ACOUSTID_KEY", "TUNESTER_ACOUSTID_HOST",
    "TUNESTER_ACOUSTID_MIN_CONF", "TUNESTER_ACOUSTID_REQUIRE",
    # LLM verdicts (OPENAI_* are the fallback endpoint's names)
    "TUNESTER_LLM_PRIMARY_BASE_URL", "TUNESTER_LLM_PRIMARY_API_KEY",
    "TUNESTER_LLM_PRIMARY_MODEL", "TUNESTER_LLM_MODEL",
    "OPENAI_BASE_URL", "OPENAI_API_KEY",
    "TUNESTER_LLM_THINK_BUDGET", "TUNESTER_LLM_TIMEOUT",
    "TUNESTER_LLM_LOAD_WAIT", "TUNESTER_LLM_FAILOVER_WAIT",
    # sizes, depths and timeouts
    "TUNESTER_SEARCH_DEPTH", "TUNESTER_SEARCH_TIMEOUT", "TUNESTER_TRIALS",
    "TUNESTER_DOWNLOAD_TIMEOUT", "TUNESTER_HTTP_TIMEOUT",
    "TUNESTER_CACHE_MAX_MB", "TUNESTER_FFMPEG", "TUNESTER_FFPROBE",
)
VAULT_KEYS = (
    "GOOGLE_EMAIL", "GOOGLE_PASSWORD", "VNC_PASSWORD", "VNC_RESOLUTION",
    "NTFY_TOPIC", "AUTO_SIGNIN_DELAY_MIN", "AUTO_SIGNIN_DELAY_MAX",
    "AUTO_SIGNIN_POLL", "AUTO_SIGNIN_FLOW_MAX_AGE", "AUTO_SIGNIN_MAX_ATTEMPTS",
    "COOKIE_POLL_INTERVAL", "COOKIE_SESSION_PROBE", "COOKIE_EXTERNAL_COOLDOWN",
    "COOKIE_IMPORT_RETRY",
    # vault-run's own lifecycle knobs (see platform/vault-run.py)
    "HUB_VAULT_GRACE", "HUB_VAULT_MAX_RUN",
)
HUB_KEYS = ("HUB_COOKIE", "HUB_RADIO_PASS", "HUB_AUTH_DEBUG",
            "HUB_OWNER_USER", "HUB_OWNER_PASSWORD")
WATCHDOG_KEYS = ("HUB_VAULT_MAX_AGE", "HUB_VAULT_SCAN", "HUB_VAULT_IDLE",
                 "HUB_VAULT_RETRY", "HUB_VAULT_RETRY_MAX",
                 "HUB_SUP_SOCK", "HUB_RELOAD_INTERVAL")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
MAX_PARTITIONS = 10


def ports(i: int) -> dict:
    return {"drivepod": 3000 + i, "grabby": 8101 if i == 0 else 8111 + i,
            "radio": 8090 if i == 0 else 8200 + i, "vnc": 5901 + i,
            "ws": 8181 + i, "driver": 8123 + i, "cdp": 9222 + i}


def load_registry() -> dict:
    try:
        with open(REGISTRY) as fh:
            reg = json.load(fh)
    except FileNotFoundError:
        reg = {}
    except json.JSONDecodeError as exc:
        print(f"gen-conf: {REGISTRY} is not valid JSON ({exc}); keeping nothing",
              file=sys.stderr)
        return {"slugs": [], "front": None, "partitions": {}}
    slugs = [s for s in reg.get("slugs", []) if SLUG_RE.match(s)]
    slugs = list(dict.fromkeys(slugs))
    parts = reg.get("partitions", {})
    # Entries no slug names are stale (a `hub partition rm` that died halfway
    # or a hand-edit): a spec with no programs, ports or proxies must not look
    # like a partition to everything that reads this map.
    parts = {s: p for s, p in parts.items() if s in slugs}
    front = reg.get("defaultSlug")
    if front not in slugs:
        front = slugs[0] if slugs else None
    for i, s in enumerate(slugs):
        spec = dict(parts.get(s) or {})
        spec["index"] = i
        spec.setdefault("mode", "single")
        spec.setdefault("name", s.capitalize())
        spec.setdefault("owner", "owner")
        spec.setdefault("radio", True)
        spec["vault_slug"] = spec.get("vault") or s
        spec.setdefault("quota", {})
        settings = spec.get("settings") or {}
        for k in unsuitable(settings):
            print(f"gen-conf: {s}: setting {k}={settings[k]!r} dropped - {REASON}",
                  file=sys.stderr)
            del settings[k]
        spec["settings"] = settings
        parts[s] = spec
    if len(slugs) > MAX_PARTITIONS:
        print(f"gen-conf: {len(slugs)} partitions > {MAX_PARTITIONS} (port map "
              f"limit); the extras are ignored", file=sys.stderr)
        slugs = slugs[:MAX_PARTITIONS]
    return {"slugs": slugs, "front": front, "partitions": parts}


def vault_hosts(reg: dict) -> dict:
    """vault_slug -> the one slug whose programs own that vault's browser family.

    A vault id names a GOOGLE ACCOUNT, not a partition: `vault: "owner"` on three
    partitions means three partitions share one sign-in. One family per vault is
    what makes that safe - two chromiums authenticating as the same account each
    re-type the password and each rotate the jar the other's consumers write back
    to, which is the two-exporters-competing bug this dedup removes.

    The host is the FIRST partition (in slug order, i.e. the order that also
    decides ports) naming the vault. When the partition a vault was named after
    is removed, the surviving member that still names it becomes the host: the
    vault id outlives the partition that gave it its name, and its profile and
    cookies under vaults/<id>/ have to belong to somebody supervision can start.
    """
    hosts: dict[str, str] = {}
    for slug in reg["slugs"]:
        hosts.setdefault(reg["partitions"][slug]["vault_slug"], slug)
    return hosts


def companion_paths(reg: dict, host: str) -> dict:
    """Every OTHER partition's jar/state paths for the vault `host` owns.

    A member partition's apps each read their own COOKIES_FILE (that is what
    separates tenants), so the host's exporter has to hand them the jar it just
    verified - otherwise a member's cookies stay stale until its own (now absent)
    browser refreshed them. gen-conf renders these as comma lists into the vault
    program's environment; cookie_exporter.py fans its writes out to them.
    """
    vault = reg["partitions"][host]["vault_slug"]
    members = [s for s in reg["slugs"]
               if reg["partitions"][s]["vault_slug"] == vault and s != host]
    return {
        "cookies": [f"{DATA}/users/{s}/cookies.txt" for s in members],
        "state": [f"{DATA}/users/{s}/session-state.json" for s in members],
    }


def tenant_vals(slug: str, spec: dict) -> dict:
    """Single source of tenant truth as a NAME -> VALUE map: every program of a
    slug gets exactly this (rendered by env_str), and the vault/radio programs
    start from it and add their own keys. A map, never a rendered string that
    something else has to parse back apart: the only parser in that pair-up is
    supervisor's, and it is the thing that can refuse the whole file."""
    p = ports(spec["index"])
    home = f"{DATA}/users/{slug}"
    vals = {
        "HUB_SLUG": slug,
        "HUB_URL_PREFIX": f"/l/{slug}",
        "HUB_USERS_DIR": DATA + "/users",
        "DATA_DIR": home,
        "CACHE_DIR": f"{home}/cache",
        "DATABASE_URL": f"file:{home}/database.db",
        "COOKIES_FILE": f"{home}/cookies.txt",
        "COOKIE_SESSION_STATE": f"{home}/session-state.json",
        "GRABBY_HOST": "127.0.0.1",
        "GRABBY_PORT": str(p["grabby"]),
        "GRABBY_OUTPUT_DIR": f"{home}/cache",
        "GRABBY_DB_PATH": f"{home}/grabbytube.db",
        "GRABBY_CATALOG_PATH": f"{home}/library/grabby-tracks.json",
        "GRABBY_TUNESTER_URL": "http://127.0.0.1:8102",
        "GRABBY_BGUTIL_URL": "http://127.0.0.1:4416",
        "GRABBY_COOKIES_FILE": f"{home}/cookies.txt",
        "GRABBY_API_URL": f"http://127.0.0.1:{p['grabby']}",
        "DRIVEPOD_BGUTIL_URL": "http://127.0.0.1:4416",
        # The downloaders' yt-dlp, as ONE argv word / one shell word: the gate
        # wrapper (platform/ytdlgated) so parallel harvests queue instead of
        # hammering the vault's session. Unset in the app defaults to plain
        # `yt-dlp`, i.e. today's ungated behaviour.
        "DRIVEPOD_YTDLP": GATED_YTDLP,
        "GRABBY_YTDLP": GATED_YTDLP,
        "MAX_CONCURRENT_YTDLP": os.environ.get("MAX_CONCURRENT_YTDLP", "2"),
        "YTDGATE_SLOTS_DIR": SLOTS_DIR,
        "PORT": str(p["drivepod"]),
        "HOST": "127.0.0.1",
        "TUNESTER_URL": "http://127.0.0.1:8102",
        "VAULT_DIR": f"{DATA}/vaults/{spec['vault_slug']}",
        "VAULT_SLUG": spec["vault_slug"],
        # Chromium's profile is per PARTITION, never per vault. Two chromiums
        # sharing one profile dir fight over its SingletonLock and the second
        # one dies with "profile is already in use" - so a shared vault shares
        # the Google credentials and the cookie jar directory, while each
        # partition's browser keeps its own profile under profiles/<slug>.
        "VAULT_PROFILE_DIR":
            f"{DATA}/vaults/{spec['vault_slug']}/profiles/{slug}",
        "VNC_DISPLAY": str(10 + spec["index"]),
        "VNC_PORT": str(p["vnc"]),
        "WEBSOCKIFY_PORT": str(p["ws"]),
        # The vault's own private chromium profile dir: the exporter, the auto
        # sign-in daemon, the keyboard driver and chromium all run with this as
        # their working dir, so the family finds the browser through these
        # numbers only - never a sibling container's service name.
        "CDP_HOST": "127.0.0.1",
        "CDP_PORT": str(p["cdp"]),
        "VNC_DRIVER_URL": f"http://127.0.0.1:{p['driver']}",
        "VNC_DRIVER_PORT": str(p["driver"]),
        "VNC_WS_URL": f"ws://127.0.0.1:{p['ws']}/websock",
        "VNC_PATH_PREFIX": f"/l/{slug}/vnc",
        "HOME": f"{DATA}/vaults/{spec['vault_slug']}",
        "HUB_DB": f"{DATA}/hub.db",
        "HUB_PORT": "8099",
    }
    if spec["index"] == 0:
        vals["RADIO_PORT"] = str(p["radio"])
    q = spec.get("quota", {})
    if "concurrentDownloads" in q:
        vals["GRABBY_MAX_CONCURRENT"] = str(int(q["concurrentDownloads"]))
    for k, v in (spec.get("settings") or {}).items():
        if ENV_RE.match(k):
            vals[k] = str(v)
    return vals


def radio_env(slug: str, spec: dict) -> str:
    """A broadcaster gets its partition's paths but its own listener: index.json,
    playlist and progress files come from the partition home, and PORT is the
    radio port (the shared tenant map would hand it the backend's port)."""
    p = ports(spec["index"])
    vals = tenant_vals(slug, spec)
    vals["PORT"] = str(p["radio"])
    vals["HOST"] = "0.0.0.0" if spec["index"] == 0 else "127.0.0.1"
    return env_str(vals)


def passthrough(names: tuple[str, ...]) -> dict:
    """Courier for credentials: the platform services read these names from
    the container's own environment, which is how they are configured today.
    Supervisor also passes its whole environment down to every program, so
    this is belt-and-braces (and it keeps the generated file honest about what
    a program actually gets); an unset name is simply absent, leaving the
    app's own default - the pre-hub behaviour.

    A name whose value supervisor's format cannot hold is dropped here (with a
    line on stderr) instead of left for env_str to raise: one unrepresentable
    password in the container's environment must not take every partition down,
    and the service that wanted it still gets supervisord's inherited copy - it
    simply is not repeated inside the generated block, which is why the warning
    goes to the log and not to a raise.
    """
    kept = {}
    for n in names:
        v = os.environ.get(n, "").strip()
        if not v:
            continue
        if env_val(v) is None:
            print(f"gen-conf: {n} left out of the generated config - {REASON}",
                  file=sys.stderr)
            continue
        kept[n] = v
    return kept


def pool_env(reg: dict) -> str:
    """The shared tuner's env. One process serves every partition, so it gets
    the pool directories (never a partition's home) and the union of every
    partition's cookie file; the pool itself picks the youngest live one per
    job, which is the contract step 4 installs (tunester reads
    TUNESTER_COOKIES as a comma list). Credentials are the container's own
    (passthrough), never a partition's `settings`: the pool is not a tenant."""
    vals = {
        "TUNESTER_OUTDIR": f"{DATA}/pool/out",
        "TUNESTER_CACHE": f"{DATA}/pool/cache",
        "TUNESTER_NZB_CACHE": f"{DATA}/pool/nzb-cache",
        # Content-addressed delivery (platform/poolimport.py): the pool writes
        # one blob per tuned file here and hands each requesting partition a
        # hardlink into its own cache. Unset (a standalone tunester deploy)
        # leaves /api/dl serving the job's own file, i.e. today's behaviour.
        "TUNESTER_BLOBS": f"{DATA}/pool/blobs",
        "TUNESTER_COOKIES": ",".join(dict.fromkeys(
            f"{DATA}/users/{slug}/cookies.txt" for slug in reg["slugs"])),
        # A search or a YouTube rip hammers the session as much as anything
        # else, so the pool goes through the same gate the downloaders use.
        # One word, because tunester uses this value as argv[0] verbatim
        # (sources.py: `YTDLP = os.environ.get(...)`) - a two-word value would
        # be taken as a single program name and fail exec.
        "TUNESTER_YTDLP": GATED_YTDLP,
        # The cap is the shared bucket's, so every caller in the box must agree
        # on it: the same value goes to the tenants (tenant_env) and here.
        "MAX_CONCURRENT_YTDLP": os.environ.get("MAX_CONCURRENT_YTDLP", "2"),
        "YTDGATE_SLOTS_DIR": SLOTS_DIR,
        # Published on :8102 for tunester.<domain>, so non-loopback.
        "TUNESTER_WEB_HOST": "0.0.0.0",
        "TUNESTER_WEB_PORT": "8102",
    }
    vals.update(passthrough(TUNESTER_KEYS))
    return env_str(vals)


def vault_env(slug: str, spec: dict, reg: dict) -> str:
    """A vault family (chromium, Xvfb, x11vnc, websockify, the VNC keyboard
    driver, the cookie exporter, the auto sign-in daemon) gets its partition's
    tenant env - which already carries the vault dirs, the loopback API URLs
    and the per-vault browser ports - plus the Google/VNC credentials, the
    public door URL and the FAN-OUT targets.

    `slug` here is the vault's HOST (see vault_hosts); the family serves every
    partition that names this vault, so the exporter's user-agent push has to
    reach all of their apps (the comma URL lists) and its writes have to land in
    all of their cookie paths:

      COOKIE_FANOUT_FILES / COOKIE_SESSION_STATE_FANOUT - the members' jars and
      state files. Each partition's apps read their own COOKIES_FILE, so the
      host's verified jar has to be copied there or a member's session ages out
      with no browser of its own to refresh it. The HOST'S OWN paths are not in
      these lists: the exporter keeps writing them the way it always did, and it
      is the only writer of `up` for this account.

    The single-valued DRIVEPOD_API_URL / GRABBY_API_URL from the tenant env stay
    as they are (the host partition), so the exporter works on one partition
    whether or not it has been taught the list forms.

    Credential precedence is the registry first: `GOOGLE_EMAIL`/`GOOGLE_PASSWORD`
    name the ACCOUNT a vault is, and a vault is per-partition configuration like
    any other, so the host partition's `settings` win and the container's own
    environment is only the fallback for a box with one account in `.env` (the
    pre-hub shape). Getting this backwards signs every vault in as the `.env`
    account while each partition's apps keep writing a different jar - the
    browser would be authenticating somebody else's session.
    """
    companions = [ports(reg["partitions"][s]["index"]) for s in reg["slugs"]
                  if reg["partitions"][s]["vault_slug"] == spec["vault_slug"]]
    paths = companion_paths(reg, slug)
    vals = {"VNC_URL": vnc_door(slug),
            "DRIVEPOD_PUSH_URLS": ",".join(
                f"http://127.0.0.1:{p['drivepod']}" for p in companions),
            "GRABBY_PUSH_URLS": ",".join(
                f"http://127.0.0.1:{p['grabby']}" for p in companions)}
    # Omitted, not empty, when the vault has no members: unset is exactly "no
    # companions" to the exporter, and an empty quoted value in the generated
    # file reads like a path that went missing.
    if paths["cookies"]:
        vals["COOKIE_FANOUT_FILES"] = ",".join(paths["cookies"])
    if paths["state"]:
        vals["COOKIE_SESSION_STATE_FANOUT"] = ",".join(paths["state"])
    merged = dict(passthrough(VAULT_KEYS))   # the container's .env: the fallback
    merged.update(tenant_vals(slug, spec))   # registry settings win over it
    merged.update(vals)                      # the family's own wiring wins most
    return env_str(merged)


def vnc_door(slug: str) -> str:
    """The backstop door's public URL, so the exporter's session-death message
    can point a human at the browser instead of hardcoding a hostname in
    source. SITE_DOMAIN is the value the picker and the VPS proxies use; unset
    leaves VNC_URL empty, which the exporter renders as a bare 'noVNC' hint.

    The `?path=` query is NOT decoration. The lite page builds its websocket
    URL as scheme://host[:port]/ + `path`, defaulting to the bare word
    `websockify` - under this gateway that would be the portal root, where no
    partition is implied and no location matches. The slug has to travel in the
    URL, and the query is the only place a static generated page can put it.
    """
    domain = SITE_DOMAIN
    if not domain:
        return ""
    return (f"http://{domain}/l/{slug}/vnc/vnc_lite.html"
            f"?path=l/{slug}/vnc/websockify")


def program(name: str, cmd: str, directory: str, env: str, prio: int,
            secs: int, stop: int, *, autostart: bool = True,
            autorestart: bool = True, stopsignal: str = "TERM") -> str:
    """One [program:] block. No `user=`: everything runs as root because /data
    is a host bind mount whose uid the container cannot predict (a dedicated
    user would have to match the host owner of ./data or every database write
    after a partition add would EACCES), and because nginx's master must bind
    :80. Chromium is launched with --no-sandbox for the same reason (see
    platform/vault-run.py), which is what its own container always did.

    Logs are one file per program under /var/log/sup (supervisor's
    childlogdir): `hub vault logs <slug>` and the watchdog both read them by
    name, so the name is part of the contract, not decoration.
    """
    log = f"/var/log/sup/{name}"
    body = (f"[program:{name}]\ncommand={cmd}\ndirectory={directory}\n")
    if env:
        body += f"environment={env}\n"
    body += (f"priority={prio}\n"
             f"autostart={'true' if autostart else 'false'}\n"
             f"autorestart={'true' if autorestart else 'false'}\n"
             f"startsecs={secs}\nstartretries=3\n"
             f"stopsignal={stopsignal}\nstopwaitsecs={stop}\n"
             f"stdout_logfile={log}.log\n"
             f"stderr_logfile={log}.err.log\nredirect_stderr=false\n\n")
    return body


# Priorities are supervisor's start order (ascending), and they encode the two
# real dependencies: the gate answers before the gateway serves a request, and
# the gateway is up before anything is proxied. Everything else is independent.
PRIO_GATE, PRIO_POOL, PRIO_TENANT, PRIO_RADIO, PRIO_PROXY = 12, 24, 20, 26, 14


def supervisor_conf(reg: dict) -> str:
    out = ["; generated by platform/gen-conf.py - do not edit\n",
           "; Programs are per partition (multi-tenancy is paths + supervision +\n",
           "; nginx, never app code) plus the shared services. Extra partitions are\n",
           "; gateway-only: only the front grabbytube (:8101) and the front\n",
           "; broadcaster (:8090) are published, so only index 0 binds non-loopback.\n"]
    for slug in reg["slugs"]:
        spec = reg["partitions"][slug]
        e = env_str(tenant_vals(slug, spec))
        # run-service does this partition's prisma push once, then execs the
        # backend, so a schema failure for one tenant cannot stop the others.
        out.append(program(
            f"drivepod-{slug}",
            f"python3 {STACK}/platform/run-service.py drivepod "
            "node dist/server.js",
            f"{STACK}/drivepod/backend", e, PRIO_TENANT, 5, 25))
        out.append(program(f"grabbytube-{slug}", "python3 main.py",
                           f"{STACK}/grabbytube", e, 22, 5, 15))
        if spec.get("radio", True):
            out.append(program(f"frogrock-{slug}", "node dist/server.js",
                               f"{STACK}/frogrock", radio_env(slug, spec),
                               PRIO_RADIO, 5, 20))
    if reg["front"]:
        # The front broadcaster doubles as the DNAS relay source: DNAS relays
        # http://stream-fix:8091 -> http://frogrock:8090 and the VPS proxy hits
        # :8090 straight through, so both stay outside the gateway by contract.
        # The shim listens non-loopback for exactly that reason.
        out.append(program("streamfix", f"python3 {STACK}/streamfix/fixer.py",
                           f"{STACK}/streamfix",
                           env_str({"LISTEN_PORT": "8091",
                                    "UPSTREAM_HOST": "127.0.0.1",
                                    "UPSTREAM_PORT": "8090"}), 30, 3, 10))

    # ---- shared services (one each, for every partition) --------------
    # POT tokens: the pip plugin is configured with this URL by both
    # downloaders, so one minting process serves the whole box. Server mode
    # keeps tokens in memory; nothing on disk is shared between tenants here.
    out.append(program(
        "bgutil", "node build/main.js --host 127.0.0.1 --port 4416",
        f"{STACK}/bgutil",
        env_str({"XDG_CACHE_HOME": "/run/bgutil-cache",
                 "NODE_OPTIONS": "--no-warnings"}),
        16, 5, 15))
    # The tuner is the only cross-partition writer: queued jobs, one blob per
    # download, hardlink delivery into each partition's cache.
    out.append(program("tunester", "python3 app.py --serve",
                       f"{STACK}/tunester", pool_env(reg), PRIO_POOL, 5, 30))
    out.append(program(
        "hubauth", "python3 hubauth.py", f"{STACK}/platform",
        env_str({"HUB_DB": f"{DATA}/hub.db", "HUB_PORT": "8099",
                 "HUB_REGISTRY": REGISTRY,
                 **passthrough(HUB_KEYS)}),
        PRIO_GATE, 3, 10))
    out.append(program(
        "watchdog", "python3 watchdog.py", f"{STACK}/platform",
        env_str({"HUB_REGISTRY": REGISTRY, "HUB_DB": f"{DATA}/hub.db",
                 "HUB_DATA_DIR": DATA, "STACK_DIR": STACK,
                 **passthrough(WATCHDOG_KEYS)}),
        18, 5, 15))
    # QUIT, not TERM: a graceful nginx stop drains the radio listeners that are
    # mid-stream instead of cutting every one of them off.
    out.append(program("nginx", 'nginx -g "daemon off;"', "/etc/nginx", "",
                       PRIO_PROXY, 3, 25, stopsignal="QUIT"))

    # ---- vaults ------------------------------------------------------
    # autostart/autorestart false: the whole point is that no browser runs
    # until a session is actually stale (the owner's invariant, enforced by
    # construction). The watchdog starts and stops these; so does
    # `hub vault start|stop`.
    #
    # ONE program per distinct vault id (vault_hosts), named after the VAULT, not
    # the partition: the vault id is the Google account, and starting one family
    # per partition sharing it is what put two chromiums and two exporters on one
    # account. The program carries the host partition's tenant env, so its
    # browser ports, profile dir and jar are that partition's; members get their
    # jar by fan-out and their door by proxy (see _locations).
    for vault, host in vault_hosts(reg).items():
        out.append(program(
            f"vault-{vault}",
            f"python3 {STACK}/platform/vault-run.py {host}",
            f"{STACK}/vault", vault_env(host, reg["partitions"][host], reg),
            40, 5, 30, autostart=False, autorestart=False))
    return "".join(out)


# --------------------------------------------------------------------------
# One generated prefix block per partition. Every proxy_pass target and every
# slug header is a LITERAL for that slug (no runtime-variable upstreams, no
# resolver dependency, no map-then-interpolate tricks), so `nginx -t` fails at
# generation time rather than at the first request.
#
# Where a prefix location has a REGEX SIBLING under the same path, the prefix
# carries no modifier. `^~` means "this prefix is the longest match, do not
# evaluate regexes", so on `/api/` and `/radio/` it would not protect anything
# from another block - it would only swallow its own regexes, and the broadcast
# stream (`~ ^/radio/stream`), the pool's job surface (`~ ^/api/jobs`) and a
# mounted partition's `/api/auth/` would all land on the prefix instead: the
# stream would get the player, the jobs would get drivepod's 404, and login
# would POST to the wrong backend. Plain prefixes still lose to a matching
# regex and still win by length against a shorter prefix, which is the routing
# this file depends on.
# `^~` is kept exactly where a prefix has no regex sibling that must win -
# /cache/ (its dotted-progress regex is NESTED inside, see below), /library/,
# /vnc/, /grabby/ and the shell prefix, where an SPA route such as
# /l/<slug>/library must never be re-routed by a sibling regex. Nothing outside
# a partition can hijack these either: the portal-root regexes in gateway.conf
# (`/main-<hash>.js`, `/_gate/`) are anchored at `/`, so they cannot match a
# `/l/<slug>/` URI.

GATE_JSON = ('    auth_request /_gate/{slug};\n'
             '    error_page 401 403 = @noauth;\n')
GATE_LOGIN = ('    auth_request /_gate/{slug};\n'
              '    error_page 401 403 = /_login-redir;\n')
SLUG_HDR = '    proxy_set_header X-Partition-Slug "{slug}";\n'
SVC = "    include /etc/nginx/snippets/proxy-svc.conf;\n"


def _locations(slug: str, prefix: str, p: dict, spec: dict,
               shell: bool = True, ws_port: int | None = None) -> str:
    """Locations for one partition.

    `prefix` is "/l/<slug>" for every partition, or "" for the front
    partition's bare aliases (the paths today's callers use: the vault's own
    callbacks, DNAS/relay, the VPS server_names' paths and any bookmark to
    /api/... or /cache/...). Every proxy_pass target and every slug header is
    a LITERAL for that slug - no runtime-variable upstreams, no resolver, no
    map-then-interpolate - so a bad generated file is caught by `nginx -t`
    rather than by the first request.

    `ws_port` is the websockify port the door proxies to, and it DIFFERS from
    this partition's own whenever the partition shares another's vault: no
    chromium runs for a member, so its noVNC door has to reach the HOST's
    browser. Omitted means "this slug is its own vault host" (vault_hosts hands
    both callers the value that is not).
    """
    gate = GATE_JSON.format(slug=slug)
    glogin = GATE_LOGIN.format(slug=slug)
    hdr = SLUG_HDR.format(slug=slug)
    home = f"{DATA}/users/{slug}"
    out = [f"\n# ==== partition {slug}: {prefix or 'bare aliases'} ====\n"]
    if prefix:
        out.append(f"location = {prefix} {{\n    return 301 {prefix}/;\n}}\n")
        out.append(f'location ~ "^{prefix}/api/auth/" {{\n'
                   "    include /etc/nginx/snippets/proxy-auth.conf;\n"
                   # hubauth's routes are bare (/login, /pair, /logout, /me,
                   # /codes), and a regex location with a URI-less proxy_pass
                   # forwards the WHOLE match, so the mounted form has to take
                   # its own mount off: /l/family/api/auth/login -> /login. The
                   # front's bare aliases need no rewrite - gateway.conf's exact
                   # locations already map /api/auth/<verb> to the verb.
                   f"    rewrite ^{prefix}/api/auth/(.*)$ /$1 break;\n"
                   f"    proxy_pass http://127.0.0.1:8099;\n}}\n")
    # Churning state files first: exact locations so they can be no-cache
    # while the mp3s beside them (written once, renamed into place) stay
    # cacheable for a day.
    for name in ("index.json", "status.json", "grabby-played.json",
                 "completed.json"):
        out.append(f"location = {prefix}/cache/{name} {{\n{gate}"
                   f"    alias {home}/cache/{name};\n"
                   "    etag on;\n"
                   '    add_header Cache-Control "no-cache" always;\n'
                   '    add_header Cross-Origin-Resource-Policy "cross-origin" always;\n'
                   '    add_header Access-Control-Allow-Origin "*" always;\n'
                   "    default_type application/json;\n}\n")
    # Per-member progress overlay - the multi-device guarantee, in TWO forms:
    #   {prefix}/cache/progress.json        -> progress-<member>.json, where
    #       <member> is whoever the gate named in X-User; with no gate identity
    #       (a direct loopback call, or a partition reached through a published
    #       port) the second try_files candidate is the partition's own
    #       progress.json, i.e. exactly the pre-hub file. A PUT here lands on
    #       405 (this is the static tree) and that is correct: the apps write
    #       these files through their own api, nginx only ever reads them.
    #   {prefix}/cache/progress.<member>.json -> that same member file, so a
    #       script can name one explicitly (the plan's dotted form).
    # `root` + `try_files` rather than `alias`: an alias assembled from an unset
    # $gate_user yields `progress-.json` and 404s a legitimate member, while a
    # try_files candidate that does not exist simply falls through to the next.
    # Verified against nginx 1.27 (PCRE compile of the quoted braced pattern,
    # alias-vs-try_files, the 304 revalidation the polling SPA depends on, and
    # the nesting described below).
    out.append(f"location = {prefix}/cache/progress.json {{\n{gate}"
               "    auth_request_set $gate_user $upstream_http_x_user;\n"
               f"    root {DATA};\n"
               f"    try_files /users/{slug}/cache/progress-$gate_user.json"
               f" /users/{slug}/cache/progress.json =404;\n"
               "    etag on;\n"
               '    add_header Cache-Control "no-cache" always;\n'
               '    add_header Cross-Origin-Resource-Policy "cross-origin" always;\n'
               '    add_header Access-Control-Allow-Origin "*" always;\n'
               "    default_type application/json;\n}\n")
    # Partition media tree: DrivePod's cache/ and GrabbyTube's library/. The
    # dotted progress form is NESTED inside the cache block instead of written
    # beside it: `^~` means "this prefix wins, stop evaluating regexes", so a
    # sibling regex location could never be reached for these URIs (the shape
    # the pre-hub config never had to care about). The regex is QUOTED because
    # nginx's config tokenizer stops a bare location pattern at the first `{`
    # of `{1,40}` - unquoted it compiles as a truncated pattern and nginx -t
    # fails with "missing closing parenthesis".
    out.append(f"location ^~ {prefix}/cache/ {{\n{gate}"
               f"    alias {home}/cache/;\n"
               f"    include /etc/nginx/snippets/media-static.conf;\n"
               '    location ~ "^' + prefix + r'/cache/progress\.(?<puser>'
               '[a-z0-9_.-]{1,40})\\.json$" {\n'
               f"{gate}"
               f"    root {DATA};\n"
               f"    try_files /users/{slug}/cache/progress-$puser.json =404;\n"
               "    add_header Cache-Control \"no-cache\" always;\n"
               "    default_type application/json;\n    }\n}\n")
    out.append(f"location ^~ {prefix}/library/ {{\n{gate}"
               f"    alias {home}/library/;\n"
               f"    include /etc/nginx/snippets/media-static.conf;\n}}\n")
    # Radio: the stream first (chunkless 1.0 framing; the ?pass= allowance is
    # judged by the gate, which sees the parent URI, so no nginx map is
    # involved), then the player UI and its status/playlist/shoutcast API.
    # Both strip this partition's mount before proxying, because the
    # broadcaster's own routes are root-level (/stream.mp3, /api/status.json,
    # /static/*) while its player emits mount-prefixed URLs derived from
    # document.baseURI - nginx is the one that removes the mount. A
    # trailing-slash proxy_pass does the strip for the prefix location; the
    # regex location needs an explicit rewrite, because a proxy_pass with no
    # URI in a regex location forwards the WHOLE matched path. A bare
    # {prefix}/radio would match neither rule and 404, so it redirects.
    out.append(f"location = {prefix}/radio {{\n    return 301 {prefix}/radio/;\n}}\n")
    out.append(f'location ~ "^{prefix}/radio/stream(?:\\.mp3)?" {{\n'
               f"    auth_request /_gate/{slug};\n"
               "    error_page 401 403 = @noauth;\n"
               f"    rewrite ^{prefix}/radio/stream(?:\\.mp3)?$"
               " /stream.mp3 break;\n"
               "    include /etc/nginx/snippets/proxy-stream.conf;\n"
               f"    proxy_pass http://127.0.0.1:{p['radio']};\n}}\n")
    out.append(f"location {prefix}/radio/ {{\n{glogin}"
               f"{hdr}"
               "    include /etc/nginx/snippets/proxy-svc.conf;\n"
               f"    proxy_pass http://127.0.0.1:{p['radio']}/;\n}}\n")
    # Vault door (chromium starts on demand; the watchdog owns its life). The
    # stock Debian noVNC tree is served from UNDER the prefix so the lite page's
    # relative module imports (./core/rfb.js) resolve inside this prefix and
    # stay gated. THREE locations, because two shapes were tried live in this
    # container and each broke something:
    #   `= {prefix}/vnc`  - the bare door, redirecting to the lite page with
    #     whatever query came in (`?path=` is how the door URL names its own
    #     websocket: rfb builds the URL as scheme://host/ + path, so the page's
    #     stock default of `websockify` would aim at the portal ROOT, where no
    #     partition is implied; vnc_door() always emits it).
    #   `= {prefix}/vnc/` - the door WITH its slash, which is what a browser
    #     gets from the picker and what the plan's check asks for (200 for a
    #     member). A `try_files $uri $uri/ …` prefix location cannot answer
    #     that: the directory candidate goes through nginx's directory-index
    #     phase, `/usr/share/novnc/` has no index.html, so the request dies as
    #     403 "directory index forbidden" and the location's own error_page
    #     turns it into a login redirect - a member with a perfectly good
    #     session bounced to /login. Serving the lite page from an EXACT
    #     location whose single try_files candidate is a FILE skips that phase
    #     entirely. sub_filter rewrites the page's default ws path to THIS
    #     partition's, so a bookmark to the door with no query still connects;
    #     sub_filter needs the body uncompressed, hence gzip off here only.
    #     A sub_filter for the bare `websockify` string instead would have
    #     rewritten rfb.js's own `ws://host/websockify` fallback too.
    #   `^~ {prefix}/vnc/` - the static tree, with NO directory candidate at
    #     all (`$uri =404`): NOVNC_DIR holds only the noVNC release, so every
    #     real request is a file, and the `alias`+index combination
    #     was worse than the bug it fixed - an appended index name produced
    #     "vnc_lite.htmlindex.html is not a directory" (500) and then served
    #     the lite page for every MISS, so a missing .js came back 200 with
    #     HTML in it instead of 404.
    mount = f"{prefix}/".lstrip("/")                 # "l/owner/" or "" at root
    stock_path = "readQueryVariable('path', 'websockify')"
    own_path = f"readQueryVariable('path', '{mount}vnc/websockify')"
    out.append(f"location = {prefix}/vnc {{\n{glogin}"
               f"    return 302 {prefix}/vnc/vnc_lite.html$is_args$args;\n}}\n")
    out.append(f"location = {prefix}/vnc/ {{\n{glogin}"
               f"    alias {NOVNC_DIR}/;\n"
               "    gzip off;\n    sub_filter_once on;\n"
               f"    sub_filter \"{stock_path}\" \"{own_path}\";\n"
               "    try_files /vnc_lite.html =404;\n}\n")
    out.append(f"location ^~ {prefix}/vnc/ {{\n{glogin}"
               f"    alias {NOVNC_DIR}/;\n"
               "    try_files $uri =404;\n}\n")
    out.append(f"location = {prefix}/vnc/websockify {{\n{gate}"
               f"    proxy_pass http://127.0.0.1:{ws_port or p['ws']};\n"
               "    proxy_http_version 1.1;\n"
               "    proxy_set_header Upgrade $http_upgrade;\n"
               '    proxy_set_header Connection "upgrade";\n'
               "    proxy_buffering off;\n    proxy_read_timeout 3600s;\n}\n")
    # GrabbyTube's OWN tuning UI: a static page at / plus /static/*, whose JS
    # derives the mount from document.baseURI. The gateway mounts it at
    # {prefix}/grabby/ and the trailing-slash proxy_pass strips the mount, so
    #   /l/family/grabby/tracks    -> /tracks        (its own API)
    #   /l/family/grabby/static/x  -> /static/x
    #   /l/family/grabby/          -> /              (its index.html)
    #   /grabby/tracks             -> /tracks        (front, no prefix)
    # Nothing else in the box sends grabby a prefixed path - DrivePod calls it
    # over loopback with bare paths - which is why grabby itself needs no
    # mount handling.
    out.append(f"location = {prefix}/grabby {{\n    return 301 {prefix}/grabby/;\n}}\n")
    out.append(f"location ^~ {prefix}/grabby/ {{\n{glogin}{hdr}"
               "    include /etc/nginx/snippets/proxy-svc.conf;\n"
               f"    proxy_pass http://127.0.0.1:{p['grabby']}/;\n}}\n")
    # The shared pool's job surface (tunester :8102). Tunester's own routes
    # already carry the /api prefix, so this rewrite removes only the mount and
    # keeps the suffix: /l/family/api/jobs -> /api/jobs, /api/jobs -> /api/jobs.
    # Everything else under api/ is the partition's backend, and grabby's API is
    # reachable only through its own mount above: its route names (/search,
    # /queue, /jobs, /cookies, /settings/..., /test/run) are its UI's private
    # surface and would need a much longer alternation to split by name, while
    # nothing in the box calls them through the gateway (DrivePod reaches grabby
    # over loopback). A half-covered `/api/(tracks|library|...)` alternation is
    # how the first draft shipped routes that 404'd on their first request.
    out.append(f'location ~ "^{prefix}/api/(?:(?:jobs|clear|health)(?:/.*)?)$" {{\n'
               f"{gate}{hdr}{SVC}"
               f"    rewrite ^{prefix}/api/(.*)$ /api/$1 break;\n"
               f"    proxy_pass http://127.0.0.1:8102;\n}}\n")
    # Everything else under api/ is this partition's backend. X-User is the
    # gate's authoritative identity: a client-sent X-User is OVERWRITTEN (no
    # one can claim someone else's resume file by sending a header), and it is
    # what lets the backend name the per-member progress file it mirrors.
    out.append(f"location {prefix}/api/ {{\n{gate}"
               "    auth_request_set $gate_user $upstream_http_x_user;\n"
               f"{hdr}"
               "    proxy_set_header X-User $gate_user;\n"
               f"{SVC}"
               f"    proxy_pass http://127.0.0.1:{p['drivepod']};\n}}\n")
    # No rewrite here on purpose: this partition's backend serves /api/...
    # itself, so the mounted URI arrives whole and the backend's own
    # HUB_URL_PREFIX middleware takes the mount off (see
    # stack/drivepod/backend/src/server.ts). A client-sent X-User never survives
    # the gate: this header is set from the identity the gate resolved.
    if not shell:
        return "".join(out)
    # SPA shell: a 401 here is a human without a session, so bounce to the
    # login page instead of printing JSON. The shell lives UNDER this prefix,
    # so Angular's client-side routes re-enter this same location (right gate,
    # right base href) rather than escaping to another partition's shell.
    # gen-conf writes the shell from the built index.html with the partition's
    # base href; the gate's X-User is substituted into its identity placeholder
    # so the app knows whose progress file is whose. gzip is off here because
    # sub_filter skips compressed bodies, and the shell is ~2 KB anyway.
    out.append(f"location ^~ {prefix}/ {{\n{glogin}"
               '    auth_request_set $gate_user $upstream_http_x_user;\n'
               "    gzip off;\n"
               "    sub_filter_once on;\n"
               "    sub_filter __HUB_USER__ $gate_user;\n"
               f"    root {SPA_WWW};\n"
               f"    try_files $uri $uri/ {prefix}/index.html;\n"
               '    add_header Cache-Control "no-cache" always;\n}\n')
    return "".join(out)


def _front_bare(front: str, p: dict, ws_port: int | None = None) -> str:
    """Front-partition aliases with no prefix, so the callers that predate the
    gateway keep working byte-identically. No shell here: the portal root is
    the picker (the decision on the legacy hostnames), and the front
    partition's own SPA lives at /l/<front>/ like every other one."""
    return _locations(front, "", p, {}, shell=False, ws_port=ws_port)


def nginx_locations(reg: dict) -> str:
    hosts = vault_hosts(reg)
    out = ["# generated by platform/gen-conf.py - do not edit\n"]
    for slug in reg["slugs"]:
        spec = reg["partitions"][slug]
        # The door follows the vault's host, so a member's noVNC reaches the one
        # browser its account has. Identity when the slug IS the host.
        host = hosts[spec["vault_slug"]]
        ws = ports(reg["partitions"][host]["index"])["ws"]
        out.append(_locations(slug, f"/l/{slug}", ports(spec["index"]), spec,
                              ws_port=ws))
    if reg["front"]:
        front = reg["partitions"][reg["front"]]
        host = hosts[front["vault_slug"]]
        ws = ports(reg["partitions"][host]["index"])["ws"]
        out.append(_front_bare(reg["front"], ports(front["index"]), ws))
    return "".join(out)


def nginx_maps(reg: dict) -> str:
    """Nothing here needs an nginx map (every gate target is a literal and the
    ?pass= allowance is judged by the auth server), but the platform template
    includes conf.d/05-maps.conf, so keep generating it - it is also where the
    RADIO_PASS contract gets recorded for whoever is reading the config."""
    radio_pass = os.environ.get("RADIO_PASS", "").strip()
    return ("# generated by platform/gen-conf.py - do not edit\n"
            "# No maps: identity decisions live in hubauth (HUB_RADIO_PASS is\n"
            "# the ?pass= allowance for radio streams, set in the environment).\n"
            f"# RADIO_PASS here: {'set' if radio_pass else 'unset'}\n")


# Relative refs in the built shell become root-absolute. The shell is served
# from /l/<slug>/, while its hashed bundles are portal-root locations (see
# nginx/gateway.conf): left relative, `main-XXXX.js` would be requested as
# `/l/<slug>/main-XXXX.js`, which the shell location answers with index.html -
# the app would receive an HTML body where it expected JavaScript.
_REL_REF = re.compile(r'\b(?P<attr>src|href)="(?!https?:|data:|#|/)(?P<val>[^"]+)"')


def absolutize_refs(html: str) -> str:
    """Root-absolute every relative `src`/`href` in the shell.

    The lookahead skips absolute paths, `http(s):`, `data:`, `mailto:` and
    fragment targets, so only Angular's `main-….js` / `styles-….css` /
    `favicon.ico` style refs are touched. Attribute VALUES only: a tag's
    `media="print" onload="this.media='all'"` survives untouched, and the
    bundles land on the gateway's immutable ungated locations at root, which
    is the shape every partition shares.
    """
    return _REL_REF.sub(lambda m: f'{m["attr"]}="/{m["val"]}"', html)


HUB_SCRIPT = ('<script>window.__HUB__={{"slug":"{slug}",'
              '"user":"__HUB_USER__"}}</script>')


def spa_shells(reg: dict) -> list[tuple[str, str]]:
    """Write one SPA shell per partition from the single built index.html.

    Two rewrites, both forced by serving one Angular build from many mounts:
    the <base href> becomes this partition's prefix (the app derives its
    apiUrl, its cache/ polls and its stream URLs from exactly that value), and
    every relative bundle ref is made root-absolute (absolutize_refs). The
    identity script is written AFTER a CLOSED <base> tag: text placed inside
    an unclosed `<base ... ` is swallowed into the start tag by the HTML
    parser, so a <script> that has not been closed never executes - which is
    how the previous version of this function silently shipped neither a
    partition base href nor __HUB__.
    """
    built = f"{SPA_WWW}/index.html.dist"
    try:
        with open(built) as fh:
            index = fh.read()
    except OSError:
        return []        # nothing built yet; shells appear on the next reload
    index = absolutize_refs(index)
    head, sep, tail = index.partition("<base")
    if not sep:
        print("gen-conf: built index.html has no <base href>; cannot make "
              "partition shells", file=sys.stderr)
        return []
    _, _, rest = tail.partition(">")     # the tag's own '>' is consumed here
    out = []
    for slug in reg["slugs"]:
        out.append((f"{SPA_WWW}/l/{slug}/index.html",
                    f'{head}<base href="/l/{slug}/">'
                    f'{HUB_SCRIPT.format(slug=slug)}{rest}'))
    if reg["front"]:
        # Front partition at the portal root too (base href "/"), for callers
        # that already have the app cached at root; the picker owns `/` itself.
        out.append((f"{SPA_WWW}/index.html",
                    f'{head}<base href="/">'
                    f'{HUB_SCRIPT.format(slug=reg["front"])}{rest}'))
    return out
def picker_html(reg: dict) -> str:
    """The public door: links to every partition from the same registry that
    drives the proxies, so a new partition appears here without a rebuild."""
    hosts = vault_hosts(reg)
    cards = []
    for slug in reg["slugs"]:
        spec = reg["partitions"][slug]
        mode = "shared" if spec["mode"] == "shared" else "personal"
        members = len(spec.get("members", [])) or 1
        vault = spec["vault_slug"]
        # Name the vault and say when it is shared: a vault is a Google account,
        # and picking between two partitions that sign in as the SAME one is a
        # different choice from picking between two accounts (one browser covers
        # both, so starting sign-in in one starts it for the other).
        users = sum(1 for s in reg["slugs"]
                    if reg["partitions"][s]["vault_slug"] == vault)
        if users == 1:
            shared = ""
        elif slug == hosts[vault]:
            shared = f" (sign-in host for {users - 1} other)"
        else:
            shared = f" (signs in via /{hosts[vault]})"
        cards.append(
            f'    <a class="card" href="/l/{slug}/">\n'
            f'      <h2>{spec["name"]}</h2>\n'
            f'      <p class="slug">/{slug}</p>\n'
            f'      <p class="meta">{mode} &middot; {members} member'
            f'{"s" if members != 1 else ""}'
            f'{" &middot; radio" if spec.get("radio", True) else ""}'
            f' &middot; vault {vault}{shared}</p>\n'
            f'    </a>')
    return ("<!doctype html>\n<html lang=en>\n<head>\n<meta charset=utf-8>\n"
            "<meta name=viewport content=\"width=device-width,initial-scale=1\">\n"
            "<title>listen</title>\n<link rel=stylesheet href=/hub.css>\n</head>\n<body>\n"
            "<header><h1>listen</h1>"
            "<nav><a href=/login>Log in</a> <a href=/pair>Pair a device</a></nav></header>\n"
            "<main>\n  <section class=cards>\n" + "\n".join(cards) + "\n  </section>\n"
            "</main>\n<footer>One machine, one stack, many listeners.</footer>\n"
            "</body>\n</html>\n")



# --------------------------------------------------------------------------
# The public pages: the picker is above, these are the three doors hubauth
# answers (/api/auth/login, /api/auth/pair, /api/auth/logout). They carry the
# same partition list as the picker because a device pairing a car must be able
# to name the partition it is joining, and the list must never disagree with
# what the proxies were generated from. hub.js is shared and static (hub/hub.js)
# and drives any form with a data-action attribute.

_LOGIN = """<!doctype html>
<html lang=en>
<head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Log in - listen</title>
<link rel=stylesheet href=/hub.css>
</head>
<body class="narrow">
<h1><a href=/>listen</a></h1>
<h2>Log in</h2>
<form data-action=login>
  <label>Partition
    <select name=slug>%(options)s</select>
  </label>
  <label>Name <input name=username autocomplete=username required></label>
  <label>Password <input name=password type=password autocomplete=current-password required></label>
  <p class=err data-error></p>
  <p><button type=submit>Log in</button> <a class=alt href=/pair>Pair a device instead</a></p>
</form>
<p class=hint>A partition name and password come from the stack owner. A session
is bound to one partition; another partition needs its own login.</p>
</body>
</html>
"""

_PAIR = """<!doctype html>
<html lang=en>
<head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Pair a device - listen</title>
<link rel=stylesheet href=/hub.css>
</head>
<body class="narrow">
<h1><a href=/>listen</a></h1>
<h2>Pair a device</h2>
<form data-action=pair>
  <label>Partition
    <select name=slug>%(options)s</select>
  </label>
  <label>Code <input name=code pattern="[A-Z0-9]{8}" autocomplete=one-time-code
         autocapitalize=characters required></label>
  <p class=err data-error></p>
  <p><button type=submit>Pair</button> <a class=alt href=/login>Use a password instead</a></p>
</form>
<p class=hint>Ask a partition admin for a code, then type it here. Codes expire
in 10 minutes and are single use, so a shared car never stores a password.
<code>hub code %(front)s</code> signs the device in as a member of its own
(<code>pair-&hellip;</code>, created when it pairs), so two listeners in one
partition never overwrite each other&rsquo;s resume point; <code>hub code
%(front)s --as car</code> signs it in as an existing member, which is how one
car keeps its position after the code is re-minted.</p>
</body>
</html>
"""

_LOGOUT = """<!doctype html>
<html lang=en>
<head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Logged out - listen</title>
<link rel=stylesheet href=/hub.css>
</head>
<body class="narrow">
<h1><a href=/>listen</a></h1>
<h2>Signing out</h2>
<form data-action=logout>
  <p><button type=submit>Sign out of this device</button></p>
</form>
<p class=hint>Only this device's session is dropped. Other devices paired to the
same partition keep listening.</p>
</body>
</html>
"""


def auth_pages(reg: dict) -> list[tuple[str, str]]:
    options = "\n".join(
        f'      <option value="{slug}">{reg["partitions"][slug]["name"]} (/{slug})</option>'
        for slug in reg["slugs"])
    front = reg["front"] or (reg["slugs"][0] if reg["slugs"] else "owner")
    out = [(f"{HUB_WWW}/login.html", _LOGIN % {"options": options}),
           (f"{HUB_WWW}/pair.html", _PAIR % {"options": options,
                                             "front": front})]
    out.append((f"{HUB_WWW}/logout.html", _LOGOUT))
    return out


def main() -> int:
    reg = load_registry()
    os.makedirs(SUP_DIR, exist_ok=True)
    os.makedirs(LOC_DIR, exist_ok=True)
    os.makedirs("/etc/nginx/conf.d", exist_ok=True)
    os.makedirs(HUB_WWW, exist_ok=True)
    for slug in reg["slugs"]:
        spec = reg["partitions"][slug]
        home = f"{DATA}/users/{slug}"
        for sub in ("cache", "library", "tmp", "logs"):
            os.makedirs(f"{home}/{sub}", exist_ok=True)
        os.makedirs(f"{DATA}/vaults/{spec['vault_slug']}", exist_ok=True)
    targets = [(f"{SUP_DIR}/stack-programs.conf", supervisor_conf(reg)),
               (f"{LOC_DIR}/10-locations.conf", nginx_locations(reg)),
               (MAPS, nginx_maps(reg)),
               (f"{HUB_WWW}/index.html", picker_html(reg))]
    targets += spa_shells(reg)
    targets += auth_pages(reg)
    for path, body in targets:
        tmp = path + ".tmp"
        # A partition's SPA shell lives under a directory that only this
        # function creates (`/var/www/drivepod/l/<slug>/`): the image ships the
        # built app's own files and nothing else, so without this the first
        # write after a `hub partition add` is a FileNotFoundError and none of
        # the generated config lands.
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as fh:
            fh.write(body)
        os.replace(tmp, path)
    print(f"gen-conf: {len(reg['slugs'])} partition(s) {reg['slugs']} "
          f"front={reg['front']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
