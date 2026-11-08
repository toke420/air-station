"""Lightweight tag reading for NAS indexing.

Reading tags over SMB across the Internet is the slow part of indexing a large
library, so for MP3 files this reads only the ID3v2 frame headers it needs and
seeks past everything else (cover art can be hundreds of KB). Other formats get
their tags from ffprobe after they are cached locally.
"""
import re
import struct
import zlib

WANTED_V23 = {"TIT2": "title", "TPE1": "artist", "TALB": "album", "TCON": "genre",
              "TYER": "year", "TDRC": "year", "TPE2": "albumartist"}
WANTED_V22 = {"TT2": "title", "TP1": "artist", "TAL": "album", "TCO": "genre",
              "TYE": "year", "TP2": "albumartist"}
MAX_TEXT_FRAME = 4096


def _syncsafe(data):
    return (data[0] << 21) | (data[1] << 14) | (data[2] << 7) | data[3]


def _decode_text(payload):
    if not payload:
        return ""
    encoding, body = payload[0], payload[1:]
    try:
        if encoding == 0:
            text = body.decode("latin-1")
        elif encoding == 1:
            text = body.decode("utf-16")
        elif encoding == 2:
            text = body.decode("utf-16-be")
        elif encoding == 3:
            text = body.decode("utf-8")
        else:
            text = payload.decode("latin-1")
    except UnicodeDecodeError:
        text = body.decode("latin-1", errors="replace")
    # ID3v2.4 separates multiple values with NUL; keep them readable.
    parts = [p.strip() for p in text.replace("﻿", "").split("\x00") if p.strip()]
    return " / ".join(parts)


def _clean_genre(value):
    # "(17)Rock" or "(17)" numeric references from ID3v1 genre table.
    match = re.fullmatch(r"\((\d+)\)(.*)", value)
    if match:
        rest = match.group(2).strip()
        if rest:
            return rest
        index = int(match.group(1))
        return ID3V1_GENRES[index] if index < len(ID3V1_GENRES) else ""
    if value.isdigit() and int(value) < len(ID3V1_GENRES):
        return ID3V1_GENRES[int(value)]
    return value


def read_id3v2(stream):
    stream.seek(0)
    header = stream.read(10)
    if len(header) < 10 or header[:3] != b"ID3":
        return {}
    major, flags = header[3], header[5]
    size = _syncsafe(header[6:10])
    end = 10 + size
    pos = 10
    if flags & 0x40 and major >= 3:  # extended header
        stream.seek(pos)
        ext = stream.read(4)
        if len(ext) < 4:
            return {}
        ext_size = _syncsafe(ext) if major == 4 else struct.unpack(">I", ext)[0] + 4
        pos += ext_size
    unsync = bool(flags & 0x80) and major < 4
    result = {}
    wanted = WANTED_V22 if major == 2 else WANTED_V23
    header_len = 6 if major == 2 else 10
    frames_seen = 0
    while pos + header_len <= end and frames_seen < 512:
        frames_seen += 1
        stream.seek(pos)
        frame_header = stream.read(header_len)
        if len(frame_header) < header_len or frame_header[0] == 0:
            break
        if major == 2:
            frame_id = frame_header[:3].decode("latin-1")
            frame_size = int.from_bytes(frame_header[3:6], "big")
        else:
            frame_id = frame_header[:4].decode("latin-1", errors="replace")
            raw = frame_header[4:8]
            frame_size = _syncsafe(raw) if major == 4 else struct.unpack(">I", raw)[0]
        if not re.fullmatch(r"[A-Z0-9]{3,4}", frame_id) or frame_size <= 0:
            break
        data_pos = pos + header_len
        if frame_id in wanted and frame_size <= MAX_TEXT_FRAME:
            stream.seek(data_pos)
            payload = stream.read(frame_size)
            if unsync:
                payload = payload.replace(b"\xff\x00", b"\xff")
            key = wanted[frame_id]
            value = _decode_text(payload)
            if value and key not in result:
                result[key] = value
        pos = data_pos + frame_size
    if "genre" in result:
        result["genre"] = _clean_genre(result["genre"])
    if "year" in result:
        result["year"] = result["year"][:4]
    return result


def _skip_terminated(data, pos, encoding):
    """Index just past a NUL-terminated string in the given ID3 text encoding."""
    if encoding in (1, 2):  # UTF-16: two-byte terminator on an even boundary
        while pos + 1 < len(data):
            if data[pos] == 0 and data[pos + 1] == 0:
                return pos + 2
            pos += 2
        return len(data)
    end = data.find(b"\x00", pos)
    return len(data) if end < 0 else end + 1


