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
| `stack/drivepod/` | `fartwhif/DrivePod` | `00f68c4d201b1af612fd30b113b457b8fb605233` (2026-11-17) | 2 |
| `stack/grabbytube/` | `fartwhif/GrabbyTube` | `02f75aa31167a34430af3feee17a621476be48b2` (2026-11-18) | 8 |
| `stack/tunester/` | `fartwhif/tunester` | `a310334e6ddcdde49bba71bc30555b6903462958` (2026-11-18) | 3 |
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

## `stack/drivepod/` — DrivePod @ 00f68c4

> **2026-11-17 re-vendor (ca04586 → 00f68c4).** One upstream commit, the follow-up
> round on the owner's report: `onerror` decided a media error's owner from
> `audio.src`, which a new assignment had already retargeted, so an aborted load
> charged the healthy track a press had just selected — two such charges hit
> `MAX_TRACK_PLAY_FAILURES` and marked watched a file nobody had heard. Upstream
> added `mediaEventTrackId` / `mediaErrorIsTrackDefect` /
> `playRejectIsTrackDefect` to `stall-guard.ts` (byte-identical copy here) and
> routed `onerror`, `onwaiting`, `onstalled` and `saveProgress` through that
> ownership gate. The mount patch set is again **byte-unchanged** (9 hunks, same
> 24 `>` and 9 `<` lines): the re-copy is a `git merge-file` with exactly ONE
> conflict, in the play-reject final-probe block, where upstream added two lines
> directly above the sixth `${this.apiUrl}/stream/…` HEAD probe — resolved by
> keeping upstream's `playRejectIsTrackDefect(er?.name)` guard and this tree's
> `apiUrl` URL. `app.component.html`, `autoplay-order.ts` and both new test files
> are unaffected; `AGENTS.md` / `ARCHITECTURE.md` are byte-copies of upstream.
> Only the hunk offsets move (new-side starts 96→99, 109→124, 571→589,
> 1182→1230, 1514→1616, 1543→1645, 1578→1689, 1587→1699, 1632→1758) and no env
> gate was added. Deployed bundle: `main-FET5B5YG.js` (`APP_VERSION 1.16.0`,
> built by the `frontend-builder` stage from THIS tree; the bare upstream tree
> builds `main-BYLUDOPH.js`).
> The pin moved with a same-day DrivePod history rewrite: a real deployment
> hostname had been added as a fixture literal in
> `tests/stall-guard.test.mjs` (it survived the 2026-11-16 sweep because that
> file was new that day), and the rewrite replaced it with `example.com` per the
> placeholder convention. The pre-scrub tip `b896cc6` is dead upstream, `00f68c4`
> is its rewrite. Patch content and both built bundles are unchanged by it — the
> rewrite touched only that one test-file blob, which no Dockerfile `COPY`
> reaches.

> **2026-11-17 re-vendor (bf1bf3a → ca04586).** One upstream commit: the owner
> asked for the ⏮️ button back. `skipPrevious()` (button + `previoustrack`
> handler) is restored as `stopAndAdvance(left, manualPrevMode(autoplayMode()))`
> — a new pure `manualPrevMode` in `autoplay-order.ts` flips the chosen mode to
> its opposite direction ('newer'/'newest' → 'older', 'older'/'oldest'/'off' →
> 'newer'), always relative, so a Next then Previous returns the origin track —
> and both halves are gated on `skipMarksWatched`: with that setting on, ⏭️
> MARKS the left track finished (out of the queue and the bag), so a back step has
> nothing to undo, hence the button is not rendered and the handler no-ops (it
> stays registered, or the OS key becomes a rewind). `app.component.html` gains
> the `@if (!skipMarksWatched())` around the button and `app.component.ts` the
> import, the `APP_VERSION 1.15.0` bump and the method; T6 grew 43 → 49
> assertions. My edit touched only regions the mount patches do not (the import
> line 15 and the handler block beside `skipNext`), so the re-copy is again a
> clean `git merge-file`, 0 conflicts, and the patch set below is **byte-unchanged
> in content** — only the `diff` line offsets of the last five `app.component.ts`
> hunks move (+54: 1514→1568, 1543→1597, 1578→1632, 1587→1641, 1632→1686), and no
> env gate was added or changed.
> `autoplay-order.ts`, `app.component.html`, `tests/autoplay-order.test.mjs`,
> `AGENTS.md` and `ARCHITECTURE.md` stay byte-identical copies of upstream.
> Deployed bundle: `main-CZLG6CYG.js` (`APP_VERSION 1.15.0`, one `skipPrevious`
> reference), built by the `frontend-builder` stage from THIS tree — the vendored
> `app.component.ts` carries the mount patches, so its hash differs from a build
> of the bare upstream tree (`main-72LZIXZ7.js`).

