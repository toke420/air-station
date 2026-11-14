"""AirStation API: public now-playing/schedule/requests, admin API, and the
AutoDJ "next track" endpoint used by Liquidsoap. Standard library only.

Caddy proxies /api/* to this service. /internal/* is only reachable on the
Docker network (Caddy never forwards it) and also requires a shared token.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
import traceback
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import branding, config, db, director, duplicates, emergency, licensing, liquidsoap, pages, stats, tags

SESSION_COOKIE = "air_admin"
SESSION_SECONDS = 12 * 3600
MAX_BODY = 256 * 1024
RECORD_MODES = ("off", "live", "all")
MIC_SOCKET = "/api/admin/mic/socket"     # Caddy passes this WebSocket to Liquidsoap


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


class Station:
    """Shared state plus the background loops."""

    def __init__(self, db_path=None, start_threads=True):
        self.db_path = db_path
        self.local = threading.local()
        self.lock = threading.Lock()
        conn = db.open_db(db_path)
        conn.close()
        self.liq = None            # last air.state reply
        self.liq_ok = False
        self.liq_error = ""
        self.identity = None
        self.started_at = int(time.time())
        self.listeners = {"total": 0, "mounts": {}}
        self.nowplaying = {}
        self.login_attempts = {}
        self.session_secret = config.secret("session_secret", default=secrets.token_hex(32))
        self.internal_token = config.secret("internal_token", default="")
        if start_threads:
            for target, interval in ((self.poll_liquidsoap, 2), (self.plan_queue, 30),
                                     (self.poll_icecast, 15), (self.enforce, 30),
                                     (self.poll_listeners, 15), (self.check_licence, 3600),
                                     (self.write_pages, 60)):
                threading.Thread(target=self._loop, args=(target, interval), daemon=True).start()
        self.refresh_nowplaying()

    # -------------------------------------------------------------- helpers
    def conn(self):
        conn = getattr(self.local, "conn", None)
        if conn is None:
            conn = db.connect(self.db_path)
            self.local.conn = conn
        return conn

    def _loop(self, target, interval):
        while True:
            try:
                target()
            except Exception:  # keep the loop alive; log the cause
                log(f"{target.__name__} failed:\n{traceback.format_exc()}")
            time.sleep(interval)

    # ---------------------------------------------------------- background
    def poll_liquidsoap(self):
        try:
            state = liquidsoap.state()
            self.liq, self.liq_ok, self.liq_error = state, True, ""
        except (liquidsoap.LiquidsoapError, ValueError) as error:
            self.liq_ok, self.liq_error = False, str(error)
            self.refresh_nowplaying()
            return
        self.record_history(state)
        self.refresh_nowplaying()

    def poll_listeners(self):
        """Listener sessions for Studio > Listeners (see stats.py)."""
        conn = self.conn()
        stats.poll(conn)
        if time.time() - getattr(self, "_pruned", 0) > 86400:
            stats.prune(conn)
            self._pruned = time.time()

    def write_pages(self):
        """Home and Live pages with the station's title and texts in the HTML (pages.py)."""
        if not (config.WEB_DIR / "index.html").is_file():
            if not getattr(self, "_pages_warned", False):
                log(f"Page templates not found in {config.WEB_DIR}: the site is served without "
                    "the station's title in the HTML (check the ./web mount of the api service).")
                self._pages_warned = True
            return
        changed = pages.write_all(self.conn())
        if changed:
            log(f"Pages updated: {', '.join(changed)}")

    def check_licence(self):
        """Refresh the licence certificate about weekly (see licensing.py)."""
        result = licensing.checkin(self.conn())
        if result not in ("not due", "nothing to check", "tried recently"):
            log(f"Licence check: {result}")

    def plan_queue(self):
        director.plan(self.conn())

    def poll_icecast(self):
        try:
            with urllib.request.urlopen(f"{config.ICECAST_URL}/status-json.xsl", timeout=5) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            return
        sources = data.get("icestats", {}).get("source") or []
        if isinstance(sources, dict):
            sources = [sources]
        mounts = {}
        for source in sources:
            mount = urlparse(source.get("listenurl", "")).path or "?"
            mounts[mount] = int(source.get("listeners") or 0)
        # The HLS packager is one Icecast listener on /radio.mp3; its listeners are counted
        # from playlist requests instead (see stats.hls_seen).
        if hls_running() and mounts.get("/radio.mp3"):
            mounts["/radio.mp3"] -= 1
        mounts[stats.HLS_MOUNT] = len(stats.hls_clients())
        self.listeners = {"total": sum(mounts.values()), "mounts": mounts}

    def enforce(self):
        """Keep Liquidsoap in line with settings; stop recording on low disk."""
        conn = self.conn()
        settings = db.all_settings(conn)
        mode = settings.get("record_mode", "off")
        if mode != "off":
            try:
                free = shutil.disk_usage(config.RECORDINGS_DIR).free
            except OSError:
                free = None
            if free is not None and free < config.DISK_RESERVE_BYTES:
                log("Disk reserve reached: recording switched off.")
                mode = "off"
                db.set_setting(conn, "record_mode", "off")
                conn.commit()
        if not self.liq_ok or not self.liq:
            return
        live_wanted = bool(settings.get("live_enabled", True))
        try:
            if self.liq.get("record") != mode:
                liquidsoap.command(f"air.record {mode}")
            if self.liq.get("live_enabled") != live_wanted:
                liquidsoap.command(f"air.live {'on' if live_wanted else 'off'}")
        except liquidsoap.LiquidsoapError as error:
            log(f"Could not apply settings to Liquidsoap: {error}")

    def resolve_air_id(self, state):
        """air_id from Liquidsoap, or (if a library track arrives without it) the
        cached track with the same artist and title, so covers and the progress
        bar still work."""
        air_id = state.get("air_id") or ""
        if air_id or (state.get("source") or "autodj") != "autodj" or not state.get("title"):
            return air_id
        row = self.conn().execute(
            "SELECT id FROM tracks WHERE cached = 1 AND title = ? AND artist = ? "
            "ORDER BY last_played DESC LIMIT 1",
            (state.get("title", ""), state.get("artist", ""))).fetchone()
        return str(row["id"]) if row else ""

    def record_history(self, state):
        source = state.get("source") or "autodj"
        air_id = self.resolve_air_id(state)
        artist, title = state.get("artist", ""), state.get("title", "")
        if source == "autodj" and (air_id.startswith("em-") or (not air_id and (artist or title))):
            source = "emergency"
        if source == "autodj" and state.get("music_stopped"):
            return                      # talk break: the last song has ended, nothing new plays
        identity = (source, air_id, artist, title)
        if identity == self.identity:
            return
        self.identity = identity
        self.started_at = int(time.time())
        if not (artist or title or air_id):
            return
        conn = self.conn()
        track_id = int(air_id) if air_id.isdigit() else None
        album = state.get("album", "")
        if track_id:
            row = conn.execute("SELECT artist, title, album FROM tracks WHERE id = ?",
                               (track_id,)).fetchone()
            if row:
                artist = artist or row["artist"]
                title = title or row["title"]
                album = album or row["album"]
            conn.execute("UPDATE tracks SET play_count = play_count + 1, last_played = ? "
                         "WHERE id = ?", (self.started_at, track_id))
        show = director.current_show(conn)
        conn.execute("INSERT INTO history (started_at, source, track_id, artist, title, album, "
                     "show_name, art) VALUES (?,?,?,?,?,?,?,?)",
                     (self.started_at, source, track_id, artist, title, album,
                      show["name"] if show else "", self.art_for(conn, air_id)))
        conn.execute("DELETE FROM history WHERE started_at < ?",
                     (self.started_at - 90 * 86400,))
        conn.commit()

    # ------------------------------------------------------------ nowplaying
    def art_for(self, conn, air_id):
        """Cover URL for a library track id or an emergency 'em-...' id, else None."""
        return art_url(air_id)

    def refresh_nowplaying(self):
        conn = self.conn()
        settings = db.all_settings(conn)
        state = self.liq if self.liq_ok else None
        show = director.current_show(conn)
        source = "offline"
        now = {"artist": "", "title": "", "album": "", "art": None, "duration": None}
        if state:
            source = (self.identity or ("autodj",))[0]
            now.update(artist=state.get("artist", ""), title=state.get("title", ""),
                       album=state.get("album", ""))
            air_id = self.resolve_air_id(state)
            if air_id.isdigit():
                row = conn.execute("SELECT * FROM tracks WHERE id = ?", (int(air_id),)).fetchone()
                if row:
                    now.update(artist=now["artist"] or row["artist"],
                               title=now["title"] or row["title"],
                               album=now["album"] or row["album"],
                               duration=row["duration"],
                               art=art_url(row["id"]))
            elif air_id.startswith("em-"):
                now["art"] = self.art_for(conn, air_id)
            if state.get("music_stopped") and source not in ("live", "obs"):
                now = {"artist": "", "title": "Talk break", "album": "", "art": None,
                       "duration": None}
        recent = []
        for row in conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT 11"):
            stored = re.match(r"/art/([\w-]+)\.jpg", row["art"] or "")
            recent.append({"artist": row["artist"], "title": row["title"],
                           "album": row["album"], "source": row["source"],
                           "started_at": row["started_at"],
                           "art": (art_url(row["track_id"]) if row["track_id"]
                                   else art_url(stored.group(1)) if stored else None)})
        if recent and state and recent[0]["title"] == now["title"] \
                and recent[0]["artist"] == now["artist"]:
            recent = recent[1:]
        live = source in ("live", "obs")
        if show:
            on_air = {"name": show["name"], "host": show["host"],
                      "description": show["description"]}
        else:
            on_air = {"name": settings.get("rotation_name"), "host": "",
                      "description": settings.get("rotation_description")}
        if live and state:
            on_air["live_title"] = state.get("meta", {}).get("live_name", "")
        self.nowplaying = {
            "station": settings.get("station_name"),
            "online": bool(state),
            "source": source,
            "is_live": live,
            "live_next": bool(state and state.get("live_waiting")),
            "now": now,
            "started_at": self.started_at,
            "server_time": int(time.time()),
            "on_air": on_air,
            "recent": recent[:10],
            "listeners": self.listeners["total"],
            "streams": [{"name": "High quality", "url": "/radio.mp3", "bitrate": 192,
                         "format": "MP3"},
                        {"name": "Data saver", "url": "/radio-low.mp3", "bitrate": 96,
                         "format": "MP3"}],
        }

    # ---------------------------------------------------------------- admin
    def admin_password(self):
        return config.secret("admin_ui_password")

    def _session_key(self):
        return hmac.new(self.session_secret.encode(), self.admin_password().encode(),
                        hashlib.sha256).digest()

    def make_session(self):
        expires = int(time.time()) + SESSION_SECONDS
        nonce = secrets.token_hex(8)
        payload = f"{expires}.{nonce}"
        sig = hmac.new(self._session_key(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    def valid_session(self, token):
        try:
            expires, nonce, sig = token.split(".")
            payload = f"{expires}.{nonce}"
            good = hmac.new(self._session_key(), payload.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, good) and int(expires) > time.time()
        except (ValueError, AttributeError, RuntimeError):
            return False

    def client_id(self, address):
        return hmac.new(self.session_secret.encode(), address.encode(),
                        hashlib.sha256).hexdigest()[:24]


# ====================================================================== HTTP

class Raw:
    """A non-JSON response (CSS, JS, redirects)."""

    def __init__(self, body=b"", content_type="text/plain; charset=utf-8", status=200,
                 headers=None, cache="no-cache"):
        self.body = body if isinstance(body, bytes) else str(body).encode()
        self.content_type = content_type
        self.status = status
        self.headers = headers or {}
        self.cache = cache


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


ROUTES = []
PAGE_INPUTS = re.compile(r"/api/admin/(settings|appearance|brand/.*|default-art)$")


def route(method, pattern, admin=False):
    regex = re.compile("^" + pattern + "$")

    def decorator(func):
        ROUTES.append((method, regex, admin, func))
        return func
    return decorator


def art_url(key):
    """Public URL of a cover in ART_DIR, or None when there is no such file.

    The file on disk is the source of truth (not tracks.has_art), so a missing
    cover shows the default image instead of a broken one. ?v= changes when a
    cover is re-extracted, so browsers never keep an old one.
    """
    key = str(key or "")
    if not (key.isdigit() or re.fullmatch(r"em-[0-9a-f]{6,40}", key)):
        return None
    try:
        mtime = int((config.ART_DIR / f"{key}.jpg").stat().st_mtime)
    except OSError:
        return None
    return f"/art/{key}.jpg?v={mtime}"


def track_public(row):
    return {"id": row["id"], "artist": row["artist"], "title": row["title"],
            "album": row["album"], "genre": row["genre"], "duration": row["duration"],
            "art": art_url(row["id"]) if row["has_art"] else None}


PUBLIC_SETTINGS = ("station_name", "tagline", "about", "genres_banner", "rotation_name",
                   "rotation_description", "requests_enabled", "social", "community",
                   "backup_stream_url", "live_video_enabled")


def default_art(settings):
    """The admin-uploaded default cover if its file still exists, else the bundled one."""
    return branding.url(settings, "cover")


def owncast_url(settings):
    return (settings.get("owncast_url") or config.OWNCAST_URL or "").rstrip("/")


# ----------------------------------------------------------------- public API

@route("GET", "/api/health")
def health(h, st):
    return {"ok": True, "liquidsoap": st.liq_ok}


@route("GET", "/api/nowplaying")
def nowplaying(h, st):
    h.cache_seconds = 3
    return st.nowplaying


@route("GET", "/api/site")
def site(h, st):
    conn = st.conn()
    settings = db.all_settings(conn)
    h.cache_seconds = 15
    result = {k: settings.get(k) for k in PUBLIC_SETTINGS}
    result["default_art"] = default_art(settings)
    result["owncast_url"] = owncast_url(settings)
    result["texts"] = branding.texts(settings)
    result["images"] = {k: v["url"] for k, v in branding.images(settings).items()}
    result["custom_images"] = [k for k, v in branding.images(settings).items() if v["custom"]]
    result["theme"] = branding.theme(settings)
    result["custom_js"] = bool((settings.get("custom_js") or "").strip())
    result["seo"] = pages.seo(settings)
    counts = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT lower(artist)), COUNT(DISTINCT lower(genre)) "
        f"FROM tracks t WHERE {director.PLAYABLE}").fetchone()
    result["library"] = {"tracks": counts[0], "artists": counts[1], "genres": counts[2]}
    return result


