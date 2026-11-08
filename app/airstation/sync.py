"""Host-side NAS job: index the library, copy upcoming tracks to the local cache,
evict old cache files. Runs from a systemd timer as the unprivileged `air` user.

The broadcast containers never touch the NAS. If the home link or SMB mount
stalls, only this job waits (systemd kills it at its timeout); the radio keeps
playing from the local cache.
"""
import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config, db, director, emergency, geoip
from .tags import image_kind, image_size, read_picture, read_tags

NAS_UNAVAILABLE = 3  # systemd treats this as success (home offline is normal)
SKIP_DIRS = {"@eaDir", "$RECYCLE.BIN", "System Volume Information", "lost+found", ".Trash"}


def log(message):
    print(message, flush=True)


# ------------------------------------------------------------------- NAS check

def nas_available(root, allow_local=False):
    root = Path(root)
    try:
        # A separate process with a timeout: touching the automount may block.
        subprocess.run(["stat", str(root)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True, timeout=40)
    except (subprocess.SubprocessError, OSError):
        return False, f"{root} is not reachable (VPN down, NAS offline or mount failed)"
    if not root.is_dir():
        return False, f"{root} is not a directory"
    if allow_local:
        return True, "local directory (test mode)"
    try:
        fstype = subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE", "-T", str(root)],
                                         text=True, timeout=20).strip()
    except (subprocess.SubprocessError, OSError):
        fstype = ""
    if not set(fstype.split()) & {"cifs", "smb3"}:
        return False, (f"{root} is on {fstype or 'an unknown filesystem'}, not the SMB mount. "
                       "Refusing to index an unmounted directory.")
    return True, "SMB mount OK"


# ----------------------------------------------------------------------- index

def _audio_files(root):
    def raise_error(error):
        raise error
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=raise_error):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d not in SKIP_DIRS
                             and not os.path.islink(os.path.join(dirpath, d)))
        for name in filenames:
            if name.startswith(".") or "\n" in name or "\r" in name:
                continue
            if os.path.splitext(name)[1].lower() not in config.AUDIO_EXTENSIONS:
                continue
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            yield Path(path)


def index_library(conn, root, now=None):
    now = int(now or time.time())
    root = Path(root)
    existing = {r["relpath"]: r for r in conn.execute(
        "SELECT id, relpath, size, mtime, missing, cached, cache_name FROM tracks")}
    seen, added, changed, checked = set(), 0, 0, 0
    for path in _audio_files(root):
        rel = path.relative_to(root).as_posix()
        stat = path.stat()
        seen.add(rel)
        checked += 1
        size, mtime = stat.st_size, int(stat.st_mtime)
        old = existing.get(rel)
        if old and old["size"] == size and old["mtime"] == mtime:
            if old["missing"]:
                conn.execute("UPDATE tracks SET missing = 0 WHERE id = ?", (old["id"],))
            continue
        tags = read_tags(path, size)
        folder = Path(rel).parent.as_posix()
        folder = "" if folder == "." else folder
        values = (folder, size, mtime, tags.get("artist", ""), tags.get("title", ""),
                  tags.get("album", ""), tags.get("genre", ""), tags.get("year", ""), now)
        if old:
            if old["cached"]:
                _remove_cache_file(old["cache_name"])
            (config.ART_DIR / f"{old['id']}.jpg").unlink(missing_ok=True)
            conn.execute(
                "UPDATE tracks SET folder=?, size=?, mtime=?, artist=?, title=?, album=?, "
                "genre=?, year=?, indexed_at=?, missing=0, bad=0, cached=0, cache_name=NULL, "
                "cache_bytes=0, duration=NULL, has_art=0, art_checked=0 WHERE id=?",
                values + (old["id"],))
            changed += 1
        else:
            conn.execute(
                "INSERT INTO tracks (folder, size, mtime, artist, title, album, genre, year, "
                "indexed_at, relpath) VALUES (?,?,?,?,?,?,?,?,?,?)", values + (rel,))
            added += 1
        if (added + changed) % 200 == 0:
            conn.commit()
    conn.commit()
    gone = [r for rel, r in existing.items() if rel not in seen and not r["missing"]]
    if existing and not seen:
        log("WARNING: the NAS folder is empty. Not marking the whole library missing; "
            "check AIR_NAS_PATH and the mount.")
        gone = []
    for row in gone:
        conn.execute("UPDATE tracks SET missing = 1 WHERE id = ?", (row["id"],))
    conn.commit()
    log(f"Index: {checked} files seen, {added} new, {changed} changed, {len(gone)} missing.")
    return {"seen": checked, "added": added, "changed": changed, "missing": len(gone)}


