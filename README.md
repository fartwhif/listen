# listen — the audio stack, one container

DrivePod (library + player), GrabbyTube (YouTube → mp3), tunester (the tuner
pool), FrogRock (live radio), stream-fix (the DNAS relay shim), the cookie
vaults and a bgutil PO-token server — all six as supervised programs inside
**one** container behind **one** authenticated gateway, partitioned into
independent libraries for several people.

The pre-hub shape was six containers, six WireGuard peers, six `.env` files and
one shared Google session that four of them fought over. Multi-tenancy here is
**paths + supervision + nginx**, never app code: a partition is a directory, a
set of supervisor programs on its own ports and a set of generated nginx
locations. `stack/` and `vendor/` hold the apps' source; see
[`VENDORED.md`](VENDORED.md) for the pins and every patch.

## Shape

```
VPS nginx ─ HTTPS ─> peer:80    gateway (nginx) ── the only gated listener
                 └─────────────> peer:8090  front broadcaster (radio, public)
                 └─────────────> peer:8101  front GrabbyTube UI
                 └─────────────> peer:8102  shared tuner pool

listen container (supervisord is PID 1, so orphans are reaped)
├ gateway      :80        nginx; auth_request -> hubauth; generated locations
├ hubauth    :8099        gate + /login /pair /api/auth/* (loopback)
├ bgutil     :4416        one PO-token server for the whole box (loopback)
├ tunester   :8102        ONE tuner for every partition (the pool)
├ per partition i: drivepod 3000+i · grabby 8101 (i=0) else 8111+i
│                        · radio 8090 (i=0) else 8200+i
│                        · vault VNC 5901+i · websockify 8181+i (autostart=off)
└ stream-fix :8091        DNAS's relay target -> 127.0.0.1:8090
```

