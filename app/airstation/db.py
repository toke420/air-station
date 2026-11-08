"""SQLite storage shared by the API container and the host-side NAS sync job.

Both processes open the same file (bind-mounted), in WAL mode with a busy
timeout, so short concurrent writes are safe.
"""
import json
import sqlite3
import time
from pathlib import Path

from . import config

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id           INTEGER PRIMARY KEY,
    relpath      TEXT NOT NULL UNIQUE,      -- path relative to the NAS music root
    folder       TEXT NOT NULL DEFAULT '',  -- relative parent directory
    size         INTEGER NOT NULL DEFAULT 0,
    mtime        INTEGER NOT NULL DEFAULT 0,
    artist       TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    album        TEXT NOT NULL DEFAULT '',
    genre        TEXT NOT NULL DEFAULT '',
    year         TEXT NOT NULL DEFAULT '',
    duration     REAL,                      -- seconds; known once cached
    enabled      INTEGER NOT NULL DEFAULT 1,
    missing      INTEGER NOT NULL DEFAULT 0,
    bad          INTEGER NOT NULL DEFAULT 0,
    indexed_at   INTEGER NOT NULL DEFAULT 0,
    cached       INTEGER NOT NULL DEFAULT 0,
    cache_name   TEXT,                      -- relative to the cache directory
    cache_bytes  INTEGER NOT NULL DEFAULT 0,
    cached_at    INTEGER,
    last_used    INTEGER,                   -- last handed to Liquidsoap / queued
    last_played  INTEGER,
    play_count   INTEGER NOT NULL DEFAULT 0,
    has_art      INTEGER NOT NULL DEFAULT 0,
    art_checked  INTEGER NOT NULL DEFAULT 0     -- cover extraction attempted
);
CREATE INDEX IF NOT EXISTS tracks_folder ON tracks(folder);
CREATE INDEX IF NOT EXISTS tracks_played ON tracks(last_played);
CREATE INDEX IF NOT EXISTS tracks_cached ON tracks(cached, last_used);
CREATE INDEX IF NOT EXISTS tracks_artist ON tracks(artist);

CREATE TABLE IF NOT EXISTS playlists (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    kind             TEXT NOT NULL CHECK (kind IN ('all', 'folders', 'genres', 'manual')),
    rule             TEXT NOT NULL DEFAULT '[]',   -- JSON list of folders or genres
    is_default       INTEGER NOT NULL DEFAULT 0,
    enabled          INTEGER NOT NULL DEFAULT 1,
    no_repeat_hours  INTEGER NOT NULL DEFAULT 24,
    artist_gap       INTEGER NOT NULL DEFAULT 4,    -- tracks before an artist may repeat
    created_at       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id  INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id     INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (playlist_id, track_id)
);