# ----------------------------------------------------------------------- cache

def _remove_cache_file(name):
    if not name:
        return
    try:
        (config.CACHE_DIR / name).unlink()
    except FileNotFoundError:
        pass


def probe(path):
    """Return (duration, tags) using ffprobe, or (None, {}) if unplayable."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:format_tags:stream=codec_type,codec_name", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60, check=True)
        info = json.loads(result.stdout)
    except (subprocess.SubprocessError, ValueError, OSError):
        return None, {}
    streams = info.get("streams") or []
    if not any(s.get("codec_type") == "audio" for s in streams):
        return None, {}
    try:
        duration = float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return None, {}
    if not (0 < duration <= config.MAX_TRACK_SECONDS):
        return None, {}
    raw = {k.lower(): str(v) for k, v in (info["format"].get("tags") or {}).items()}
    tags = {"artist": raw.get("artist") or raw.get("album_artist", ""),
            "title": raw.get("title", ""), "album": raw.get("album", ""),
            "genre": raw.get("genre", ""), "year": (raw.get("date") or raw.get("year", ""))[:4]}
    has_picture = any(s.get("codec_type") == "video" for s in streams)
    return duration, dict(tags, _has_picture=has_picture)


def _ffmpeg_cover(source, temporary):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
                    "-map", "0:v:0", "-an", "-frames:v", "1", "-update", "1",
                    "-vf", "scale='min(500,iw)':-2", "-q:v", "3", str(temporary)],
                   check=True, timeout=60, capture_output=True)


TEMPORARY = "temporary: "   # prefix for failures worth retrying soon
ART_RECHECK_SECONDS = 14 * 86400   # tracks without a cover are re-checked this often


def _transient(error):
    """True for failures that say nothing about the file (ffmpeg missing, timeout,
    killed by the OOM killer, disk/permission problems)."""
    if isinstance(error, subprocess.TimeoutExpired):
        return True
    if isinstance(error, subprocess.CalledProcessError):
        return error.returncode < 0
    return isinstance(error, OSError)


def extract_art(source, name):
    """Write ART_DIR/<name>.jpg from the file's embedded cover.

    1) ffmpeg reads the attached picture (MP3, M4A, FLAC, OGG...).
    2) If that fails, read the ID3 APIC frame directly (covers ffmpeg can't map,
       e.g. WebP thumbnails from YouTube converters) and convert it.
    Returns (True, how) or (False, reason). Reasons starting with TEMPORARY mean
    the check should be repeated (see art_done()).
    """
    target = config.ART_DIR / f"{name}.jpg"
    temporary = config.ART_DIR / f".{name}.tmp.jpg"
    try:
        config.ART_DIR.mkdir(parents=True, exist_ok=True)
        if not os.access(config.ART_DIR, os.W_OK):
            return False, f"{TEMPORARY}cannot write to {config.ART_DIR} (run init.py to fix ownership)"
    except OSError as error:
        return False, f"{TEMPORARY}{error}"
    first_transient = None
    try:
        _ffmpeg_cover(source, temporary)
        temporary.chmod(0o644)
        temporary.replace(target)
        return True, "ffmpeg"
    except (subprocess.SubprocessError, OSError) as error:
        temporary.unlink(missing_ok=True)
        first_error = getattr(error, "stderr", b"") or b""
        if _transient(error):
            first_transient = ("ffmpeg is not installed on the VPS (apt install ffmpeg)"
                               if isinstance(error, FileNotFoundError) else
                               f"ffmpeg failed ({type(error).__name__})")
    picture = None
    if Path(source).suffix.lower() == ".mp3":
        try:
            with open(source, "rb") as stream:
                picture = read_picture(stream)
        except OSError as error:
            return False, f"{TEMPORARY}could not read the file ({error})"
    if not picture:
        if first_transient:
            return False, TEMPORARY + first_transient
        reason = first_error.decode("utf-8", "replace").strip().splitlines()
        return False, "no embedded cover" if not reason else f"no usable cover ({reason[-1][:120]})"
    mime, data = picture
    kind = image_kind(data) or "bin"
    raw = config.ART_DIR / f".{name}.raw.{kind}"
    try:
        raw.write_bytes(data)
        try:
            _ffmpeg_cover(raw, temporary)
        except (subprocess.SubprocessError, OSError) as error:
            temporary.unlink(missing_ok=True)
            if kind != "jpeg":
                if _transient(error):
                    return False, TEMPORARY + (first_transient or f"embedded {mime or kind} picture: ffmpeg failed")
                return False, f"embedded {mime or kind} picture could not be decoded"
            temporary.write_bytes(data)
        temporary.chmod(0o644)
        temporary.replace(target)
        return True, f"ID3 APIC ({mime or kind})"
    except OSError as error:
        temporary.unlink(missing_ok=True)
        return False, f"{TEMPORARY}{error}"
    finally:
        raw.unlink(missing_ok=True)


def art_done(ok, how, now=None):
    """Value for tracks.art_checked: 0 = check again on the next run, otherwise
    the time of the check (tracks without a cover are re-checked later)."""
    if not ok and how.startswith(TEMPORARY):
        return 0
    return int(now or time.time())


def art_files():
    """Ids of library tracks that have a cover file."""
    try:
        return {int(p.stem) for p in config.ART_DIR.glob("*.jpg") if p.stem.isdigit()}
    except OSError:
        return set()


def verify_art(conn):
    """Keep tracks.has_art in step with the files in ART_DIR.

    A cover file that disappeared (folder restored without art, moved install,
    cleaned disk...) is re-extracted; a cover file the database forgot is used.
    """
    present = art_files()
    if not present and not config.ART_DIR.is_dir():
        return 0, 0
    known = {r["id"] for r in conn.execute("SELECT id FROM tracks WHERE has_art = 1")}
    lost = sorted(known - present)
    found = [i for i in sorted(present - known)
             if conn.execute("SELECT 1 FROM tracks WHERE id = ?", (i,)).fetchone()]
    for chunk in range(0, len(lost), 500):
        ids = lost[chunk:chunk + 500]
        conn.execute(f"UPDATE tracks SET has_art = 0, art_checked = 0 WHERE id IN "
                     f"({','.join('?' * len(ids))})", ids)
    for chunk in range(0, len(found), 500):
        ids = found[chunk:chunk + 500]
        conn.execute(f"UPDATE tracks SET has_art = 1 WHERE id IN ({','.join('?' * len(ids))})", ids)
    conn.commit()
    if lost or found:
        log(f"Cover art: {len(lost)} cover files were missing (re-extracting), "
            f"{len(found)} existing files re-linked.")
    return len(lost), len(found)


def backfill_art(conn, limit=200, now=None):
    """Try covers for cached tracks not checked yet (also runs after upgrades),
    and re-check tracks without a cover every ART_RECHECK_SECONDS."""
    now = int(now or time.time())
    rows = conn.execute("SELECT id, cache_name FROM tracks WHERE cached = 1 AND cache_name IS NOT NULL "
                        "AND (art_checked = 0 OR (has_art = 0 AND art_checked < ?)) "
                        "ORDER BY art_checked, last_played DESC LIMIT ?",
                        (now - ART_RECHECK_SECONDS, limit)).fetchall()
    found = retry = 0
    problems = {}
    for row in rows:
        ok, how = extract_art(config.CACHE_DIR / row["cache_name"], row["id"])
        found += ok
        checked = art_done(ok, how, now)
        if not checked:
            retry += 1
            problems[how] = problems.get(how, 0) + 1
        conn.execute("UPDATE tracks SET has_art = ?, art_checked = ? WHERE id = ?",
                     (int(ok), checked, row["id"]))
    conn.commit()
    if rows:
        log(f"Cover art: checked {len(rows)} cached tracks, found {found}"
            + (f", {retry} to retry" if retry else "") + ".")
    for how, count in problems.items():
        log(f"Cover art problem ({count} tracks): {how[len(TEMPORARY):]}")
    return found


def emergency_art(conn, limit=300):
    """Covers for the emergency folder (keyed like the API's air_id 'em-...')."""
    files, _ = emergency.scan(force=True)
    tried = db.get_setting(conn, "_emergency_art") or {}
    base = emergency.root()
    done = 0
    for rel in files:
        key = emergency.art_key(rel)
        try:
            stamp = int((base / rel).stat().st_mtime)
        except OSError:
            continue
        if tried.get(key) == stamp:
            continue
        if done >= limit:
            break
        extract_art(base / rel, key)
        tried[key] = stamp
        done += 1
    if done:
        live = {emergency.art_key(r) for r in files}
        tried = {k: v for k, v in tried.items() if k in live}
        db.set_setting(conn, "_emergency_art", tried)
        conn.commit()
    return done


def art_check(conn, text):
    """Explain cover extraction for tracks matching text (airctl art-check)."""
    like = f"%{text}%"
    rows = conn.execute("SELECT * FROM tracks WHERE artist LIKE ? OR title LIKE ? OR relpath LIKE ? "
                        "LIMIT 5", (like, like, like)).fetchall()
    if not rows:
        print("No matching tracks in the library.")
    for row in rows:
        print(f"#{row['id']} {row['artist']} - {row['title']}\n  NAS: {row['relpath']}")
        if not row["cached"]:
            print("  not cached on the VPS yet (art is read from the cached copy)")
            continue
        path = config.CACHE_DIR / row["cache_name"]
        try:
            streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                      "stream=index,codec_type,codec_name:stream_disposition=attached_pic",
                                      "-of", "compact", str(path)], capture_output=True, text=True,
                                     timeout=30).stdout.strip()
        except (subprocess.SubprocessError, OSError) as error:
            streams = str(error)
        print("  ffprobe streams:\n    " + streams.replace("\n", "\n    "))
        picture = None
        if path.suffix.lower() == ".mp3":
            with open(path, "rb") as stream:
                picture = read_picture(stream)
        print("  ID3 picture:", f"{picture[0]}, {len(picture[1])} bytes, {image_size(picture[1])}"
              if picture else "none")
        ok, how = extract_art(path, row["id"])
        conn.execute("UPDATE tracks SET has_art = ?, art_checked = ? WHERE id = ?",
                     (int(ok), art_done(ok, how), row["id"]))
        conn.commit()
        print("  result:", f"cover saved via {how}" if ok else how)


