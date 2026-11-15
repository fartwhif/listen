#!/usr/bin/env python3
"""Identity server for the listen stack: the auth_request gate plus the login,
pairing, logout and code-minting endpoints.

Runs on 127.0.0.1:8099, i.e. loopback-only: the gateway is the only thing that
talks to it, and the gateway exposes exactly the endpoints a browser needs
(/api/auth/*) plus its own internal gate subrequests. Everything arrives
already proxied, so the real client IP comes from X-Forwarded-For.

Why auth_request instead of checking identity in each app: the apps stay
unchanged (drivepod/grabbytube/tunester/frogrock know nothing about users),
the media tree is served straight off disk by nginx with no app hop, and one
place decides who may enter which partition - which is exactly what a partition
boundary has to mean.

Hot path: every media file, index.json poll and api call is a subrequest here,
so the gate does one indexed SELECT on a token hash, no password KDF, no disk
I/O beyond the WAL. Sessions are throttled to one last_seen write per 5 minutes
so a chatty player cannot serialize the DB behind write traffic.
"""
import json
import os
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hubdb

PORT = int(os.environ.get("HUB_PORT", "8099"))
COOKIE = os.environ.get("HUB_COOKIE", "listen_sess")
# The registry path gen-conf supervises this program with (HUB_REGISTRY). It
# used to be a literal here, so a relocated HUB_DATA_DIR moved the file out
# from under a hardcoded reader: the gate then saw NO partitions and answered
# every subrequest 401 unknown-partition, which looks identical to "nobody is
# logged in" and is very hard to spot from the outside.
REALM_SLUGS = os.environ.get("HUB_REGISTRY") or \
    (os.environ.get("HUB_DATA_DIR", "/data").rstrip("/") + "/partitions.json")
# Stream allowance for players that cannot hold a cookie or a bearer key
# (FrogRock's ?pass= contract, kept byte-identical). Judged here rather than in
# nginx so exactly one place decides, and only on the stream path.
RADIO_PASS = os.environ.get("HUB_RADIO_PASS", "").strip()
# In-process only: a restart clears it. Persistent lockout would need another
# table; the point is just to make a password guesser cost more than 1 req/s.
FAIL_WINDOW = 300
FAIL_LIMIT = 12
_failures: dict[str, list[float]] = {}


def slugs() -> set[str]:
    """Registry read fresh each time (gen-conf rewrites it on every partition
    change) so a brand-new partition is gateable without a restart."""
    try:
        with open(REALM_SLUGS) as fh:
            reg = json.load(fh)
        return set(reg.get("slugs", []))
    except (OSError, json.JSONDecodeError):
        return set()


