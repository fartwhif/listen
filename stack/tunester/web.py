"""HTTP API + web UI for tunester (served at tunester.example.com).

Deployed inside the listen hub this is one SHARED tuner behind a partition
gateway, which is why the three hub-only behaviours below are all keyed on
something an upstream request never carries:
  * X-Partition-Slug (sent by the gateway) turns on slug bookkeeping and the
    single-flight attach; without the header a tune queues exactly as before;
  * TUNESTER_BLOBS turns on content-addressed delivery through
    platform/poolimport.py; unset, /api/dl serves the job's own file.
An upstream deployment sets neither, so it gets today's responses key for key.
"""
import json
import os
import re
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pipeline import Options, tune
from sources import _norm


def _load_poolimport():
    """The hub's blob store module, or None when this tree is deployed
    standalone (no platform/ next to the trees it drives).

    Three layouts are possible and all are tried, because which one applies
    depends on where this file was unpacked and on whether STACK_DIR was handed
    to us: STACK_DIR (gen-conf sends the pool the stack root), the image layout
    (/srv/stack/tunester beside /srv/stack/platform, i.e. one parent up), and
    the vendored repo layout (listen/stack/tunester beside listen/platform, two
    parents up). A standalone tunester checkout has no platform/ at any of
    those, which is how it gets None and today's /api/dl. Each candidate is
    added to sys.path only for its own import, so a platform module can never
    shadow one of tunester's."""
    here = Path(__file__).resolve().parent
    roots = [os.environ.get("STACK_DIR"), str(here.parent), str(here.parent.parent)]
    for root in filter(None, roots):
        path = os.path.join(root, "platform")
        sys.path.insert(0, path)
        try:
            import poolimport
            return poolimport
        except ImportError:
            pass
        finally:
            sys.path.remove(path)
    return None


_POOLIMPORT = _load_poolimport()


def _blobs_dir() -> str | None:
    """Where finished files are content-addressed, or None to keep that off.
    One rule, and it is this: TUNESTER_BLOBS set non-empty enables it (gen-conf
    hands the pool <data>/pool/blobs); unset is today's standalone behaviour,
    where every job keeps its own copy in the output dir and /api/dl serves
    that with no hub header."""
    return os.environ.get("TUNESTER_BLOBS") or None


# Hub single-flight. The key is the song identity the pipeline's own delivery
# cache keys on - sources._norm of artist and title, the shape _delivery_key
# hashes - approximated before discovery from the requested name (the pipeline
# normalizes its target the same way), so a request for a song the pool is
# already finding attaches to it instead of fetching it a second time. Consulted
# only for requests that carry X-Partition-Slug, so a header-less request queues
# a new job exactly as it does today.
SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")  # = platform/gen-conf.py:42
# normalized requested name -> the RUNNING job every later request attaches to;
# dropped the moment that job is no longer running (a finished job is never an
# attach target, so a repeat request re-runs or hits today's cache path).
_PENDING: dict[str, dict] = {}


def _pending_key(name: str) -> str:
    return _norm(name)

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


class _Cancelled(Exception):
    """Raised from a job's log callback when the queue is cleared."""


def _clear_all() -> int:
    """Cancel every job and drop all job state. In-flight workers abort
    at their next log() call, which is bounded by the current LLM or
    download step (TUNESTER_LLM_TIMEOUT / TUNESTER_DOWNLOAD_TIMEOUT).
    Downloaded files are left in place."""
    with _LOCK:
        jobs = list(_JOBS.values())
        for job in jobs:
            job["cancelled"] = True
        _JOBS.clear()
        # Hub waiters are not jobs: they hang off the running job they attached
        # to, so that record goes with the job it points at.
        _PENDING.clear()
    return len(jobs)


def _job_record(name: str, outdir: Path, slug: str) -> dict:
    """Build a job and register it as this song's in-flight download. The lock
    must already be held: _queue_job checks for a running download of the same
    song and registers this one in the same hold, which is what makes two
    simultaneous requests for one song produce ONE download instead of two.
    Only hub requests (a slug) register: upstream has nobody to attach to."""
    job = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "status": "running",
        "log": [],
        "file": None,
        "filename": None,
        "artist": None,
        "title": None,
        "year": None,
        "error": None,
    }
    if slug:
        # Hub bookkeeping, added only for a request the gateway labelled with
        # its partition: a standalone queue keeps the exact keys - and values -
        # its responses carry today. `attached` is true only in an ATTACH
        # response (see _queue_job), never on a job of its own; `waiters` is the
        # list of slugs that waited on this download, so len() is the waiter
        # count the hub reports; `blob` is the content-addressed copy (see
        # _blobs_dir). ckey is the single-flight lookup key.
        job.update(slug=slug, attached=False, waiters=[], blob=None)
        job["ckey"] = _pending_key(name)
        _PENDING[job["ckey"]] = job
    _JOBS[job["id"]] = job
    return job


