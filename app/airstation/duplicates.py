"""Duplicate track finder.

Two kinds of match:
  * "title": same artist + title after normalising away the noise that YouTube
    rips and re-releases add: "(Official Video)", "[HD]", "feat. X",
    "Remastered 2011", punctuation and case.
  * "file":  identical file size (and length, when known). Almost always the
    same file copied to two folders.

Nothing is deleted (the NAS is mounted read-only). Resolving a group disables
the extra copies so AutoDJ never plays them; the NAS paths are listed so the
files can be removed at home if wanted.
"""
import re
from collections import defaultdict

NOISE_WORDS = (r"official|music|video|audio|lyrics?|visuali[sz]er|hd|hq|4k|1080p|720p|explicit|"
               r"clean|dirty|remaster(?:ed)?|radio edit|mv|full song|out now|premiere|"
               r"original mix|extended|version|single|album|mp3|320|kbps|free download|bass ?boosted")
_BRACKETS = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
_NOISE_IN_BRACKET = re.compile(rf"\b({NOISE_WORDS}|feat\.?|ft\.?|featuring|prod\.?|\d{{4}})\b", re.I)
_FEAT = re.compile(r"\s(?:feat\.?|ft\.?|featuring|prod\.?(?: by)?)\s.*$", re.I)
_NOISE = re.compile(rf"\b({NOISE_WORDS})\b", re.I)
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_LEADING_NUMBER = re.compile(r"^\d{1,3}\s*[-.]\s*")


def normalise(text):
    text = (text or "").lower().replace("&", " and ")
    text = _BRACKETS.sub(lambda m: " " if _NOISE_IN_BRACKET.search(m.group(1)) else f" {m.group(1)} ", text)
    text = _FEAT.sub("", text)
    text = _LEADING_NUMBER.sub("", text.strip())
    text = _NOISE.sub(" ", text)
    return " ".join(_NON_WORD.sub(" ", text).split())


def title_key(artist, title):
    artist, title = artist or "", title or ""
    if not artist and " - " in title:
        artist, title = title.split(" - ", 1)
    t = normalise(title)
    if not t:
        return None
    return f"{normalise(artist)}|{t}"


def _suggest(tracks):
    """The copy to keep: enabled, has cover, biggest file (usually best bitrate), most played."""
    return max(tracks, key=lambda t: (t["enabled"], not t["missing"], t["has_art"],
                                      t["size"], t["play_count"], -t["id"]))["id"]


def find(conn, include_resolved=False, limit=500):
    rows = [dict(r) for r in conn.execute(
        "SELECT id, relpath, folder, artist, title, album, size, duration, cached, enabled, "
        "missing, bad, play_count, has_art FROM tracks WHERE bad = 0")]
    groups = []
    by_title = defaultdict(list)
    by_file = defaultdict(list)
    for row in rows:
        key = title_key(row["artist"], row["title"])
        if key:
            by_title[key].append(row)
        if row["size"] > 100 * 1024:
            by_file[row["size"]].append(row)
    seen_pairs = set()
    for size, tracks in by_file.items():
        if len(tracks) < 2:
            continue
        # Same size and (when both known) same length to within a second.
        clusters = []
        for t in tracks:
            for cluster in clusters:
                d1, d2 = t["duration"], cluster[0]["duration"]
                if d1 is None or d2 is None or abs(d1 - d2) <= 1:
                    cluster.append(t)
                    break
            else:
                clusters.append([t])
        for cluster in clusters:
            if len(cluster) > 1:
                groups.append({"reason": "file", "label": "Identical file size", "tracks": cluster})
                seen_pairs.add(frozenset(t["id"] for t in cluster))
    for key, tracks in by_title.items():
        if len(tracks) < 2 or frozenset(t["id"] for t in tracks) in seen_pairs:
            continue
        groups.append({"reason": "title", "label": "Same artist and title", "tracks": tracks})
    result = []
    for group in groups:
        enabled = [t for t in group["tracks"] if t["enabled"] and not t["missing"]]
        group["resolved"] = len(enabled) <= 1
        if group["resolved"] and not include_resolved:
            continue
        group["tracks"].sort(key=lambda t: t["relpath"].lower())
        group["keep"] = _suggest(group["tracks"])
        result.append(group)
    result.sort(key=lambda g: (g["reason"] != "file", -len(g["tracks"]),
                               g["tracks"][0]["artist"].lower()))
    return {"groups": result[:limit], "total_groups": len(result),
            "extra_copies": sum(len(g["tracks"]) - 1 for g in result)}


def resolve(conn, keep, disable):
    ids = [int(i) for i in disable if int(i) != int(keep)]
    conn.executemany("UPDATE tracks SET enabled = 0 WHERE id = ?", [(i,) for i in ids])
    conn.executemany("DELETE FROM queue WHERE track_id = ?", [(i,) for i in ids])
    conn.execute("UPDATE tracks SET enabled = 1 WHERE id = ?", (int(keep),))
    conn.commit()
    return len(ids)