@route("GET", "/api/schedule")
def schedule(h, st):
    conn = st.conn()
    shows = db.rows(conn.execute(
        "SELECT id, name, host, description, days, start_time, end_time, is_live "
        "FROM shows WHERE enabled = 1 ORDER BY start_time"))
    h.cache_seconds = 30
    return {"timezone": config.TZ, "upcoming": director.upcoming(conn, limit=12), "shows": shows}


@route("GET", "/api/search")
def search(h, st):
    q = (h.query.get("q", [""])[0] or "").strip()
    conn = st.conn()
    if len(q) < 2:
        return {"results": []}
    like = "%" + q.replace("%", "").replace("_", " ") + "%"
    rows = conn.execute(
        f"SELECT * FROM tracks t WHERE {director.PLAYABLE} AND "
        "(t.artist LIKE ? OR t.title LIKE ? OR t.album LIKE ?) "
        "ORDER BY t.artist, t.title LIMIT 30", (like, like, like))
    return {"results": [track_public(r) for r in rows]}


@route("POST", "/api/request")
def request_track(h, st):
    body = h.json_body()
    try:
        track_id = int(body.get("track_id"))
    except (TypeError, ValueError):
        raise ApiError(400, "Choose a track first.")
    try:
        track = director.request_track(st.conn(), track_id, st.client_id(h.client_address_real()))
    except director.RequestRefused as refused:
        raise ApiError(409, str(refused))
    return {"ok": True, "message": f"Requested: {track['artist']} – {track['title']}"}


