"""Offline IP geolocation for listener and visitor statistics.

Reads MaxMind DB (.mmdb) files with the standard library only. Works with the free
DB-IP "IP to City Lite" database (CC BY 4.0, downloaded monthly by
`airctl geoip update`) and with MaxMind GeoLite2-City.mmdb if you prefer that.

Without a database every lookup returns None and statistics simply show
"Unknown location".

    python3 -m airstation.geoip update      # download / refresh DB-IP City Lite
    python3 -m airstation.geoip lookup IP   # test a lookup
"""
import datetime
import functools
import gzip
import ipaddress
import mmap
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from . import config

DBIP_URL = "https://download.db-ip.com/free/dbip-city-lite-{year}-{month:02d}.mmdb.gz"
ATTRIBUTION = ('IP geolocation by <a href="https://db-ip.com" target="_blank" '
               'rel="noopener">DB-IP</a> (CC BY 4.0)')
MAX_AGE_DAYS = 32
METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"


def geoip_dir():
    return Path(config.env("AIR_GEOIP_DIR", str(config.DATA_DIR / "geoip")))


def database_path():
    """First database found: GeoLite2 (if you added one), then DB-IP lite."""
    folder = geoip_dir()
    for name in ("GeoLite2-City.mmdb", "dbip-city-lite.mmdb"):
        if (folder / name).is_file():
            return folder / name
    return None


class InvalidDatabase(ValueError):
    pass


class Reader:
    """Minimal MaxMind DB reader (format spec v2.0)."""

    def __init__(self, path):
        self.path = Path(path)
        with open(self.path, "rb") as f:
            self._buf = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        start = self._buf.rfind(METADATA_MARKER, max(0, len(self._buf) - 128 * 1024))
        if start < 0:
            raise InvalidDatabase(f"{path}: not a MaxMind DB file")
        meta_start = start + len(METADATA_MARKER)
        self.metadata, _ = self._decode(meta_start, base=meta_start)
        try:
            self.node_count = int(self.metadata["node_count"])
            self.record_size = int(self.metadata["record_size"])
            self.ip_version = int(self.metadata["ip_version"])
        except (KeyError, TypeError, ValueError):
            raise InvalidDatabase(f"{path}: incomplete metadata")
        if self.record_size not in (24, 28, 32):
            raise InvalidDatabase(f"{path}: unsupported record size {self.record_size}")
        self.node_bytes = self.record_size * 2 // 8
        self.tree_size = self.node_bytes * self.node_count
        self.data_start = self.tree_size + 16
        self._ipv4_start = None

    def close(self):
        self._buf.close()

    # ---------------------------------------------------------------- tree
    def _record(self, node, bit):
        off = node * self.node_bytes
        b = self._buf[off:off + self.node_bytes]
        if self.record_size == 24:
            return int.from_bytes(b[3:6] if bit else b[0:3], "big")
        if self.record_size == 28:
            if bit:
                return ((b[3] & 0x0F) << 24) | int.from_bytes(b[4:7], "big")
            return ((b[3] & 0xF0) << 20) | int.from_bytes(b[0:3], "big")
        return int.from_bytes(b[4:8] if bit else b[0:4], "big")

    def _start_node(self, bits):
        if self.ip_version == 6 and bits == 32:
            if self._ipv4_start is None:
                node = 0
                for _ in range(96):
                    if node >= self.node_count:
                        break
                    node = self._record(node, 0)
                self._ipv4_start = node
            return self._ipv4_start
        return 0

    def get(self, ip):
        address = ipaddress.ip_address(ip)
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        if address.version == 6 and self.ip_version == 4:
            return None
        packed = address.packed
        bits = len(packed) * 8
        node = self._start_node(bits)
        number = int.from_bytes(packed, "big")
        for i in range(bits):
            if node >= self.node_count:
                break
            node = self._record(node, (number >> (bits - 1 - i)) & 1)
        if node == self.node_count:
            return None
        if node > self.node_count:
            offset = node - self.node_count - 16 + self.data_start
            return self._decode(offset, base=self.data_start)[0]
        raise InvalidDatabase("invalid search tree")

    # ---------------------------------------------------------------- data
    def _decode(self, offset, base):
        buf = self._buf
        ctrl = buf[offset]
        offset += 1
        kind = ctrl >> 5
        if kind == 1:                                   # pointer
            ss, vvv = (ctrl >> 3) & 0x3, ctrl & 0x7
            if ss == 0:
                ptr = (vvv << 8) | buf[offset]
                offset += 1
            elif ss == 1:
                ptr = ((vvv << 16) | int.from_bytes(buf[offset:offset + 2], "big")) + 2048
                offset += 2
            elif ss == 2:
                ptr = ((vvv << 24) | int.from_bytes(buf[offset:offset + 3], "big")) + 526336
                offset += 3
            else:
                ptr = int.from_bytes(buf[offset:offset + 4], "big")
                offset += 4
            value, _ = self._decode(base + ptr, base)
            return value, offset
        if kind == 0:                                   # extended type
            kind = 7 + buf[offset]
            offset += 1
        size = ctrl & 0x1F
        if size >= 29:
            extra = size - 28
            n = int.from_bytes(buf[offset:offset + extra], "big")
            offset += extra
            size = 29 + n if extra == 1 else (285 + n if extra == 2 else 65821 + n)
        if kind == 2:
            return buf[offset:offset + size].decode("utf-8", "replace"), offset + size
        if kind == 3:
            return struct.unpack(">d", buf[offset:offset + 8])[0], offset + 8
        if kind == 4:
            return bytes(buf[offset:offset + size]), offset + size
        if kind in (5, 6, 9, 10):
            return int.from_bytes(buf[offset:offset + size], "big"), offset + size
        if kind == 7:
            out = {}
            for _ in range(size):
                key, offset = self._decode(offset, base)
                value, offset = self._decode(offset, base)
                out[key] = value
            return out, offset
        if kind == 8:
            raw = int.from_bytes(buf[offset:offset + size], "big") if size else 0
            if size == 4 and raw & 0x80000000:
                raw -= 1 << 32
            return raw, offset + size
        if kind == 11:
            items = []
            for _ in range(size):
                value, offset = self._decode(offset, base)
                items.append(value)
            return items, offset
        if kind == 14:
            return bool(size), offset
        if kind == 15:
            return struct.unpack(">f", buf[offset:offset + 4])[0], offset + 4
        raise InvalidDatabase(f"unsupported data type {kind}")