State lives in ONE directory — `$HUB_DATA_DIR` inside the container, which is
`$HUB_HOST_DIR/listen-data` on the host (the bind mount `docker-compose.yml`
creates; `HUB_HOST_DIR` is the knob, defaults to the repo's parent). One mount,
not a volume plus side binds, because `os.link()` is a same-**mount** operation:
the first-start import hardlinks the pre-hub media trees, and a second bind of
the same filesystem would make it a 41 GB copy (see Gotchas).

```
<state>/hub.db                       users, memberships, sessions, pair codes
<state>/partitions.json              the registry: the ONLY partition config
<state>/pool/{out,cache,nzb-cache,jobs,blobs}   shared tuner pool + blob store
<state>/vaults/<vault>/                        one Google account: profile root
<state>/vaults/<vault>/profiles/<slug>/          per-partition chromium profile
<state>/users/<slug>/cache/          media (DrivePod library, GrabbyTube harvests)
<state>/users/<slug>/cookies.txt     this partition's jar (a shared vault's comes
                                     from the vault host's browser, by fan-out)
<state>/users/<slug>/{radio,library}/            radio state, tuned catalog
<state>/users/<slug>/{grabbytube.db,database.db} per-partition databases
```

Moving it to another drive: `docker compose down`, `mv` the directory, set
`HUB_HOST_DIR` to the new parent, `up -d`. Needs a filesystem with hardlinks
(ext4/xfs/btrfs), not a network mount.

## Everything comes from the registry

`platform/gen-conf.py` turns `partitions.json` into supervisor programs, nginx
locations, the picker page, the per-partition SPA shells and the login/pair
pages. `platform/watchdog.py` re-runs it when the registry changes
(`supervisorctl reread/update` + `nginx -s reload`) and owns the cookie vaults'
lives. **Adding a partition is one JSON edit** (or one `hub partition add`) — no
rebuild, no `docker compose up`. Slug index decides ports, and index 0 (the
front) keeps today's port numbers so existing callers and VPS proxies are
unaffected. Ten partitions max: the port map is arithmetic.

```json
{ "slugs": ["owner", "family"],
  "defaultSlug": "owner",
  "partitions": {
    "owner":  { "mode": "single", "name": "Home",   "owner": "owner",
                "radio": true, "vault": "owner" },
    "family": { "mode": "shared", "name": "Kids",   "owner": "owner",
                "radio": false, "vault": "owner",
                "quota": { "concurrentDownloads": 1 } } } }
```

`"vault": "owner"` means *this partition signs in as owner's Google account*. A
vault id is an **account**, not a partition, so it gets **one** browser family:
supervisor program `vault-<vault>`, run by the **host** — the first partition in
slug order naming that vault. Every other partition on it gets the app programs,
its own chromium **profile** under `vaults/<vault>/profiles/<slug>/` (two
chromiums on one profile dir fight over the `SingletonLock`) and its own
`cookies.txt`, which the host's exporter **fans out** to — along with its
`session-state.json`, so a member's watchdog keeps seeing the account's real
liveness with no browser of its own. That is the whole reason for the dedup: two
chromiums authenticating as one account each re-type the password and each
rotate the jar the other's yt-dlp write-backs maintain. `mode: shared` allows
several members; `single` pins the partition to one user.
`quota.concurrentDownloads` caps that partition's concurrent tuner jobs, so one
member's queue cannot starve another's.

A second Google account is a **second vault id plus credentials on its host
partition**: `"settings": { "GOOGLE_EMAIL": "<account>@example.com",
"GOOGLE_PASSWORD": "…" }` on the partition that hosts that vault (the first in
slug order naming it). The registry wins over `.env`, and any name the settings
do not carry falls back to `.env` — so `GOOGLE_EMAIL`/`GOOGLE_PASSWORD` in `.env`
is the one-account-per-box default, and two vault ids sharing it would run two
browsers on one account, which is exactly the fight the dedup exists to prevent.

## Path contract

- `/` — partition picker (public).
- `/<front>/…` — the front partition answers at **root**, so every existing
  bookmark, car UI, vault callback and VPS `server_name` keeps working
  byte-identically. `/l/<slug>/…` is every other partition; an unknown slug
  matches no location and 404s, so a typo can never borrow someone else's data.
- `GET /health` — liveness, public.
- Media is served by nginx off the state directory, not by the apps:
  `…/cache/…` and `…/library/…` with the gate in front. The churning JSON
  (`index.json`, `progress.json`, `status.json`) is `no-cache` with an `ETag`;
  the mp3s beside it are cacheable for a day.
- `…/cache/progress.json` is a **per-member** file: the gate names the member in
  `X-User` and nginx serves `progress-<user>.json`, falling back to the
  partition's own `progress.json` when there is no gate identity. Two people in
  one partition (or one person's phone and car) never overwrite each other's
  resume point.

Credentials the gate accepts, in order: `Authorization: Bearer <api-key>`, the
session cookie, `?key=<api-key>`, and — **stream path only** — `?pass=<HUB_RADIO_PASS>`
for anonymous broadcast listeners (checked in hubauth against `X-Gate-Uri`, so
`?pass=` cannot unlock an API route). Unauthenticated hits redirect to `/login`;
API paths get `401` + JSON instead.

The `/login` and `/pair` doors are plain `method=post` forms carrying a
`data-action` attribute, and `hub/hub.js` (served ungated at `/hub.js`) is what
turns that into a `fetch` to `/api/auth/*` and paints the result. Both halves
are load-bearing: with scripting on the answer is JSON (`{"redirect": …}`
followed by the script, errors shown inline); with scripting off the same
keystrokes post the form and hubauth answers a browser `Accept: text/html` with
the 302 into the partition. Either way the password rides in the request body,
never in the URL.

## The nav dock

Every browsable page carries one collapsed tab (`#hk-dock`) that names the
partitions, so a page under `/l/family/grabby/` can still get you home or to
another tenant without any app knowing the hub exists. `hub/dock.css` +
`hub/dock.js` are served ungated at the portal root (like `hub.css`) and are
referenced root-absolute; their content comes from one JSON payload,
`window.__HUB__`, which `gen-conf.py` either **writes** into a page it owns (the
picker, the SPA shells) or hands to nginx as a `sub_filter` that appends the
three tags after the upstream's `</head>` (the radio player, GrabbyTube's index,
the noVNC door).

Two details are load-bearing, and both are asserted against a live nginx in
`platform/tests/test_gen_conf.py`:

- `proxy_set_header Accept-Encoding ""` on the injected page blocks:
  `sub_filter` refuses a compressed body, and every app here gzips its own HTML.
  gzip stays ON so `/radio/api/status.json` and noVNC's `core/rfb.js` are not
  shipped raw; the filter only needs the body uncompressed on the way in.
- the payload's `user` is nobody until the page's own gate answers, so the
  file-written form carries `__HUB_USER__` (replaced by the picker's own
  `sub_filter`, which is why that location needs `gzip off`) and the injected
  form carries `$gate_user`, filled by the same `auth_request_set` that names
  `X-User` for the APIs. The picker is gated SOFTLY through the internal
  `/_whoami` (hubauth's slug-free identity: 204 for an anonymous browser, so the
  partition list is never walled) and `dock.js` offers Sign in with `?next=` of
  the current page only when that page sits inside a mount of the partition the
  payload is about — the only `next` values hubauth honours.

## Operator CLI

`hub` is the only writer of the registry and the identity DB from the shell. It
runs where the data is:

```bash
docker compose exec listen hub status                       # programs + sessions
docker compose exec listen hub user add bob                 # prompts for password
docker compose exec listen hub partition add family --mode shared --owner owner \
                                           --vault owner --no-radio --quota 1
docker compose exec listen hub members add family kid
docker compose exec listen hub code family                  # one-time pairing code
docker compose exec listen hub vault restart family         # force a sign-in now
docker compose exec listen hub vault logs family --lines 200
docker compose exec listen hub partition default owner
docker compose exec listen hub reload    # regenerate + apply (nginx, supervisor)
```

`hub vault <verb> <slug>` addresses the **account** that slug signs in as, so on
a shared vault it starts/stops/logs the host's one family — and prints which host
it touched plus every partition that shares it. `restart family` when `family`
signs in as `owner` restarts `vault-owner`. `hub partition list` labels each line
`(browser)` or `(via <host>)` for the same reason, and `partition rm`/`set
--vault` say where a vault's browser moved when its host goes away.

`hub reload` is the only verb that applies anything: it re-runs the generator,
validates the result with `nginx -t`, reloads nginx only if that passed, and
then does supervisor's `reread`/`update`. Everything else edits
`partitions.json` and leaves the apply to the watchdog (~10 s). Skipping the
nginx half is what made a new partition come up with live backends and a 404 at
its mount: the gateway keeps serving the location set it was last reloaded with.

Devices pair at `/pair`: mint a code, type it on the device, it gets a session
cookie bound to that partition. Users are global, sessions are per partition; a
user in two partitions needs two sessions, and one session cannot read the
other partition. Long-lived headless callers (scripts, other containers) use
`hub user key <name>` and send it as `Authorization: Bearer`.

## Configure

`cp .env.example .env`, then fill it: WG keys, `SITE_DOMAIN`,
`HUB_OWNER_USER`/`HUB_OWNER_PASSWORD` (first start only — the DB owns the
password after that), `HUB_RADIO_PASS`, the vault account, the tuner's NNTP /
NZB / AcoustID / LLM credentials and `GRABBY_API_TOKEN`.

`WG_*` unset means **no tunnel**: everything still works on the host and the
LAN, it is just not reachable from the VPS. `HUB_OWNER_PASSWORD` unset means the
bootstrap prints nothing and you set the owner password with
`hub user passwd owner` (useful if you would rather not put it in a file).

Two WG details that have bitten this stack before, both handled:
`WG_PEER_ALLOWEDIPS` must not swallow the docker bridge or the LAN (the
entrypoint re-routes every locally-connected subnet back onto `eth0` in the
pinned table), and the container's derived pubkey must match the VPS peer block
or the handshake never completes (`docker compose exec listen wg show`).

## Run

```bash
docker compose up -d --build            # first build is long: Angular + prisma
docker compose logs -f listen           # gen-conf, migrate report, supervisord
docker compose exec listen supervisorctl status
curl -s localhost:8200/health
docker compose exec listen nginx -t
```

The first start of an existing box also **imports the pre-hub state**: media is
hardlinked (zero copy, `stat -c %h` = 2 is the proof), databases are snapshotted
through the sqlite backup API (never linked — WAL), and the source trees are
left alone. See `platform/migrate.py`; the state directory's `.migrated` marker
makes it one-shot — delete it to re-run (idempotent, and it never deletes from a
source).

## Gotchas

- **A program is a `[program:]` in `/etc/supervisor/conf.d/stack-programs.conf`,
  generated.** Hand-edits to generated files are overwritten within 10 s; edit
  `partitions.json` (or the generator), then `hub reload` to apply it.
- **Logs**: `docker compose logs` shows supervisord and whatever wrote to PID 1;
  each program's own log is a file — `docker compose exec listen tail -100
  /var/log/sup/grabbytube-owner.err.log`, or `hub vault logs <slug>`.
- **`docker exec` with a stray env**: a shell inside the container does not
  inherit a tenant's `environment=` block, so run app code through the
  supervised process or via its HTTP API, not `docker exec … python app.py`.
  The platform side (gen-conf, `hub`, hubauth, watchdog, seed-registry) resolves
  the state directory from container-level `HUB_DATA_DIR` for exactly that
  reason: gen-conf used to hardcode `/data/partitions.json`, so a hand-run
  generator with the container's env read a registry that does not exist and
  rewrote every generated file to "no partitions" — the live gateway 404'd every
  partition while `partitions.json` sat untouched.
- **Chromium is started on demand** (`autostart=false`), and **once per vault,
  not per partition**: a healthy box runs zero chromium, and two tenants sharing
  a Google account share one browser family (`vault-<vault>`, the host partition,
  see the registry section). If a vault seems dead it is usually just idle; `hub
  vault status <slug>` says why from the HOST's files (a member is never asked
  for a browser of its own, so a member with no files of its own still counts as
  live), and the watchdog starts the family when the jar goes stale
  (`HUB_VAULT_MAX_AGE`).
- **The tuner is shared.** Two partitions asking for the same verified rip get
  one download and one blob (`<state>/pool/blobs/<sha256>.mp3`) delivered as a
  hardlink into each cache; a file any partition then rewrites detaches its own
  copy first (`VENDORED.md`).
- **Hardlinks are same-MOUNT, not same-filesystem.** Measured on this box: two
  bind mounts of one ext4 (identical `st_dev`) → `EXDEV`, a named volume beside a
  bind → `EXDEV`, one bind covering both names → `nlink=2`. That is why the state
  directory and the legacy trees arrive through the single `/mnt/stack` mount and
  why the import paths are `environment:` entries and not `volumes:` entries: a
  "tidier" `../DrivePod:/srv/legacy/drivepod:ro` costs a full copy of the media,
  silently, because migrate.py falls back to copying rather than failing.
  Verify any new mount shape with `stat -c %h` on an imported file.
- **One yt-dlp bucket for the box** (`MAX_CONCURRENT_YTDLP`): the harvesters and
  the tuner go through `platform/ytdlgated`, which queues on a file lock,
  because they all hammer the same Google session.
- **Published ports are the contract.** `8090` and `8091` stay unauthenticated
  on purpose (that is what a broadcast stream is) and `frogrock.example.com` must
  keep pointing at `8090` or the Shoutcast listing and DNAS registration break.
- **The old hostnames are portals now.** `drivepod./grabbytube./tunester./
  ytcookies.example.com` answer with a path-preserving `301` to the matching
  portal path (`/l/owner`, `/l/owner/grabby`, `/`, `/l/owner/vnc`); their
  certificates and ACME webroots stay on the VPS, so certbot keeps renewing
  them. `ytcookies` deliberately drops its `auth_basic` door — the portal
  session plus the VNC password are the gate, and the scanner rate limit
  survives. DNAS relays from `streamrelayurl_1=http://<peer>:8091/stream.mp3`
  (the hub's shim), so `FrogRock/dnas/sc_serv2.conf` names the peer IP rather
  than a compose service name: DNAS runs in the OLD FrogRock group and reaches
  the hub through WireGuard.

## Tests

```bash
docker run --rm -v $PWD:/src -w /src -e LISTEN_TEST_NGINX=1 \
  --entrypoint sh listen-hub:latest \
  -c "python3 -m pytest platform/tests -q"
```

`LISTEN_TEST_NGINX=1` is what enables the gateway checks: they need a real
`nginx -t` plus the stack snippets at `/etc/nginx/snippets`, i.e. the image.
Without it those tests skip and the rest still runs, so a bare run on the host
(`python3 -m pytest platform/tests -q`, needs pytest) covers everything except
the generated nginx config.