_video_cache = {"at": 0.0, "data": None}


@route("GET", "/api/video-status")
def video_status(h, st):
    """Owncast status, fetched server-side (no browser CORS issues), cached 15 s."""
    settings = db.all_settings(st.conn())
    base = owncast_url(settings)
    h.cache_seconds = 10
    info = {"enabled": bool(settings.get("live_video_enabled", True)) and bool(base),
            "url": base, "online": False, "viewers": 0, "title": "",
            "embed_video": f"{base}/embed/video" if base else "",
            "embed_chat": f"{base}/embed/chat/readwrite" if base else ""}
    if not info["enabled"]:
        return info
    now = time.time()
    if _video_cache["data"] is None or now - _video_cache["at"] > 15 or _video_cache["data"].get("url") != base:
        status = {}
        try:
            req = urllib.request.Request(f"{base}/api/status", headers={"User-Agent": "AirStation/2"})
            with urllib.request.urlopen(req, timeout=4) as r:
                status = json.loads(r.read().decode("utf-8", "replace"))
        except (OSError, ValueError):
            status = {"_error": True}
        _video_cache.update(at=now, data={
            "url": base, "online": bool(status.get("online")),
            "viewers": int(status.get("viewerCount") or 0),
            "title": str(status.get("streamTitle") or ""),
            "reachable": not status.get("_error")})
    info.update({k: v for k, v in _video_cache["data"].items() if k != "url"})
    return info


_forum_cache = {"at": 0.0, "data": None}


@route("GET", "/api/community/latest")
def community_latest(h, st):
    """Latest forum topics (NodeBB read API over the Docker network), cached 60 s."""
    h.cache_seconds = 30
    internal = config.env("AIR_FORUM_INTERNAL_URL", "")
    public = f"https://{config.env('AIR_FORUM_DOMAIN', '')}".rstrip("/")
    now = time.time()
    if _forum_cache["data"] is not None and now - _forum_cache["at"] < 60:
        return _forum_cache["data"]
    posts = []
    if internal:
        try:
            req = urllib.request.Request(f"{internal}/api/recent",
                                         headers={"Accept": "application/json", "User-Agent": "AirStation/2"})
            with urllib.request.urlopen(req, timeout=4) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            for topic in (data.get("topics") or [])[:6]:
                replies = max(0, int(topic.get("postcount") or 1) - 1)
                posts.append({
                    "title": str(topic.get("title") or "")[:140],
                    "url": f"{public}/topic/{topic.get('slug')}",
                    "category": str((topic.get("category") or {}).get("name") or ""),
                    "user": str((topic.get("user") or {}).get("username") or ""),
                    "replies": replies,
                    "time": str(topic.get("lastposttimeISO") or topic.get("timestampISO") or ""),
                })
        except (OSError, ValueError, TypeError):
            posts = []
    result = {"available": bool(posts), "forum_url": public if internal else "", "posts": posts}
    _forum_cache.update(at=now, data=result)
    return result


# --------------------------------------------------------------- internal API

@route("GET", "/internal/next")
def internal_next(h, st):
    if not st.internal_token or not hmac.compare_digest(
            h.headers.get("X-Air-Token", ""), st.internal_token):
        raise ApiError(403, "forbidden")
    track = director.next_track(st.conn())
    h.plain = True
    if track:
        log(f"AutoDJ next: #{track['id']} {track['artist']} - {track['title']}")
        return director.liquidsoap_uri(track)
    rel = emergency.pick()
    if rel:
        log(f"AutoDJ next (emergency folder): {rel}")
        return emergency.liquidsoap_uri(rel)
    log("AutoDJ: nothing to play (no cached library tracks, emergency folder empty or unreadable)")
    return ""


# ------------------------------------------------------------------ admin API

