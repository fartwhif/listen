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
<state>/vaults/<vault>/profiles/<slug>/          chromium profile (per partition)
<state>/users/<slug>/cache/          media (DrivePod library, GrabbyTube harvests)
<state>/users/<slug>/cookies.txt     that partition's vault export
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

`"vault": "owner"` means *this partition shares owner's Google account and
cookie jar*; each partition still gets its **own chromium profile** (two
chromiums on one profile dir fight over the `SingletonLock`) and its own
`cookies.txt` export. `mode: shared` allows several members; `single` pins the
partition to one user. `quota.concurrentDownloads` caps that partition's
concurrent tuner jobs, so one member's queue cannot starve another's.

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
docker compose exec listen hub reload                       # force re-read
```

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
  `partitions.json` (or the generator) instead.
- **Logs**: `docker compose logs` shows supervisord and whatever wrote to PID 1;
  each program's own log is a file — `docker compose exec listen tail -100
  /var/log/sup/grabbytube-owner.err.log`, or `hub vault logs <slug>`.
- **`docker exec` with a stray env**: a shell inside the container does not
  inherit a tenant's `environment=` block, so run app code through the
  supervised process or via its HTTP API, not `docker exec … python app.py`.
- **Chromium is started on demand** (`autostart=false`): a healthy partition
  runs zero chromium. If the vault seems dead, it usually means it is idle; `hub
  vault status <slug>` says why, and the watchdog starts it when
  `cookies.txt` goes stale (`HUB_VAULT_MAX_AGE`).
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
docker run --rm -v $PWD:/src -w /src --entrypoint sh listen-hub:latest \
  -c "python3 -m pytest platform/tests -q"
```
