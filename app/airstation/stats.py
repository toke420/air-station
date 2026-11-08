"""Listener and website statistics (Studio > Listeners).

Listeners: every 15 s the API asks Icecast for the connected clients
(/admin/listclients). Caddy appends the visitor's real address to the User-Agent it
forwards to Icecast ("[airstation-ip:...]"), because Icecast itself only sees Caddy.
Each connection becomes a session with start, end, duration, player, stream and a
city-level location (see geoip.py).

Website visits: brand.js sends one cookieless beacon per page view to /api/hit.

Privacy: raw IP addresses are never stored. "visitor" is a keyed hash, only used
to count unique people; locations are city level (about 1 km); old rows are deleted
after the retention period (365 days by default).
"""
import base64
import hashlib
import html
import hmac
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from . import config, db, geoip

MOUNTS = ("/radio.mp3", "/radio-low.mp3")          # Icecast mounts
HLS_MOUNT = "/radio.m3u8"                           # HLS: counted from playlist requests
HLS_GONE_AFTER = 30         # seconds without a playlist request = HLS listener left
IP_MARKER = re.compile(r"\s*\[airstation-ip:([0-9A-Fa-f:.]+)\]")
MIN_SESSION = 10            # seconds; shorter connections are probes, not listening
STALE_AFTER = 180           # close sessions we haven't seen for this long
HIT_DEDUPE = 30 * 60        # same visitor + page within 30 min counts once
HIT_LIMIT = 20              # and at most this many different pages per visitor in that time
BOT = re.compile(r"bot|crawl|spider|slurp|curl|wget|python-|go-http|httpclient|"
                 r"headless|monitor|uptime|pingdom|preview|facebookexternalhit", re.I)


# ----------------------------------------------------------------- helpers
def visitor_key(ip):
    secret = (config.secret("session_secret", default="airstation") or "airstation").encode()
    return hmac.new(secret, f"visitor:{ip}".encode(), hashlib.sha256).hexdigest()[:16]


def split_agent(user_agent):
    """(clean user agent, real client IP or None) from what Icecast reports."""
    matches = IP_MARKER.findall(user_agent or "")
    if not matches:
        return (user_agent or "").strip(), None
    # Caddy appends its marker last; anything earlier was sent by the client itself.
    return IP_MARKER.sub("", user_agent).strip(), matches[-1]


def player_name(ua):
    """Short, human name for a player / browser."""
    ua = ua or ""
    apps = (("VLC", "VLC"), ("Lavf", "FFmpeg"), ("ffmpeg", "FFmpeg"), ("AppleCoreMedia", "Apple (iOS/macOS)"),
            ("iTunes", "iTunes"), ("Winamp", "Winamp"), ("foobar", "foobar2000"), ("Sonos", "Sonos"),
            ("Alexa", "Amazon Alexa"), ("TuneIn", "TuneIn"), ("Radio Garden", "Radio Garden"),
            ("MPlayer", "MPlayer"), ("mpv", "mpv"), ("Kodi", "Kodi"), ("XBMC", "Kodi"),
            ("Mixxx", "Mixxx"), ("ExoPlayer", "Android app"), ("Dalvik", "Android app"),
            ("stagefright", "Android"), ("Liquidsoap", "Liquidsoap"))
    for needle, name in apps:
        if needle.lower() in ua.lower():
            return name
    if BOT.search(ua):
        return "Bot / script"
    browser = ("Edge" if "Edg/" in ua else "Opera" if "OPR/" in ua else "Samsung Internet" if "SamsungBrowser" in ua
               else "Firefox" if "Firefox/" in ua else "Chrome" if "Chrome/" in ua or "CriOS" in ua
               else "Safari" if "Safari/" in ua else "")
    system = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua
              else "Windows" if "Windows" in ua else "macOS" if "Mac OS X" in ua else "Linux" if "Linux" in ua
              else "")
    if browser:
        return f"{browser} · {system}" if system else browser
    return system or "Other"


def device_kind(ua):
    return "Mobile" if re.search(r"Mobi|iPhone|Android|iPad", ua or "") else "Desktop"


def _geo(ip):
    geo = geoip.lookup(ip) if ip else None
    return geo or {"country": "", "country_name": "", "region": "", "city": "", "lat": None, "lon": None}