@route("POST", "/api/admin/login")
def login(h, st):
    client = h.client_address_real()
    now = time.time()
    attempts = [t for t in st.login_attempts.get(client, []) if now - t < 900]
    if len(attempts) >= 10:
        raise ApiError(429, "Too many attempts. Wait 15 minutes.")
    password = str(h.json_body().get("password", ""))
    if not hmac.compare_digest(password.encode(), st.admin_password().encode()):
        attempts.append(now)
        st.login_attempts[client] = attempts
        time.sleep(1)
        raise ApiError(401, "Wrong password.")
    st.login_attempts.pop(client, None)
    h.set_cookie = (f"{SESSION_COOKIE}={st.make_session()}; Path=/api/admin; HttpOnly; "
                    f"Secure; SameSite=Strict; Max-Age={SESSION_SECONDS}")
    return {"ok": True}


@route("POST", "/api/admin/logout")
def logout(h, st):
    h.set_cookie = f"{SESSION_COOKIE}=; Path=/api/admin; HttpOnly; Secure; SameSite=Strict; Max-Age=0"
    return {"ok": True}


@route("GET", "/api/admin/me", admin=True)
def me(h, st):
    s = licensing.state(st.conn(), use_cache=True)
    return {"ok": True, "licence": {k: s.get(k) for k in ("status", "locked", "days_left", "message")}}


# ------------------------------------------------------------------ licence
@route("GET", "/api/admin/licence", admin=True)
def admin_licence(h, st):
    return licensing.state(st.conn())


@route("POST", "/api/admin/licence/activate", admin=True)
def admin_licence_activate(h, st):
    body = h.json_body()
    try:
        if str(body.get("certificate") or "").strip():
            licensing.use_certificate(st.conn(), body["certificate"])
        else:
            licensing.activate(st.conn(), body.get("key", ""))
    except licensing.LicenceRefused as refused:
        raise ApiError(400, str(refused))
    except ConnectionError as error:
        raise ApiError(502, str(error))
    return licensing.state(st.conn())


@route("POST", "/api/admin/licence/deactivate", admin=True)
def admin_licence_deactivate(h, st):
    try:
        licensing.deactivate(st.conn())
    except licensing.LicenceRefused as refused:
        raise ApiError(400, str(refused))
    except ConnectionError as error:
        raise ApiError(502, str(error) + " Nothing was changed.")
    return licensing.state(st.conn())


@route("POST", "/api/admin/licence/check", admin=True)
def admin_licence_check(h, st):
    result = licensing.checkin(st.conn(), force=True)
    return dict(licensing.state(st.conn()), result=result)


@route("GET", "/api/admin/status", admin=True)
def admin_status(h, st):
    conn = st.conn()
    settings = db.all_settings(conn)
    counts = conn.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(cached),0) AS cached, "
        "COALESCE(SUM(missing),0) AS missing, COALESCE(SUM(bad),0) AS bad, "
        "COALESCE(SUM(1 - enabled),0) AS disabled FROM tracks").fetchone()
    try:
        usage = shutil.disk_usage(config.DB_PATH.parent)
        disk = {"total": usage.total, "free": usage.free}
    except OSError:
        disk = {}
    return {
        "nowplaying": st.nowplaying, "liquidsoap": st.liq if st.liq_ok else None,
        "liquidsoap_error": st.liq_error, "listeners": st.listeners,
        "sync": settings.get("_sync") or {}, "sync_request": settings.get("_sync_request") or {},
        "library": dict(counts), "cached_bytes": director.cached_bytes(conn),
        "emergency": emergency.status(),
        "cache_max_bytes": config.CACHE_MAX_BYTES, "disk": disk,
        "record_mode": settings.get("record_mode"), "live_enabled": settings.get("live_enabled"),
        "playlist_now": director.playlist_for_time(conn, time.time()),
        "show_now": director.current_show(conn),
    }


@route("POST", "/api/admin/skip", admin=True)
def admin_skip(h, st):
    try:
        return {"ok": True, "reply": liquidsoap.command("air.skip")}
    except liquidsoap.LiquidsoapError as error:
        raise ApiError(502, str(error))


def _desk(st, command):
    """Send a studio desk command and reply with Liquidsoap's fresh state."""
    try:
        liquidsoap.command(command)
    except liquidsoap.LiquidsoapError as error:
        raise ApiError(502, str(error))
    st.poll_liquidsoap()
    return {"ok": True, "liquidsoap": st.liq if st.liq_ok else None}


@route("POST", "/api/admin/volume", admin=True)
def admin_volume(h, st):
    """Level of the music under the microphone, 0 to 100."""
    value = h.json_body().get("volume")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ApiError(400, "volume must be a number from 0 to 100")
    return _desk(st, f"air.volume {int(round(value))}")


@route("POST", "/api/admin/pause", admin=True)
def admin_pause(h, st):
    """Talk break: AutoDJ stops when the current song ends, until switched off."""
    return _desk(st, f"air.pause {'on' if h.json_body().get('paused') else 'off'}")


@route("POST", "/api/admin/mic", admin=True)
def admin_mic(h, st):
    """One-time token for the browser microphone (harbor mount /mic, reached through
    Caddy at MIC_SOCKET). Each call replaces the previous token."""
    token = secrets.token_hex(24)
    reply = _desk(st, f"air.mic_token {token}")
    return dict(reply, token=token, socket=MIC_SOCKET, login="source")


@route("POST", "/api/admin/live", admin=True)
def admin_live(h, st):
    enabled = bool(h.json_body().get("enabled"))
    conn = st.conn()
    db.set_setting(conn, "live_enabled", enabled)
    conn.commit()
    st.enforce()
    return {"ok": True, "live_enabled": enabled}


@route("POST", "/api/admin/record", admin=True)
def admin_record(h, st):
    mode = h.json_body().get("mode")
    if mode not in RECORD_MODES:
        raise ApiError(400, "mode must be off, live or all")
    conn = st.conn()
    db.set_setting(conn, "record_mode", mode)
    conn.commit()
    st.enforce()
    return {"ok": True, "record_mode": mode}


@route("POST", "/api/admin/sync", admin=True)
def admin_sync(h, st):
    conn = st.conn()
    db.set_setting(conn, "_sync_request", {"index": True, "at": int(time.time())})
    conn.commit()
    return {"ok": True, "message": "A full rescan will start within 5 minutes."}