CREATE TABLE IF NOT EXISTS shows (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    host         TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    days         TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6',  -- 0 = Monday
    start_time   TEXT NOT NULL,                          -- HH:MM, station time zone
    end_time     TEXT NOT NULL,                          -- HH:MM; may be <= start (overnight)
    playlist_id  INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
    is_live      INTEGER NOT NULL DEFAULT 0,             -- a DJ is expected to broadcast
    enabled      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS queue (
    id           INTEGER PRIMARY KEY,
    track_id     INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    playlist_id  INTEGER,
    reason       TEXT NOT NULL CHECK (reason IN ('auto', 'request', 'manual')),
    created_at   INTEGER NOT NULL,
    note         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS history (
    id           INTEGER PRIMARY KEY,
    started_at   INTEGER NOT NULL,
    source       TEXT NOT NULL,            -- autodj | live | obs | emergency
    track_id     INTEGER,
    artist       TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    album        TEXT NOT NULL DEFAULT '',
    show_name    TEXT NOT NULL DEFAULT '',
    art          TEXT                      -- cover URL at the time it played
);
CREATE INDEX IF NOT EXISTS history_started ON history(started_at);

CREATE TABLE IF NOT EXISTS requests (
    id           INTEGER PRIMARY KEY,
    track_id     INTEGER NOT NULL,
    client       TEXT NOT NULL,            -- salted hash of the requester address
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_client ON requests(client, created_at);

-- Listener statistics (v3). No raw IP addresses are stored: "visitor" is a keyed
-- hash used only to count unique listeners; locations are city level.
CREATE TABLE IF NOT EXISTS listener_sessions (
    id           INTEGER PRIMARY KEY,
    client_key   TEXT NOT NULL,            -- "<mount>#<icecast client id>"
    mount        TEXT NOT NULL,
    visitor      TEXT NOT NULL,
    internal     INTEGER NOT NULL DEFAULT 0,   -- 1 = not via the website/HTTPS (health checks)
    player       TEXT NOT NULL DEFAULT '',
    started_at   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    ended_at     INTEGER,                  -- NULL while still listening
    duration     INTEGER NOT NULL DEFAULT 0,
    country      TEXT NOT NULL DEFAULT '',
    country_name TEXT NOT NULL DEFAULT '',
    region       TEXT NOT NULL DEFAULT '',
    city         TEXT NOT NULL DEFAULT '',
    lat          REAL,
    lon          REAL
);
CREATE INDEX IF NOT EXISTS listener_sessions_open ON listener_sessions(ended_at);
CREATE INDEX IF NOT EXISTS listener_sessions_started ON listener_sessions(started_at);

CREATE TABLE IF NOT EXISTS listener_samples (
    at           INTEGER PRIMARY KEY,      -- minute (unix time, multiple of 60)
    listeners    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS page_hits (
    id           INTEGER PRIMARY KEY,
    at           INTEGER NOT NULL,
    page         TEXT NOT NULL,
    visitor      TEXT NOT NULL,
    device       TEXT NOT NULL DEFAULT '',
    referrer     TEXT NOT NULL DEFAULT '',   -- host name only
    country      TEXT NOT NULL DEFAULT '',
    country_name TEXT NOT NULL DEFAULT '',
    region       TEXT NOT NULL DEFAULT '',
    city         TEXT NOT NULL DEFAULT '',
    lat          REAL,
    lon          REAL
);
CREATE INDEX IF NOT EXISTS page_hits_at ON page_hits(at);
CREATE INDEX IF NOT EXISTS page_hits_visitor ON page_hits(visitor, at);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "station_name": "AirStation Radio",
    "tagline": "Live radio • Requests • 24/7",
    "about": ("An independent internet radio station run by music lovers: our library on "
              "shuffle around the clock, live DJ sets and listener requests."),
    "genres_banner": ["Live DJs", "New music", "Requests", "and more..."],
    "rotation_name": "Non-stop music",
    "rotation_description": "Our library on shuffle, 24/7",
    "requests_enabled": True,
    "request_gap_minutes": 15,
    "request_repeat_hours": 3,
    "live_enabled": True,
    "record_mode": "live",
    "social": {"discord": "", "instagram": "", "youtube": "", "x": "", "spotify": ""},
    "community": {"forum_url": "", "signup_url": "", "login_url": "",
                  "video_chat_url": "", "posts": []},
    "backup_stream_url": "",
    "default_art": "",          # empty = bundled /assets/img/default-art.png
    "owncast_url": "",          # empty = AIR_OWNCAST_URL
    "live_video_enabled": True,
    "brand_images": {},         # slot -> /branding/... (Admin > Appearance)
    "texts": {},                # page text overrides, see branding.TEXTS
    "theme": {},                # {"accent": "#rrggbb", "highlight": "#rrggbb"}
    "custom_css": "",
    "custom_js": "",
    "stats_enabled": True,      # listener sessions + website visits (Listeners tab)
    "stats_retention_days": 365,
    "map_style": "osm-dark",
    "map_carto_key": "",
    "map_tiles_url": "",
    "map_attribution": "",
}


def connect(path=None, readonly=False):
    path = Path(path or config.DB_PATH)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30,
                               check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


MIGRATIONS = {  # table -> {column: definition}; added if missing (v1 -> v2)
    "tracks": {"art_checked": "INTEGER NOT NULL DEFAULT 0"},
    "history": {"art": "TEXT"},
}


def _migrate(conn):
    for table, columns in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns.items():
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init(conn):
    conn.executescript(SCHEMA)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        _migrate(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    if not conn.execute("SELECT 1 FROM playlists WHERE is_default = 1").fetchone():
        conn.execute(
            "INSERT OR IGNORE INTO playlists (name, kind, rule, is_default, created_at) "
            "VALUES ('All Music', 'all', '[]', 1, ?)", (int(time.time()),))
        conn.execute("UPDATE playlists SET is_default = 1 WHERE name = 'All Music'")
    conn.commit()


def open_db(path=None):
    conn = connect(path)
    init(conn)
    return conn


# ---------------------------------------------------------------- settings

def get_setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key)
    try:
        return json.loads(row["value"])
    except ValueError:
        return DEFAULT_SETTINGS.get(key)


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value)))


def all_settings(conn):
    result = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        try:
            result[row["key"]] = json.loads(row["value"])
        except ValueError:
            pass
    return result


def rows(cursor):
    return [dict(r) for r in cursor]