def enabled(conn):
    return bool(db.get_setting(conn, "stats_enabled"))


# --------------------------------------------------------------- listeners
def fetch_clients(mount, icecast_url=None, password=None, timeout=5):
    """[{"id", "ip", "agent", "connected"}] for one mount, or None if Icecast can't say."""
    url = f"{icecast_url or config.ICECAST_URL}/admin/listclients?mount={urllib.parse.quote(mount)}"
    password = password if password is not None else config.secret("admin_password", default="")
    if not password:
        return None
    token = base64.b64encode(f"admin:{password}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}",
                                               "User-Agent": "AirStation stats"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            root = ET.fromstring(r.read())
    except urllib.error.HTTPError as error:
        return [] if error.code in (400, 404) else None    # mount not up = nobody listening
    except (OSError, ET.ParseError):
        return None
    clients = []
    for item in root.iter("listener"):
        def text(tag):
            node = item.find(tag)
            return (node.text or "").strip() if node is not None and node.text else ""
        try:
            connected = int(text("Connected") or 0)
        except ValueError:
            connected = 0
        clients.append({"id": item.get("id") or text("ID"), "ip": text("IP"),
                        "agent": text("UserAgent"), "connected": connected})
    return clients


# ------------------------------------------------------------- HLS listeners
# HLS players fetch /radio.m3u8 every few seconds (the API serves it). Each address +
# player that keeps doing so is one listener; they become sessions like Icecast ones.
_hls = {}
_hls_lock = threading.Lock()


def hls_seen(ip, user_agent="", now=None):
    now = time.time() if now is None else now
    key = hashlib.sha256(f"{ip}|{user_agent}".encode()).hexdigest()[:16]
    with _hls_lock:
        entry = _hls.get(key)
        if entry is None or now - entry["last"] > HLS_GONE_AFTER:
            entry = _hls[key] = {"first": now, "ip": ip, "agent": (user_agent or "")[:300]}
        entry["last"] = now
        if len(_hls) > 20000:                     # never grow without bound
            for k in [k for k, v in _hls.items() if now - v["last"] > HLS_GONE_AFTER]:
                del _hls[k]


def hls_clients(now=None):
    """Current HLS listeners in the same shape as fetch_clients()."""
    now = time.time() if now is None else now
    with _hls_lock:
        for key in [k for k, v in _hls.items() if now - v["last"] > HLS_GONE_AFTER]:
            del _hls[key]
        return [{"id": key, "ip": v["ip"], "agent": f"{v['agent']} [airstation-ip:{v['ip']}]",
                 "connected": int(now - v["first"])} for key, v in _hls.items()]


def hls_reset():
    with _hls_lock:
        _hls.clear()


def track(conn, mount, clients, now=None):
    """Update sessions for one mount from its current client list. Returns open count."""
    now = int(now or time.time())
    open_rows = {r["client_key"]: r for r in conn.execute(
        "SELECT * FROM listener_sessions WHERE ended_at IS NULL AND mount = ?", (mount,))}
    seen = set()
    for client in clients:
        key = f"{mount}#{client['id']}"
        started = now - max(0, int(client["connected"]))
        row = open_rows.get(key)
        if row is not None and abs(row["started_at"] - started) > 60:
            _close(conn, row)                   # Icecast restarted and reused the id
            row = None
        if row is not None:
            conn.execute("UPDATE listener_sessions SET last_seen = ?, duration = ? WHERE id = ?",
                         (now, max(row["duration"], now - row["started_at"]), row["id"]))
        else:
            agent, real_ip = split_agent(client["agent"])
            ip = real_ip or client["ip"]
            geo = _geo(ip)
            conn.execute(
                "INSERT INTO listener_sessions (client_key, mount, visitor, internal, player, started_at, "
                "last_seen, duration, country, country_name, region, city, lat, lon) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, mount, visitor_key(ip), 0 if real_ip else 1, player_name(agent), started, now,
                 now - started, geo["country"], geo["country_name"], geo["region"], geo["city"],
                 geo["lat"], geo["lon"]))
        seen.add(key)
    for key, row in open_rows.items():
        if key not in seen:
            _close(conn, row)
    conn.commit()
    return len(seen)