@route("GET", "/api/admin/tracks", admin=True)
def admin_tracks(h, st):
    q = h.query
    conditions, params = ["1"], []
    text = (q.get("q", [""])[0] or "").strip()
    if text:
        like = f"%{text}%"
        conditions.append("(artist LIKE ? OR title LIKE ? OR album LIKE ? OR relpath LIKE ?)")
        params += [like] * 4
    folder = (q.get("folder", [""])[0] or "").strip("/")
    if folder:
        conditions.append("(folder = ? OR folder LIKE ?)")
        params += [folder, folder + "/%"]
    status = q.get("status", [""])[0]
    status_sql = {"cached": "cached = 1", "missing": "missing = 1", "bad": "bad = 1",
                  "disabled": "enabled = 0", "never": "last_played IS NULL AND missing = 0"}
    if status in status_sql:
        conditions.append(status_sql[status])
    playlist_id = q.get("playlist", [""])[0]
    if playlist_id.isdigit():
        conditions.append("id IN (SELECT track_id FROM playlist_tracks WHERE playlist_id = ?)")
        params.append(int(playlist_id))
    page = max(0, int(q.get("page", ["0"])[0] or 0))
    where = " AND ".join(conditions)
    conn = st.conn()
    total = conn.execute(f"SELECT COUNT(*) FROM tracks WHERE {where}", params).fetchone()[0]
    rows = db.rows(conn.execute(
        f"SELECT id, relpath, folder, artist, title, album, genre, year, duration, enabled, "
        f"missing, bad, cached, last_played, play_count, has_art FROM tracks WHERE {where} "
        f"ORDER BY folder, artist, title LIMIT 100 OFFSET ?", params + [page * 100]))
    return {"total": total, "page": page, "tracks": rows}


@route("PATCH", r"/api/admin/tracks/(\d+)", admin=True)
def admin_track_update(h, st, track_id):
    body = h.json_body()
    conn = st.conn()
    fields = {}
    if "enabled" in body:
        fields["enabled"] = 1 if body["enabled"] else 0
    for key in ("artist", "title", "album", "genre"):
        if key in body:
            fields[key] = str(body[key])[:300]
    if not fields:
        raise ApiError(400, "nothing to update")
    conn.execute(f"UPDATE tracks SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?",
                 list(fields.values()) + [int(track_id)])
    if fields.get("enabled") == 0:
        conn.execute("DELETE FROM queue WHERE track_id = ?", (int(track_id),))
    conn.commit()
    return {"ok": True}


@route("GET", "/api/admin/folders", admin=True)
def admin_folders(h, st):
    rows = st.conn().execute(
        "SELECT folder, COUNT(*) AS tracks FROM tracks WHERE missing = 0 GROUP BY folder "
        "ORDER BY folder")
    totals = {}
    for row in rows:
        parts = row["folder"].split("/") if row["folder"] else []
        for depth in range(0, len(parts) + 1):
            key = "/".join(parts[:depth])
            totals[key] = totals.get(key, 0) + row["tracks"]
    return {"folders": [{"folder": k, "tracks": v} for k, v in sorted(totals.items())]}


@route("GET", "/api/admin/genres", admin=True)
def admin_genres(h, st):
    rows = st.conn().execute(
        "SELECT genre, COUNT(*) AS tracks FROM tracks WHERE missing = 0 AND genre != '' "
        "GROUP BY lower(genre) ORDER BY tracks DESC LIMIT 200")
    return {"genres": db.rows(rows)}


@route("GET", "/api/admin/queue", admin=True)
def admin_queue(h, st):
    items = director.queue_items(st.conn())
    return {"queue": [{"queue_id": i["queue_id"], "reason": i["reason"], "id": i["id"],
                       "artist": i["artist"], "title": i["title"], "cached": i["cached"],
                       "duration": i["duration"]} for i in items]}


@route("POST", "/api/admin/queue", admin=True)
def admin_queue_add(h, st):
    try:
        track_id = int(h.json_body().get("track_id"))
    except (TypeError, ValueError):
        raise ApiError(400, "track_id required")
    director.enqueue(st.conn(), track_id, "manual", note="admin")
    return {"ok": True}


@route("DELETE", r"/api/admin/queue/(\d+)", admin=True)
def admin_queue_delete(h, st, queue_id):
    conn = st.conn()
    conn.execute("DELETE FROM queue WHERE id = ?", (int(queue_id),))
    conn.commit()
    return {"ok": True}


def _playlist_payload(body, existing=None):
    data = dict(existing or {})
    for key in ("name", "kind"):
        if key in body:
            data[key] = str(body[key]).strip()
    if "rule" in body:
        rule = body["rule"]
        if not isinstance(rule, list):
            raise ApiError(400, "rule must be a list")
        data["rule"] = json.dumps([str(r).strip() for r in rule if str(r).strip()])
    for key in ("enabled", "is_default"):
        if key in body:
            data[key] = 1 if body[key] else 0
    for key, low, high in (("no_repeat_hours", 0, 24 * 60), ("artist_gap", 0, 50)):
        if key in body:
            try:
                data[key] = max(low, min(high, int(body[key])))
            except (TypeError, ValueError):
                raise ApiError(400, f"{key} must be a number")
    if not data.get("name"):
        raise ApiError(400, "Playlist name is required.")
    if data.get("kind") not in ("all", "folders", "genres", "manual"):
        raise ApiError(400, "kind must be all, folders, genres or manual")
    return data


@route("GET", "/api/admin/playlists", admin=True)
def admin_playlists(h, st):
    conn = st.conn()
    result = []
    for row in db.rows(conn.execute("SELECT * FROM playlists ORDER BY is_default DESC, name")):
        row["rule"] = json.loads(row["rule"] or "[]")
        row.update(director.playlist_count(conn, row))
        result.append(row)
    return {"playlists": result}


@route("POST", "/api/admin/playlists", admin=True)
def admin_playlist_create(h, st):
    data = _playlist_payload(h.json_body())
    conn = st.conn()
    data.setdefault("rule", "[]")
    try:
        cur = conn.execute(
            "INSERT INTO playlists (name, kind, rule, enabled, is_default, no_repeat_hours, "
            "artist_gap, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (data["name"], data["kind"], data["rule"], data.get("enabled", 1), 0,
             data.get("no_repeat_hours", 24), data.get("artist_gap", 4), int(time.time())))
    except Exception as error:
        raise ApiError(409, f"Could not create playlist: {error}")
    if data.get("is_default"):
        _make_default(conn, cur.lastrowid)
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


def _make_default(conn, playlist_id):
    conn.execute("UPDATE playlists SET is_default = (id = ?)", (playlist_id,))


@route("PUT", r"/api/admin/playlists/(\d+)", admin=True)
def admin_playlist_update(h, st, playlist_id):
    conn = st.conn()
    row = conn.execute("SELECT * FROM playlists WHERE id = ?", (int(playlist_id),)).fetchone()
    if not row:
        raise ApiError(404, "No such playlist")
    body = h.json_body()
    data = _playlist_payload(body, dict(row))
    if row["is_default"] and not data.get("enabled", 1):
        raise ApiError(400, "The default playlist cannot be disabled.")
    conn.execute("UPDATE playlists SET name=?, kind=?, rule=?, enabled=?, no_repeat_hours=?, "
                 "artist_gap=? WHERE id=?",
                 (data["name"], data["kind"], data["rule"], data["enabled"],
                  data["no_repeat_hours"], data["artist_gap"], int(playlist_id)))
    if body.get("is_default"):
        _make_default(conn, int(playlist_id))
    # Re-plan from the new rules.
    conn.execute("DELETE FROM queue WHERE reason = 'auto' AND playlist_id = ?", (int(playlist_id),))
    conn.commit()
    return {"ok": True}