# ------------------------------------------------------------------ lookups
_lock = threading.Lock()
_state = {"reader": None, "path": None, "mtime": 0, "checked": 0}


def _reader():
    """The current reader, reopened when the file is replaced (checked every minute)."""
    now = time.time()
    with _lock:
        if now - _state["checked"] < 60 and _state["reader"] is not None:
            return _state["reader"]
        _state["checked"] = now
        path = database_path()
        mtime = path.stat().st_mtime if path else 0
        if path != _state["path"] or mtime != _state["mtime"]:
            old = _state["reader"]
            try:
                _state["reader"] = Reader(path) if path else None
            except (OSError, InvalidDatabase):
                _state["reader"] = None
            _state.update(path=path, mtime=mtime)
            _lookup.cache_clear()
            if old is not None:
                try:
                    old.close()
                except (BufferError, ValueError):
                    pass
        return _state["reader"]


def _name(record, key):
    part = (record or {}).get(key) or {}
    names = part.get("names") or {}
    return names.get("en") or next(iter(names.values()), "") if names else ""


@functools.lru_cache(maxsize=8192)
def _lookup(ip):
    reader = _reader()
    if reader is None:
        return None
    try:
        rec = reader.get(ip)
    except (ValueError, InvalidDatabase, IndexError):
        return None
    if not rec:
        return None
    country = rec.get("country") or rec.get("registered_country") or {}
    location = rec.get("location") or {}
    subdivisions = rec.get("subdivisions") or [{}]
    lat, lon = location.get("latitude"), location.get("longitude")
    return {
        "country": (country.get("iso_code") or "").upper(),
        "country_name": _name({"c": country}, "c"),
        "region": _name({"s": subdivisions[0]}, "s"),
        "city": _name(rec, "city"),
        # City level only: two decimals (about 1 km) is plenty for the map.
        "lat": round(float(lat), 2) if lat is not None else None,
        "lon": round(float(lon), 2) if lon is not None else None,
    }


def lookup(ip):
    """{"country", "country_name", "region", "city", "lat", "lon"} or None.

    Private, loopback and other non-public addresses return a "Local network" entry.
    """
    try:
        address = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return None
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    if not address.is_global:
        return {"country": "", "country_name": "Local network", "region": "", "city": "",
                "lat": None, "lon": None}
    _reader()                                      # notice a replaced file
    return _lookup(str(address))


def info():
    """What the admin shows about the database."""
    path = database_path()
    if not path:
        return {"available": False, "attribution": ATTRIBUTION,
                "help": "Run: bash scripts/airctl geoip update"}
    reader = _reader()
    meta = reader.metadata if reader else {}
    built = meta.get("build_epoch")
    return {"available": reader is not None, "file": path.name,
            "type": meta.get("database_type", ""),
            "built": datetime.datetime.fromtimestamp(built, datetime.timezone.utc).date().isoformat() if built else "",
            "attribution": ATTRIBUTION if "dbip" in path.name else
            'IP geolocation by MaxMind GeoLite2 (<a href="https://www.maxmind.com" target="_blank" rel="noopener">maxmind.com</a>)'}


# ----------------------------------------------------------------- download
def update(force=False, log=print):
    """Download this month's DB-IP City Lite (or last month's) into geoip_dir()."""
    folder = geoip_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "dbip-city-lite.mmdb"
    if not force and target.is_file() and time.time() - target.stat().st_mtime < MAX_AGE_DAYS * 86400:
        log(f"GeoIP database is up to date ({target}).")
        return True
    today = datetime.date.today()
    months = [(today.year, today.month)]
    months.append((today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1))
    for year, month in months:
        url = DBIP_URL.format(year=year, month=month)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".dbip-", suffix=".mmdb")
        os.close(fd)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AirStation geoip update"})
            with urllib.request.urlopen(req, timeout=120) as resp, \
                    gzip.GzipFile(fileobj=resp) as gz, open(tmp, "wb") as out:
                shutil.copyfileobj(gz, out, 1024 * 1024)
            reader = Reader(tmp)
            ok = reader.get("8.8.8.8") is not None
            reader.close()
            if not ok:
                raise InvalidDatabase("test lookup failed")
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
            log(f"GeoIP database updated from {url}")
            return True
        except (OSError, EOFError, InvalidDatabase) as error:
            log(f"Could not use {url}: {error}")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return False


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["update"]:
        return 0 if update(force="--force" in args) else 1
    if args[:1] == ["lookup"] and len(args) == 2:
        print(lookup(args[1]) or "no result (is the database installed?)")
        return 0
    if args[:1] == ["info"]:
        print(info())
        return 0
    print("usage: python3 -m airstation.geoip update [--force] | lookup IP | info")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