class Hub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    conn = None  # per-thread; set in setup()

    # ---- plumbing ----------------------------------------------------
    def setup(self):
        super().setup()
        self.conn = hubdb.connect()

    def log_message(self, fmt, *args):
        if os.environ.get("HUB_AUTH_DEBUG"):
            sys.stderr.write(f"hubauth: {self.address_string()} {fmt % args}\n")

    def client_ip(self) -> str:
        fwd = self.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "?"

    def json_in(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(min(n, 8192)) if n else b""
        if not raw:
            return {}
        ctype = self.headers.get("Content-Type", "")
        if "json" in ctype:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}
        try:
            # `.items()` matters: iterating a dict yields its KEYS, so `for k, v
            # in parse_qs(...)` unpacks each key STRING - a 4-char name like
            # `code` raises "too many values to unpack", the blanket except below
            # swallows it, and every form-encoded POST parsed as an empty body:
            # /api/auth/login answered "pick a partition" with the slug present.
            # hub.js posts JSON, so only a JS-less client (a car UI, a script,
            # curl --data-urlencode) ever took this branch.
            return {k: v[0] for k, v in
                    parse_qs(raw.decode("utf-8", "replace")).items()}
        except Exception:
            return {}

    def send_json(self, code: int, body: dict, extra: dict | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def wants_html(self) -> bool:
        """True for a browser form post - the no-JS fallback on the door pages.

        hub.js names JSON in its Accept header and an api client sends `*/*`
        (or nothing), so only a top-level form submission asks for text/html
        WITHOUT naming json. Such a caller cannot render `{"redirect": ...}`,
        so it gets the 302 hub.js would have followed. Failures stay JSON: an
        inline error needs the script, and the scripted path is what humans
        have.
        """
        accept = self.headers.get("Accept", "")
        return "text/html" in accept and "application/json" not in accept

    def send_result(self, body: dict, extra: dict) -> None:
        """200 + JSON for hub.js and api callers, 302 for a browser form post."""
        if self.wants_html():
            self.send_response(302)
            self.send_header("Location", body["redirect"])
            self.send_header("Cache-Control", "no-store")
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            return
        self.send_json(200, body, extra)

    def cookies(self) -> dict:
        out = {}
        for part in self.headers.get("Cookie", "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def set_session_cookie(self, token: str) -> str:
        secure = ""
        if self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https":
            secure = "; Secure"
        # Lax, not Strict: the picker and the partition shell are top-level
        # navigations, and Strict would drop the cookie on the first one.
        # Path=/ so a partition's /api/ and /cache/ requests both carry it.
        return (f"{COOKIE}={token}; Path=/; Max-Age={hubdb.SESSION_TTL}; "
                f"HttpOnly; SameSite=Lax{secure}")

    # ---- identity ----------------------------------------------------
    def bearer(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip() or None
        return None

    def gate_uri(self) -> str:
        """The FULL URI the gateway is gating - path AND query.

        The parent's original request arrives here only through this header.
        The `auth_request` subrequest nginx sends us has an EMPTY $args of its
        own: measured against nginx 1.22 (and true for the exact-, regex- and
        no-URI-form proxy_pass idioms alike), the subrequest's request line is
        `/gate/<slug>` with no `?...` at all, so `$is_args$args` appends nothing
        and `self.path` never carries the caller's `?key=`/`?pass=`. Only
        `$request_uri` - the unparsed original - does, which is why the gate
        location sets this header instead of trusting the forwarded path.
        """
        return self.headers.get("X-Gate-Uri") or self.path

    def gate_path(self) -> str:
        """The PATH the gateway is gating. The ?pass= allowance is scoped to it,
        so ?pass= cannot be smuggled onto an api or media request."""
        return urlparse(self.gate_uri()).path

    def gate_query(self) -> dict:
        """Query of the gated request; empty off the gateway (see gate_uri)."""
        return parse_qs(urlparse(self.gate_uri()).query)

    def identify(self, slug: str) -> tuple[str | None, str | None, str]:
        """(username, reason, effective slug) for the requested partition. API
        keys win over cookies so a headless client never depends on cookie
        policy; the ?key= form exists for podcast apps and car players that
        cannot set a header on a stream URL.

        The third element is the partition the answer is about, which is NOT
        always the one asked for: a session carries its own binding, and `/me`
        with no slug is a client asking "which partition am I in", so the
        session's binding is the answer. An api key is bound to no partition,
        so it leaves the slug empty and only a caller that required one accepts
        the result (see do_GET's /me and handle_codes).
        """
        # gate_query(), NOT self.path's query: on the gateway path that is the
        # ONLY place the caller's ?key= / ?pass= survives (see gate_uri), and
        # off the gateway gate_query() falls back to self.path, i.e. the request
        # line the direct loopback caller actually sent. One rule, both paths.
        query = self.gate_query()
        token = self.bearer() or self.cookies().get(COOKIE) or \
            query.get("key", [""])[0] or None
        if not token:
            if RADIO_PASS and query.get("pass", [""])[0] == RADIO_PASS and \
                    self.gate_path().endswith(("/radio/stream", "/radio/stream.mp3")):
                # Broadcast listener: the pass key names no member, so media
                # writes fall back to the partition's own progress file.
                return "listener", None, slug
            return None, "no-credentials", slug
        if token.startswith("lk_"):
            row = hubdb.lookup_api_key(self.conn, token)
            if not row:
                return None, "bad-key", slug
            username = row["username"]
        else:
            row = hubdb.lookup_session(self.conn, token)
            if not row:
                return None, "bad-session", slug
            if row["slug"] != slug:
                if slug:
                    # A session is bound to the partition it was minted for.
                    # The requested slug is what the GATEWAY gated on (its
                    # location proxies to that partition's apps, and its
                    # X-Partition-Slug header tells them which tenant to serve),
                    # so the session has to match THAT, not be substituted for
                    # it: substituting let an owner-only session walk into
                    # /l/family/… - and, because the bare front aliases gate on
                    # the front slug, let a family session read the front
                    # partition's media - while both membership checks passed on
                    # the session's own partition. Membership is global, a
                    # session is one partition's, and that is the boundary the
                    # README promises: two partitions need two sessions.
                    return None, "wrong-partition", slug
                slug = row["slug"]
            username = row["username"]
            hubdb.touch_session(self.conn, row["token_hash"])
        if not slug:
            # Only an api key gets here: a session always names a partition (its
            # own binding fills the gap). The user is real, the partition is not
            # decided, so membership is nobody's business until the caller picks.
            return username, None, ""
        if slug not in slugs():
            return None, "unknown-partition", slug
        member = self.conn.execute(
            "SELECT role FROM memberships WHERE slug=? AND username=?",
            (slug, username)).fetchone()
        if not member:
            return None, "not-a-member", slug
        return username, None, slug

    def throttle(self) -> bool:
        now = time.time()
        hits = [t for t in _failures.get(self.client_ip(), []) if now - t < FAIL_WINDOW]
        return len(hits) >= FAIL_LIMIT

    def strike(self) -> None:
        now = time.time()
        hits = [t for t in _failures.get(self.client_ip(), []) if now - t < FAIL_WINDOW]
        hits.append(now)
        _failures[self.client_ip()] = hits

    # ---- routes ------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self.send_json(200, {"ok": True, "service": "hubauth"})
        if u.path.startswith("/gate/"):
            return self.handle_gate(u.path[len("/gate/"):])
        if u.path == "/me":
            q = parse_qs(u.query)
            slug = q.get("slug", [""])[0]
            username, reason, eff = self.identify(slug)
            if not username:
                return self.send_json(401, {"error": reason})
            if not eff:
                # Only an api key leaves the slug unresolved: it is bound to
                # the user, not to a partition, so "am I logged in" is easy but
                # "which partition am I in" has no answer until the caller
                # names one. A 400 says that; a 401 would say "bad key".
                return self.send_json(400, {"error": "name a partition",
                                            "partitions": sorted(slugs())})
            row = self.conn.execute(
                "SELECT role FROM memberships WHERE slug=? AND username=?",
                (eff, username)).fetchone()
            return self.send_json(200, {"slug": eff, "username": username,
                                        "role": row["role"]})
        return self.send_json(404, {"error": "not-found"})

    def do_HEAD(self):
        self.do_GET()

    def handle_gate(self, slug: str) -> None:
        """auth_request target. nginx asks "may this request into partition
        slug happen?"; we answer with 200 (+ who) or 401. The subrequest
        carries the parent's Cookie/Authorization headers, and auth_request_set
        copies X-User back for the upstream to name per-member files."""
        if not slug or slug not in slugs():
            # A gate for a slug the registry does not know is a config error or
            # a stale generated block: deny rather than default-open.
            return self.send_json(401, {"error": "unknown-partition"})
        username, reason, _ = self.identify(slug)
        if not username:
            return self.send_json(401, {"error": reason},
                                  {"WWW-Authenticate": 'Bearer realm="listen"'})
        return self.send_json(200, {"ok": True, "username": username},
                              {"X-User": username, "X-Partition-Slug": slug})

    def do_POST(self):
        u = urlparse(self.path)
        body = self.json_in()
        if u.path == "/login":
            return self.handle_login(body)
        if u.path == "/pair":
            return self.handle_pair(body)
        if u.path == "/logout":
            return self.handle_logout()
        if u.path == "/codes":
            return self.handle_codes(body)
        return self.send_json(404, {"error": "not-found"})

    def login_target(self, body: dict, slug: str) -> str:
        """Where a successful login or pairing lands.

        A gated page navigation bounces through `/_login-redir` to
        `/login?next=<the page>`, and hub.js echoes that back in the POST body.
        Only a path INSIDE the partition just logged into is trusted: a foreign
        `/l/<other>/…` would be a redirect into a partition this session may not
        enter, and `//host` or `http:` would leave the portal entirely. Anything
        else lands on the partition's own root, which is also what a login from
        the picker (no `next`) gets.
        """
        nxt = (body.get("next") or "").strip()
        prefix = f"/l/{slug}/"
        if nxt.startswith(prefix) and not nxt.startswith("//"):
            return nxt
        return prefix

    def handle_login(self, body: dict) -> None:
        slug = (body.get("slug") or "").strip()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if slug not in slugs():
            return self.send_json(400, {"error": "pick a partition"})
        if self.throttle():
            return self.send_json(429, {"error": "too many attempts"})
        if len(password) < 8:
            return self.send_json(400, {"error": "password too short"})
        row = hubdb.check_credentials(self.conn, username, password)
        if not row:
            self.strike()
            return self.send_json(401, {"error": "wrong username or password"})
        member = self.conn.execute(
            "SELECT 1 FROM memberships WHERE slug=? AND username=?",
            (slug, username)).fetchone()
        if not member:
            self.strike()
            return self.send_json(403, {"error": "not a member of that partition"})
        token = hubdb.mint_session(self.conn, slug, username,
                                   self.headers.get("User-Agent", "")[:80])
        return self.send_result({"slug": slug, "username": username,
                                 "redirect": self.login_target(body, slug)},
                                {"Set-Cookie": self.set_session_cookie(token)})

    def handle_pair(self, body: dict) -> None:
        """No password on a shared/car device: type the code the owner shows."""
        slug = (body.get("slug") or "").strip()
        code = (body.get("code") or "").strip()
        if slug not in slugs():
            return self.send_json(400, {"error": "pick a partition"})
        if self.throttle():
            return self.send_json(429, {"error": "too many attempts"})
        if len(code) < 6:
            return self.send_json(400, {"error": "enter the 8-character code"})
        token = hubdb.redeem_code(self.conn, slug, code,
                                  self.headers.get("User-Agent", "")[:80])
        if not token:
            self.strike()
            return self.send_json(401, {"error": "code not recognised or already used"})
        user = hubdb.lookup_session(self.conn, token)
        return self.send_result({"slug": slug, "username": user["username"],
                                 "redirect": self.login_target(body, slug)},
                                {"Set-Cookie": self.set_session_cookie(token)})

    def handle_logout(self) -> None:
        token = self.cookies().get(COOKIE) or (self.bearer() or "")
        if token and not token.startswith("lk_"):
            hubdb.drop_session(self.conn, token)
        return self.send_result({"ok": True, "redirect": "/"},
                                {"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; "
                                               "HttpOnly; SameSite=Lax"})

    def handle_codes(self, body: dict) -> None:
        slug = (body.get("slug") or "").strip()
        member = (body.get("member") or "").strip()
        if not slug:
            # Minting is per partition, and the gateway does not gate
            # /api/auth/codes (it has no partition to gate), so the caller has
            # to say which one - especially an api key, which names no slug.
            return self.send_json(400, {"error": "name a partition",
                                        "partitions": sorted(slugs())})
        username, reason, _ = self.identify(slug)
        if not username:
            return self.send_json(401, {"error": reason})
        role = self.conn.execute("SELECT role FROM memberships WHERE slug=? AND "
                                 "username=?", (slug, username)).fetchone()
        if not role or role["role"] != "admin":
            return self.send_json(403, {"error": "only a partition admin can "
                                                 "mint pairing codes"})
        try:
            code, ttl = hubdb.mint_code(self.conn, slug, member)
        except ValueError as exc:
            # The member is decided HERE, at mint time, so the admin learns the
            # typo now instead of watching a device fail to pair later.
            return self.send_json(400, {"error": str(exc)})
        return self.send_json(200, {"code": code, "slug": slug,
                                    "member": member or None, "expiresIn": ttl})


def main() -> int:
    hubdb.init()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Hub)
    srv.daemon_threads = True
    print(f"hubauth: gate on 127.0.0.1:{PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