@route("DELETE", r"/api/admin/playlists/(\d+)", admin=True)
def admin_playlist_delete(h, st, playlist_id):
    conn = st.conn()
    row = conn.execute("SELECT is_default FROM playlists WHERE id = ?", (int(playlist_id),)).fetchone()
    if not row:
        raise ApiError(404, "No such playlist")
    if row["is_default"]:
        raise ApiError(400, "Choose another default playlist before deleting this one.")
    conn.execute("DELETE FROM queue WHERE playlist_id = ?", (int(playlist_id),))
    conn.execute("DELETE FROM playlists WHERE id = ?", (int(playlist_id),))
    conn.commit()
    return {"ok": True}


@route("POST", r"/api/admin/playlists/(\d+)/tracks", admin=True)
def admin_playlist_tracks(h, st, playlist_id):
    body = h.json_body()
    ids = [int(i) for i in body.get("track_ids", []) if str(i).isdigit()]
    conn = st.conn()
    if body.get("action") == "remove":
        conn.executemany("DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
                         [(int(playlist_id), i) for i in ids])
    else:
        conn.executemany("INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, position) "
                         "VALUES (?, ?, 0)", [(int(playlist_id), i) for i in ids])
    conn.commit()
    return {"ok": True, "count": len(ids)}


def _show_payload(body):
    name = str(body.get("name", "")).strip()
    if not name:
        raise ApiError(400, "Show name is required.")
    for key in ("start_time", "end_time"):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(body.get(key, ""))):
            raise ApiError(400, f"{key} must look like 18:00")
    days = body.get("days", [])
    if isinstance(days, str):
        days = days.split(",")
    days = sorted({int(d) for d in days if str(d).strip().isdigit() and 0 <= int(d) <= 6})
    if not days:
        raise ApiError(400, "Choose at least one day.")
    playlist_id = body.get("playlist_id")
    playlist_id = int(playlist_id) if str(playlist_id or "").isdigit() else None
    return (name, str(body.get("host", ""))[:200], str(body.get("description", ""))[:500],
            ",".join(map(str, days)), body["start_time"], body["end_time"], playlist_id,
            1 if body.get("is_live") else 0, 0 if body.get("enabled") is False else 1)


@route("GET", "/api/admin/shows", admin=True)
def admin_shows(h, st):
    return {"shows": db.rows(st.conn().execute("SELECT * FROM shows ORDER BY start_time")),
            "timezone": config.TZ}


@route("POST", "/api/admin/shows", admin=True)
def admin_show_create(h, st):
    values = _show_payload(h.json_body())
    conn = st.conn()
    cur = conn.execute("INSERT INTO shows (name, host, description, days, start_time, end_time, "
                       "playlist_id, is_live, enabled) VALUES (?,?,?,?,?,?,?,?,?)", values)
    conn.execute("DELETE FROM queue WHERE reason = 'auto'")
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


@route("PUT", r"/api/admin/shows/(\d+)", admin=True)
def admin_show_update(h, st, show_id):
    values = _show_payload(h.json_body())
    conn = st.conn()
    conn.execute("UPDATE shows SET name=?, host=?, description=?, days=?, start_time=?, "
                 "end_time=?, playlist_id=?, is_live=?, enabled=? WHERE id=?",
                 values + (int(show_id),))
    conn.execute("DELETE FROM queue WHERE reason = 'auto'")
    conn.commit()
    return {"ok": True}


@route("DELETE", r"/api/admin/shows/(\d+)", admin=True)
def admin_show_delete(h, st, show_id):
    conn = st.conn()
    conn.execute("DELETE FROM shows WHERE id = ?", (int(show_id),))
    conn.execute("DELETE FROM queue WHERE reason = 'auto'")
    conn.commit()
    return {"ok": True}


EDITABLE_SETTINGS = {
    "station_name": str, "tagline": str, "about": str, "genres_banner": list,
    "rotation_name": str, "rotation_description": str, "requests_enabled": bool,
    "request_gap_minutes": int, "request_repeat_hours": int, "social": dict,
    "community": dict, "backup_stream_url": str, "owncast_url": str,
    "live_video_enabled": bool, "stats_enabled": bool, "stats_retention_days": int,
    "map_style": str, "map_carto_key": str, "map_tiles_url": str, "map_attribution": str,
}


@route("GET", "/api/admin/settings", admin=True)
def admin_settings(h, st):
    conn = st.conn()
    settings = db.all_settings(conn)
    result = {k: settings.get(k) for k in EDITABLE_SETTINGS}
    result["default_art"] = default_art(settings)
    result["owncast_url"] = settings.get("owncast_url") or config.OWNCAST_URL
    counts = conn.execute("SELECT COALESCE(SUM(cached), 0), COALESCE(SUM(cached * has_art), 0) "
                          "FROM tracks").fetchone()
    files = art_files()
    missing = sum(1 for r in conn.execute("SELECT id FROM tracks WHERE has_art = 1 AND cached = 1")
                  if r["id"] not in files)
    saved = settings.get("default_art") or ""
    result["_art_stats"] = {"cached": counts[0], "with_art": counts[1] - missing,
                            "missing_files": missing,
                            "default_missing": bool(saved) and result["default_art"] != saved}
    return result


@route("PUT", "/api/admin/settings", admin=True)
def admin_settings_update(h, st):
    body = h.json_body()
    conn = st.conn()
    for key, kind in EDITABLE_SETTINGS.items():
        if key not in body:
            continue
        value = body[key]
        try:
            value = kind(value) if kind is not bool else bool(value)
        except (TypeError, ValueError):
            raise ApiError(400, f"{key} has the wrong type")
        if key == "owncast_url":
            value = value.strip().rstrip("/")
            if value and not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?(/[\w./-]*)?", value):
                raise ApiError(400, "Owncast address must look like https://cast.example.com")
        if key == "map_style" and value not in (*stats.MAP_STYLES, "custom"):
            raise ApiError(400, "Unknown map style")
        if key == "map_carto_key":
            value = value.strip()
            if value and not re.fullmatch(r"[^\s\"'<>]{8,300}", value):
                raise ApiError(400, "That doesn't look like a CARTO key (copy it from the e-mail CARTO sent)")
        if key == "map_tiles_url":
            value = value.strip()
            if value and not stats.valid_tile_url(value):
                raise ApiError(400, "Tile address must start with https:// and contain {z}, {x} and {y}")
        if key == "map_attribution":
            value = value.strip()[:300]
        db.set_setting(conn, key, value)
    conn.commit()
    return {"ok": True}


