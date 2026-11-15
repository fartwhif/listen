"""GrabbyTube - YouTube to MP3 web app."""
import logging
import os
import re as _re
from contextlib import asynccontextmanager
from hmac import compare_digest

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from db import init_db, get_config
from api.routes import router, start_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("grabbytube")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("GrabbyTube starting up...")
    init_db()
    persisted_ua = get_config("user_agent")
    if persisted_ua and "GRABBY_USER_AGENT" not in os.environ:
        settings.user_agent = persisted_ua
        logger.info("User agent restored from database")
    # Same for the yt-dlp executable / bgutil URL edited on the settings page:
    # an explicit env var (the hub sets both) outranks what a previous operator
    # typed into the UI. The env names are spelled out: the setting is `ytdlp`,
    # not `yt_dlp_path`, so a name derived from the config key would not match.
    for key, attr, env in (("yt_dlp_path", "ytdlp", "GRABBY_YTDLP"),
                           ("bgutil_url", "bgutil_url", "GRABBY_BGUTIL_URL")):
        persisted = get_config(key)
        if persisted and env not in os.environ:
            setattr(settings, attr, persisted)
            logger.info(f"{key} restored from database")
    start_worker()
    logger.info(f"Output directory: {settings.output_path}")
    yield
    logger.info("GrabbyTube shutting down")


app = FastAPI(title="GrabbyTube", lifespan=lifespan)

# Guarded endpoints require a shared secret (X-Grabby-Token header) when
# GRABBY_API_TOKEN is set. Protects the public API (grabbytube.example.com) from
# arbitrary track deletion / name rewrites / library-wide flag resets; DrivePod
# sends the same token from its .env.
PROTECTED_DELETE_PATHS = (
    "/tracks/",          # DELETE /tracks/{video_id}, POST /tracks/batch-delete
    "/jobs",             # DELETE /jobs, /jobs/all
)
PROTECTED_POST_PATHS = (
    "/tracks/batch-delete",
    "/tracks/clear-tuned",       # library-wide tuned-flag reset
    "/tracks/tunester/clear",    # cancels a running tunester batch
)
# POST /tracks/{video_id}/meta rewrites that track's MP3 tags, item JSON and
# catalog entry, so it is guarded like the destructive paths. Regex instead of
# a prefix: "/tracks/" would also catch POST /tracks/tunester (a run request).
_PROTECTED_POST_TRACK_META = _re.compile(r"^/tracks/[^/]+/meta$")


@app.middleware("http")
async def verify_guarded_token(request: Request, call_next):
    path = request.url.path
    is_destructive = (
        request.method == "DELETE" and path.startswith(PROTECTED_DELETE_PATHS)
    ) or (request.method == "POST" and (path in PROTECTED_POST_PATHS
                                        or _PROTECTED_POST_TRACK_META.match(path)))
    if is_destructive and settings.api_token and not compare_digest(request.headers.get("x-grabby-token", ""), settings.api_token):
        return JSONResponse(status_code=401, content={"detail": "Invalid or missing token"})
    return await call_next(request)


app.include_router(router)
app.mount("/static", StaticFiles(directory="static", html=True), name="static")


@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")


# No mount-prefix handling here on purpose. The hub's gateway owns the mount:
# `location ^~ /l/<slug>/grabby/` proxies with a trailing-slash target, which
# strips the mount, so the UI served under `/l/<slug>/grabby/` asks for
# `/l/<slug>/grabby/static/...` (relative to itself) and arrives back as
# `/static/...`, and `/l/<slug>/grabby/tracks` arrives as `/tracks`. Nothing in
# the box sends this app a prefixed path otherwise: DrivePod and the vault reach
# it over loopback with bare paths, and the gateway has no /api/ alias for
# grabby's route names because nothing calls them through it (see
# platform/gen-conf.py's api/ split).
# A stripping middleware here would make this app a second place that knows
# the mount - and the token guard above compares bare paths, so it would have
# to run first or start rejecting (or admitting) the wrong requests.

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