_FRAME_ID = re.compile(rb"[A-Z0-9]{4}")


def _frame_ok(stream, pos, end):
    """True if a plausible ID3v2.3/2.4 frame header (or padding/end) is at pos."""
    if pos == end:
        return True
    if pos > end:
        return False
    stream.seek(pos)
    head = stream.read(4)
    return head[:1] == b"\x00" or bool(_FRAME_ID.fullmatch(head))


def _find_image(image):
    """Image bytes, skipping a few junk bytes some taggers leave before the picture."""
    if image_kind(image):
        return image
    for magic in (b"\xff\xd8\xff", b"\x89PNG", b"RIFF"):
        at = image.find(magic, 0, 64)
        if at > 0 and image_kind(image[at:]):
            return image[at:]
    return image


def _picture_in_tag(stream, start, max_bytes):
    """Scan one ID3v2 tag at start: ((mime, bytes) front cover, first picture, tag end)."""
    stream.seek(start)
    header = stream.read(10)
    if len(header) < 10 or header[:3] != b"ID3" or header[3] not in (2, 3, 4):
        return None, None, None
    major, flags = header[3], header[5]
    end = start + 10 + _syncsafe(header[6:10])
    pos = start + 10
    if flags & 0x40 and major >= 3:
        stream.seek(pos)
        ext = stream.read(4)
        pos += _syncsafe(ext) if major == 4 else struct.unpack(">I", ext)[0] + 4
    unsync = bool(flags & 0x80) and major < 4
    header_len = 6 if major == 2 else 10
    first = None
    for _ in range(1024):
        if pos + header_len > end:
            break
        stream.seek(pos)
        fh = stream.read(header_len)
        if len(fh) < header_len or fh[0] == 0:
            break
        frame_flags = 0
        if major == 2:
            frame_id, size = fh[:3], int.from_bytes(fh[3:6], "big")
        else:
            frame_id = fh[:4]
            plain = struct.unpack(">I", fh[4:8])[0]
            size = plain
            if major == 4:
                size = _syncsafe(fh[4:8])
                # Some taggers write v2.4 frame sizes as plain integers.
                if plain != size and not _frame_ok(stream, pos + 10 + size, end) \
                        and _frame_ok(stream, pos + 10 + plain, end):
                    size = plain
            frame_flags = fh[9]
        if size <= 0:
            break
        data_pos = pos + header_len
        if frame_id in (b"APIC", b"PIC") and size <= max_bytes:
            stream.seek(data_pos)
            data = stream.read(size)
            if unsync:
                data = data.replace(b"\xff\x00", b"\xff")
            if major == 4 and frame_flags & 0x02:      # per-frame unsynchronisation
                data = data.replace(b"\xff\x00", b"\xff")
            compressed = (major == 3 and frame_flags & 0x80) or (major == 4 and frame_flags & 0x08)
            encrypted = (major == 3 and frame_flags & 0x40) or (major == 4 and frame_flags & 0x04)
            if (major == 4 and frame_flags & 0x01) or (major == 3 and compressed):
                data = data[4:]                         # data length / decompressed size
            if compressed and not encrypted:
                try:
                    data = zlib.decompress(data)
                except zlib.error:
                    data = b""
            if len(data) > 4 and not encrypted:
                encoding = data[0]
                if frame_id == b"PIC":
                    fmt = data[1:4].decode("latin-1", "replace").lower()
                    mime = "image/png" if fmt == "png" else "image/jpeg"
                    ptype, cursor = data[4], 5
                else:
                    mime_end = data.find(b"\x00", 1)
                    if mime_end < 0:
                        mime_end = 1
                    mime = data[1:mime_end].decode("latin-1", "replace").lower() or "image/jpeg"
                    ptype, cursor = data[mime_end + 1], mime_end + 2
                cursor = _skip_terminated(data, cursor, encoding)
                image = _find_image(data[cursor:])
                if image and mime != "-->":           # "-->" = link to an external file
                    if ptype == 3:
                        return (mime, image), first, end
                    first = first or (mime, image)
        pos = data_pos + size
    return None, first, end


