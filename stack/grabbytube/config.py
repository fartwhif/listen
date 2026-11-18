import os
from pathlib import Path
from pydantic_settings import BaseSettings

PROJECT_ROOT = Path(__file__).parent.resolve()

# Default: share DrivePod's cache directory (relative to this project; overridable via GRABBY_OUTPUT_DIR)
DEFAULT_CACHE_DIR = str((PROJECT_ROOT / ".." / "DrivePod" / "cache").resolve())

class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8100
    output_dir: str = DEFAULT_CACHE_DIR
    max_concurrent: int = 2
    preferred_audio_format: str = "mp3"
    audio_bitrate: str = "160k"  # e.g. 128k, 192k, 256k, 320k
    audio_bitrate_mode: str = "cbr"  # cbr or vbr (vbr uses -q:a 2)
    audio_sample_rate: int = 44100  # 44.1 kHz (22050, 44100, 48000)
    audio_channels: int = 2  # 1 = mono, 2 = stereo

    # Where this instance keeps its job queue and track catalog. The hub gives
    # every partition its own pair (GRABBY_DB_PATH / GRABBY_CATALOG_PATH under
    # /data/users/<slug>/): the vendored tree is ONE directory shared by all
    # tenants, so left at the defaults two partitions would queue into one
    # sqlite file and overwrite one catalog. Empty = today's files beside the
    # code, which is what upstream's own container has.
    db_path: str = ""
    catalog_path: str = ""

    # tunester pipeline (separate container on the shared docker network)
    tunester_url: str = "http://tunester:8102"
    tunester_timeout: int = 3600  # max seconds to wait on one tunester job

    # yt-dlp invocation. `ytdlp` is the executable every harvest runs: the hub
    # sets GRABBY_YTDLP to the flock token bucket (platform/ytdlgated) so
    # parallel harvests QUEUE instead of hammering one Google session.
    # `bgutil_url` is the PO-Token sidecar base URL - the compose service name
    # upstream, 127.0.0.1:4416 (one shared sidecar) in the hub. Both default to
    # today's values, so behaviour is unchanged with the vars unset.
    ytdlp: str = "yt-dlp"
    bgutil_url: str = "http://grabbytube-bgutil:4416"

    # LLM duplicate detection. `llm_model` is the id sent to the endpoint: the
    # default is the 27B model this feature was written against; a deployment on
    # a different served model overrides it with GRABBY_LLM_MODEL rather than
    # editing code (an endpoint serving one model aliases any name, so the stale
    # default still answers - it just names the wrong model in the request).
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = "qwen3.6-27b"

    # YouTube cookies (Netscape format)
    youtube_cookies: str = ""

    # User agent for yt-dlp
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"

    # Shared secret for destructive endpoints (DrivePod interop). Empty = auth disabled.
    api_token: str = ""

    # The cookie file this instance owns: where uploads land, and where the
    # hub's per-partition vault export lives (GRABBY_COOKIES_FILE, else the
    # platform's unprefixed COOKIES_FILE, picked up in model_post_init).
    # Empty = today's ./data/cookies.txt. processor.effective_cookies_file()
    # still only *uses* a file that exists and is non-empty, so a fresh
    # partition with no export yet behaves exactly as today (no --cookies).
    cookies_file: str = ""

    # Which partition this process belongs to, sent to the hub's ONE shared
    # tuner pool as X-Partition-Slug so a job can be attributed to the
    # partition that asked for it. The platform hands it unprefixed as HUB_SLUG
    # (see platform/gen-conf.py tenant_env), which model_post_init below
    # prefers; GRABBY_PARTITION_SLUG is the fallback for anyone who wants to
    # set it through the normal env_prefix. Empty (both unset, i.e. today's
    # standalone container) means tunester.py sends no header at all.
    partition_slug: str = ""

    model_config = {
        "env_prefix": "GRABBY_",
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    def model_post_init(self, __context) -> None:
        # HUB_SLUG carries no GRABBY_ prefix, so pydantic's env_prefix cannot
        # map it; take it here so the hub needs no extra variable.
        hub_slug = os.environ.get("HUB_SLUG", "").strip()
        if hub_slug:
            self.partition_slug = hub_slug
        # Same for COOKIES_FILE (GRABBY_COOKIES_FILE already won above).
        if not self.cookies_file:
            self.cookies_file = os.environ.get("COOKIES_FILE", "").strip()

    @property
    def output_path(self) -> Path:
        p = Path(self.output_dir or DEFAULT_CACHE_DIR).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

settings = Settings()