def art_doctor(conn):
    """Print everything the cover-art feature depends on (airctl art-doctor)."""
    problems = []

    def line(label, value, bad=False):
        print(f"  {'!!' if bad else 'ok'} {label:<28} {value}")
        if bad:
            problems.append(label)

    print("Cover art doctor (runs as uid %d)" % os.getuid())
    for tool in ("ffmpeg", "ffprobe"):
        where = shutil.which(tool)
        line(tool, where or "NOT INSTALLED: apt install -y ffmpeg", not where)
    for label, folder in (("art folder", config.ART_DIR), ("branding folder", config.BRANDING_DIR)):
        try:
            st = folder.stat()
            writable = os.access(folder, os.W_OK)
            line(label, f"{folder} owner {st.st_uid}:{st.st_gid} mode {oct(st.st_mode & 0o777)}"
                 + ("" if writable else "  NOT WRITABLE: run python3 scripts/init.py"),
                 not writable and label == "art folder")
        except OSError as error:
            line(label, f"{folder}: {error}", True)
    present = art_files()
    c = conn.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(cached), 0) AS cached, "
        "COALESCE(SUM(cached * has_art), 0) AS with_art, "
        "COALESCE(SUM(CASE WHEN cached = 1 AND art_checked = 0 THEN 1 ELSE 0 END), 0) AS pending "
        "FROM tracks").fetchone()
    known = {r["id"] for r in conn.execute("SELECT id FROM tracks WHERE has_art = 1")}
    line("library / cached", f"{c['total']} tracks, {c['cached']} cached on the VPS")
    line("cached tracks with a cover", f"{c['with_art']} of {c['cached']}")
    line("cover files on disk", f"{len(present)}")
    lost = len(known - present)
    line("covers missing their file", f"{lost}" + ("  (sync re-extracts them)" if lost else ""), lost > 0)
    line("waiting to be checked", f"{c['pending']}" + ("  (200 per sync run)" if c["pending"] else ""))
    setting = db.get_setting(conn, "default_art") or ""
    if setting.startswith("/branding/"):
        exists = (config.BRANDING_DIR / setting[len("/branding/"):]).is_file()
        line("default cover", setting + ("" if exists else "  FILE MISSING: the site now uses the "
                                          "bundled cover; upload yours again in Admin"), not exists)
    else:
        line("default cover", "bundled /assets/img/default-art.png")
    print("\nLast tracks played:")
    for row in conn.execute("SELECT h.track_id, h.artist, h.title, t.has_art, t.cached, t.cache_name "
                            "FROM history h LEFT JOIN tracks t ON t.id = h.track_id "
                            "WHERE h.track_id IS NOT NULL ORDER BY h.id DESC LIMIT 5"):
        tid = row["track_id"]
        state = ("cover file OK" if tid in present else
                 "has_art=1 but file missing" if row["has_art"] else
                 "no cover found yet")
        print(f"  #{tid} {row['artist']} - {row['title']}: {state}")
    print()
    if problems:
        print("Problems: " + ", ".join(problems))
    else:
        print("No problems found on the VPS side.")
    return 1 if problems else 0