> **2026-11-17 re-vendor (0642f8a → bf1bf3a).** Three upstream commits, on
> history that was rewritten the same day (the hub-hostname scrub), so every
> DrivePod SHA quoted here is post-rewrite — the `23b03a9` / `1db5b42` /
> `1deb2bb` names this doc used before are gone upstream. `c610d0d` is the
> owner's three reported symptoms: `folderCorruptionReason()`
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
> button and the `previoustrack` handler removed. `e1c7c6a` + `bf1bf3a` are the
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

> **2026-11-16 re-vendor (c3a2b15 → 0642f8a).** Upstream changed the grabby
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
`tests/autoplay-order.test.mjs` (T6), `tests/stall-guard.test.mjs` (T4, 64
assertions), `tests/pure-logic.test.mjs` (T2 §16-§18: the authorship marker, the
`folderCorruptionReason` verdict, the consume route), `tests/content-audit.mjs`
(A14/A14b key orphan and grabby-label checks on the `createdAt` marker),
`AGENTS.md`, `ARCHITECTURE.md`.

**Upstream c3a2b15 is the authorship-marker fix, and it changes what the hub's
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

c3a2b15 moved the "is this the same track?" decision into
`stall-guard.streamTrackId(audio.src)` (`/\/api\/stream\/([A-Za-z0-9_-]+)/`) —
the one place a mount prefix could have broken track identity, and it does not:
the src this file assigns is `${MOUNT}/api/stream/<id>`, and the regex reads the
`/api/stream/<id>` tail, so it matches at the portal root (`/api/stream/x`) and
under a mount (`/l/family/api/stream/x`) alike. That holds only while the
gateway leaves the tail intact, which it does — `location /l/<slug>/api/`
proxies with no rewrite (only the tuner's `/l/<slug>/api/` rewrites, and that is
tunester, never the stream) and the front partition's bare aliases forward
`/api/stream/…` unchanged. T4's `streamTrackId` cases prove both shapes
(`node tests/stall-guard.test.mjs`, now 64 assertions). mediaSession artwork
still uses `video.thumbnailPath` exactly as index.json carries it — prefixed by
the backend, so nothing here needed a change.

## `stack/grabbytube/` — GrabbyTube @ 02f75aa

> **2026-11-18 re-vendor (bcf7832 → 02f75aa).** One upstream commit, the owner's
> "add a button to the library items that opens the original YouTube video": the
> track cards gained a `YT` link (`btn btn-secondary btn-yt-track`,
> `target="_blank" rel="noopener"`) beside Play/DL/Edit/Del, and `index.html`'s
> `?v=` cache-bust moved 24 → 25. No backend change: the catalog has keyed on the
> YouTube id since its first commit (`videoId`, and jobs store the watch URL that id
> was extracted from), so the link is built in the template from data the card
> already has. It needs **no hub patch of its own** — `youtube.com/watch?v=…` is an
> absolute cross-origin URL, so it resolves identically standalone and mounted
> (unlike the `API`/ref patches below, which exist only because those paths are
> relative to the mount). `static/index.html` conflicts on the script tag because
> both sides patch the same line (hub: relative ref; upstream: the bump), so that
> one hunk resolved by hand to relative + `?v=25`; `app.js` merged clean with
> `git merge-file` (0 conflicts — upstream's insertion is at the card action list,
> the hub patch is at the top of the file). Patch set proven **unchanged in
> content**: the vendored-vs-pin vs vendored-vs-new-pin `>`/`<` CONTENT line-sets
> for `app.js` are identical (14 lines both sides), and `index.html`'s differ only
> in the two lines upstream itself moved (the `?v=` tag and the comment that
> referenced it) — no hub gate added or removed, and the other six patched files
> stay at `bcf7832` blob-for-blob.

