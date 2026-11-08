"""AirStation tests. Run on the VPS (or anywhere with python3 + ffmpeg):

    cd /opt/air-station && python3 -m unittest discover -s tests -v

They use temporary directories and generated audio; nothing touches the NAS,
the running containers or real data.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "tests"))

TMP = tempfile.mkdtemp(prefix="air-test-")
os.environ.update(AIR_ROOT=TMP, AIR_DATA_DIR=f"{TMP}/data", AIR_SECRETS_DIR=f"{TMP}/secrets",
                  AIR_DISK_RESERVE_GB="0", AIR_POOL_GB="1", AIR_CACHE_MAX_GB="1",
                  AIR_TZ="Australia/Brisbane")

from airstation import branding, config, db, director, duplicates, ed25519, emergency, geoip, icecast_start, licensing, pages, stats, sync, tags  # noqa: E402
import mmdb_writer  # noqa: E402

BNE = ZoneInfo("Australia/Brisbane")


def make_mp3(path, artist="Artist", title="Title", seconds=1, freq=440, genre="Hip-Hop", art=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
           "-i", f"sine=frequency={freq}:duration={seconds}"]
    if art:
        cmd += ["-f", "lavfi", "-i", "color=c=blue:s=32x32:d=1", "-map", "0", "-map", "1",
                "-frames:v", "1", "-disposition:v", "attached_pic"]
    cmd += ["-c:a", "libmp3lame", "-b:a", "64k", "-id3v2_version", "3",
            "-metadata", f"artist={artist}", "-metadata", f"title={title}",
            "-metadata", f"genre={genre}", str(path)]
    subprocess.run(cmd, check=True)
    return path


def ts(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=BNE).timestamp())


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=TMP))
        config.DB_PATH = self.dir / "air.sqlite3"
        config.CACHE_DIR = self.dir / "cache"
        config.ART_DIR = self.dir / "art"
        self.conn = db.open_db(config.DB_PATH)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def add_tracks(self, n, cached=True, folder="Music", artists=None):
        ids = []
        for i in range(n):
            artist = artists[i % len(artists)] if artists else f"Artist {i}"
            cur = self.conn.execute(
                "INSERT INTO tracks (relpath, folder, size, artist, title, cached, cache_name, "
                "cache_bytes, duration) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"{folder}/{len(ids)}-{i}-{time.time_ns()}.mp3", folder, 1000, artist, f"Song {i}",
                 int(cached), f"x/{i}.mp3" if cached else None, 1000 if cached else 0, 200))
            ids.append(cur.lastrowid)
        self.conn.commit()
        return ids


class TagTests(unittest.TestCase):
    def test_id3_tags_and_filename_fallback(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        tagged = make_mp3(d / "x.mp3", "Nujabes", "Feather", genre="Hip-Hop", art=True)
        self.assertEqual(tags.read_tags(tagged)["artist"], "Nujabes")
        self.assertEqual(tags.read_tags(tagged)["title"], "Feather")
        untagged = d / "03 - Little Simz - Gorilla.mp3"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=duration=1", "-c:a", "libmp3lame", "-write_id3v2", "0",
                        str(untagged)], check=True)
        guessed = tags.read_tags(untagged)
        self.assertEqual((guessed["artist"], guessed["title"]), ("Little Simz", "Gorilla"))

    def test_numeric_genre(self):
        self.assertEqual(tags._clean_genre("(7)"), "Hip-Hop")
        self.assertEqual(tags._clean_genre("15"), "Rap")


class ScheduleTests(Base):
    def test_overnight_show_and_playlist_switch(self):
        pid = self.conn.execute("INSERT INTO playlists (name, kind, rule) VALUES "
                                "('Chill', 'folders', '[\"Chill\"]')").lastrowid
        self.conn.execute("INSERT INTO shows (name, days, start_time, end_time, playlist_id) "
                          "VALUES ('Night Vibes', '0', '22:00', '02:00', ?)", (pid,))
        self.conn.commit()
        # Monday 2026-09-28 23:00 and the following Tuesday 01:30 are both in the show.
        self.assertEqual(director.current_show(self.conn, ts(2026, 9, 28, 23, 0))["name"], "Night Vibes")
        self.assertEqual(director.current_show(self.conn, ts(2026, 9, 29, 1, 30))["name"], "Night Vibes")
        self.assertIsNone(director.current_show(self.conn, ts(2026, 9, 29, 2, 0)))
        self.assertIsNone(director.current_show(self.conn, ts(2026, 9, 29, 23, 0)))  # Tuesday
        self.assertEqual(director.playlist_for_time(self.conn, ts(2026, 9, 28, 23, 0))["name"], "Chill")
        self.assertEqual(director.playlist_for_time(self.conn, ts(2026, 9, 28, 12, 0))["name"], "All Music")

    def test_upcoming_lists_current_first(self):
        self.conn.execute("INSERT INTO shows (name, days, start_time, end_time) VALUES "
                          "('Lunch', '0,1,2,3,4,5,6', '10:00', '14:00')")
        self.conn.commit()
        items = director.upcoming(self.conn, ts(2026, 9, 29, 11, 0))
        self.assertTrue(items[0]["on_air"])
        self.assertFalse(items[1]["on_air"])


class DirectorTests(Base):
    def test_fair_shuffle_plays_everything_before_repeating(self):
        ids = set(self.add_tracks(30))
        now = ts(2026, 9, 29, 12, 0)
        seen = []
        for i in range(30):
            track = director.next_track(self.conn, now + i * 200)
            seen.append(track["id"])
        self.assertEqual(set(seen), ids)
        self.assertEqual(len(seen), len(set(seen)))

    def test_artist_gap(self):
        self.add_tracks(40, artists=["A", "B", "C", "D", "E", "F"])
        playlist = director.default_playlist(self.conn)
        self.conn.execute("UPDATE playlists SET artist_gap = 3")
        self.conn.commit()
        now = ts(2026, 9, 29, 12, 0)
        artists = []
        for i in range(24):
            t = director.next_track(self.conn, now + i * 200)
            self.conn.execute("INSERT INTO history (started_at, source, track_id, artist, title) "
                              "VALUES (?, 'autodj', ?, ?, ?)", (now + i * 200, t["id"], t["artist"], t["title"]))
            self.conn.commit()
            artists.append(t["artist"])
        for i in range(3, len(artists)):
            self.assertNotIn(artists[i], artists[i - 3:i], artists)
        self.assertIsNotNone(playlist)

    def test_queue_prefers_requests_and_skips_uncached(self):
        cached = self.add_tracks(5)
        uncached = self.add_tracks(1, cached=False)
        director.enqueue(self.conn, uncached[0], "request")
        director.enqueue(self.conn, cached[3], "request")
        chosen = director.next_track(self.conn)
        self.assertEqual(chosen["id"], cached[3])
        # The uncached request stays queued until the sync job fetches it.
        self.assertTrue(self.conn.execute("SELECT 1 FROM queue WHERE track_id = ?", (uncached[0],)).fetchone())

    def test_nothing_cached_returns_none(self):
        self.add_tracks(3, cached=False)
        self.assertIsNone(director.next_track(self.conn))

    def test_plan_fills_queue_and_wanted_for_cache(self):
        self.add_tracks(20, cached=False)
        added = director.plan(self.conn, ts(2026, 9, 29, 12, 0))
        self.assertGreater(added, 5)
        wanted = director.wanted_for_cache(self.conn, 10 ** 9, 10 ** 9)
        queued = [r[0] for r in self.conn.execute("SELECT track_id FROM queue ORDER BY id")]
        self.assertEqual([w["id"] for w in wanted][:len(queued)], queued)

    def test_request_rules(self):
        ids = self.add_tracks(5)
        now = ts(2026, 9, 29, 12, 0)
        director.request_track(self.conn, ids[0], "client-a", now)
        with self.assertRaises(director.RequestRefused):
            director.request_track(self.conn, ids[1], "client-a", now + 60)   # too soon
        with self.assertRaises(director.RequestRefused):
            director.request_track(self.conn, ids[0], "client-b", now + 60)   # already queued
        director.request_track(self.conn, ids[1], "client-b", now + 60)
        self.conn.execute("UPDATE tracks SET last_played = ? WHERE id = ?", (now, ids[2]))
        self.conn.commit()
        with self.assertRaises(director.RequestRefused):
            director.request_track(self.conn, ids[2], "client-c", now + 60)   # played recently
        db.set_setting(self.conn, "requests_enabled", False)
        with self.assertRaises(director.RequestRefused):
            director.request_track(self.conn, ids[3], "client-d", now + 60)

    def test_folder_playlist_includes_subfolders_only(self):
        self.add_tracks(3, folder="HipHop")
        self.add_tracks(2, folder="HipHop/Aus")
        self.add_tracks(4, folder="HipHopper")
        pl = {"id": 99, "kind": "folders", "rule": '["HipHop"]'}
        self.assertEqual(director.playlist_count(self.conn, pl)["tracks"], 5)


class SyncTests(Base):
    def setUp(self):
        super().setUp()
        self.nas = self.dir / "nas"
        for i in range(12):
            make_mp3(self.nas / ("A" if i % 2 else "B") / f"{i:02d}.mp3", f"Artist {i}", f"Song {i}",
                     freq=300 + i * 20, art=(i == 0))
        (self.nas / "B" / "broken.mp3").write_text("not audio")
        (self.nas / "B" / "._resource.mp3").write_text("mac junk")

    def test_index_fetch_evict_and_missing(self):
        sync.index_library(self.conn, self.nas)
        total = self.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        self.assertEqual(total, 13)  # 12 + broken; AppleDouble skipped
        result = sync.fetch(self.conn, self.nas, budget=10 ** 9)
        self.assertEqual(result["fetched"], 12)
        bad = self.conn.execute("SELECT bad FROM tracks WHERE relpath = 'B/broken.mp3'").fetchone()[0]
        self.assertEqual(bad, 1)
        art = self.conn.execute("SELECT id, has_art FROM tracks WHERE relpath = 'B/00.mp3'").fetchone()
        self.assertEqual(art["has_art"], 1)
        self.assertTrue((config.ART_DIR / f"{art['id']}.jpg").exists())
        uri = director.liquidsoap_uri(director.next_track(self.conn))
        self.assertRegex(uri, r'^annotate:air_id="\d+":/cache/\d\d/\d+\.mp3$')
        # A file disappears from the NAS; the next scan marks it missing.
        (self.nas / "A" / "01.mp3").unlink()
        sync.index_library(self.conn, self.nas)
        self.assertEqual(self.conn.execute("SELECT missing FROM tracks WHERE relpath='A/01.mp3'").fetchone()[0], 1)
        # Eviction frees old, unprotected cache files when over budget.
        old = int(time.time()) - 10 * 3600
        self.conn.execute("UPDATE tracks SET last_used = ?", (old,))
        self.conn.commit()
        config.CACHE_MAX_BYTES = 1
        try:
            removed = sync.evict(self.conn)
        finally:
            config.CACHE_MAX_BYTES = 10 ** 9
        self.assertGreater(removed, 0)

    def test_empty_nas_never_marks_library_missing(self):
        sync.index_library(self.conn, self.nas)
        empty = self.dir / "empty"
        empty.mkdir()
        sync.index_library(self.conn, empty)
        self.assertEqual(self.conn.execute("SELECT SUM(missing) FROM tracks").fetchone()[0], 0)

    def test_automount_reports_autofs_and_cifs(self):
        # findmnt lists both the autofs trigger and the CIFS mount on top of it.
        from unittest import mock
        with mock.patch.object(sync.subprocess, "check_output", return_value="autofs\ncifs\n"), \
             mock.patch.object(sync.subprocess, "run"):
            ok, _ = sync.nas_available(self.nas)
        self.assertTrue(ok)
        with mock.patch.object(sync.subprocess, "check_output", return_value="autofs\n"), \
             mock.patch.object(sync.subprocess, "run"):
            ok, _ = sync.nas_available(self.nas)
        self.assertFalse(ok)

    def test_unmounted_path_refused(self):
        ok, message = sync.nas_available(self.nas, allow_local=False)
        self.assertFalse(ok)
        self.assertIn("Refusing", message)
        ok, _ = sync.nas_available(self.dir / "does-not-exist", allow_local=True)
        self.assertFalse(ok)


def id3_with_apic(audio_path, image, mime):
    """Prepend a hand-built ID3v2.3 tag with an APIC frame (like third-party taggers write)."""
    import struct
    payload = b"\x00" + mime.encode() + b"\x00" + b"\x03" + b"Cover\x00" + image
    frame = b"APIC" + struct.pack(">I", len(payload)) + b"\x00\x00" + payload
    tit2 = b"\x03Song"
    frame = b"TIT2" + struct.pack(">I", len(tit2)) + b"\x00\x00" + tit2 + frame
    size = len(frame)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    data = Path(audio_path).read_bytes()
    Path(audio_path).write_bytes(b"ID3\x03\x00\x00" + syncsafe + frame + data)


class CoverArtTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=TMP))
        config.ART_DIR = self.dir / "art"

    def test_webp_cover_ffmpeg_cannot_map_is_still_found(self):
        webp = self.dir / "c.webp"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                        "color=c=orange:s=320x180", "-frames:v", "1", str(webp)], check=True)
        mp3 = self.dir / "t.mp3"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=1",
                        "-c:a", "libmp3lame", "-write_id3v2", "0", str(mp3)], check=True)
        id3_with_apic(mp3, webp.read_bytes(), "image/webp")
        with mp3.open("rb") as f:
            mime, data = tags.read_picture(f)
        self.assertEqual((mime, tags.image_kind(data), tags.image_size(data)), ("image/webp", "webp", (320, 180)))
        ok, how = sync.extract_art(mp3, 77)
        self.assertTrue(ok, how)
        self.assertEqual((config.ART_DIR / "77.jpg").read_bytes()[:3], b"\xff\xd8\xff")

    def test_normal_jpeg_cover_via_ffmpeg_and_no_cover(self):
        mp3 = make_mp3(self.dir / "a.mp3", art=True)
        self.assertEqual(sync.extract_art(mp3, 1), (True, "ffmpeg"))
        plain = make_mp3(self.dir / "b.mp3")
        ok, how = sync.extract_art(plain, 2)
        self.assertFalse(ok)


def id3v24_with_apic(audio_path, image, plain_sizes=False, prefix=b""):
    """ID3v2.4 tag with an APIC frame; plain_sizes mimics taggers that ignore syncsafe sizes."""
    import struct
    def ss(n):
        return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    payload = b"\x00image/jpeg\x00\x03\x00" + prefix + image
    size = struct.pack(">I", len(payload)) if plain_sizes else ss(len(payload))
    frames = b"APIC" + size + b"\x00\x00" + payload + b"TIT2" + ss(5) + b"\x00\x00" + b"\x03Song"
    data = Path(audio_path).read_bytes()
    Path(audio_path).write_bytes(b"ID3\x04\x00\x00" + ss(len(frames) + 64) + frames + b"\x00" * 64 + data)


class CoverHealingTests(Base):
    """Covers recover by themselves when files go missing or a check fails once."""

    def cached_track(self, art):
        tid = self.add_tracks(1)[0]
        name = f"{tid % 100:02d}/{tid}.mp3"
        make_mp3(config.CACHE_DIR / name, art=art)
        self.conn.execute("UPDATE tracks SET cache_name = ? WHERE id = ?", (name, tid))
        self.conn.commit()
        return tid

    def row(self, tid):
        return self.conn.execute("SELECT has_art, art_checked FROM tracks WHERE id = ?", (tid,)).fetchone()

    def test_missing_cover_file_is_re_extracted(self):
        tid = self.cached_track(art=True)
        sync.backfill_art(self.conn)
        self.assertEqual(self.row(tid)["has_art"], 1)
        (config.ART_DIR / f"{tid}.jpg").unlink()           # e.g. data/art lost in a move
        self.assertEqual(sync.verify_art(self.conn), (1, 0))
        self.assertEqual(tuple(self.row(tid)), (0, 0))
        sync.backfill_art(self.conn)
        self.assertEqual(self.row(tid)["has_art"], 1)
        self.assertTrue((config.ART_DIR / f"{tid}.jpg").is_file())

    def test_forgotten_cover_file_is_relinked(self):
        tid = self.cached_track(art=True)
        sync.backfill_art(self.conn)
        self.conn.execute("UPDATE tracks SET has_art = 0")
        self.conn.commit()
        self.assertEqual(sync.verify_art(self.conn), (0, 1))
        self.assertEqual(self.row(tid)["has_art"], 1)

    def test_temporary_failure_is_retried_not_remembered(self):
        from unittest import mock
        tid = self.cached_track(art=True)
        with mock.patch.object(sync, "_ffmpeg_cover", side_effect=FileNotFoundError("ffmpeg")):
            ok, how = sync.extract_art(config.CACHE_DIR / f"{tid % 100:02d}/{tid}.mp3", tid)
            self.assertFalse(ok)
            self.assertTrue(how.startswith(sync.TEMPORARY), how)
            sync.backfill_art(self.conn)
        self.assertEqual(tuple(self.row(tid)), (0, 0))       # still queued
        sync.backfill_art(self.conn)
        self.assertEqual(self.row(tid)["has_art"], 1)

    def test_tracks_without_cover_are_rechecked_later(self):
        tid = self.cached_track(art=False)
        now = int(time.time())
        sync.backfill_art(self.conn, now=now)
        self.assertEqual(tuple(self.row(tid)), (0, now))
        calls = []
        real = sync.extract_art
        with unittest.mock.patch.object(sync, "extract_art", side_effect=lambda *a: calls.append(a) or real(*a)):
            sync.backfill_art(self.conn, now=now + 3600)
            self.assertEqual(calls, [])                      # checked recently
            sync.backfill_art(self.conn, now=now + sync.ART_RECHECK_SECONDS + 1)
            self.assertEqual(len(calls), 1)
        # Values written by v2.1/v2.2 (art_checked = 1) are re-checked once.
        self.conn.execute("UPDATE tracks SET art_checked = 1")
        self.conn.commit()
        self.assertEqual(sync.backfill_art(self.conn), 0)
        self.assertGreater(self.row(tid)["art_checked"], 1)

    def test_id3v24_plain_sizes_and_junk_before_image(self):
        jpg = self.dir / "c.jpg"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                        "color=c=red:s=300x300", "-frames:v", "1", str(jpg)], check=True)
        image = jpg.read_bytes()
        self.assertGreater(len(image), 200)                  # sizes differ syncsafe vs plain
        for plain, prefix in ((True, b""), (False, b"\x00\x00")):
            mp3 = self.dir / f"v24-{plain}.mp3"
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            "sine=d=1", "-c:a", "libmp3lame", "-write_id3v2", "0", str(mp3)], check=True)
            id3v24_with_apic(mp3, image, plain_sizes=plain, prefix=prefix)
            with mp3.open("rb") as f:
                mime, data = tags.read_picture(f)
            self.assertEqual((tags.image_kind(data), tags.image_size(data)), ("jpeg", (300, 300)))

    def test_second_id3_tag_holds_the_cover(self):
        mp3 = make_mp3(self.dir / "two.mp3", art=True)       # ffmpeg tag with a cover
        import struct
        tit2 = b"\x03New"
        frame = b"TIT2" + struct.pack(">I", len(tit2)) + b"\x00\x00" + tit2
        mp3.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00" + bytes([len(frame)]) + frame + mp3.read_bytes())
        with mp3.open("rb") as f:
            self.assertIsNotNone(tags.read_picture(f))


def png_bytes(width, height, directory):
    path = Path(directory) / f"img-{width}x{height}.png"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=navy:s={width}x{height}", "-frames:v", "1", str(path)], check=True)
    return path.read_bytes()


class BrandingTests(Base):
    def setUp(self):
        super().setUp()
        config.BRANDING_DIR = self.dir / "branding"

    def test_slot_rules_and_warnings(self):
        ok = png_bytes(600, 140, self.dir)
        self.assertEqual(branding.validate("logo", ok)[2], "")
        warn = branding.validate("logo", png_bytes(400, 400, self.dir))[2]
        self.assertIn("600 × 140", warn)                    # accepted, with advice
        with self.assertRaises(branding.BrandError):
            branding.validate("logo", png_bytes(100, 30, self.dir))     # too small
        with self.assertRaises(branding.BrandError):
            branding.validate("icon", png_bytes(512, 300, self.dir))    # not square
        with self.assertRaises(branding.BrandError):
            branding.validate("feature", b"<svg xmlns='http://www.w3.org/2000/svg'/>")  # no SVG here
        svg = b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'><rect width='10' height='10'/></svg>"
        self.assertEqual(branding.validate("logo", svg)[0], "svg")
        for bad in (b"<svg><script>alert(1)</script></svg>", b"<svg onload='x()'></svg>"):
            with self.assertRaises(branding.BrandError):
                branding.validate("logo", bad)

    def test_save_reset_and_defaults(self):
        settings = db.all_settings(self.conn)
        self.assertEqual(branding.url(settings, "feature"), "/assets/img/feature.jpg")
        self.assertIsNone(branding.url(settings, "logo"))
        result = branding.save(self.conn, "feature", png_bytes(800, 600, self.dir))
        settings = db.all_settings(self.conn)
        self.assertEqual(branding.url(settings, "feature"), result["url"])
        self.assertTrue((config.BRANDING_DIR / result["url"].split("/")[-1]).is_file())
        branding.save(self.conn, "feature", png_bytes(800, 600, self.dir))   # replaces, no pile-up
        self.assertEqual(len(list(config.BRANDING_DIR.glob("brand-feature-*"))), 1)
        branding.reset(self.conn, "feature")
        self.assertEqual(branding.url(db.all_settings(self.conn), "feature"), "/assets/img/feature.jpg")
        self.assertEqual(list(config.BRANDING_DIR.glob("brand-feature-*")), [])

    def test_texts_theme_and_css(self):
        self.assertEqual(branding.clean_texts({"slogan": "Hello *world*", "nope": "x",
                                               "brand_tag": branding.TEXT_DEFAULTS["brand_tag"]}),
                         {"slogan": "Hello *world*"})
        with self.assertRaises(branding.BrandError):
            branding.clean_theme({"accent": "red"})
        db.set_setting(self.conn, "theme", {"accent": "#00aa55"})
        db.set_setting(self.conn, "custom_css", "body { color: pink }")
        css = branding.custom_css(db.all_settings(self.conn))
        self.assertIn("--blue: #00aa55", css)
        self.assertIn("rgba(0, 170, 85, .35)", css)
        self.assertIn("body { color: pink }", css)
        self.assertEqual(branding.custom_js({}), "")
        css = branding.custom_css({"theme": {"accent": "#00aa55"},
                                   "custom_css": "h1{}\n@import url(\"x.css\");\np{}"})
        self.assertTrue(css.startswith('@import url("x.css");'), css)
        self.assertEqual(css.count("@import"), 1)

    def test_theme_pack_round_trip(self):
        branding.save(self.conn, "logo", png_bytes(600, 140, self.dir))
        db.set_setting(self.conn, "station_name", "Test FM")
        db.set_setting(self.conn, "texts", {"slogan": "Loud *and* proud"})
        db.set_setting(self.conn, "theme", {"highlight": "#ffaa00"})
        db.set_setting(self.conn, "custom_css", ".x{}")
        self.conn.commit()
        pack = self.dir / "pack"
        manifest = branding.export_pack(self.conn, pack)
        self.assertEqual(manifest["images"], {"logo": "logo.png"})
        # Fresh install: apply the pack.
        other = db.open_db(self.dir / "other.sqlite3")
        config.BRANDING_DIR = self.dir / "branding2"
        log = branding.import_pack(other, pack)
        self.assertIn("image logo", log)
        settings = db.all_settings(other)
        self.assertEqual(settings["station_name"], "Test FM")
        self.assertEqual(branding.texts(settings)["slogan"], "Loud *and* proud")
        self.assertEqual(branding.theme(settings)["highlight"], "#ffaa00")
        self.assertTrue(branding.custom_url(settings, "logo"))
        # A station that already has its own name keeps it.
        db.set_setting(other, "station_name", "Mine FM")
        other.commit()
        self.assertIn("kept your station_name", branding.import_pack(other, pack))
        self.assertEqual(db.all_settings(other)["station_name"], "Mine FM")
        other.close()


GEO_NETS = [
    ("8.8.8.0/24", {"city": {"names": {"en": "Mountain View"}},
                    "country": {"iso_code": "US", "names": {"en": "United States"}},
                    "location": {"latitude": 37.386, "longitude": -122.0838},
                    "subdivisions": [{"names": {"en": "California"}}]}),
    ("1.1.1.0/24", {"city": {"names": {"en": "Brisbane"}},
                    "country": {"iso_code": "AU", "names": {"en": "Australia"}},
                    "location": {"latitude": -27.4679, "longitude": 153.0281},
                    "subdivisions": [{"names": {"en": "Queensland"}}]}),
    ("2a01:4f8::/32", {"city": {"names": {"en": "Berlin"}},
                       "country": {"iso_code": "DE", "names": {"en": "Germany"}},
                       "location": {"latitude": 52.52, "longitude": 13.405}}),
]


def install_geo_db(folder, record_size=28):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    mmdb_writer.write(folder / "dbip-city-lite.mmdb", GEO_NETS, record_size)
    os.environ["AIR_GEOIP_DIR"] = str(folder)
    geoip._state.update(checked=0, path=None, mtime=0)
    geoip._lookup.cache_clear()


class GeoIPTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=TMP))

    def test_reader_all_record_sizes(self):
        for size in (24, 28, 32):
            path = self.dir / f"t{size}.mmdb"
            mmdb_writer.write(path, GEO_NETS, size)
            r = geoip.Reader(path)
            self.assertEqual(r.get("8.8.8.8")["city"]["names"]["en"], "Mountain View")
            self.assertEqual(r.get("1.1.1.200")["country"]["iso_code"], "AU")
            self.assertAlmostEqual(r.get("1.1.1.1")["location"]["latitude"], -27.4679)
            self.assertEqual(r.get("2a01:4f8::42")["city"]["names"]["en"], "Berlin")
            self.assertEqual(r.get("::ffff:8.8.8.8")["country"]["iso_code"], "US")
            self.assertIsNone(r.get("9.9.9.9"))
            self.assertEqual(r.metadata["database_type"], "Test-City")
            r.close()

    def test_lookup_helper_and_local_addresses(self):
        install_geo_db(self.dir / "geo")
        self.assertEqual(geoip.lookup("8.8.8.8"), {"country": "US", "country_name": "United States",
                                                    "region": "California", "city": "Mountain View",
                                                    "lat": 37.39, "lon": -122.08})
        self.assertEqual(geoip.lookup("192.168.1.5")["country_name"], "Local network")
        self.assertIsNone(geoip.lookup("9.9.9.9"))
        self.assertIsNone(geoip.lookup("not an ip"))
        self.assertTrue(geoip.info()["available"])
        with self.assertRaises(geoip.InvalidDatabase):
            bad = self.dir / "bad.mmdb"
            bad.write_bytes(b"nope" * 100)
            geoip.Reader(bad)


class StatsTests(Base):
    def setUp(self):
        super().setUp()
        install_geo_db(self.dir / "geo")

    @staticmethod
    def client(cid, ip, connected, ua="Mozilla/5.0 (Windows NT 10.0) Chrome/120.0 Safari/537.36"):
        return {"id": str(cid), "ip": "172.18.0.4", "agent": f"{ua} [airstation-ip:{ip}]", "connected": connected}

    def test_agent_parsing_and_player_names(self):
        self.assertEqual(stats.split_agent("VLC/3.0.20 LibVLC/3.0.20 [airstation-ip:8.8.8.8]"),
                         ("VLC/3.0.20 LibVLC/3.0.20", "8.8.8.8"))
        self.assertEqual(stats.split_agent("curl/8.0"), ("curl/8.0", None))
        # a client can't pick its own location: Caddy's marker (the last one) wins
        self.assertEqual(stats.split_agent("x [airstation-ip:9.9.9.9] [airstation-ip:8.8.4.4]"), ("x", "8.8.4.4"))
        self.assertEqual(stats.player_name("VLC/3.0.20 LibVLC/3.0.20"), "VLC")
        self.assertEqual(stats.player_name("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari/604.1"), "Safari · iPhone")
        self.assertEqual(stats.player_name("Mozilla/5.0 (X11; Linux x86_64) Firefox/131.0"), "Firefox · Linux")
        self.assertEqual(stats.player_name("Googlebot/2.1"), "Bot / script")

    def test_sessions_open_update_close_and_report(self):
        t0 = 1_790_000_000
        stats.track(self.conn, "/radio.mp3", [self.client(1, "8.8.8.8", 0), self.client(2, "1.1.1.1", 30)], t0)
        stats.track(self.conn, "/radio-low.mp3", [{"id": "9", "ip": "127.0.0.1", "agent": "check.sh", "connected": 5}], t0)
        rows = self.conn.execute("SELECT * FROM listener_sessions ORDER BY id").fetchall()
        self.assertEqual([(r["city"], r["internal"], r["player"]) for r in rows],
                         [("Mountain View", 0, "Chrome · Windows"), ("Brisbane", 0, "Chrome · Windows"), ("", 1, "Other")])
        self.assertEqual(rows[1]["started_at"], t0 - 30)
        self.assertNotIn("8.8.8.8", str([tuple(r) for r in rows]))      # no raw IPs stored
        stats.track(self.conn, "/radio.mp3", [self.client(1, "8.8.8.8", 600), self.client(2, "1.1.1.1", 630)], t0 + 600)
        stats.track(self.conn, "/radio.mp3", [self.client(2, "1.1.1.1", 1230)], t0 + 1200)   # #1 left
        one = self.conn.execute("SELECT * FROM listener_sessions WHERE client_key = '/radio.mp3#1'").fetchone()
        self.assertEqual((one["ended_at"], one["duration"]), (t0 + 600, 600))
        # Icecast restarted and handed id 2 to someone new: old session closes, new one opens.
        stats.track(self.conn, "/radio.mp3", [self.client(2, "2a01:4f8::5", 10)], t0 + 1300)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM listener_sessions WHERE client_key = '/radio.mp3#2'").fetchone()[0], 2)
        stats.sample(self.conn, 2, t0 + 600)
        stats.sample(self.conn, 1, t0 + 610)                # same minute keeps the max
        self.assertEqual(self.conn.execute("SELECT listeners FROM listener_samples").fetchone()[0], 2)
        r = stats.report(self.conn, "24h", now=t0 + 1300)
        self.assertEqual(r["totals"]["sessions"], 3)        # internal check and none-too-short excluded
        self.assertEqual(r["totals"]["listeners"], 3)
        self.assertEqual(r["totals"]["peak"], 2)
        self.assertEqual({c["country"] for c in r["countries"]}, {"US", "AU", "DE"})
        self.assertEqual(len(r["points"]), 3)
        self.assertEqual([l["city"] for l in r["live"]], ["Berlin"])
        self.assertEqual(sum(b["sessions"] for b in r["lengths"]), 3)
        self.assertEqual(len(r["timeline"]), 25)
        csv = stats.sessions_csv(self.conn, "30d", now=t0 + 1300)
        self.assertIn('"Mountain View"', csv)
        self.assertEqual(len(csv.strip().splitlines()), 4)

    def test_stale_sessions_close_and_poll_respects_switch(self):
        t0 = 1_790_000_000
        stats.track(self.conn, "/radio.mp3", [self.client(1, "8.8.8.8", 0)], t0)
        stats.close_stale(self.conn, t0 + stats.STALE_AFTER + 1)
        self.assertIsNotNone(self.conn.execute("SELECT ended_at FROM listener_sessions").fetchone()[0])
        calls = []
        self.assertEqual(stats.poll(self.conn, fetch=lambda m: calls.append(m) or ([self.client(5, "1.1.1.1", 3)] if m == "/radio.mp3" else []),
                                    now=t0 + 400), 1)
        self.assertEqual(calls, list(stats.MOUNTS))
        db.set_setting(self.conn, "stats_enabled", False)
        self.assertEqual(stats.poll(self.conn, fetch=lambda m: 1 / 0, now=t0 + 500), 0)

    def test_map_config(self):
        cfg = stats.map_config({})
        self.assertEqual((cfg["style"], cfg["darken"]), ("osm-dark", True))
        self.assertTrue(cfg["tiles"].startswith("https://tile.openstreetmap.org/"))
        cfg = stats.map_config({"map_style": "carto-dark"})                  # no key yet
        self.assertEqual(cfg["style"], "osm-dark")
        self.assertIn("CARTO key", cfg["note"])
        cfg = stats.map_config({"map_style": "carto-dark", "map_carto_key": " ab c/1234 "})
        self.assertTrue(cfg["tiles"].endswith("?key=ab%20c%2F1234"))
        self.assertIn("CARTO", cfg["attribution"])
        cfg = stats.map_config({"map_style": "custom", "map_tiles_url": "https://t.example.com/{z}/{x}/{y}.png",
                                "map_attribution": "<b>Me</b>"})
        self.assertEqual((cfg["tiles"], cfg["attribution"]), ("https://t.example.com/{z}/{x}/{y}.png", "&lt;b&gt;Me&lt;/b&gt;"))
        self.assertEqual(stats.map_config({"map_style": "custom", "map_tiles_url": "javascript:alert(1)"})["style"], "osm-dark")
        self.assertEqual(stats.map_config({}, "https://env.example/{z}/{x}/{y}.png")["style"], "env")

    def test_hls_listeners_become_sessions(self):
        stats.hls_reset()
        t0 = 1_790_000_000
        ua = "TiviMate/5.1 (Android 14)"
        for dt in range(0, 61, 6):                         # a player refreshing the playlist
            stats.hls_seen("8.8.8.8", ua, now=t0 + dt)
        stats.hls_seen("1.1.1.1", ua, now=t0 + 60)
        self.assertEqual(len(stats.hls_clients(now=t0 + 60)), 2)
        self.assertEqual(stats.poll(self.conn, fetch=lambda m: [], now=t0 + 60), 2)
        row = self.conn.execute("SELECT * FROM listener_sessions WHERE mount = '/radio.m3u8' AND city = 'Mountain View'").fetchone()
        self.assertEqual((row["internal"], row["started_at"]), (0, t0))
        # gone quiet for longer than HLS_GONE_AFTER: the session closes
        self.assertEqual(stats.hls_clients(now=t0 + 60 + stats.HLS_GONE_AFTER + 1), [])
        stats.poll(self.conn, fetch=lambda m: [], now=t0 + 100)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM listener_sessions WHERE ended_at IS NULL").fetchone()[0], 0)
        r = stats.report(self.conn, "24h", now=t0 + 200)
        self.assertIn("/radio.m3u8", [m["mount"] for m in r["mounts"]])
        stats.hls_reset()

    def test_hit_flood_is_capped(self):
        ua = "Mozilla/5.0 (Windows NT 10.0) Chrome/120.0"
        stored = [stats.record_hit(self.conn, "8.8.8.8", f"/p{i}", "", ua, now=1_790_000_000 + i) for i in range(30)]
        self.assertEqual(sum(stored), stats.HIT_LIMIT)

    def test_page_hits(self):
        ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Mobile Safari/604.1"
        t0 = 1_790_000_000
        self.assertTrue(stats.record_hit(self.conn, "8.8.8.8", "/", "https://www.google.com/search", ua, "radio.example.com", t0))
        self.assertFalse(stats.record_hit(self.conn, "8.8.8.8", "/", "", ua, "radio.example.com", t0 + 60))   # repeat
        self.assertTrue(stats.record_hit(self.conn, "8.8.8.8", "/live/", "https://radio.example.com/", ua, "radio.example.com", t0 + 70))
        self.assertFalse(stats.record_hit(self.conn, "1.1.1.1", "/", "", "Googlebot/2.1", "", t0))
        self.assertFalse(stats.record_hit(self.conn, "1.1.1.1", "/<script>", "", ua, "", t0))
        web = stats.report(self.conn, "24h", now=t0 + 100)["web"]
        self.assertEqual((web["views"], web["visitors"]), (2, 1))
        self.assertEqual([p["page"] for p in web["pages"]], ["/", "/live"] if web["pages"][0]["page"] == "/" else ["/live", "/"])
        self.assertEqual([r["referrer"] for r in web["referrers"]], ["www.google.com"])   # own site not a referrer
        self.assertEqual(web["devices"][0]["device"], "Mobile")
        self.assertEqual(web["points"][0]["city"], "Mountain View")


class DuplicateTests(Base):
    def test_groups_by_title_and_file(self):
        rows = [("A/Little Simz - Gorilla (Official Video).mp3", "", "Little Simz - Gorilla (Official Video)", 5000000),
                ("B/gorilla.mp3", "Little Simz", "Gorilla", 6000000),
                ("C/x.mp3", "Nujabes", "Feather", 7000000),
                ("D/copy of x.mp3", "Nujabes", "Feather (Remastered)", 7000000),
                ("E/remix.mp3", "Little Simz", "Gorilla (Remix)", 5500000)]
        for rel, artist, title, size in rows:
            self.conn.execute("INSERT INTO tracks (relpath, artist, title, size) VALUES (?,?,?,?)",
                              (rel, artist, title, size))
        self.conn.commit()
        found = duplicates.find(self.conn)
        sets = [sorted(t["relpath"] for t in g["tracks"]) for g in found["groups"]]
        self.assertIn(["A/Little Simz - Gorilla (Official Video).mp3", "B/gorilla.mp3"], sets)
        self.assertIn(["C/x.mp3", "D/copy of x.mp3"], sets)
        self.assertEqual(found["total_groups"], 2)  # the remix is a different track
        group = next(g for g in found["groups"] if g["reason"] == "title")
        others = [t["id"] for t in group["tracks"] if t["id"] != group["keep"]]
        duplicates.resolve(self.conn, group["keep"], others)
        self.assertEqual(duplicates.find(self.conn)["total_groups"], 1)
        self.assertEqual(duplicates.find(self.conn, include_resolved=True)["total_groups"], 2)


class EmergencyTests(unittest.TestCase):
    def test_subfolders_with_spaces_and_rotation(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        for i in range(4):
            make_mp3(d / "Alex Jones" / f"Track {i}.mp3", "Alex Jones", f"T{i}")
        (d / "notes.txt").write_text("not audio")
        os.environ["AIR_EMERGENCY_DIR"] = str(d)
        try:
            files, unreadable = emergency.scan(force=True)
            self.assertEqual(len(files), 4)
            self.assertTrue(all(f.startswith("Alex Jones/") for f in files))
            picks = [emergency.pick() for _ in range(2)]
            self.assertNotEqual(picks[0], picks[1])  # no immediate repeat
            uri = emergency.liquidsoap_uri(picks[0])
            self.assertRegex(uri, r'^annotate:air_id="em-[0-9a-f]{16}":/emergency/Alex Jones/')
        finally:
            os.environ.pop("AIR_EMERGENCY_DIR")
            emergency.scan(force=True)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fake_liquidsoap import FakeLiquidsoap
        from airstation import server
        cls.tmp = Path(tempfile.mkdtemp(dir=TMP))
        secrets = Path(os.environ["AIR_SECRETS_DIR"])
        secrets.mkdir(parents=True, exist_ok=True)
        for name, value in (("admin_ui_password", "letmein"), ("internal_token", "tok"),
                            ("session_secret", "s3"), ("dj_password", "dj"),
                            ("obs_publish_password", "obs")):
            (secrets / name).write_text(value)
        config.DB_PATH = cls.tmp / "api.sqlite3"
        config.ICECAST_URL = "http://127.0.0.1:9"
        conn = db.open_db(config.DB_PATH)
        for i in range(8):
            conn.execute("INSERT INTO tracks (relpath, folder, size, artist, title, cached, cache_name, "
                         "cache_bytes, duration) VALUES (?, 'F', 1, ?, ?, 1, ?, 1, 180)",
                         (f"F/{i}.mp3", f"Artist {i}", f"Song {i}", f"0{i}/{i}.mp3"))
        conn.commit()
        conn.close()
        cls.httpd = server.serve("127.0.0.1", 0, db_path=config.DB_PATH, start_threads=False)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.fake = FakeLiquidsoap(cls.base, "tok")
        config.LIQ_HOST, config.LIQ_TELNET_PORT = "127.0.0.1", cls.fake.port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.station = server.Handler.station

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.fake.stop()

    def call(self, path, method="GET", body=None, headers=None, cookie=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        if cookie:
            h["Cookie"] = cookie
        req = urllib.request.Request(self.base + path, method=method, headers=h,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                raw = r.read().decode()
                return r.status, raw, r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), e.headers

    def login(self):
        status, _, headers = self.call("/api/admin/login", "POST", {"password": "letmein"})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";")[0]

    def test_internal_next_requires_token(self):
        self.assertEqual(self.call("/internal/next")[0], 403)
        status, body, _ = self.call("/internal/next", headers={"X-Air-Token": "tok"})
        self.assertEqual(status, 200)
        self.assertIn("annotate:air_id=", body)

    def test_nowplaying_follows_liquidsoap_and_records_history(self):
        self.fake.advance()
        self.station.poll_liquidsoap()
        status, body, _ = self.call("/api/nowplaying")
        data = json.loads(body)
        self.assertEqual(data["source"], "autodj")
        self.assertTrue(data["now"]["title"].startswith("Song"))
        self.fake.go_live("Friday Set", "DJ Example")
        self.station.poll_liquidsoap()
        data = json.loads(self.call("/api/nowplaying")[1])
        self.assertTrue(data["is_live"])
        self.assertEqual(data["now"]["artist"], "DJ Example")
        self.fake.state.update(source="autodj", live_connected=False)

    def test_page_hit_beacon_and_stats_report(self):
        install_geo_db(self.tmp / "geo")
        ua = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/131.0", "Content-Type": "text/plain"}
        req = urllib.request.Request(self.base + "/api/hit", method="POST", headers=ua,
                                     data=json.dumps({"p": "/live/", "r": "https://www.google.com/x"}).encode())
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertTrue(json.loads(r.read())["ok"])
        status, body, _ = self.call("/api/hit", "POST", {"p": "/"}, headers={"User-Agent": "curl/8"})
        self.assertFalse(json.loads(body)["ok"])                      # scripts aren't visitors
        self.assertEqual(self.call("/api/admin/stats")[0], 401)
        cookie = self.login()
        status, body, _ = self.call("/api/admin/stats?range=30d", cookie=cookie)
        self.assertEqual(status, 200, body)
        data = json.loads(body)
        self.assertGreaterEqual(data["web"]["views"], 1)
        self.assertIn({"referrer": "www.google.com", "views": 1, "visitors": 1}, data["web"]["referrers"])
        self.assertTrue(data["map"]["tiles"].startswith("https://tile.openstreetmap.org/"))
        self.assertEqual(len(data["timeline"]), 31)
        status, body, headers = self.call("/api/admin/stats/sessions.csv?range=7d", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith("started_utc,"))
        self.assertIn("attachment", headers["Content-Disposition"])
        h = {"X-Requested-With": "air-admin"}
        status, body, _ = self.call("/api/admin/settings", "PUT", {"map_style": "nope"}, h, cookie)
        self.assertEqual(status, 400)
        status, body, _ = self.call("/api/admin/settings", "PUT", {"map_tiles_url": "http://x/{z}"}, h, cookie)
        self.assertEqual(status, 400)
        status, body, _ = self.call("/api/admin/settings", "PUT",
                                    {"map_style": "carto-dark", "map_carto_key": "KEY_12345678"}, h, cookie)
        self.assertEqual(status, 200, body)
        data = json.loads(self.call("/api/admin/stats", cookie=cookie)[1])
        self.assertTrue(data["map"]["tiles"].endswith("dark_all/{z}/{x}/{y}.png?key=KEY_12345678"))
        self.assertNotIn("KEY_", self.call("/api/site")[1])                     # never public
        self.call("/api/admin/settings", "PUT", {"map_style": "osm-dark", "map_carto_key": ""}, h, cookie)
        status, body, _ = self.call("/api/admin/settings", "PUT", {"stats_retention_days": 90}, h, cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(self.call("/api/admin/settings", cookie=cookie)[1])["stats_retention_days"], 90)

    def test_studio_locks_after_trial_but_station_and_dashboard_work(self):
        cookie = self.login()
        h = {"X-Requested-With": "air-admin"}
        with unittest.mock.patch.object(licensing, "PUBLIC_KEY", ed25519.public_key(bytes(32)).hex()):
            conn = db.connect(config.DB_PATH)
            db.set_setting(conn, "_trial_started", 1)
            conn.commit()
            licensing.forget()
            status, body, _ = self.call("/api/admin/playlists", cookie=cookie)
            self.assertEqual(status, 402, body)
            self.assertEqual(self.call("/api/admin/status", cookie=cookie)[0], 200)
            self.assertEqual(json.loads(self.call("/api/admin/me", cookie=cookie)[1])["licence"]["status"], "expired")
            data = json.loads(self.call("/api/admin/licence", cookie=cookie)[1])
            self.assertEqual(len(data["install_id"]), 32)
            status, body, _ = self.call("/api/admin/licence/activate", "POST", {"key": "nope"}, h, cookie)
            self.assertEqual(status, 400)
            self.assertEqual(self.call("/api/nowplaying")[0], 200)                     # public site unaffected
            self.assertEqual(self.call("/internal/next", headers={"X-Air-Token": "tok"})[0], 200)   # AutoDJ too
            conn.execute("DELETE FROM settings WHERE key = '_trial_started'")     # fresh trial again
            conn.commit()
        licensing.forget()
        self.assertEqual(self.call("/api/admin/playlists", cookie=cookie)[0], 200)

    def test_hls_playlist(self):
        stats.hls_reset()
        hls = self.tmp / "hls"
        with unittest.mock.patch.object(config, "HLS_DIR", hls):
            self.assertEqual(self.call("/radio.m3u8")[0], 503)
            hls.mkdir(exist_ok=True)
            (hls / "radio.m3u8").write_text("#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nradio_1.ts\n")
            status, body, headers = self.call("/radio.m3u8", headers={"User-Agent": "VLC/3.0", "X-Forwarded-For": "8.8.8.8"})
            self.assertEqual(status, 200)
            self.assertTrue(body.startswith("#EXTM3U"))
            self.assertEqual(headers["Content-Type"], "application/vnd.apple.mpegurl")
            self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
            req = urllib.request.Request(self.base + "/radio.m3u8", method="HEAD")
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual((r.status, r.read()), (200, b""))
            self.assertEqual([c["ip"] for c in stats.hls_clients()], ["8.8.8.8", "127.0.0.1"])
            cookie = self.login()
            streams = json.loads(self.call("/api/admin/connections", cookie=cookie)[1])["streams"]
            self.assertTrue(streams["hls"].endswith("/radio.m3u8"))
            self.assertTrue(streams["hls_running"])
        stats.hls_reset()

    def test_saving_the_look_rewrites_the_pages(self):
        site_dir = self.tmp / "site"
        web = Path(__file__).resolve().parents[1] / "web"
        with unittest.mock.patch.object(config, "SITE_DIR", site_dir), unittest.mock.patch.object(config, "WEB_DIR", web):
            cookie = self.login()
            h = {"X-Requested-With": "air-admin"}
            status, body, _ = self.call("/api/admin/appearance", "PUT",
                                        {"texts": {"page_title": "{station} | Test Title"}}, h, cookie)
            self.assertEqual(status, 200, body)
            self.assertIn("| Test Title</title>", (site_dir / "index.html").read_text())
            seo = json.loads(self.call("/api/site")[1])["seo"]
            self.assertTrue(seo["title"].endswith("| Test Title"))
            self.call("/api/admin/appearance", "PUT", {"texts": {"page_title": ""}}, h, cookie)

    def test_admin_requires_login_and_header(self):
        self.assertEqual(self.call("/api/admin/status")[0], 401)
        self.assertEqual(self.call("/api/admin/login", "POST", {"password": "nope"})[0], 401)
        cookie = self.login()
        self.assertEqual(self.call("/api/admin/status", cookie=cookie)[0], 200)
        # State-changing calls need the admin header (CSRF guard).
        self.assertEqual(self.call("/api/admin/skip", "POST", {}, cookie=cookie)[0], 403)
        status, _, _ = self.call("/api/admin/skip", "POST", {}, cookie=cookie,
                                 headers={"X-Requested-With": "air-admin"})
        self.assertEqual(status, 200)
        self.assertIn("air.skip", self.fake.commands)

    def test_playlist_show_settings_roundtrip(self):
        cookie = self.login()
        h = {"X-Requested-With": "air-admin"}
        status, body, _ = self.call("/api/admin/playlists", "POST",
                                    {"name": "Folder F", "kind": "folders", "rule": ["F"]}, h, cookie)
        self.assertEqual(status, 200, body)
        pid = json.loads(body)["id"]
        status, body, _ = self.call("/api/admin/shows", "POST",
                                    {"name": "Test Show", "days": [0, 1, 2, 3, 4, 5, 6],
                                     "start_time": "00:00", "end_time": "00:00",
                                     "playlist_id": pid}, h, cookie)
        self.assertEqual(status, 200, body)
        sched = json.loads(self.call("/api/schedule")[1])
        self.assertEqual(sched["upcoming"][0]["name"], "Test Show")
        self.assertEqual(self.call("/api/admin/shows", "POST", {"name": "Bad", "days": [1],
                         "start_time": "25:00", "end_time": "01:00"}, h, cookie)[0], 400)
        self.call("/api/admin/settings", "PUT", {"tagline": "Hello", "social": {"x": "https://x.test"}},
                  h, cookie)
        site = json.loads(self.call("/api/site")[1])
        self.assertEqual(site["tagline"], "Hello")
        self.assertNotIn("record_mode", site)
        pls = json.loads(self.call("/api/admin/playlists", cookie=cookie)[1])["playlists"]
        self.assertEqual(next(p for p in pls if p["id"] == pid)["tracks"], 8)

    def test_cover_urls_follow_files_and_default_falls_back(self):
        from airstation import server
        config.ART_DIR = self.tmp / "art"
        config.ART_DIR.mkdir(exist_ok=True)
        config.BRANDING_DIR = self.tmp / "branding2"
        config.BRANDING_DIR.mkdir(exist_ok=True)
        conn = db.open_db(config.DB_PATH)
        conn.execute("UPDATE tracks SET has_art = 1 WHERE id = 1")
        conn.execute("UPDATE tracks SET has_art = 0 WHERE id = 2")
        conn.commit()
        self.assertIsNone(server.art_url(1))                 # has_art but no file
        (config.ART_DIR / "2.jpg").write_bytes(b"\xff\xd8\xff")
        self.assertRegex(server.art_url(2), r"^/art/2\.jpg\?v=\d+$")
        self.assertIsNone(server.art_url("../etc/passwd"))
        self.assertIsNone(server.art_url("em-0123456789abcdef"))
        (config.ART_DIR / "em-0123456789abcdef.jpg").write_bytes(b"\xff\xd8\xff")
        self.assertRegex(server.art_url("em-0123456789abcdef"), r"^/art/em-0123456789abcdef\.jpg\?v=")
        db.set_setting(conn, "default_art", "/branding/default-art-1.png")
        conn.commit()
        self.assertEqual(json.loads(self.call("/api/site")[1])["default_art"], config.DEFAULT_ART_URL)
        (config.BRANDING_DIR / "default-art-1.png").write_bytes(b"x")
        self.assertEqual(json.loads(self.call("/api/site")[1])["default_art"], "/branding/default-art-1.png")
        db.set_setting(conn, "default_art", "")
        conn.execute("UPDATE tracks SET has_art = 0")
        conn.commit()
        conn.close()
        (config.ART_DIR / "2.jpg").unlink()

    def test_track_without_air_id_is_matched_by_tags(self):
        st = self.station
        state = {"source": "autodj", "title": "Song 3", "artist": "Artist 3", "air_id": ""}
        self.assertEqual(st.resolve_air_id(state), "4")
        self.assertEqual(st.resolve_air_id(dict(state, air_id="7")), "7")
        self.assertEqual(st.resolve_air_id(dict(state, source="live")), "")
        self.assertEqual(st.resolve_air_id(dict(state, title="Unknown")), "")

    def test_appearance_api_brand_redirect_and_custom_code(self):
        cookie = self.login()
        h = {"X-Requested-With": "air-admin"}
        config.BRANDING_DIR = self.tmp / "branding3"
        import base64
        d = Path(tempfile.mkdtemp(dir=TMP))
        import http.client

        def brand(slot):
            conn = http.client.HTTPConnection(self.base.split("//")[1])
            conn.request("GET", f"/api/brand/{slot}")
            r = conn.getresponse()
            r.read()
            conn.close()
            return r.status, r.getheader("Location")
        self.assertEqual(brand("feature"), (302, "/assets/img/feature.jpg"))
        self.assertEqual(brand("logo")[0], 404)          # no default: text is shown instead
        data = base64.b64encode(png_bytes(800, 600, d)).decode()
        status, body, _ = self.call("/api/admin/brand/feature", "POST", {"data": data}, h, cookie)
        self.assertEqual(status, 200, body)
        new_url = json.loads(body)["url"]
        self.assertEqual(brand("feature"), (302, new_url))
        self.assertEqual(self.call("/api/admin/brand/feature", "POST", {"data": "eA=="}, h, cookie)[0], 400)
        self.assertEqual(self.call("/api/admin/brand/nope", "POST", {"data": data}, h, cookie)[0], 404)
        status, body, _ = self.call("/api/admin/appearance", cookie=cookie)
        info = json.loads(body)
        self.assertTrue(next(i for i in info["images"] if i["key"] == "feature")["custom"])
        self.assertIn("1400 × 330", next(i for i in info["images"] if i["key"] == "hero")["size"])
        self.assertEqual(self.call("/api/admin/appearance", "PUT", {"theme": {"accent": "blue"}}, h, cookie)[0], 400)
        self.assertEqual(self.call("/api/admin/appearance", "PUT", {
            "texts": {"slogan": "Hi *there*"}, "theme": {"accent": "#112233"},
            "custom_css": "h1{color:red}", "custom_js": "console.log(1)"}, h, cookie)[0], 200)
        status, css, headers = self.call("/api/custom.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers["Content-Type"])
        self.assertIn("--blue: #112233", css)
        self.assertIn("h1{color:red}", css)
        req = urllib.request.Request(self.base + "/api/custom.css", headers={"If-None-Match": headers["ETag"]})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 304)
        self.assertIn("console.log(1)", self.call("/api/custom.js")[1])
        site = json.loads(self.call("/api/site")[1])
        self.assertEqual(site["texts"]["slogan"], "Hi *there*")
        self.assertIn("feature", site["custom_images"])
        self.call("/api/admin/brand/feature", "DELETE", None, h, cookie)
        self.call("/api/admin/appearance", "PUT", {"texts": {}, "theme": {}, "custom_css": "", "custom_js": ""}, h, cookie)
        self.assertEqual(self.call("/api/custom.js")[1], "")

    def test_default_art_upload_rules(self):
        import base64
        cookie = self.login()
        h = {"X-Requested-With": "air-admin"}
        config.BRANDING_DIR = self.tmp / "branding"
        d = Path(tempfile.mkdtemp(dir=TMP))
        for name, size in (("ok.png", "512x512"), ("wide.png", "800x400"), ("tiny.png", "100x100")):
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                            f"color=c=black:s={size}", "-frames:v", "1", str(d / name)], check=True)
        def up(name):
            data = base64.b64encode((d / name).read_bytes()).decode()
            return self.call("/api/admin/default-art", "POST", {"data": "data:image/png;base64," + data}, h, cookie)
        self.assertEqual(up("wide.png")[0], 400)
        self.assertEqual(up("tiny.png")[0], 400)
        status, body, _ = up("ok.png")
        self.assertEqual(status, 200, body)
        url = json.loads(body)["url"]
        self.assertTrue((config.BRANDING_DIR / url.split("/")[-1]).is_file())
        self.assertEqual(json.loads(self.call("/api/site")[1])["default_art"], url)
        self.call("/api/admin/default-art", "DELETE", None, h, cookie)
        self.assertEqual(json.loads(self.call("/api/site")[1])["default_art"], config.DEFAULT_ART_URL)

    def test_video_status_proxy(self):
        from unittest import mock
        from airstation import server
        server._video_cache.update(at=0, data=None)
        fake = mock.MagicMock()
        fake.__enter__.return_value.read.return_value = json.dumps(
            {"online": True, "viewerCount": 7, "streamTitle": "Friday Session"}).encode()
        real = urllib.request.urlopen

        def fake_urlopen(req, *a, **kw):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            return fake if url.endswith("/api/status") else real(req, *a, **kw)
        with mock.patch.object(server.urllib.request, "urlopen", side_effect=fake_urlopen), \
                mock.patch.object(config, "OWNCAST_URL", "https://cast.example.com"):
            data = json.loads(self.call("/api/video-status")[1])
        self.assertTrue(data["online"])
        self.assertEqual((data["viewers"], data["title"]), (7, "Friday Session"))
        self.assertTrue(data["embed_video"].endswith("/embed/video"))

    def test_community_latest_from_nodebb(self):
        from unittest import mock
        from airstation import server
        server._forum_cache.update(at=0, data=None)
        fake = mock.MagicMock()
        fake.__enter__.return_value.read.return_value = json.dumps({"topics": [
            {"title": "Technics 1210 for sale", "slug": "12/technics-1210-for-sale", "postcount": 3,
             "lastposttimeISO": "2026-10-06T01:00:00.000Z", "category": {"name": "Studio & DJ Gear"},
             "user": {"username": "dj_example"}}]}).encode()
        real = urllib.request.urlopen

        def fake_urlopen(req, *a, **kw):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            return fake if url.endswith("/api/recent") else real(req, *a, **kw)
        os.environ["AIR_FORUM_INTERNAL_URL"] = "http://nodebb:4567"
        os.environ["AIR_FORUM_DOMAIN"] = "forum.example.org"
        try:
            with mock.patch.object(server.urllib.request, "urlopen", side_effect=fake_urlopen):
                data = json.loads(self.call("/api/community/latest")[1])
        finally:
            os.environ.pop("AIR_FORUM_INTERNAL_URL")
            os.environ.pop("AIR_FORUM_DOMAIN")
            server._forum_cache.update(at=0, data=None)
        self.assertTrue(data["available"])
        self.assertEqual(data["posts"][0]["url"], "https://forum.example.org/topic/12/technics-1210-for-sale")
        self.assertEqual(data["posts"][0]["replies"], 2)

    def test_public_request_flow(self):
        conn = db.connect(config.DB_PATH)
        conn.execute("UPDATE tracks SET last_played = NULL")  # other tests play tracks
        conn.execute("DELETE FROM queue")
        conn.commit()
        conn.close()
        results = json.loads(self.call("/api/search?q=Song%205")[1])["results"]
        self.assertTrue(results)
        status, body, _ = self.call("/api/request", "POST", {"track_id": results[0]["id"]},
                                    headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(status, 200, body)
        status, body, _ = self.call("/api/request", "POST", {"track_id": results[0]["id"]},
                                    headers={"X-Forwarded-For": "203.0.113.10"})
        self.assertEqual(status, 409)


class CommunityTests(unittest.TestCase):
    @unittest.skipUnless(os.geteuid() == 0, "needs root (chown to uid 10001)")
    def test_rauthy_config_renders_valid_toml_with_awkward_password(self):
        import importlib.util
        import tomllib
        spec = importlib.util.spec_from_file_location("community", ROOT / "scripts" / "community.py")
        community = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(community)
        base = Path(tempfile.mkdtemp(dir=TMP))
        community.ROOT = base
        community.SECRETS = base / "secrets"
        community.RAUTHY_DIR = base / "data" / "community" / "rauthy"
        (base / "data" / "community").mkdir(parents=True)
        community.secret_file("smtp_password", value='p@ss"wo\\rd\'x')
        community.render_rauthy({"AIR_ID_DOMAIN": "id.example.org", "AIR_ADMIN_EMAIL": "me@example.org",
                                 "AIR_SMTP_HOST": "smtp-relay.brevo.com", "AIR_SMTP_PORT": "587",
                                 "AIR_SMTP_USERNAME": "user@example.org", "AIR_SMTP_TLS": "starttls"})
        conf = tomllib.loads((community.RAUTHY_DIR / "config.toml").read_text())
        self.assertEqual(conf["server"]["pub_url"], "id.example.org")
        self.assertTrue(conf["server"]["proxy_mode"])
        self.assertEqual(conf["email"]["smtp_password"], 'p@ss"wo\\rd\'x')
        self.assertEqual(conf["email"]["smtp_port"], 587)
        key_id = conf["encryption"]["key_active"]
        entry = conf["encryption"]["keys"][0]
        self.assertTrue(entry.startswith(key_id + "/"))
        self.assertEqual(len(base64_decode(entry.split("/", 1)[1])), 32)
        self.assertEqual(len(key_id), 16)
        self.assertEqual(conf["webauthn"]["rp_origin"], "https://id.example.org:443")  # Rauthy requires the port
        self.assertEqual((community.RAUTHY_DIR / "config.toml").stat().st_mode & 0o777, 0o600)

    def test_caddy_snippets_use_env_domains(self):
        for name in ("id.caddy", "forum-live.caddy", "forum-setup.caddy"):
            text = (ROOT / "config" / "community" / name).read_text()
            self.assertIn("{$AIR_", text)
        self.assertIn("import /etc/caddy/Caddyfile.d/*.caddy", (ROOT / "config" / "Caddyfile").read_text())


def base64_decode(text):
    import base64
    return base64.b64decode(text)


class LicensingTests(unittest.TestCase):
    """Trial, certificates and the licence lock (the seller's server is faked here)."""
    SEED = bytes(range(32))

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=TMP))
        self.conn = db.open_db(self.dir / "lic.sqlite3")
        self.patches = [unittest.mock.patch.object(licensing, "PUBLIC_KEY", ed25519.public_key(self.SEED).hex()),
                        unittest.mock.patch.object(config, "DOMAIN", "radio.example.com"),
                        unittest.mock.patch.object(licensing, "LICENSE_SERVER", "")]
        for p in self.patches:
            p.start()
        licensing.forget()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        licensing.forget()

    def cert(self, install=None, domain="radio.example.com", kind="online", seed=None, issued=1000):
        import base64
        payload = {"v": 1, "lic": "L00007", "hint": "RC5NB", "name": "Ann", "edition": "lifetime",
                   "install": install or licensing.install_id(self.conn), "domain": domain, "kind": kind,
                   "issued": issued, "checkin_after": issued + 7 * 86400}
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        return "AIRCERT." + b64(body) + "." + b64(ed25519.sign(seed or self.SEED, body))

    def test_developer_build_is_never_locked(self):
        with unittest.mock.patch.object(licensing, "PUBLIC_KEY", ""):
            s = licensing.state(self.conn, now=10 ** 10)
        self.assertEqual((s["status"], s["locked"]), ("developer", False))

    def test_trial_then_lock(self):
        t0 = 1_790_000_000
        s = licensing.state(self.conn, now=t0)
        self.assertEqual((s["status"], s["days_left"], s["locked"]), ("trial", 30, False))
        self.assertEqual(licensing.state(self.conn, now=t0 + 29.5 * 86400)["days_left"], 1)
        s = licensing.state(self.conn, now=t0 + 30 * 86400 + 1)
        self.assertEqual((s["status"], s["locked"]), ("expired", True))
        self.assertTrue(licensing.allowed_when_locked("/api/admin/licence/activate"))
        self.assertTrue(licensing.allowed_when_locked("/api/admin/status"))
        self.assertFalse(licensing.allowed_when_locked("/api/admin/playlists"))

    def test_certificates(self):
        licensing.state(self.conn, now=0)                        # trial long over
        licensing.use_certificate(self.conn, self.cert(kind="offline"))
        s = licensing.state(self.conn, now=10 ** 10)
        self.assertEqual((s["status"], s["licence"]["id"], s["licence"]["kind"]), ("licensed", "L00007", "offline"))
        for bad, why in ((self.cert(seed=b"\x09" * 32), "isn't valid"), (self.cert(install="f" * 32), "another install"),
                         (self.cert(domain="other.example.com"), "other.example.com")):
            with self.assertRaises(licensing.LicenceRefused) as refused:
                licensing.use_certificate(self.conn, bad)
            self.assertIn(why, str(refused.exception))
        # the station moved to another domain: the licence no longer applies
        with unittest.mock.patch.object(config, "DOMAIN", "new.example.com"):
            licensing.forget()
            s = licensing.state(self.conn, now=10 ** 10)
            self.assertEqual(s["status"], "expired")
            self.assertIn("Activate it again", s["message"])

    def test_key_format(self):
        self.assertIsNone(licensing.normalise_key("AIRS-12345-12345-12345-12345"))
        body = "7KQ2M9XD4PH3T8WR"
        key = "AIRS-" + "-".join((body + licensing._checksum(body))[i:i + 5] for i in range(0, 20, 5))
        self.assertEqual(licensing.normalise_key(key.lower().replace("-", "")), key)

    def test_online_activation_and_checkin(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        test = self
        replies = {"/v1/activate": (200, None), "/v1/checkin": (200, None), "/v1/deactivate": (200, {"ok": True})}
        seen = []

        class Fake(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, body))
                status, data = replies.get(self.path, (404, {"error": "not_found"}))
                if data is None:
                    data = {"certificate": test.cert(install=body.get("install_id") or None, issued=int(time.time()))}
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        with unittest.mock.patch.object(licensing, "LICENSE_SERVER", f"http://127.0.0.1:{httpd.server_address[1]}"):
            with self.assertRaises(licensing.LicenceRefused):
                licensing.activate(self.conn, "AIRS-not-a-key")
            body = "7KQ2M9XD4PH3T8WR"
            key = "AIRS-" + "-".join((body + licensing._checksum(body))[i:i + 5] for i in range(0, 20, 5))
            licensing.activate(self.conn, key)
            self.assertEqual(seen[-1][1]["domain"], "radio.example.com")
            self.assertEqual(licensing.state(self.conn)["status"], "licensed")
            self.assertEqual(licensing.checkin(self.conn), "not due")
            self.assertEqual(licensing.checkin(self.conn, force=True), "refreshed")
            replies["/v1/checkin"] = (403, {"error": "revoked", "message": "This licence has been cancelled."})
            self.assertTrue(licensing.checkin(self.conn, force=True).startswith("removed"))
            self.assertNotEqual(licensing.state(self.conn)["status"], "licensed")
            licensing.activate(self.conn, key)
            replies["/v1/checkin"] = (503, {})
            self.assertIn("unreachable", licensing.checkin(self.conn, force=True))
            self.assertEqual(licensing.state(self.conn)["status"], "licensed")   # an outage never unlicenses
            replies["/v1/activate"] = (409, {"error": "limit", "message": "already in use by the station at a.example.com"})
            licensing.deactivate(self.conn)
            with self.assertRaises(licensing.LicenceRefused) as refused:
                licensing.activate(self.conn, key)
            self.assertIn("a.example.com", str(refused.exception))