@route("POST", "/api/admin/default-art", admin=True)
def admin_default_art_upload(h, st):
    return admin_brand_upload(h, st, "cover")


@route("DELETE", "/api/admin/default-art", admin=True)
def admin_default_art_reset(h, st):
    return admin_brand_reset(h, st, "cover")


# ---------------------------------------------------------------- appearance

@route("GET", "/api/brand/([a-z_]+)")
def brand_image(h, st, slot):
    """Stable address for each image (CSS, favicon, link previews) -> current file."""
    if slot not in branding.SLOTS:
        raise ApiError(404, "Unknown image")
    target = branding.url(db.all_settings(st.conn()), slot)
    if not target:
        raise ApiError(404, "No image set")
    return Raw(b"", status=302, headers={"Location": target}, cache="public, max-age=60")


def _etagged(h, body, content_type):
    tag = '"' + hashlib.sha1(body.encode()).hexdigest()[:20] + '"'
    if h.headers.get("If-None-Match") == tag:
        return Raw(b"", content_type, status=304, headers={"ETag": tag})
    return Raw(body, content_type, headers={"ETag": tag})


@route("POST", "/api/hit")
def page_hit(h, st):
    """Cookieless page-view beacon from brand.js (website visits in Studio > Listeners)."""
    h.max_body = 4096
    try:
        body = h.json_body()
    except ApiError:
        return {"ok": False}
    stored = stats.record_hit(st.conn(), h.client_address_real(), body.get("p"), body.get("r"),
                              h.headers.get("User-Agent", ""), config.DOMAIN)
    return {"ok": stored}


def _tz_offset():
    try:
        from zoneinfo import ZoneInfo
        import datetime as _dt
        return int(_dt.datetime.now(ZoneInfo(config.TZ)).utcoffset().total_seconds())
    except Exception:  # unknown zone: plain UTC buckets
        return 0


@route("GET", "/api/admin/stats", admin=True)
def admin_stats(h, st):
    report = stats.report(st.conn(), h.query.get("range", ["7d"])[0], tz_offset=_tz_offset())
    report["map"] = stats.map_config(db.all_settings(st.conn()), config.env("AIR_MAP_TILES") or "",
                                     config.env("AIR_MAP_ATTRIBUTION") or "")
    report["timezone"] = config.TZ
    return report


@route("GET", "/api/admin/stats/sessions.csv", admin=True)
def admin_stats_csv(h, st):
    range_key = h.query.get("range", ["30d"])[0]
    body = stats.sessions_csv(st.conn(), range_key)
    return Raw(body, "text/csv; charset=utf-8", cache="no-store", headers={
        "Content-Disposition": f'attachment; filename="listeners-{range_key}.csv"'})


@route("GET", "/api/manifest.webmanifest")
def web_manifest(h, st):
    settings = db.all_settings(st.conn())
    name = settings.get("station_name") or "AirStation"
    body = json.dumps({"name": name, "short_name": name[:24], "start_url": "/",
                       "display": "standalone", "background_color": "#07090d",
                       "theme_color": "#07090d",
                       "icons": [{"src": "/api/brand/icon", "sizes": "512x512", "type": "image/png"}]})
    return Raw(body, "application/manifest+json", cache="public, max-age=300")


@route("GET", "/api/custom.css")
def custom_css(h, st):
    return _etagged(h, branding.custom_css(db.all_settings(st.conn())), "text/css; charset=utf-8")


@route("GET", "/api/custom.js")
def custom_js(h, st):
    return _etagged(h, branding.custom_js(db.all_settings(st.conn())),
                    "application/javascript; charset=utf-8")


@route("GET", "/api/admin/appearance", admin=True)
def admin_appearance(h, st):
    settings = db.all_settings(st.conn())
    current = branding.images(settings)
    slots = []
    for key, spec in branding.SLOTS.items():
        slots.append({"key": key, "title": spec["title"], "where": spec["where"],
                      "size": spec["size"], "tip": spec["tip"], "max_mb": spec["max_mb"],
                      "min": "{}×{}".format(*spec["min"]),
                      "formats": ["JPG" if f == "jpeg" else f.upper() for f in spec["formats"]],
                      "accept": ",".join(branding.MIME[f] for f in spec["formats"]),
                      "url": current[key]["url"], "custom": current[key]["custom"]})
    saved = settings.get("texts") or {}
    texts = [{"key": k, "label": label, "default": default, "multiline": multi, "hint": hint,
              "value": saved.get(k, "")} for k, label, default, multi, hint in branding.TEXTS]
    return {"images": slots, "texts": texts, "theme": branding.theme(settings),
            "theme_defaults": branding.THEME_DEFAULTS,
            "custom_css": settings.get("custom_css") or "",
            "custom_js": settings.get("custom_js") or ""}


@route("PUT", "/api/admin/appearance", admin=True)
def admin_appearance_update(h, st):
    h.max_body = 512 * 1024
    body = h.json_body()
    conn = st.conn()
    try:
        if "texts" in body:
            db.set_setting(conn, "texts", branding.clean_texts(body["texts"]))
        if "theme" in body:
            db.set_setting(conn, "theme", branding.clean_theme(body["theme"]))
        if "custom_css" in body:
            db.set_setting(conn, "custom_css", branding.clean_code(body["custom_css"], "CSS"))
        if "custom_js" in body:
            db.set_setting(conn, "custom_js", branding.clean_code(body["custom_js"], "JS"))
    except branding.BrandError as error:
        raise ApiError(400, str(error))
    conn.commit()
    return {"ok": True}


@route("POST", "/api/admin/brand/([a-z_]+)", admin=True)
def admin_brand_upload(h, st, slot):
    if slot not in branding.SLOTS:
        raise ApiError(404, "Unknown image")
    h.max_body = 4 * 1024 * 1024
    body = h.json_body()
    try:
        result = branding.save(st.conn(), slot, branding.decode_upload(body.get("data")))
    except branding.BrandError as error:
        raise ApiError(400, str(error))
    if slot == "cover":
        st.refresh_nowplaying()
    return {"ok": True, **result}


@route("DELETE", "/api/admin/brand/([a-z_]+)", admin=True)
def admin_brand_reset(h, st, slot):
    if slot not in branding.SLOTS:
        raise ApiError(404, "Unknown image")
    return {"ok": True, **branding.reset(st.conn(), slot)}


def art_files():
    try:
        return {int(p.stem) for p in config.ART_DIR.glob("*.jpg") if p.stem.isdigit()}
    except OSError:
        return set()