def _close(conn, row):
    conn.execute("UPDATE listener_sessions SET ended_at = ?, duration = ? WHERE id = ?",
                 (row["last_seen"], max(0, row["last_seen"] - row["started_at"]), row["id"]))


def close_stale(conn, now=None):
    now = int(now or time.time())
    for row in conn.execute("SELECT * FROM listener_sessions WHERE ended_at IS NULL AND last_seen < ?",
                            (now - STALE_AFTER,)).fetchall():
        _close(conn, row)
    conn.commit()


def sample(conn, listeners, now=None):
    """Concurrent listeners, kept per minute (the highest value seen in that minute)."""
    minute = int(now or time.time()) // 60 * 60
    conn.execute("INSERT INTO listener_samples (at, listeners) VALUES (?, ?) "
                 "ON CONFLICT(at) DO UPDATE SET listeners = max(listeners, excluded.listeners)",
                 (minute, int(listeners)))
    conn.commit()


def poll(conn, fetch=fetch_clients, now=None):
    """One round: all mounts. Returns the number of public listeners now."""
    if not enabled(conn):
        return 0
    now = int(now or time.time())
    for mount in MOUNTS:
        clients = fetch(mount)
        if clients is not None:
            track(conn, mount, clients, now)
    track(conn, HLS_MOUNT, hls_clients(now), now)
    close_stale(conn, now)
    count = conn.execute("SELECT COUNT(*) FROM listener_sessions WHERE ended_at IS NULL AND internal = 0"
                         ).fetchone()[0]
    sample(conn, count, now)
    return count


def prune(conn, now=None):
    days = min(3650, max(7, int(db.get_setting(conn, "stats_retention_days") or 365)))
    cutoff = int(now or time.time()) - max(7, days) * 86400
    conn.execute("DELETE FROM listener_sessions WHERE ended_at IS NOT NULL AND ended_at < ?", (cutoff,))
    conn.execute("DELETE FROM listener_samples WHERE at < ?", (cutoff,))
    conn.execute("DELETE FROM page_hits WHERE at < ?", (cutoff,))
    conn.commit()


# ---------------------------------------------------------------- visits
def record_hit(conn, ip, page, referrer="", user_agent="", own_host="", now=None):
    """Store one page view (if it's a person and not a repeat). Returns True if stored."""
    if not enabled(conn) or BOT.search(user_agent or "") or not user_agent:
        return False
    now = int(now or time.time())
    page = "/" + str(page or "/").split("?")[0].split("#")[0].strip("/")[:80]
    if not re.fullmatch(r"/[\w./-]*", page):
        return False
    visitor = visitor_key(ip)
    recent = conn.execute("SELECT COUNT(*), SUM(page = ?) FROM page_hits WHERE visitor = ? AND at > ?",
                          (page, visitor, now - HIT_DEDUPE)).fetchone()
    if recent[1] or recent[0] >= HIT_LIMIT:      # a repeat, or someone filling the table
        return False
    host = (urllib.parse.urlparse(str(referrer or "")).hostname or "").lower()[:120]
    if host and own_host and (host == own_host.lower() or host.endswith("." + own_host.lower())):
        host = ""
    geo = _geo(ip)
    conn.execute(
        "INSERT INTO page_hits (at, page, visitor, device, referrer, country, country_name, region, city, lat, lon) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (now, page, visitor, device_kind(user_agent), host, geo["country"], geo["country_name"],
         geo["region"], geo["city"], geo["lat"], geo["lon"]))
    conn.commit()
    return True


# ------------------------------------------------------------- reporting
RANGES = {"24h": (86400, 3600), "7d": (7 * 86400, 6 * 3600), "30d": (30 * 86400, 86400),
          "90d": (90 * 86400, 86400), "365d": (365 * 86400, 7 * 86400)}
LENGTHS = ((60, "Under 1 min"), (300, "1–5 min"), (900, "5–15 min"), (3600, "15–60 min"),
           (3 * 3600, "1–3 hours"), (None, "3 hours +"))


def _rows(cursor):
    return [dict(r) for r in cursor]