> **2026-11-18 re-vendor (6964c03 → bcf7832).** One upstream commit, from the
> owner's "use this for the GrabbyTube API URL and access information" instruction
> (`https://…/v1` + an `sk-…` bearer key): `find_duplicates` — GrabbyTube's ONLY
> LLM call, behind `GET /tracks/duplicates` — hardcoded `"model": "qwen3.6-27b"`,
> so re-pointing `GRABBY_LLM_BASE_URL`/`_API_KEY` at a server serving a different
> model left the request naming an id the server honoured only by ALIASING (this
> box's llama.cpp serves exactly one model and answers any id, naming the model it
> actually ran — so the feature looked fine while lying about what judged the
> library). Upstream added `settings.llm_model` (`GRABBY_LLM_MODEL`, default
> `qwen3.6-27b` ⇒ the standalone container's request body is unchanged); the hub
> ships the new trio base URL / key / model, and
> `platform/tests/test_grabby_dedup_llm.py` (8 tests, `aiohttp` replaced by a
> recorder) locks the request's URL + bearer header + model id, the
> unset-name default, the cleaned numbered prompt, the `content`-OR-
> `reasoning_content` parse fallback, and that the credential gate and a
> <2-track library send NO request at all. Byte-level cutover of the two files
> upstream changed; patch set proven **unchanged in content** — the vendored-vs-
> pin `>`/`<` line-sets for `config.py` (46 lines) and `db.py` (47) are identical
> at both pins, so no hub gate moved — and the untouched vendored files stay at
> `6964c03` blob-for-blob.

> **2026-11-17 re-vendor (f0f0aae → 6964c03).** One upstream commit, the owner's
> "the retune queue is empty when I come back" report: the tunester list lived
> only in `_progress`, so `GET /tracks/tunester` was empty after any container
> start and a mid-flight batch was never resumed. Upstream persists the batch to
> the `config` table (row `tunester_run`) on status changes and resumes it from
> the lifespan; `platform/tests/test_tunester_run_state.py` (18 tests) locks that
> behaviour against THIS tree. Upstream's additions and the hub patches touched
> **disjoint** regions (`_slug_header()`/`_hardlink_delivery()` sit outside
> `restore()`/`_persist()`/`clear_run()`, and the new `from db import …
> get_config, … set_config` line extends the import line the patch table already
> relies on), so the re-copy is a byte-level cutover of the four files upstream
> changed, not a merge: the patch set below is **unchanged in content** — the same
> 23 `>`/`<` lines in `main.py`, 79 in `tunester.py`, 14 in `app.js`, 9 in
> `index.html` — and no env gate was added or removed. Only `main.py`'s and
> `tunester.py`'s hunk offsets move (`main.py` -28→-29, -76→-83; `tunester.py`
> -31→-33, -154→-322, -163→-331, -323→-493) because upstream grew the file above
> them; `config.py`, `db.py`, `api/routes.py`, `processor.py` and every byte-equal
> file are untouched by the upstream commit (blob hashes identical at both pins).
> `6d636e0` is dead upstream — the 2026-11-17 GrabbyTube history rewrite moved the
> old pin to `f0f0aae` (tree blob-for-blob identical) to scrub one Windows
> media-mount path out of an early commit body; the pin moved with it.