def copy_file(source, destination, expected_size):
    before = source.stat()
    copied = 0
    with source.open("rb") as inp, destination.open("xb") as out:
        while True:
            block = inp.read(1024 * 1024)
            if not block:
                break
            copied += len(block)
            if copied > expected_size:
                raise RuntimeError("source grew during copy")
            out.write(block)
        out.flush()
        os.fsync(out.fileno())
    after = source.stat()
    if copied != expected_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("source changed during copy")


def disk_free():
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(config.CACHE_DIR).free


def evict(conn, need_bytes=0, now=None):
    over = director.cached_bytes(conn) + need_bytes - config.CACHE_MAX_BYTES
    shortfall = config.DISK_RESERVE_BYTES + need_bytes - disk_free()
    target = max(over, shortfall)
    removed = 0
    if target <= 0:
        return 0
    for track in director.evictable(conn, now):
        if target <= 0:
            break
        _remove_cache_file(track["cache_name"])
        conn.execute("UPDATE tracks SET cached = 0, cache_name = NULL, cache_bytes = 0 "
                     "WHERE id = ?", (track["id"],))
        target -= track["cache_bytes"]
        removed += 1
    conn.commit()
    if removed:
        log(f"Evicted {removed} cached tracks.")
    return removed


def fetch(conn, root, now=None, budget=None):
    now = int(now or time.time())
    root = Path(root)
    budget = config.FETCH_PER_RUN_BYTES if budget is None else budget
    wanted = director.wanted_for_cache(conn, config.POOL_TARGET_BYTES, budget, now)
    fetched = fetched_bytes = failed = 0
    for track in wanted:
        size = track["size"]
        if size <= 0 or size > config.MAX_TRACK_BYTES:
            conn.execute("UPDATE tracks SET bad = 1 WHERE id = ?", (track["id"],))
            conn.commit()
            continue
        evict(conn, size, now)
        if director.cached_bytes(conn) + size > config.CACHE_MAX_BYTES:
            log("Cache is full of protected tracks; will retry later.")
            break
        if disk_free() - size < config.DISK_RESERVE_BYTES:
            log("Disk reserve reached; not copying more tracks.")
            break
        source = root / track["relpath"]
        suffix = Path(track["relpath"]).suffix.lower()
        name = f"{track['id'] % 100:02d}/{track['id']}{suffix}"
        final = config.CACHE_DIR / name
        final.parent.mkdir(parents=True, exist_ok=True)
        partial = final.with_name(final.name + ".part")
        partial.unlink(missing_ok=True)
        try:
            copy_file(source, partial, size)
        except FileNotFoundError:
            partial.unlink(missing_ok=True)
            conn.execute("UPDATE tracks SET missing = 1 WHERE id = ?", (track["id"],))
            conn.commit()
            continue
        except (OSError, RuntimeError) as error:
            partial.unlink(missing_ok=True)
            failed += 1
            log(f"Copy failed for {track['relpath']}: {error}")
            if isinstance(error, OSError):
                break  # NAS/link problem: stop this run, keep what we have.
            continue
        duration, tags = probe(partial)
        if duration is None:
            partial.unlink(missing_ok=True)
            conn.execute("UPDATE tracks SET bad = 1 WHERE id = ?", (track["id"],))
            conn.commit()
            log(f"Unplayable, skipped: {track['relpath']}")
            continue
        partial.chmod(0o644)
        partial.replace(final)
        has_art, how = extract_art(final, track["id"])
        updates = {"cached": 1, "cache_name": name, "cache_bytes": size, "cached_at": now,
                   "duration": round(duration, 2), "has_art": int(has_art),
                   "art_checked": art_done(has_art, how, now)}
        prefer_probe = suffix != ".mp3"
        for key in ("artist", "title", "album", "genre", "year"):
            value = (tags.get(key) or "").strip()[:300]
            if value and (prefer_probe or not track.get(key)):
                updates[key] = value
        assignments = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE tracks SET {assignments} WHERE id = ?",
                     list(updates.values()) + [track["id"]])
        conn.commit()
        fetched += 1
        fetched_bytes += size
    if fetched:
        log(f"Cached {fetched} tracks ({fetched_bytes / 1048576:.1f} MiB).")
    return {"fetched": fetched, "fetched_bytes": fetched_bytes, "failed": failed}