def report(conn, range_key="7d", now=None, tz_offset=0):
    """Everything the Listeners tab shows, for the chosen period."""
    now = int(now or time.time())
    span, bucket = RANGES.get(range_key, RANGES["7d"])
    since = now - span
    # Sessions that overlap the period; listening time is clipped to it.
    base = ("FROM listener_sessions WHERE internal = 0 AND duration >= ? "
            "AND COALESCE(ended_at, ?) >= ? AND started_at <= ?")
    args = (MIN_SESSION, now, since, now)
    secs = "(MIN(COALESCE(ended_at, last_seen), ?) - MAX(started_at, ?))"
    totals = conn.execute(
        f"SELECT COUNT(*) AS sessions, COUNT(DISTINCT visitor) AS listeners, "
        f"COALESCE(SUM({secs}), 0) AS seconds, COALESCE(AVG(duration), 0) AS avg_duration, "
        f"COALESCE(MAX(duration), 0) AS longest {base}", (now, since) + args).fetchone()
    peak = conn.execute("SELECT listeners, at FROM listener_samples WHERE at >= ? "
                        "ORDER BY listeners DESC, at DESC LIMIT 1", (since,)).fetchone()

    def grouped(fields, order="seconds DESC", limit=50, extra=""):
        return _rows(conn.execute(
            f"SELECT {fields}, COUNT(*) AS sessions, COUNT(DISTINCT visitor) AS listeners, "
            f"SUM({secs}) AS seconds, AVG(duration) AS avg_duration {base} {extra} "
            f"GROUP BY {fields} ORDER BY {order} LIMIT {int(limit)}", (now, since) + args))

    countries = grouped("country, country_name")
    points = grouped("lat, lon, city, region, country, country_name", limit=1000,
                     extra="AND lat IS NOT NULL")
    players = grouped("player", limit=15)
    mounts = grouped("mount", limit=5)
    lengths = []
    low = 0
    for high, label in LENGTHS:
        cond = "duration >= ?" + (" AND duration < ?" if high else "")
        params = (MIN_SESSION, now, since, now, max(low, MIN_SESSION)) + ((high,) if high else ())
        count = conn.execute(f"SELECT COUNT(*) {base} AND {cond}", params).fetchone()[0]
        lengths.append({"label": label, "sessions": count})
        low = high or low
    # Concurrent listeners over time: highest and average per bucket.
    timeline = []
    rows = {r["b"]: r for r in conn.execute(
        "SELECT (at + ?) / ? AS b, MAX(listeners) AS peak, AVG(listeners) AS avg "
        "FROM listener_samples WHERE at >= ? GROUP BY b", (tz_offset, bucket, since))}
    first = (since + tz_offset) // bucket
    last = (now + tz_offset) // bucket
    for b in range(first, last + 1):
        r = rows.get(b)
        timeline.append({"at": b * bucket - tz_offset, "peak": r["peak"] if r else 0,
                         "avg": round(r["avg"], 2) if r else 0})
    live = _rows(conn.execute(
        "SELECT mount, player, started_at, country, country_name, region, city, lat, lon "
        "FROM listener_sessions WHERE ended_at IS NULL AND internal = 0 ORDER BY started_at"))
    for item in live:
        item["seconds"] = now - item.pop("started_at")
    # Website visits.
    hbase = "FROM page_hits WHERE at >= ?"
    web_totals = conn.execute(f"SELECT COUNT(*) AS views, COUNT(DISTINCT visitor) AS visitors {hbase}",
                              (since,)).fetchone()

    def hits(fields, limit=20, extra=""):
        return _rows(conn.execute(
            f"SELECT {fields}, COUNT(*) AS views, COUNT(DISTINCT visitor) AS visitors {hbase} {extra} "
            f"GROUP BY {fields} ORDER BY views DESC LIMIT {int(limit)}", (since,)))

    web = {"views": web_totals["views"], "visitors": web_totals["visitors"],
           "countries": hits("country, country_name", 50),
           "points": hits("lat, lon, city, region, country, country_name", 1000, "AND lat IS NOT NULL"),
           "pages": hits("page", 10), "referrers": hits("referrer", 15, "AND referrer != ''"),
           "devices": hits("device", 5)}
    return {
        "range": range_key if range_key in RANGES else "7d", "since": since, "now": now,
        "bucket": bucket,
        "totals": {"sessions": totals["sessions"], "listeners": totals["listeners"],
                   "hours": round((totals["seconds"] or 0) / 3600, 1),
                   "avg_minutes": round((totals["avg_duration"] or 0) / 60, 1),
                   "longest_minutes": round((totals["longest"] or 0) / 60),
                   "peak": peak["listeners"] if peak else 0, "peak_at": peak["at"] if peak else None,
                   "now": len(live)},
        "live": live, "timeline": timeline, "countries": countries, "points": points,
        "players": players, "mounts": mounts, "lengths": lengths, "web": web,
        "min_session": MIN_SESSION, "geoip": geoip.info(),
        "enabled": enabled(conn),
    }


