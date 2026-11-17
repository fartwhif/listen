# Vendored trees

`stack/` and `vendor/` hold the six services' source, copied in from the
standalone repos at the SHAs below. The hub builds ONE image from them, so these
trees are what runs. Upstream stays the place fixes land: edit the source repo,
then re-copy the tracked files here and re-apply the patch table below.

**Every patch is env-gated.** Each one is either a default-preserving
`os.environ.get(...)`/`process.env.X || <today's value>` or a behaviour keyed on
something only the hub sets (`HUB_SLUG`, `HUB_URL_PREFIX`,
`X-Partition-Slug`, `TUNESTER_COOKIES`, `TUNESTER_BLOBS`). With the new
variables unset a tree behaves exactly as its standalone container does today —
that is the invariant to preserve when re-vendoring, and the thing to test.

## Pin

| Tree | Upstream | SHA | Patched files |
|---|---|---|---|
| `stack/drivepod/` | `fartwhif/DrivePod` | `aa6554771af640f9aade2db9ce4f028700b3def6` (2026-11-17) | 2 |
| `stack/grabbytube/` | `fartwhif/GrabbyTube` | `6d636e06736304a452ca901451849e2618fb1b9f` (2026-11-15) | 8 |
| `stack/tunester/` | `fartwhif/tunester` | `92bcc1f41c559f3498c30a51cd36013d9b5902e0` | 3 |
| `stack/frogrock/` | `fartwhif/FrogRock` | `7585c5479ad23d234065c379761a78de2c266591` | 2 |
| `stack/vault/` | `fartwhif/ytcookies` | `8c3d0903b58f1576e349ff2b6c8d97d688a61851` | 1 |
| `stack/streamfix/` | `FrogRock/dnas/stream-fix` (same SHA) | `7585c5479ad23d234065c379761a78de2c266591` | 0 |
| `vendor/bgutil/server/` | `Brainicism/bgutil-ytdlp-pot-provider` tag `2.0.2` | tag 2.0.2 | 0 |

## What is NOT vendored

Each upstream's own deployment scaffolding — `Dockerfile`,
`docker-compose.yml`, `entrypoint.sh`, `wireguard/`, `.env`, `.env.example`,
`data/` — plus DrivePod's `screenshots/`, FrogRock's `dnas/` (except
`dnas/stream-fix/`, which becomes `stack/streamfix/`) and its `frogrock.log`.
The hub replaces all of it: one Dockerfile, one compose file, one entrypoint,
one WireGuard tunnel, one `.env`.

`stack/grabbytube/grabbytube/`, `grabbytube.db`, `grabby-tracks.json` and
`stack/tunester/downloads/` are runtime state the standalone containers wrote
beside their code. `.dockerignore` keeps them out of the image; `migrate.py`
moves the real state to `/data`, where the hub keeps it.

---

## `stack/drivepod/` — DrivePod @ aa65547