class PagesTests(unittest.TestCase):
    """The home and live pages carry the station's own title and texts in the HTML."""
    WEB = Path(__file__).resolve().parents[1] / "web"

    def test_render_home(self):
        settings = {"station_name": "A.i.R Station", "tagline": "Hip Hop • Rap",
                    "texts": {"page_title": "{station} | Independent Hip Hop & Rap Radio", "hero_band": "HIP *HOP*"}}
        page = pages.render((self.WEB / "index.html").read_text(), settings, "home", "listen.example.com")
        self.assertIn("<title>A.i.R Station | Independent Hip Hop &amp; Rap Radio</title>", page)
        self.assertEqual(page.count("<title>"), 1)
        self.assertIn('<meta name="description" content="A.i.R Station: Hip Hop • Rap">', page)
        self.assertIn('<meta property="og:title" content="A.i.R Station | Independent Hip Hop &amp; Rap Radio">', page)
        self.assertIn('<link rel="canonical" href="https://listen.example.com/">', page)
        self.assertIn('data-station data-hero-name>A.i.R Station</h1>', page)
        self.assertIn('data-t="hero_band">HIP <b>HOP</b></div>', page)
        self.assertNotIn("AirStation Radio", page)
        live = pages.render((self.WEB / "live" / "index.html").read_text(), settings, "live", "listen.example.com")
        self.assertIn("<title>Live Video | A.i.R Station</title>", live)
        self.assertIn('href="https://listen.example.com/live/"', live)

    def test_description_and_escaping(self):
        s = {"station_name": 'Rock <"&> FM', "texts": {"page_description": "Best *rock* in {station}."}}
        info = pages.seo(s)
        self.assertEqual(info["description"], 'Best rock in Rock <"&> FM.')
        page = pages.render((self.WEB / "index.html").read_text(), s)
        self.assertIn("<title>Rock &lt;&quot;&amp;&gt; FM | Internet Radio</title>", page)
        self.assertNotIn('<"&>', page)
        self.assertEqual(pages.seo({})["description"], pages.DEFAULT_DESCRIPTION)

    def test_write_all_only_rewrites_changes(self):
        tmp = Path(tempfile.mkdtemp(dir=TMP))
        conn = db.open_db(tmp / "p.sqlite3")
        self.assertEqual(sorted(pages.write_all(conn, self.WEB, tmp / "site", "r.example.com")),
                         ["index.html", "live/index.html"])
        self.assertEqual(pages.write_all(conn, self.WEB, tmp / "site", "r.example.com"), [])
        db.set_setting(conn, "station_name", "New Name")
        conn.commit()
        self.assertEqual(sorted(pages.write_all(conn, self.WEB, tmp / "site", "r.example.com")),
                         ["index.html", "live/index.html"])
        self.assertIn("New Name", (tmp / "site" / "index.html").read_text())
        self.assertEqual(pages.write_all(conn, tmp / "missing", tmp / "site2"), [])     # never raises

    def test_site_js_keeps_the_title(self):
        js = (self.WEB / "assets" / "js" / "site.js").read_text()
        self.assertNotIn("document.title", js)


