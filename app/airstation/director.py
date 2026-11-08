"""Programme director: schedule, playlists, queue and track selection.

Selection rule ("fair shuffle"): within the active playlist, the tracks that
have gone longest without playing are preferred (never-played first), with a
random choice among them, an artist gap, and a no-repeat window. Across a whole
library this plays everything before anything repeats, while still sounding
shuffled. Scheduled shows switch the active playlist by time of day.
"""
import json
import random
import time
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from . import config, db

DEFAULT_TRACK_SECONDS = 240
PLAN_HORIZON_SECONDS = 2 * 3600
PLAN_MAX_ITEMS = 30
HANDOUT_GUARD_SECONDS = 20 * 60
STALE_QUEUE_SECONDS = 6 * 3600
EVICT_PROTECT_SECONDS = 3 * 3600
_rng = random.SystemRandom()


def tz():
    return ZoneInfo(config.TZ)


def local(ts):
    return datetime.fromtimestamp(ts, tz())


def _hhmm(value):
    hour, minute = value.split(":")
    return dtime(int(hour), int(minute))


def _days(show):
    return {int(d) for d in str(show["days"]).split(",") if d.strip().isdigit()}


# ------------------------------------------------------------------ schedule

def show_is_active(show, when):
    days = _days(show)
    start, end = _hhmm(show["start_time"]), _hhmm(show["end_time"])
    now_t = when.time().replace(second=0, microsecond=0)
    weekday = when.weekday()
    if start < end:
        return weekday in days and start <= now_t < end
    # Overnight (or 24h when start == end).
    return ((weekday in days and now_t >= start)
            or ((weekday - 1) % 7 in days and now_t < end))


def enabled_shows(conn):
    return db.rows(conn.execute("SELECT * FROM shows WHERE enabled = 1 ORDER BY start_time"))


def current_show(conn, ts=None):
    when = local(ts or time.time())
    for show in enabled_shows(conn):
        if show_is_active(show, when):
            return show
    return None


def upcoming(conn, ts=None, limit=6):
    """Show occurrences from now over the next 7 days (current one first)."""
    ts = ts or time.time()
    now = local(ts)
    items = []
    for show in enabled_shows(conn):
        days = _days(show)
        start_t, end_t = _hhmm(show["start_time"]), _hhmm(show["end_time"])
        for offset in range(-1, 8):
            day = (now + timedelta(days=offset)).date()
            if day.weekday() not in days:
                continue
            start = datetime.combine(day, start_t, tzinfo=now.tzinfo)
            end = datetime.combine(day, end_t, tzinfo=now.tzinfo)
            if end <= start:
                end += timedelta(days=1)
            if end <= now:
                continue
            items.append({
                "id": show["id"], "name": show["name"], "host": show["host"],
                "description": show["description"], "is_live": bool(show["is_live"]),
                "start": int(start.timestamp()), "end": int(end.timestamp()),
                "start_local": start.strftime("%H:%M"), "day": start.strftime("%a"),
                "on_air": start <= now < end,
            })
    items.sort(key=lambda s: (not s["on_air"], s["start"]))
    return items[:limit]


def default_playlist(conn):
    row = conn.execute("SELECT * FROM playlists WHERE is_default = 1 AND enabled = 1 "
                       "ORDER BY id LIMIT 1").fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM playlists WHERE enabled = 1 ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else None


def playlist_for_time(conn, ts):
    show = current_show(conn, ts)
    if show and show.get("playlist_id"):
        row = conn.execute("SELECT * FROM playlists WHERE id = ? AND enabled = 1",
                           (show["playlist_id"],)).fetchone()
        if row:
            return dict(row)
    return default_playlist(conn)


# ----------------------------------------------------------------- playlists

PLAYABLE = "t.enabled = 1 AND t.missing = 0 AND t.bad = 0"


def playlist_filter(playlist):
    """SQL condition (on alias t) and parameters selecting a playlist's tracks."""
    kind = playlist["kind"]
    rule = playlist.get("rule") or "[]"
    if isinstance(rule, str):
        try:
            rule = json.loads(rule)
        except ValueError:
            rule = []
    rule = [str(r).strip() for r in rule if str(r).strip()]
    if kind == "all":
        return PLAYABLE, []
    if kind == "folders":
        if not rule:
            return "0", []
        parts, params = [], []
        for folder in rule:
            folder = folder.strip("/")
            escaped = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parts.append("(t.folder = ? OR t.folder LIKE ? ESCAPE '\\')")
            params += [folder, escaped + "/%"]
        return f"{PLAYABLE} AND ({' OR '.join(parts)})", params
    if kind == "genres":
        if not rule:
            return "0", []
        parts = ["instr(lower(t.genre), ?) > 0" for _ in rule]
        return f"{PLAYABLE} AND ({' OR '.join(parts)})", [g.lower() for g in rule]
    if kind == "manual":
        return (f"{PLAYABLE} AND t.id IN (SELECT track_id FROM playlist_tracks "
                "WHERE playlist_id = ?)", [playlist["id"]])
    return "0", []