> **2026-11-17 re-vendor (23b03a9 → aa65547).** Two upstream commits.
> `d3f39d5` is the owner's three reported symptoms: `folderCorruptionReason()`
> judges a cache folder by EXACT file names (FrogRock's `<id>.mp3.tr64k.mp3`
> radio transcode and GrabbyTube's `<id>.mp3.bak` are no longer "Multiple .mp3
> files" that get a healthy grabby row — and its watched/progress state —
> deleted), `DELETE /api/video/:id/grabby` decides library-track-ness from the
> sidecar `createdAt` marker instead of the DB row (a row already gone is a
> landing consume, not a 404 the Delete key alerts on), and
> `frontend/src/app/autoplay-order.ts`'s pure `pickNextTrack(queue, mode,
> finished?)` is now the one pick behind EVERY advance — page-load autoplay,
> defunct skip, unplayable give-up, post-refresh fallback, the manual ⏭️
> (`manualNextMode`: `off` → `older`) and `consumedGrabbyTrack()` — with the ⏮️
> button and the `previoustrack` handler removed. `aa65547` is the matching
> `AGENTS.md`/`ARCHITECTURE.md`. Upstream's new lines and the hub patches again
> touched **disjoint** regions, so this re-copy is a clean 3-way merge
> (`git merge-file`, 0 conflicts): the patch set below is byte-unchanged and no
> env gate was introduced or changed. The frontend gains a file
> (`autoplay-order.ts`, imported by `app.component.ts`, never touches a URL so
> there is no mount concern) and T6 (`tests/autoplay-order.test.mjs`), both
> carried byte-identical; `tests/run-scratch.sh` gained
> `--add-host grabbytube:127.0.0.1` because T2 §18 drives the consume route
> against a stubbed grabbytube on that hostname. Deployed bundle:
> `main-J5K7G6VO.js` (`APP_VERSION 1.14.0`).

> **2026-11-16 re-vendor (1db5b42 → 23b03a9).** Upstream changed the grabby
> shufflebag (played tracks now leave the bag: `initShuffleBag` set-asides
> watched rows, `reconcileShuffleBag` gained a third `pruneIds` argument,
> `distributeGrabbyItems` skips played pulls, and the watched/unwatched routes
> call a new `syncBagForWatched`) and the frontend's `newest` autoplay (a new
> `_playFreshest` that re-fetches `index.json` before picking — it reuses the
> existing `fetchIndexWithProgress()` helper, so it inherits this tree's
> `${MOUNT}/cache/…` patch for free). Upstream's new lines and the eleven hub
> patches touched **disjoint** regions of both patched files, so that re-copy
> was a clean 3-way merge too.

`backend/src/server.ts` (11 hunks)

| Anchor | Patch | Gate |
|---|---|---|
| `const port` / `app.listen` | `PORT` and `HOST` are read from the environment (`3000`/`0.0.0.0` defaults); the hub supervises one backend per partition on its own loopback port | unset ⇒ today |
| top of `app` setup | `URL_PREFIX = HUB_URL_PREFIX` registers ONE middleware that peels the mount off `req.url` (routes stay declared bare; `req.originalUrl` keeps what the client asked for). `MEDIA_PREFIX` = `/cache` or `<prefix>/cache`. `GRABBY_API_URL` replaces the hardcoded `http://grabbytube:8101` | `HUB_URL_PREFIX` unset ⇒ no middleware, plain `/cache` |
| `updateIndexFile()` | returns the `ProgressEntry[]` it just wrote (`null` on failure) so a caller can mirror it; `audioPath`/`thumbnailPath` in **index.json only** get `MEDIA_PREFIX` (DB rows and per-item JSON keep bare filesystem paths, and the guard accepts both forms so nothing is mounted twice). Upstream's `revertForeignGrabbyRows()` also calls it — the revert needs no change, and its rewrite of index.json comes out prefixed exactly like every other writer's | `MEDIA_PREFIX` = `/cache` ⇒ byte-identical output |
| `MEMBER_USER_RE` + `writeMemberProgress()` | mirrors the progress list into `progress-<X-User>.json`, the file the gateway's `cache/progress.json` alias serves, so two members of one partition (or one user's phone and car) never overwrite each other's resume point. Name must match `hubdb`'s shape or the mirror is skipped | no `X-User` header ⇒ no extra file |
| `POST …/watched` | passes the freshly derived list to `writeMemberProgress(req.headers['x-user'], …)`; a failed regeneration leaves both files alone | |
| remote delete (`resolveGrabbyAudioPath` owner) | `DELETE` goes to `GRABBY_API_URL` and carries `X-Partition-Slug` (only when `HUB_SLUG` is set — an empty header value is a different request than none) plus `X-User` when the gate named one | unset ⇒ today's URL, today's headers |
| harvest `cmd` | yt-dlp executable from `DRIVEPOD_YTDLP`; PO-token sidecar from `DRIVEPOD_BGUTIL_URL` | both default to today's values |
| `writeTempCookiesFile()` / `deleteTempCookiesFile()` | `COOKIES_FILE` set ⇒ this partition's vault OWNS that path, so a harvest neither unlinks it at start/finish (that threw away the vault's export in the first round and left every later yt-dlp run cookieless, which is what the bot check then punished) nor overwrites it from an uploaded cookie file | `COOKIES_FILE` unset ⇒ both helpers byte-identical |