class ConfigFileTests(unittest.TestCase):
    def test_icecast_render_escapes_and_sets_hostname(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        for kind in ("source", "relay", "admin"):
            (d / f"{kind}_password").write_text("a<&b")
        out = d / "icecast.xml"
        icecast_start.render(ROOT / "config" / "icecast.xml", d, out, "radio.example")
        import xml.etree.ElementTree as ET
        tree = ET.parse(out)
        self.assertEqual(tree.find("authentication/admin-password").text, "a<&b")
        self.assertEqual(tree.find("hostname").text, "radio.example")
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)

    def test_radio_liq_references_existing_secrets(self):
        liq = (ROOT / "config" / "radio.liq").read_text()
        compose = (ROOT / "compose.yaml").read_text()
        import re
        for name in re.findall(r'read_secret\("([a-z_]+)"\)', liq):
            self.assertIn(f"{name}:", compose, name)
        for cmd in ("state", "skip", "live", "record"):
            self.assertIn(f'"{cmd}", air_{cmd}', liq)

    def test_mediamtx_template_placeholders(self):
        text = (ROOT / "config" / "mediamtx.yml.template").read_text()
        self.assertIn("@@OBS_PUBLISH_PASSWORD@@", text)
        self.assertIn("@@OBS_HARBOR_PASSWORD@@", text)


if __name__ == "__main__":
    unittest.main()