def read_picture(stream, max_bytes=8 * 1024 * 1024):
    """Embedded cover from ID3v2 APIC (v2.3/2.4) or PIC (v2.2): (mime, bytes) or None.

    Prefers the front cover (picture type 3); otherwise the first picture. Also
    looks in a second ID3v2 tag written straight after the first (some converters
    add a new tag in front of the old one instead of replacing it).
    """
    start, first = 0, None
    for _ in range(3):
        front, any_picture, end = _picture_in_tag(stream, start, max_bytes)
        if front:
            return front
        first = first or any_picture
        if end is None:
            break
        stream.seek(end)
        gap = stream.read(4096)
        skip = len(gap) - len(gap.lstrip(b"\x00"))
        if gap[skip:skip + 3] != b"ID3":
            break
        start = end + skip
    return first


def image_kind(data):
    """'jpeg', 'png', 'webp', 'gif' or None, from magic bytes."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def image_size(data):
    """(width, height) for PNG, JPEG, WebP or GIF data; None if unknown."""
    kind = image_kind(data)
    try:
        if kind == "png":
            return struct.unpack(">II", data[16:24])
        if kind == "gif":
            return struct.unpack("<HH", data[6:10])
        if kind == "webp":
            chunk = data[12:16]
            if chunk == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1,
                        int.from_bytes(data[27:30], "little") + 1)
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                b = data[21:25]
                w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                return w, h
        if kind == "jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    except (struct.error, IndexError):
        return None
    return None


def read_id3v1(stream, size):
    if size < 128:
        return {}
    stream.seek(size - 128)
    block = stream.read(128)
    if len(block) < 128 or block[:3] != b"TAG":
        return {}

    def text(chunk):
        return chunk.split(b"\x00")[0].decode("latin-1").strip()

    result = {"title": text(block[3:33]), "artist": text(block[33:63]),
              "album": text(block[63:93]), "year": text(block[93:97])}
    genre = block[127]
    if genre < len(ID3V1_GENRES):
        result["genre"] = ID3V1_GENRES[genre]
    return {k: v for k, v in result.items() if v}


def guess_from_filename(stem):
    """'01 - Artist - Title' / 'Artist - Title' / 'Title'."""
    name = re.sub(r"[_]+", " ", stem).strip()
    name = re.sub(r"^\d{1,3}[\s.\-_]+", "", name)
    parts = [p.strip() for p in re.split(r"\s+[-–]\s+", name) if p.strip()]
    if len(parts) >= 2:
        return {"artist": parts[0], "title": " - ".join(parts[1:])}
    return {"title": name}


def read_tags(path, size=None):
    """Best-effort tags for any supported file without reading the audio."""
    from pathlib import Path
    path = Path(path)
    tags = {}
    if path.suffix.lower() == ".mp3":
        try:
            with path.open("rb") as stream:
                tags = read_id3v2(stream)
                if not tags.get("title") or not tags.get("artist"):
                    if size is None:
                        size = path.stat().st_size
                    v1 = read_id3v1(stream, size)
                    for key, value in v1.items():
                        tags.setdefault(key, value)
        except OSError:
            tags = {}
    if not tags.get("title"):
        guess = guess_from_filename(path.stem)
        for key, value in guess.items():
            tags.setdefault(key, value)
    if not tags.get("artist") and tags.get("albumartist"):
        tags["artist"] = tags["albumartist"]
    return {k: str(v)[:300] for k, v in tags.items() if k in
            ("title", "artist", "album", "genre", "year")}


ID3V1_GENRES = [
    "Blues", "Classic Rock", "Country", "Dance", "Disco", "Funk", "Grunge", "Hip-Hop",
    "Jazz", "Metal", "New Age", "Oldies", "Other", "Pop", "R&B", "Rap", "Reggae", "Rock",
    "Techno", "Industrial", "Alternative", "Ska", "Death Metal", "Pranks", "Soundtrack",
    "Euro-Techno", "Ambient", "Trip-Hop", "Vocal", "Jazz+Funk", "Fusion", "Trance",
    "Classical", "Instrumental", "Acid", "House", "Game", "Sound Clip", "Gospel", "Noise",
    "AlternRock", "Bass", "Soul", "Punk", "Space", "Meditative", "Instrumental Pop",
    "Instrumental Rock", "Ethnic", "Gothic", "Darkwave", "Techno-Industrial", "Electronic",
    "Pop-Folk", "Eurodance", "Dream", "Southern Rock", "Comedy", "Cult", "Gangsta", "Top 40",
    "Christian Rap", "Pop/Funk", "Jungle", "Native American", "Cabaret", "New Wave",
    "Psychadelic", "Rave", "Showtunes", "Trailer", "Lo-Fi", "Tribal", "Acid Punk",
    "Acid Jazz", "Polka", "Retro", "Musical", "Rock & Roll", "Hard Rock",
]