# ---------------------------------------------------------------- map tiles
OSM_ATTRIBUTION = ('&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" '
                   'rel="noopener">OpenStreetMap</a> contributors')
MAP_STYLES = {
    # No key needed. The tiles are OpenStreetMap's own; Studio darkens them in the browser.
    "osm-dark": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png", OSM_ATTRIBUTION, True),
    "osm": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png", OSM_ATTRIBUTION, False),
    # CARTO needs a free key since Aug 2026: https://carto.com/basemaps/apikey
    "carto-dark": ("https://basemaps.cartocdn.com/rastertiles/dark_all/{z}/{x}/{y}.png?key={key}",
                   OSM_ATTRIBUTION + ' &copy; <a href="https://carto.com/attributions" target="_blank" '
                   'rel="noopener">CARTO</a>', False),
    "carto-voyager": ("https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png?key={key}",
                      OSM_ATTRIBUTION + ' &copy; <a href="https://carto.com/attributions" target="_blank" '
                      'rel="noopener">CARTO</a>', False),
}
TILE_URL = re.compile(r"https://[A-Za-z0-9.{}-]+(:\d+)?/[^\s\"'<>]*")


def valid_tile_url(url):
    url = (url or "").strip()
    return bool(TILE_URL.fullmatch(url)) and all(p in url for p in ("{z}", "{x}", "{y}"))


def map_config(settings, env_tiles="", env_attribution=""):
    """Tile URL, attribution and whether to darken, from Site settings (or .env)."""
    if env_tiles:                                   # AIR_MAP_TILES in .env wins
        return {"style": "env", "tiles": env_tiles, "attribution": env_attribution or OSM_ATTRIBUTION,
                "darken": False, "note": "Set by AIR_MAP_TILES in .env."}
    style = settings.get("map_style") or "osm-dark"
    note = ""
    if style == "custom":
        url = (settings.get("map_tiles_url") or "").strip()
        if valid_tile_url(url):
            return {"style": style, "tiles": url, "darken": False, "note": "",
                    "attribution": html.escape(settings.get("map_attribution") or "") or OSM_ATTRIBUTION}
        style, note = "osm-dark", "The custom tile address is missing or invalid, so OpenStreetMap is shown."
    if style.startswith("carto"):
        key = (settings.get("map_carto_key") or "").strip()
        if not key:
            style, note = "osm-dark", "Add your free CARTO key in Site settings to use CARTO maps."
    if style not in MAP_STYLES:
        style = "osm-dark"
    tiles, attribution, darken = MAP_STYLES[style]
    if "{key}" in tiles:
        tiles = tiles.replace("{key}", urllib.parse.quote(settings.get("map_carto_key").strip(), safe=""))
    return {"style": style, "tiles": tiles, "attribution": attribution, "darken": darken, "note": note}


def sessions_csv(conn, range_key="30d", now=None):
    now = int(now or time.time())
    span = RANGES.get(range_key, RANGES["30d"])[0]
    lines = ["started_utc,ended_utc,minutes,stream,player,country,region,city"]
    for r in conn.execute(
            "SELECT * FROM listener_sessions WHERE internal = 0 AND duration >= ? "
            "AND COALESCE(ended_at, ?) >= ? ORDER BY started_at", (MIN_SESSION, now, now - span)):
        fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) if t else ""
        cells = [fmt(r["started_at"]), fmt(r["ended_at"]), f"{r['duration'] / 60:.1f}", r["mount"],
                 r["player"], r["country_name"], r["region"], r["city"]]
        # A leading = + - @ would make spreadsheets treat a player name as a formula.
        cells = ["'" + c if isinstance(c, str) and c[:1] in "=+-@" else c for c in cells]
        lines.append(",".join('"' + str(c).replace('"', '""') + '"' for c in cells))
    return "\n".join(lines) + "\n"