Carried along byte-identical from upstream (no listen patch):
`frontend/src/app/autoplay-order.ts` (the pure advance pick, imported by
`app.component.ts`), `frontend/src/app/stall-guard.ts` (exports `streamTrackId`),
`tests/autoplay-order.test.mjs` (T6), `tests/stall-guard.test.mjs` (T4, 53
assertions), `tests/pure-logic.test.mjs` (T2 §16-§18: the authorship marker, the
`folderCorruptionReason` verdict, the consume route), `tests/content-audit.mjs`
(A14/A14b key orphan and grabby-label checks on the `createdAt` marker),
`AGENTS.md`, `ARCHITECTURE.md`.

**Upstream 1db5b42 is the authorship-marker fix, and it changes what the hub's
`/data` looks like on first start.** Ownership of a `cache/<id>/` folder is now
GrabbyTube's sidecar `createdAt` (`isGrabbyAuthored` /
`sidecarIsGrabbyAuthored`), never "a sidecar has a `videoId`" (DrivePod's own
writers emit that key too) and never "the index lists it". Consequences:
`loadGrabbyIndex()` drops unmarked entries and logs the count; the cleanup
orphan/corrupt rules protect a folder only when the marker agrees with the DB
label; `initShuffleBag` (now exported) seeds from the filtered index;
`distributeGrabbyItems` refuses to convert an `isGrabby:false` row twice (the
planner spends no slot on it, and a row inserted between filter and placement
is refused again); and `revertForeignGrabbyRows()` runs FIRST at startup —
before the bag seed, the artist backfill and the first harvest — restoring
`publishedAt`/`channelId` from the sidecar and recreating a deleted channel
INACTIVE. For a migrated hub that means: rows the old bare sweep flipped to
`isGrabby` come back to the normal queue on the first start (watched,
progress, ignored and protected untouched; no sidecar ⇒ stays grabby), while
`grabby-index.json` itself keeps its foreign entries until some GrabbyTube
event rebuilds it (download, delete, meta edit or a tunester run) — harmless,
because every DrivePod reader filters on the marker, and the entry simply
stops being offered to the bag until then. Nothing outside this tree has to
change for that.

`frontend/src/app/app.component.ts` (9 hunks)

`mountBase(baseURI)` derives the mount from `<base href>` (empty at `/`), so ONE
hashed bundle serves the portal root and every `/l/<slug>/` shell. `apiUrl` and
the `/api/stream/…` sites (play, HEAD probes, retries) are built from it.
`index.json`/`progress.json` are fetched as `${MOUNT}/cache/…`. No build-time
prefix: the gateway writes the per-partition shell (`platform/gen-conf.py`).

1db5b42 moved the "is this the same track?" decision into
`stall-guard.streamTrackId(audio.src)` (`/\/api\/stream\/([A-Za-z0-9_-]+)/`) —
the one place a mount prefix could have broken track identity, and it does not:
the src this file assigns is `${MOUNT}/api/stream/<id>`, and the regex reads the
`/api/stream/<id>` tail, so it matches at the portal root (`/api/stream/x`) and
under a mount (`/l/family/api/stream/x`) alike. That holds only while the
gateway leaves the tail intact, which it does — `location /l/<slug>/api/`
proxies with no rewrite (only the tuner's `/l/<slug>/api/` rewrites, and that is
tunester, never the stream) and the front partition's bare aliases forward
`/api/stream/…` unchanged. T4's `streamTrackId` cases prove both shapes
(`node tests/stall-guard.test.mjs`, now 53 assertions). mediaSession artwork
still uses `video.thumbnailPath` exactly as index.json carries it — prefixed by
the backend, so nothing here needed a change.