def _queue_job(name: str, outdir: Path, slug: str = "") -> tuple[dict, bool]:
    """(job, attached) for one tune request.

    A hub request for a song the pool is ALREADY finding attaches to that
    download instead of hammering the sources a second time for it; the job
    records the waiter, so N partitions asking for one song cost one download
    and land on one blob. A finished job is never an attach target - a repeat
    request re-runs (or hits the pipeline's own verified-delivery cache).
    ``attached`` is False and ``slug`` "" for an upstream request, which carries
    no X-Partition-Slug and therefore takes today's path unchanged."""
    with _LOCK:
        if slug:
            target = _PENDING.get(_pending_key(name))
            if target is not None and target["status"] == "running":
                target["waiters"].append(slug)
                target["log"].append(
                    f"[{slug}] same song as the download already running - "
                    f"waiting on it instead of fetching it again")
                return target, True
        job = _job_record(name, outdir, slug)
    threading.Thread(target=_run, args=(job, name, outdir), daemon=True).start()
    return job, False


def _job_view(job: dict) -> dict:
    """One /api/jobs entry: today's projection, plus the requesting partition
    and the partitions that attached to its download when the job is the hub's.
    Keyed on the slug rather than on a config flag so a standalone box returns
    exactly the JSON body it returns today."""
    view = {k: job[k] for k in ("id", "name", "status", "filename", "error")}
    if job.get("slug"):
        view["slug"] = job["slug"]
        view["waiters"] = list(job.get("waiters") or [])
    return view


def _job_public(job: dict) -> dict:
    """A job as it leaves the server: everything the record holds, less
    `ckey` - the single-flight lookup key, internal bookkeeping that is nobody
    else's business. A standalone job has no such key, so this is the whole
    dict, exactly as today's handler returned it."""
    return {k: v for k, v in job.items() if k != "ckey"}


def _tune_one(name: str, outdir: Path, log) -> object:
    """Run the pipeline once, turning the SystemExit that a sys.exit() inside
    it raises into an ordinary error.

    Converting it HERE rather than in the worker's `except SystemExit` arm is
    what lets that arm keep its meaning: as an arm it also caught a SystemExit
    raised by the bookkeeping below it, reported a blob-import failure as "the
    worker exited", and threw away the `target` fixup data the arm above it had
    just collected.
    """
    try:
        return tune(name, Options(outdir=outdir), log=log)
    except SystemExit as e:
        raise RuntimeError(f"worker exited (SystemExit {e.code})") from e


def _run(job: dict, name: str, outdir: Path) -> None:
    def log(msg: str) -> None:
        if job.get("cancelled"):
            raise _Cancelled()
        # Hub: say which partition asked, with today's wording either way.
        msg = f"[{job['slug']}] {msg}" if job.get("slug") else msg
        job["log"].append(msg)
        print(msg, flush=True)

    try:
        res = _tune_one(name, outdir, log)
        job["file"] = str(res.file)
        job["filename"] = res.file.name
        job["artist"] = res.artist
        job["title"] = res.title
        job["year"] = res.year
        if blobs := _blobs_dir():
            if _POOLIMPORT is None:
                # The hub asked for blob serving but platform/poolimport.py is
                # not importable from where this tree was deployed: say so IN
                # THE JOB, instead of quietly never producing a blob.
                log("      blob store unavailable: platform/poolimport.py not "
                    "importable from here - serving the output-dir copy")
            else:
                # Content-addressed: the same verified rip from two partitions
                # (or a re-tune of a cached song) lands on the SAME blob inode,
                # so it was downloaded once and every partition hardlinks it.
                try:
                    job["blob"] = _POOLIMPORT.blob_for(str(res.file), blobs)
                except Exception as e:
                    # The rip itself succeeded, so the job stays done and
                    # /api/dl serves the output-dir file: losing the shared
                    # inode must not look to the caller like a failed tune.
                    log(f"      blob store unavailable: {e}")
        job["status"] = "done"
    except _Cancelled:
        # The job entry is already gone from _JOBS; this just ends the
        # worker thread at the next stage boundary instead of letting it
        # burn through the source fallback chain.
        job["status"] = "cancelled"
        print(f"job {job['id']} cancelled (queue cleared)", flush=True)
    except Exception as e:
        job["error"] = str(e)
        # Discovery may have succeeded even when no verified file was
        # delivered: keep the fixup data (artist/title/year) on the job.
        t = getattr(e, "target", None)
        if t is not None:
            job["artist"] = t.artist
            job["title"] = t.title
            job["year"] = t.year
        job["status"] = "error"
    except SystemExit as e:
        # A SystemExit that escapes the bookkeeping above is a BaseException:
        # a plain `except Exception` let such worker threads die silently,
        # leaving the job stuck at "running" forever. The pipeline's own
        # sys.exit() is converted in _tune_one, so this arm no longer shadows
        # an error whose `target` fixup data it would have dropped.
        job["error"] = f"worker exited (SystemExit {e.code})"
        job["status"] = "error"
    finally:
        with _LOCK:
            # This song is no longer being fetched, so it must not stay an
            # attach target: a later request re-runs (or hits the pipeline's
            # own verified-delivery cache). The identity check matters - a
            # request that arrived after this job finished has already claimed
            # the key for its own download, and must keep it.
            key = job.get("ckey") or ""
            if key and _PENDING.get(key) is job:
                _PENDING.pop(key)