| File | Patch | Gate |
|---|---|---|
| `config.py` | new settings, each defaulting to today's value: `ytdlp`, `bgutil_url`, `cookies_file`, `db_path`, `catalog_path`, `partition_slug`. `model_post_init` prefers unprefixed `HUB_SLUG` for `partition_slug` and picks up `COOKIES_FILE` when `GRABBY_COOKIES_FILE` is unset (pydantic's `env_prefix` cannot map names it does not own) | unset ⇒ today |
| `db.py` | `DB_PATH`/`CATALOG_PATH` from those settings. Required under the hub: one vendored directory hosts every tenant's process, so the defaults would put all partitions in one sqlite file and one catalog. `get_pending_jobs` → `get_claimable_jobs`, plus `claim_job` (`UPDATE … WHERE id=? AND status='queued'`, won = `rowcount==1`) and `reclaim_stale_jobs`: the claim is what lets the worker keep N harvests in flight without double-taking one job; the reclaim re-queues rows a crashed process left 'running'/'downloading'/'transcoding', safe because it runs once at worker start when nothing of ours can be in flight | empty ⇒ today's files beside the code |
| `main.py` | startup restores `yt_dlp_path`/`bgutil_url` from the persisted settings store **only when** `GRABBY_YTDLP`/`GRABBY_BGUTIL_URL` are absent (the hub's env outranks what an earlier operator typed into the UI). Comment records that mount stripping is the gateway's job | |
| `api/routes.py` | `GET/POST /settings/yt-dlp` read/write `settings.ytdlp` and `settings.bgutil_url` instead of hardcoded text, persisted through the existing `set_config` store | |
| `processor.py` | `_COOKIES_FILE` is the env-named partition file (upload/delete write a real partition path); `effective_cookies_file()` offers `[settings.cookies_file] + (shared, local)` with the same exists-and-non-empty skip, so a fresh partition with no export still passes no `--cookies`; bgutil `base_url` and both `["yt-dlp"]` argv heads come from settings; **`_write_mp3_tags` copy-on-write** — when a partition slug is set and the file has `st_nlink > 1`, copy + `os.replace` gives this copy a private inode before mutagen saves, because mutagen writes through the fd and retitling a blob link would rewrite the shared `/data/pool/blobs/<sha256>.mp3` and leave its hash-name describing the wrong bytes | slug unset ⇒ no `stat()`, in-place save as today |
| `processor.py` (worker) | real concurrency cap: upstream `await`ed one job before taking the next, so `settings.max_concurrent` could never exceed 1 and `quota.concurrentDownloads` was decorative. The hub loop claims jobs into `asyncio.create_task` up to `max_concurrent`, reaps with `asyncio.wait(FIRST_COMPLETED)`, and calls `reclaim_stale_jobs()` at start. Proven per partition: the quota-1 test partition's second job stayed `queued` until the first harvest ended, while a quota-2 partition ran two 6 s harvests in exact overlap | standalone: `max_concurrent`'s default 2 now genuinely means 2 |
| `processor.py` (index) | `audioPath`/`thumbnailPath` stay **relative and unprefixed**: DrivePod resolves them against its own `CACHE_DIR` (`resolveGrabbyAudioPath`), and this app's UI sits under its own mount where a relative path already lands right. f0f0aae rewrote this same file's `update_index_file()` so the index lists **only what this app authored** — an item JSON with a non-empty `createdAt` AND a catalog record (via `load_catalog()`, imported here already) — counting foreign folders and WARNing on `createdAt`-without-catalog, so an index entry is now an ownership statement, which is what DrivePod's `isGrabbyAuthored` filter reads. Output dir (`GRABBY_OUTPUT_DIR`) and catalog (`GRABBY_CATALOG_PATH`) still come from `config.py` settings, so a sweep sees only this tenant's cache | — |
| `tunester.py` | `_slug_header()` sends `X-Partition-Slug` on `POST /api/tune`, `GET /api/job/<id>`, `GET /api/dl/<jid>`, `POST /api/clear`; `_hardlink_delivery()` uses `os.link` + `os.replace` when the download response carries `X-Pool-Path` (see `platform/poolimport.py`), else today's streamed write | header absent ⇒ no header; no `X-Pool-Path` ⇒ streamed bytes |
| `static/index.html`, `static/js/app.js` | `API` derived from `document.baseURI` (empty standalone, `/l/<slug>/grabby` under the gateway); shell refs made relative | |

## `stack/tunester/` — tunester @ a310334

| File | Patch | Gate |
|---|---|---|
| `pipeline.py` | `cookie_candidates()`: when `TUNESTER_COOKIES` lists every partition's jar, offer only readable non-empty files sorted **newest-mtime-first** — the vault exports while its session is live, so the newest jar is the one a browser refreshed last, and a dead jar on YouTube looks like "all sources exhausted". `find_cookies()` keeps an explicit `--cookies` outranking them. The download passes the chosen jar to `browser_user_agent(cookies)` | `TUNESTER_COOKIES` unset ⇒ `COOKIES_CANDIDATES`, unchanged |
| `sources.py` | `browser_user_agent(cookies)` reads the state file **next to the jar it is describing** (`session-state.json` is written beside each export): the hub's newest-jar-wins choice otherwise pairs one partition's session with another's UA, and a mismatched UA on a live session is a kill signal. Lookup keyed on `TUNESTER_COOKIES` being set; the search call site passes the jar too | unset ⇒ today's single state file |
| `web.py` | single-flight: `_PENDING` keyed on `sources._norm(name)` (the shape the delivery cache hashes), consulted only for requests carrying `X-Partition-Slug`; a request for a song already being found ATTACHES (`attached: true`, waiter recorded) instead of fetching again, and the key is released in a `finally` with an identity check so a later request keeps its own claim. Blob serving: on success `platform/poolimport.blob_for()` puts the rip in the content-addressed store, `/api/dl` serves the BLOB and names it in `X-Pool-Path`; a blob-store failure is logged in the job and the output-dir copy is served, so it never looks like a failed tune. `_job_view`/`_job_public` add `slug`/`waiters` only for hub jobs. `_tune_one()` converts the pipeline's own `sys.exit()` into a `RuntimeError` so the worker's `except SystemExit` arm stops swallowing blob failures and dropping the `target` fixup. `_load_poolimport()` resolves `platform/` via `STACK_DIR` or one/two parents up, `None` standalone | header unset ⇒ today's responses key for key; `TUNESTER_BLOBS` unset ⇒ job keeps its own file |

2026-11-18 re-vendor (`b65948c` → `79ffd99` → `c5df587`: the `lookups.py`
answer store — one shared TTL cache for every MusicBrainz / AcoustID /
nzb.life / archive.org / yt-dlp **listing** reply, the NNTP tracklist text and
the LLM verdicts — plus source bytes published at FETCH time, then two fixes
that store exposed) installed with a **clean 3-way merge** (`git merge-file`,
0 conflicts) both times: upstream's changes land in `lookups.py` (new file),
`_acoustid_lookup`, `_nzb_sidecar_text`, `llm.LLM._send` and the discovery/QC
call sites, the hub patches in the cookie selection of `pipeline.py`,
`sources.browser_user_agent` and `web.py` — disjoint regions. Patch set proven
byte-unchanged at the new pin: the vendored-vs-upstream added/removed CONTENT
line-sets are 2/50, and the 2 removed lines plus those 50 are exactly the
`TUNESTER_COOKIES` cookie-selection patch — the ten acoustid `body` lines that
counted as vendored-only at the old pin are upstream's own now, so they cancel.
`AGENTS.md`/`README.md`/`lookups.py`/`llm.py`/`app.py`/`LICENSE` were byte-copies
of `c5df587` (`eaba24e8…`, `8f59535d…`, `9c6801ac…`, `93e8f688…`, `cba7eb54…`).
`pipeline.py`/`sources.py`/`web.py` keep only the patches above. Upstream's two
code commits changed `llm.py` and `pipeline.py`, and `COPY stack/tunester
./tunester` bakes the docs too, so the pin bump DOES want a rebuild.

2026-11-18 (`c5df587` → `a310334`: `lookups.key` now stringifies its parts,
because `("ytdlp", prefix, depth, …)` names a listing by the NUMBER of results
it asked for and `"\x1f".join` refused it — every yt-dlp-backed source
(YouTube and SoundCloud) died inside the wrapper with `sequence item 2:
expected str instance, int found`, so a tune logged an empty catalog instead of
missing a key). `a310334`'s range is exactly `lookups.py` + `AGENTS.md`, both
byte-copies here, so the install is two `cp`s with nothing to merge:
`pipeline.py`/`sources.py`/`web.py` are untouched by upstream and their
vendored-vs-pin added/removed CONTENT line-sets (52 / 25 / 231 lines) are
IDENTICAL at `c5df587` and at `a310334` — the patch set did not move.
Byte-copies at the new pin: `lookups.py` `582f7e19…`, `AGENTS.md` `dbc3eb24…`
(`README.md`/`llm.py`/`app.py`/`LICENSE` unchanged from `c5df587`). The doc
change lands in the vendored `AGENTS.md`, and `COPY stack/tunester ./tunester`
bakes it, so this pin bump wants a rebuild.

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