def playlist_count(conn, playlist):
    where, params = playlist_filter(playlist)
    total = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(cached), 0) FROM tracks t WHERE {where}",
                         params).fetchone()
    return {"tracks": total[0], "cached": total[1]}


def recent_artists(conn, count):
    if count <= 0:
        return set()
    names = [r[0] for r in conn.execute(
        "SELECT artist FROM history WHERE source = 'autodj' ORDER BY id DESC LIMIT ?", (count,))]
    names += [r[0] for r in conn.execute(
        "SELECT t.artist FROM queue q JOIN tracks t ON t.id = q.track_id "
        "ORDER BY q.id DESC LIMIT ?", (count,))]
    return {n.strip().lower() for n in names if n and n.strip()}


def pick_track(conn, playlist, now, cached_only=False, relax=False):
    if not playlist:
        return None
    where, params = playlist_filter(playlist)
    conditions = [where, "t.id NOT IN (SELECT track_id FROM queue)",
                  "(t.last_played IS NULL OR t.last_played < ?)"]
    params = list(params) + [now - HANDOUT_GUARD_SECONDS]
    if cached_only:
        conditions.append("t.cached = 1")
    if not relax:
        conditions.append("(t.last_played IS NULL OR t.last_played < ?)")
        params.append(now - int(playlist.get("no_repeat_hours") or 0) * 3600)
    sql = (f"SELECT t.* FROM tracks t WHERE {' AND '.join(conditions)} "
           "ORDER BY COALESCE(t.last_played, 0) ASC, random() LIMIT 80")
    candidates = db.rows(conn.execute(sql, params))
    if not candidates:
        return None if relax else pick_track(conn, playlist, now, cached_only, relax=True)
    avoid = recent_artists(conn, int(playlist.get("artist_gap") or 0))
    spaced = [c for c in candidates if c["artist"].strip().lower() not in avoid] or candidates
    # Prefer the stalest part of the list, but keep it random.
    pool = spaced[:max(5, len(spaced) // 3)]
    return _rng.choice(pool)


# --------------------------------------------------------------------- queue

def queue_items(conn):
    return db.rows(conn.execute(
        "SELECT q.id AS queue_id, q.reason, q.playlist_id, q.created_at, q.note, t.* "
        "FROM queue q JOIN tracks t ON t.id = q.track_id "
        "ORDER BY (q.reason = 'auto'), q.id"))


def plan(conn, now=None, horizon=PLAN_HORIZON_SECONDS, max_items=PLAN_MAX_ITEMS):
    """Top up the queue so the sync job can fetch upcoming tracks in advance."""
    now = int(now or time.time())
    items = queue_items(conn)
    total = sum((i["duration"] or DEFAULT_TRACK_SECONDS) for i in items)
    added = 0
    while total < horizon and len(items) + added < max_items:
        playlist = playlist_for_time(conn, now + total)
        track = pick_track(conn, playlist, now)
        if not track:
            break
        conn.execute("INSERT INTO queue (track_id, playlist_id, reason, created_at) "
                     "VALUES (?, ?, 'auto', ?)", (track["id"], playlist["id"], now))
        conn.execute("UPDATE tracks SET last_used = ? WHERE id = ?", (now, track["id"]))
        total += track["duration"] or DEFAULT_TRACK_SECONDS
        added += 1
    conn.commit()
    return added


def _mark_handed_out(conn, track_id, now):
    conn.execute("UPDATE tracks SET last_used = ?, last_played = ? WHERE id = ?",
                 (now, now, track_id))


def next_track(conn, now=None):
    """Choose the next AutoDJ track that is available locally; None if nothing is."""
    now = int(now or time.time())
    current = playlist_for_time(conn, now)
    chosen = None
    for item in queue_items(conn):
        unusable = (not item["enabled"] or item["missing"] or item["bad"])
        off_schedule = (item["reason"] == "auto" and current is not None
                        and item["playlist_id"] != current["id"])
        stale = (not item["cached"] and now - item["created_at"] > STALE_QUEUE_SECONDS)
        if unusable or off_schedule or stale:
            conn.execute("DELETE FROM queue WHERE id = ?", (item["queue_id"],))
            continue
        if item["cached"]:
            conn.execute("DELETE FROM queue WHERE id = ?", (item["queue_id"],))
            chosen = item
            break
    if chosen is None:
        chosen = (pick_track(conn, current, now, cached_only=True)
                  or pick_track(conn, default_playlist(conn), now, cached_only=True))
    if chosen is None:
        row = conn.execute(
            f"SELECT t.* FROM tracks t WHERE {PLAYABLE} AND t.cached = 1 "
            "ORDER BY COALESCE(t.last_played, 0) ASC, random() LIMIT 1").fetchone()
        chosen = dict(row) if row else None
    if chosen is None:
        conn.commit()
        return None
    _mark_handed_out(conn, chosen["id"], now)
    conn.commit()
    return chosen


def liquidsoap_uri(track):
    name = track["cache_name"].replace('"', "")
    return f'annotate:air_id="{track["id"]}":{config.LIQ_CACHE_PREFIX}/{name}'


def enqueue(conn, track_id, reason, note="", now=None):
    now = int(now or time.time())
    conn.execute("INSERT INTO queue (track_id, playlist_id, reason, created_at, note) "
                 "VALUES (?, NULL, ?, ?, ?)", (track_id, reason, now, note))
    conn.execute("UPDATE tracks SET last_used = ? WHERE id = ?", (now, track_id))
    conn.commit()


class RequestRefused(Exception):
    pass


def request_track(conn, track_id, client, now=None):
    now = int(now or time.time())
    settings = db.all_settings(conn)
    if not settings.get("requests_enabled"):
        raise RequestRefused("Requests are switched off right now.")
    track = conn.execute(f"SELECT * FROM tracks t WHERE t.id = ? AND {PLAYABLE}",
                         (track_id,)).fetchone()
    if not track:
        raise RequestRefused("That track is not available.")
    if conn.execute("SELECT 1 FROM queue WHERE track_id = ?", (track_id,)).fetchone():
        raise RequestRefused("That track is already coming up.")
    repeat = int(settings.get("request_repeat_hours") or 0) * 3600
    if track["last_played"] and now - track["last_played"] < repeat:
        raise RequestRefused("That track played recently. Try another one.")
    gap = int(settings.get("request_gap_minutes") or 0) * 60
    last = conn.execute("SELECT MAX(created_at) FROM requests WHERE client = ?",
                        (client,)).fetchone()[0]
    if last and now - last < gap:
        wait = max(1, (gap - (now - last)) // 60)
        raise RequestRefused(f"One request at a time. Try again in about {wait} min.")
    pending = conn.execute("SELECT COUNT(*) FROM queue WHERE reason = 'request'").fetchone()[0]
    if pending >= 10:
        raise RequestRefused("The request list is full. Try again soon.")
    conn.execute("INSERT INTO requests (track_id, client, created_at) VALUES (?, ?, ?)",
                 (track_id, client, now))
    enqueue(conn, track_id, "request", now=now)
    return dict(track)


# ---------------------------------------------------------------- cache plan

def cached_bytes(conn):
    return conn.execute("SELECT COALESCE(SUM(cache_bytes), 0) FROM tracks WHERE cached = 1"
                        ).fetchone()[0]


def wanted_for_cache(conn, pool_target, budget_bytes, now=None):
    """Tracks the sync job should copy next: queued first, then pool refill."""
    now = int(now or time.time())
    wanted, total = [], 0
    for item in queue_items(conn):
        if item["cached"] or item["missing"] or item["bad"] or not item["enabled"]:
            continue
        if total + item["size"] > budget_bytes and wanted:
            return wanted
        wanted.append(item)
        total += item["size"]
    have = cached_bytes(conn)
    if have + total >= pool_target:
        return wanted
    playlist = default_playlist(conn)
    if not playlist:
        return wanted
    where, params = playlist_filter(playlist)
    taken = {w["id"] for w in wanted}
    for row in conn.execute(
            f"SELECT t.* FROM tracks t WHERE {where} AND t.cached = 0 "
            "ORDER BY COALESCE(t.last_played, 0) ASC, random() LIMIT 400", params):
        row = dict(row)
        if row["id"] in taken:
            continue
        if total + row["size"] > budget_bytes or have + total >= pool_target:
            break
        wanted.append(row)
        total += row["size"]
    return wanted


def evictable(conn, now=None):
    now = int(now or time.time())
    return db.rows(conn.execute(
        "SELECT * FROM tracks WHERE cached = 1 "
        "AND id NOT IN (SELECT track_id FROM queue) "
        "AND COALESCE(last_used, cached_at, 0) < ? "
        "ORDER BY missing DESC, bad DESC, COALESCE(last_used, cached_at, 0) ASC",
        (now - EVICT_PROTECT_SECONDS,)))