def clean_partials():
    if config.CACHE_DIR.exists():
        for leftover in config.CACHE_DIR.rglob("*.part"):
            leftover.unlink(missing_ok=True)


# ------------------------------------------------------------------------ main

def run(root=None, force_index=False, allow_local=False, now=None):
    now = int(now or time.time())
    root = Path(root or config.NAS_PATH)
    conn = db.open_db()
    status = dict(db.get_setting(conn, "_sync") or {})
    request = db.get_setting(conn, "_sync_request") or {}
    status.update(at=now, message="", fetched=0, evicted=0)
    clean_partials()
    ok, message = nas_available(root, allow_local)
    status["nas_ok"] = ok
    status["message"] = message
    exit_code = 0
    if ok:
        try:
            due = now - int(status.get("last_index_at") or 0) >= config.INDEX_INTERVAL_SECONDS
            if force_index or due or request.get("index"):
                result = index_library(conn, root, now)
                status.update(last_index_at=now, last_index=result)
                db.set_setting(conn, "_sync_request", {})
                conn.commit()
            director.plan(conn, now)
            status.update(fetch(conn, root, now))
        except OSError as error:
            status["nas_ok"] = False
            status["message"] = f"NAS read failed: {error}"
            exit_code = NAS_UNAVAILABLE
    else:
        exit_code = NAS_UNAVAILABLE
    status["evicted"] = evict(conn, 0, now)
    try:
        verify_art(conn)
        backfill_art(conn, now=now)
        emergency_art(conn)
    except OSError as error:
        log(f"Cover art pass skipped: {error}")
    # Keep the free GeoIP database (listener map) fresh: checked once a day,
    # downloaded about once a month. A failure only means no new locations.
    if db.get_setting(conn, "stats_enabled") and now - int(db.get_setting(conn, "_geoip_checked") or 0) > 86400:
        db.set_setting(conn, "_geoip_checked", now)
        conn.commit()
        try:
            geoip.update(log=log)
        except Exception as error:  # noqa: BLE001 - never let this break the music sync
            log(f"GeoIP update skipped: {error}")
    counts = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(cached), 0), COALESCE(SUM(cache_bytes), 0) "
        "FROM tracks WHERE missing = 0").fetchone()
    status.update(library=counts[0], cached=counts[1], cached_bytes=counts[2],
                  disk_free=disk_free())
    db.set_setting(conn, "_sync", status)
    conn.commit()
    log(f"NAS: {status['message']} | library {counts[0]} tracks, {counts[1]} cached "
        f"({counts[2] / 1073741824:.2f} GiB).")
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description="AirStation NAS index and cache job")
    parser.add_argument("--index", action="store_true", help="force a full library scan")
    parser.add_argument("--source", help="music root (default AIR_NAS_PATH)")
    parser.add_argument("--allow-local", action="store_true",
                        help="accept a plain local directory (testing without SMB)")
    parser.add_argument("--art-retry", action="store_true",
                        help="retry cover extraction for every cached track without art")
    parser.add_argument("--art-doctor", action="store_true",
                        help="check everything cover art depends on")
    parser.add_argument("--art-check", metavar="TEXT",
                        help="explain cover extraction for tracks matching TEXT")
    args = parser.parse_args(argv)
    if args.art_doctor:
        return art_doctor(db.open_db())
    if args.art_check or args.art_retry:
        conn = db.open_db()
        if args.art_check:
            art_check(conn, args.art_check)
        else:
            verify_art(conn)
            conn.execute("UPDATE tracks SET art_checked = 0 WHERE has_art = 0")
            conn.commit()
            found = backfill_art(conn, limit=1000000)
            emergency_art(conn, limit=1000000)
            print(f"Cover art retry finished: {found} new covers.")
        return 0
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with (config.DB_PATH.parent / ".sync.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Another sync is running; exiting.")
            return 0
        return run(args.source, args.index, args.allow_local)


if __name__ == "__main__":
    sys.exit(main())
