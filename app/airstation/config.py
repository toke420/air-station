"""Runtime configuration.

Values come from environment variables (set by docker compose from `.env`) and,
for host-side tools, from the `.env` file in the install directory.
Secrets are read from files, never from the environment.
"""
import os
from pathlib import Path

ROOT = Path(os.environ.get("AIR_ROOT", Path(__file__).resolve().parents[2]))


def _load_dotenv(path):
    values = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


_DOTENV = _load_dotenv(ROOT / ".env")


def env(name, default=None):
    return os.environ.get(name, _DOTENV.get(name, default))


def env_int(name, default):
    try:
        return int(float(env(name, default)))
    except (TypeError, ValueError):
        return default


GIB = 1024 ** 3

DATA_DIR = Path(env("AIR_DATA_DIR", str(ROOT / "data")))
DB_PATH = Path(env("AIR_DB_PATH", str(DATA_DIR / "db" / "air.sqlite3")))
CACHE_DIR = Path(env("AIR_CACHE_DIR", str(DATA_DIR / "cache")))
ART_DIR = Path(env("AIR_ART_DIR", str(DATA_DIR / "art")))
SECRETS_DIR = Path(env("AIR_SECRETS_DIR", str(ROOT / "secrets")))
RECORDINGS_DIR = Path(env("AIR_RECORDINGS_DIR", str(DATA_DIR / "recordings")))
HLS_DIR = Path(env("AIR_HLS_DIR", str(DATA_DIR / "hls")))   # written by the hls service (ffmpeg)
WEB_DIR = Path(env("AIR_WEB_DIR", str(ROOT / "web")))                # page templates
SITE_DIR = Path(env("AIR_SITE_DIR", str(DATA_DIR / "site")))         # finished pages (pages.py)
BRANDING_DIR = Path(env("AIR_BRANDING_DIR", str(DATA_DIR / "branding")))
OWNCAST_URL = env("AIR_OWNCAST_URL", "")
DEFAULT_ART_URL = "/assets/img/default-art.png"

TZ = env("AIR_TZ", "UTC")
DOMAIN = env("AIR_DOMAIN", "radio.example.com")

# Path Liquidsoap sees for the cache directory (inside its container).
LIQ_CACHE_PREFIX = env("AIR_LIQ_CACHE_PREFIX", "/cache")
LIQ_HOST = env("AIR_LIQ_HOST", "liquidsoap")
LIQ_TELNET_PORT = env_int("AIR_LIQ_TELNET_PORT", 1234)
ICECAST_URL = env("AIR_ICECAST_URL", "http://icecast:8000")

NAS_PATH = env("AIR_NAS_PATH", "/mnt/air-nas/Music")
CACHE_MAX_BYTES = env_int("AIR_CACHE_MAX_GB", 12) * GIB
POOL_TARGET_BYTES = env_int("AIR_POOL_GB", 6) * GIB
DISK_RESERVE_BYTES = env_int("AIR_DISK_RESERVE_GB", 10) * GIB
FETCH_PER_RUN_BYTES = env_int("AIR_FETCH_PER_RUN_MB", 600) * 1024 ** 2
INDEX_INTERVAL_SECONDS = env_int("AIR_INDEX_HOURS", 6) * 3600
MAX_TRACK_BYTES = env_int("AIR_MAX_TRACK_MB", 200) * 1024 ** 2
MAX_TRACK_SECONDS = env_int("AIR_MAX_TRACK_MINUTES", 180) * 60

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav"}


def secret(name, default=None):
    """Read a secret file (docker secrets first, then the install directory)."""
    for base in (Path("/run/secrets"), SECRETS_DIR):
        path = base / name
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    if default is not None:
        return default
    raise RuntimeError(f"Secret {name!r} is missing; run scripts/init.py")