UI_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tunester</title>
<style>
  :root { color-scheme: dark; }
  body { font: 15px/1.5 system-ui, sans-serif; background: #0e1116; color: #d8dee9;
         max-width: 720px; margin: 4rem auto; padding: 0 1rem; }
  h1 { font-size: 1.6rem; } h1 span { color: #4fd6be; }
  p.sub { color: #8a93a5; }
  textarea { width: 100%; box-sizing: border-box; min-height: 90px; resize: vertical;
             background: #171c24; color: #d8dee9; border: 1px solid #2c3442;
             border-radius: 8px; padding: .7rem; font: inherit; }
  button { margin-top: .6rem; background: #4fd6be; color: #0e1116; border: 0; border-radius: 8px;
           padding: .6rem 1.4rem; font: inherit; font-weight: 600; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  pre { background: #12161d; border: 1px solid #2c3442; border-radius: 8px; padding: .8rem;
        font: 12.5px/1.6 ui-monospace, monospace; white-space: pre-wrap; word-break: break-word;
        min-height: 2rem; max-height: 400px; overflow: auto; }
  a.dl { display: inline-block; margin-top: .8rem; color: #4fd6be; font-weight: 600; }
  .err { color: #f28fad; }
  .q { margin-top: 2.5rem; border-top: 1px solid #2c3442; padding-top: 1rem; }
  .q .row { display: flex; justify-content: space-between; align-items: center; }
  .q h2 { font-size: 1.15rem; margin: 0; }
  .q button { margin-top: 0; padding: .45rem 1.1rem; font-size: 13.5px; }
  .q ul { list-style: none; padding: 0; margin: .6rem 0 0; }
  .q li { display: flex; gap: .6rem; align-items: baseline; padding: .3rem 0;
          color: #8a93a5; font-size: 13.5px; }
  .q .st { font-weight: 600; flex: none; min-width: 64px; }
  .st.running { color: #f2c94c; } .st.done { color: #4fd6be; }
  .st.error, .st.cancelled { color: #f28fad; }
  .q a { color: #4fd6be; text-decoration: none; }
  .q .err { font-size: 12.5px; }
</style></head>
<body>
<h1>tune<span>ster</span></h1>
<p class="sub">Dirty song name in, verified uncensored MP3 out
(160 kbps CBR / 44.1 kHz / stereo).<br>
Sources, best to worst: YouTube, SoundCloud, archive.org, YouTube (deep).</p>
<textarea id="name" placeholder="e.g. the loud rtm song about killing in the name"></textarea>
<button id="go" onclick="tuneIt()">Tune it</button>
<pre id="log"></pre>
<a id="dl" class="dl" hidden></a>
<div class="q">
  <div class="row"><h2>Queue</h2>
  <button id="clear" onclick="clearQueue()" disabled>Clear queue (0)</button></div>
  <ul id="jobs"></ul>
</div>
<script>
let job = null, timer = null;
async function tuneIt() {
  const name = document.getElementById('name').value.trim();
  if (!name) return;
  const btn = document.getElementById('go'), log = document.getElementById('log'),
        dl = document.getElementById('dl');
  btn.disabled = true; log.textContent = ''; dl.hidden = true;
  const r = await fetch('/api/tune', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name})});
  const d = await r.json();
  if (!r.ok) { log.textContent = 'error: ' + (d.error || r.status); btn.disabled = false; return; }
  job = d.id;
  timer = setInterval(poll, 1500);
  poll();
  refreshJobs();
}
async function poll() {
  if (!job) return;
  const r = await fetch('/api/job/' + job);
  const j = await r.json();
  refreshJobs();
  if (r.status === 404) {
    // the job was cleared while we were polling
    clearInterval(timer);
    document.getElementById('log').textContent += '\\njob cleared';
    return;
  }
  document.getElementById('log').textContent =
    (j.log || []).join('\\n') + (j.status === 'running' ? '\\n...' : '');
  if (j.status !== 'running') {
    clearInterval(timer);
    const btn = document.getElementById('go'), dl = document.getElementById('dl');
    btn.disabled = false;
    if (j.status === 'done' && j.filename) {
      dl.href = '/api/dl/' + job; dl.textContent = 'Download: ' + j.filename;
      dl.hidden = false;
    } else if (j.error) {
      document.getElementById('log').textContent += '\\nerror: ' + j.error;
      document.getElementById('log').classList.add('err');
    }
  }
}
async function refreshJobs() {
  const r = await fetch('/api/jobs');
  const d = await r.json();
  const ul = document.getElementById('jobs');
  const btn = document.getElementById('clear');
  ul.innerHTML = '';
  btn.disabled = !d.jobs.length;
  btn.textContent = 'Clear queue (' + d.jobs.length + ')';
  for (const j of d.jobs) {
    const li = document.createElement('li');
    const st = document.createElement('span');
    st.className = 'st ' + j.status;
    st.textContent = j.status;
    const nm = document.createElement('span');
    nm.textContent = j.name;
    li.append(st, nm);
    if (j.status === 'done' && j.filename) {
      const a = document.createElement('a');
      a.href = '/api/dl/' + j.id;
      a.textContent = j.filename;
      li.append(a);
    } else if (j.error) {
      const er = document.createElement('span');
      er.className = 'err';
      er.textContent = j.error.slice(0, 140);
      li.append(er);
    }
    ul.appendChild(li);
  }
}
async function clearQueue() {
  const btn = document.getElementById('clear');
  btn.disabled = true;
  const r = await fetch('/api/clear', {method: 'POST'});
  const d = await r.json();
  document.getElementById('log').textContent +=
    '\\ncleared ' + d.cleared + ' job(s) from the queue';
  if (job) { job = null; if (timer) clearInterval(timer); }
  document.getElementById('go').disabled = false;
  refreshJobs();
}
refreshJobs();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "tunester/1.0"
    outdir: Path = Path("downloads")

    def log_message(self, fmt, *args):
        print("[http]", fmt % args, flush=True)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if self.path == "/":
            self._send(200, UI_HTML.encode(), "text/html; charset=utf-8")
            return
        if self.path == "/api/health":
            self._json(200, {"ok": True})
            return
        if self.path == "/api/jobs":
            with _LOCK:
                jobs = [_job_view(job) for job in _JOBS.values()]
            jobs.reverse()
            self._json(200, {"jobs": jobs})
            return
        m = re.match(r"^/api/job/([\w]+)$", self.path)
        if m:
            with _LOCK:
                job = _JOBS.get(m.group(1))
            self._json(200 if job else 404,
                       _job_public(job) if job else {"error": "unknown job"})
            return
        m = re.match(r"^/api/dl/([\w]+)$", self.path)
        if m:
            with _LOCK:
                job = _JOBS.get(m.group(1))
            if not job or job.get("status") != "done" or not job.get("file"):
                self._json(404, {"error": "not ready"})
                return
            # Blob serving is on (TUNESTER_BLOBS) and this job's rip made it
            # into the store: serve the BLOB, not the output-dir copy, and name
            # it in X-Pool-Path so the requesting partition's app can hardlink
            # it into its own cache. The blob is content-addressed, so identical
            # rips asked for by different partitions converge on ONE inode and
            # the second delivery costs a link, not a download.
            path = Path(job.get("blob") or job["file"])
            if not path.exists():
                self._json(410, {"error": "file gone"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{job["filename"]}"')
            if job.get("blob"):
                self.send_header("X-Pool-Path", job["blob"])
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            with open(path, "rb") as f:
                self.wfile.write(f.read())
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/clear":
            n = _clear_all()
            print(f"queue cleared: {n} job(s)", flush=True)
            self._json(200, {"cleared": n})
            return
        if self.path != "/api/tune":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            name = str(body.get("name") or "").strip()
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
            return
        if not name:
            self._json(400, {"error": "name required"})
            return
        # The hub gateway labels every request it proxies with the partition
        # that made it; upstream clients send no such header, so slug stays ""
        # and this responds {"id": ...} exactly as it does today.
        slug = (self.headers.get("X-Partition-Slug") or "").strip()
        if slug and not SLUG_RE.match(slug):
            self._json(400, {"error": "invalid partition slug"})
            return
        job, attached = _queue_job(name, self.outdir, slug)
        if not attached:
            self._json(202, {"id": job["id"]})
            return
        with _LOCK:
            body = {"id": job["id"], "slug": slug, "attached": True,
                    "waiters": list(job["waiters"]), "log": list(job["log"])}
        self._json(202, body)


def serve(host: str, port: int, outdir: Path) -> None:
    Handler.outdir = outdir
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        raise SystemExit(f"cannot create output dir {outdir}: {e} (use -o OUTDIR)")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"tunester listening on http://{host}:{port} (output: {outdir})", flush=True)
    httpd.serve_forever()