## `stack/grabbytube/` — GrabbyTube @ 6d636e0

| File | Patch | Gate |
|---|---|---|
| `config.py` | new settings, each defaulting to today's value: `ytdlp`, `bgutil_url`, `cookies_file`, `db_path`, `catalog_path`, `partition_slug`. `model_post_init` prefers unprefixed `HUB_SLUG` for `partition_slug` and picks up `COOKIES_FILE` when `GRABBY_COOKIES_FILE` is unset (pydantic's `env_prefix` cannot map names it does not own) | unset ⇒ today |
| `db.py` | `DB_PATH`/`CATALOG_PATH` from those settings. Required under the hub: one vendored directory hosts every tenant's process, so the defaults would put all partitions in one sqlite file and one catalog. `get_pending_jobs` → `get_claimable_jobs`, plus `claim_job` (`UPDATE … WHERE id=? AND status='queued'`, won = `rowcount==1`) and `reclaim_stale_jobs`: the claim is what lets the worker keep N harvests in flight without double-taking one job; the reclaim re-queues rows a crashed process left 'running'/'downloading'/'transcoding', safe because it runs once at worker start when nothing of ours can be in flight | empty ⇒ today's files beside the code |
| `main.py` | startup restores `yt_dlp_path`/`bgutil_url` from the persisted settings store **only when** `GRABBY_YTDLP`/`GRABBY_BGUTIL_URL` are absent (the hub's env outranks what an earlier operator typed into the UI). Comment records that mount stripping is the gateway's job | |
| `api/routes.py` | `GET/POST /settings/yt-dlp` read/write `settings.ytdlp` and `settings.bgutil_url` instead of hardcoded text, persisted through the existing `set_config` store | |
| `processor.py` | `_COOKIES_FILE` is the env-named partition file (upload/delete write a real partition path); `effective_cookies_file()` offers `[settings.cookies_file] + (shared, local)` with the same exists-and-non-empty skip, so a fresh partition with no export still passes no `--cookies`; bgutil `base_url` and both `["yt-dlp"]` argv heads come from settings; **`_write_mp3_tags` copy-on-write** — when a partition slug is set and the file has `st_nlink > 1`, copy + `os.replace` gives this copy a private inode before mutagen saves, because mutagen writes through the fd and retitling a blob link would rewrite the shared `/data/pool/blobs/<sha256>.mp3` and leave its hash-name describing the wrong bytes | slug unset ⇒ no `stat()`, in-place save as today |
| `processor.py` (worker) | real concurrency cap: upstream `await`ed one job before taking the next, so `settings.max_concurrent` could never exceed 1 and `quota.concurrentDownloads` was decorative. The hub loop claims jobs into `asyncio.create_task` up to `max_concurrent`, reaps with `asyncio.wait(FIRST_COMPLETED)`, and calls `reclaim_stale_jobs()` at start. Proven per partition: the quota-1 test partition's second job stayed `queued` until the first harvest ended, while a quota-2 partition ran two 6 s harvests in exact overlap | standalone: `max_concurrent`'s default 2 now genuinely means 2 |
| `processor.py` (index) | `audioPath`/`thumbnailPath` stay **relative and unprefixed**: DrivePod resolves them against its own `CACHE_DIR` (`resolveGrabbyAudioPath`), and this app's UI sits under its own mount where a relative path already lands right. 6d636e0 rewrote this same file's `update_index_file()` so the index lists **only what this app authored** — an item JSON with a non-empty `createdAt` AND a catalog record (via `load_catalog()`, imported here already) — counting foreign folders and WARNing on `createdAt`-without-catalog, so an index entry is now an ownership statement, which is what DrivePod's `isGrabbyAuthored` filter reads. Output dir (`GRABBY_OUTPUT_DIR`) and catalog (`GRABBY_CATALOG_PATH`) still come from `config.py` settings, so a sweep sees only this tenant's cache | — |
| `tunester.py` | `_slug_header()` sends `X-Partition-Slug` on `POST /api/tune`, `GET /api/job/<id>`, `GET /api/dl/<jid>`, `POST /api/clear`; `_hardlink_delivery()` uses `os.link` + `os.replace` when the download response carries `X-Pool-Path` (see `platform/poolimport.py`), else today's streamed write | header absent ⇒ no header; no `X-Pool-Path` ⇒ streamed bytes |
| `static/index.html`, `static/js/app.js` | `API` derived from `document.baseURI` (empty standalone, `/l/<slug>/grabby` under the gateway); shell refs made relative | |

## `stack/tunester/` — tunester @ 92bcc1f

| File | Patch | Gate |
|---|---|---|
| `pipeline.py` | `cookie_candidates()`: when `TUNESTER_COOKIES` lists every partition's jar, offer only readable non-empty files sorted **newest-mtime-first** — the vault exports while its session is live, so the newest jar is the one a browser refreshed last, and a dead jar on YouTube looks like "all sources exhausted". `find_cookies()` keeps an explicit `--cookies` outranking them. The download passes the chosen jar to `browser_user_agent(cookies)` | `TUNESTER_COOKIES` unset ⇒ `COOKIES_CANDIDATES`, unchanged |
| `sources.py` | `browser_user_agent(cookies)` reads the state file **next to the jar it is describing** (`session-state.json` is written beside each export): the hub's newest-jar-wins choice otherwise pairs one partition's session with another's UA, and a mismatched UA on a live session is a kill signal. Lookup keyed on `TUNESTER_COOKIES` being set; the search call site passes the jar too | unset ⇒ today's single state file |
| `web.py` | single-flight: `_PENDING` keyed on `sources._norm(name)` (the shape the delivery cache hashes), consulted only for requests carrying `X-Partition-Slug`; a request for a song already being found ATTACHES (`attached: true`, waiter recorded) instead of fetching again, and the key is released in a `finally` with an identity check so a later request keeps its own claim. Blob serving: on success `platform/poolimport.blob_for()` puts the rip in the content-addressed store, `/api/dl` serves the BLOB and names it in `X-Pool-Path`; a blob-store failure is logged in the job and the output-dir copy is served, so it never looks like a failed tune. `_job_view`/`_job_public` add `slug`/`waiters` only for hub jobs. `_tune_one()` converts the pipeline's own `sys.exit()` into a `RuntimeError` so the worker's `except SystemExit` arm stops swallowing blob failures and dropping the `target` fixup. `_load_poolimport()` resolves `platform/` via `STACK_DIR` or one/two parents up, `None` standalone | header unset ⇒ today's responses key for key; `TUNESTER_BLOBS` unset ⇒ job keeps its own file |

## `stack/frogrock/` — FrogRock @ 7585c54

| File | Patch | Gate |
|---|---|---|
| `server.ts` | `HOST` env (default `0.0.0.0`, i.e. today) and `server.listen(PORT, HOST)`: the hub publishes only the front broadcaster, so the others bind loopback. `normalizePath()` also strips a gateway mount (`/l/<slug>/cache/`) because DrivePod writes index.json **gateway-prefixed** and FrogRock reads that same file off disk — it owns the inverse of the prefixing, and the mount is matched generically because a broadcaster is never told its own slug and the index can name any partition | `HOST` unset ⇒ today |
| `public/player.html` | `API_BASE = new URL('.', document.baseURI).pathname` (`''` at `/`, the mount otherwise) applied to the status/playlist/shoutcast calls, `/api/thumb`, the stream URL and mediaSession artwork; the Shoutcast icon ref made relative. Works under `/`, `/radio/` and `/l/<slug>/radio/` with one file | served at `/` ⇒ `API_BASE` empty ⇒ today's URLs |

## `stack/vault/` — ytcookies @ 8c3d090

`cookie_exporter.py` (4 hunks)

`DRIVEPOD_PUSH_URLS` / `GRABBY_PUSH_URLS` accept comma lists (one vault serves
every partition that shares it, so the UA push has to reach all of their apps),
`_push_ua()` pushes to each and reports `ok` if any landed — which is what stops
`main()`'s retry loop from pushing again — with the pre-hub one-target wording
kept verbatim for a single URL. `sync_user_agent()` adds `X-Partition-Slug` so a
shared vault cannot let one partition's exporter speak for another.

Nothing else needed patching: the exporter already reads its paths and endpoints
from the environment (`COOKIES_FILE`, `COOKIE_SESSION_STATE`, `CDP_HOST`/`CDP_PORT`,
`VNC_DRIVER_URL`, `VNC_URL`), which is how `tenant_env`/`vault_env` aim it at a
partition home and loopback ports. `auto_signin.py` and
`vncdriver/vncdriver.py` are byte-identical to upstream; the browser family
(Xvfb/x11vnc/websockify/chromium) is launched by `platform/vault-run.py`
instead of the old `entrypoint.sh`.

## `stack/streamfix/` and `vendor/bgutil/` — unmodified

`fixer.py` + `Dockerfile` are byte-identical to `FrogRock/dnas/stream-fix` (the
header-normalizing shim DNAS relays through). Measured on the wire: through the
shim the broadcaster answers `HTTP/1.1 200` with **no** `Transfer-Encoding:
chunked` — the HTTP/1.0 downgrade this vintage ICY client needs, since a direct
`:8090` request gets chunked framing that DNAS 2.6.1 cannot parse — and the
relay logs `stream detected MPEG v2 layer 3 mono` and keeps issuing `Title
update` lines across track transitions. Only the `Dockerfile` is inert here —
`gen-conf.py` supervises `python3 stack/streamfix/fixer.py` directly, so the
shim runs in the hub and DNAS reaches it as `http://<peer>:8091/stream.mp3`
over WireGuard.

`vendor/bgutil/server/` is the upstream `2.0.2` server subtree as-is; the pip
plugin `bgutil-ytdlp-pot-provider==2.0.2` is the matching version, and the
Dockerfile follows upstream's own build recipe (`npm ci` + `tsc` →
`build/main.js`).