@route("POST", "/api/admin/art-retry", admin=True)
def admin_art_retry(h, st):
    conn = st.conn()
    files = art_files()
    lost = [r["id"] for r in conn.execute("SELECT id FROM tracks WHERE has_art = 1")
            if r["id"] not in files]
    for i in range(0, len(lost), 500):
        ids = lost[i:i + 500]
        conn.execute(f"UPDATE tracks SET has_art = 0 WHERE id IN ({','.join('?' * len(ids))})", ids)
    count = conn.execute("UPDATE tracks SET art_checked = 0 WHERE has_art = 0 AND cached = 1").rowcount
    db.set_setting(conn, "_emergency_art", {})
    conn.commit()
    return {"ok": True, "message": f"{count} cached tracks without art will be re-checked by the next sync runs (200 per run, every 5 minutes)."}


@route("GET", "/api/admin/duplicates", admin=True)
def admin_duplicates(h, st):
    include = h.query.get("resolved", ["0"])[0] == "1"
    return duplicates.find(st.conn(), include_resolved=include)


@route("POST", "/api/admin/duplicates/resolve", admin=True)
def admin_duplicates_resolve(h, st):
    body = h.json_body()
    try:
        keep = int(body.get("keep"))
        disable = [int(i) for i in body.get("disable", [])]
    except (TypeError, ValueError):
        raise ApiError(400, "keep and disable are required")
    return {"ok": True, "disabled": duplicates.resolve(st.conn(), keep, disable)}


@route("GET", "/api/admin/history", admin=True)
def admin_history(h, st):
    limit = min(500, max(1, int(h.query.get("limit", ["100"])[0] or 100)))
    return {"history": db.rows(st.conn().execute(
        "SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)))}


def hls_running(max_age=30):
    try:
        return time.time() - (config.HLS_DIR / "radio.m3u8").stat().st_mtime < max_age
    except OSError:
        return False


@route("GET", r"/radio\.m3u8")
def hls_playlist(h, st):
    """HLS playlist (AAC) for IPTV apps and playlists; segments are served by Caddy."""
    try:
        body = (config.HLS_DIR / "radio.m3u8").read_bytes()
    except OSError:
        raise ApiError(503, "The HLS stream is starting. Try again in a minute.")
    stats.hls_seen(h.client_address_real(), h.headers.get("User-Agent", ""))
    return Raw(body, "application/vnd.apple.mpegurl", cache="no-cache",
               headers={"Access-Control-Allow-Origin": "*"})


@route("GET", "/api/admin/connections", admin=True)
def admin_connections(h, st):
    """Connection details for Mixxx and OBS (shown only to the logged-in admin)."""
    domain = config.DOMAIN
    obs_pass = config.secret("obs_publish_password", default="")
    return {
        "mixxx": {"type": "Icecast 2", "host": domain, "port": 8005, "mount": "/live",
                  "login": "source", "password": config.secret("dj_password", default=""),
                  "format": "MP3 192 kbps, 44.1 kHz stereo"},
        "obs_srt": f"srt://{domain}:8890?streamid=publish:obs:obs:{obs_pass}&pkt_size=1316",
        "obs_rtmp": {"server": f"rtmp://{domain}", "key": f"obs?user=obs&pass={obs_pass}"},
        "streams": {"mp3": f"https://{domain}/radio.mp3", "mp3_low": f"https://{domain}/radio-low.mp3",
                    "hls": f"https://{domain}/radio.m3u8", "hls_running": hls_running(),
                    "logo": f"https://{domain}/api/brand/icon",
                    "station": db.get_setting(st.conn(), "station_name") or "AirStation Radio"},
    }


# ------------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "AirStation/2"
    station = None

    def log_message(self, fmt, *args):
        if os.environ.get("AIR_ACCESS_LOG"):
            super().log_message(fmt, *args)

    def client_address_real(self):
        forwarded = self.headers.get("X-Forwarded-For", "")
        return forwarded.split(",")[0].strip() or self.client_address[0]

    def json_body(self):
        if hasattr(self, "_body"):
            return self._body
        length = int(self.headers.get("Content-Length") or 0)
        if length > getattr(self, "max_body", MAX_BODY):
            raise ApiError(413, "Request too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "Invalid JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "Expected a JSON object")
        self._body = body
        return body

    def is_admin(self):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and self.station.valid_session(value):
                return True
        return False

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        self.query = parse_qs(parsed.query)
        self.cache_seconds = 0
        self.max_body = MAX_BODY
        self.plain = False
        self.set_cookie = None
        path = parsed.path.rstrip("/") or "/"
        status, payload = 200, None
        try:
            for route_method, regex, admin, func in ROUTES:
                match = regex.match(path)
                if not match:
                    continue
                if route_method != method and not (method == "HEAD" and route_method == "GET"):
                    continue
                if admin:
                    if not self.is_admin():
                        raise ApiError(401, "Please log in.")
                    if method != "GET" and self.headers.get("X-Requested-With") != "air-admin":
                        raise ApiError(403, "Missing admin request header.")
                    if not licensing.allowed_when_locked(path) and licensing.locked(self.station.conn()):
                        raise ApiError(402, "The trial has ended. Enter a licence key in Studio > Licence. "
                                            "The station keeps broadcasting.")
                payload = func(self, self.station, *match.groups())
                if admin and method != "GET" and PAGE_INPUTS.match(path):
                    try:                          # the look changed: refresh the finished pages now
                        self.station.write_pages()
                    except Exception:
                        log(f"Writing pages failed:\n{traceback.format_exc()}")
                break
            else:
                raise ApiError(404, "Not found")
        except ApiError as error:
            status, payload = error.status, {"error": error.message}
            self.plain = False
        except Exception:
            log(f"Error handling {method} {path}:\n{traceback.format_exc()}")
            status, payload = 500, {"error": "Server error"}
            self.plain = False
        extra = {}
        cache = (f"public, max-age={self.cache_seconds}" if self.cache_seconds and status == 200
                 else "no-store")
        if isinstance(payload, Raw):
            status, body, content_type = payload.status, payload.body, payload.content_type
            extra, cache = payload.headers, payload.cache
        elif self.plain:
            body = str(payload).encode()
            content_type = "text/plain; charset=utf-8"
        else:
            body = json.dumps(payload, ensure_ascii=False).encode()
            content_type = "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for name, value in extra.items():
            self.send_header(name, value)
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.set_cookie:
            self.send_header("Set-Cookie", self.set_cookie)
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):              # stream checkers often ask with HEAD first
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")


def serve(host="0.0.0.0", port=8080, db_path=None, start_threads=True):
    Handler.station = Station(db_path, start_threads=start_threads)
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    log(f"AirStation API listening on {host}:{port}")
    return server


def main():
    server = serve(port=int(os.environ.get("AIR_API_PORT", "8080")))
    server.serve_forever()


if __name__ == "__main__":
    main()