---

## Re-vendor checklist

```bash
# 1. parity check: only the files in the table above may differ
cd ~/projects/listen/stack
for pair in drivepod:DrivePod grabbytube:GrabbyTube tunester:tunester \
            frogrock:FrogRock vault:ytcookies; do
  v=${pair%%:*}; u=${pair##*:}
  diff -rq --exclude=.git --exclude=node_modules --exclude=dist \
           --exclude=__pycache__ --exclude=.venv --exclude=.angular \
           --exclude=data --exclude=cache --exclude=downloads --exclude=dnas \
           --exclude='*.db' --exclude='*.log' --exclude='*.json' \
           --exclude=.env --exclude=.env.example --exclude=Dockerfile \
           --exclude=docker-compose.yml --exclude=entrypoint.sh \
           --exclude=wireguard --exclude=screenshots --exclude=.vscode \
           --exclude=.hermes ~/projects/$u/ $v/
done
diff -rq --exclude=__pycache__ ~/projects/FrogRock/dnas/stream-fix streamfix
```

2. Re-copy the tracked files, re-apply every hunk in the table, and confirm each
   new behaviour is still gated: unset the hub variables and diff the tree
   against upstream again — only commented/no-op hunks may remain.
3. `python3 -m py_compile` every changed `.py` **inside `python:3.11`** (the
   image's interpreter; the host runs 3.14) and `node --check` changed `.js`;
   `npx tsc` the TS trees via the image build.
4. Record the new SHA in the table above in the same commit.
