"""Emergency folder: local music on the VPS that plays when the library cache
has nothing to offer (first start, long home outage).

The API picks emergency tracks itself (any folder depth, fair rotation, no
recent repeats) and hands Liquidsoap a /emergency/... path. Liquidsoap's own
playlist of the folder remains as a last resort if the API is down.
"""
import hashlib
import os
import random
import threading
import time
from pathlib import Path

from . import config

LIQ_PREFIX = "/emergency"
_SCAN_SECONDS = 60
_lock = threading.Lock()
_cache = {"at": 0.0, "root": None, "files": [], "unreadable": []}
_recent = []
_rng = random.SystemRandom()


def root():
    return Path(config.env("AIR_EMERGENCY_DIR", str(config.DATA_DIR / "emergency")))


def scan(force=False):
    """Return (playable relative paths, unreadable relative paths)."""
    base = root()
    with _lock:
        if not force and _cache["root"] == base and time.time() - _cache["at"] < _SCAN_SECONDS:
            return list(_cache["files"]), list(_cache["unreadable"])
        files, unreadable = [], []
        if base.is_dir():
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                blocked = [d for d in dirnames if not os.access(os.path.join(dirpath, d), os.R_OK | os.X_OK)]
                for d in blocked:
                    unreadable.append(os.path.relpath(os.path.join(dirpath, d), base) + "/")
                dirnames[:] = sorted(d for d in dirnames if d not in blocked and not d.startswith("."))
                for name in sorted(filenames):
                    if name.startswith(".") or "\n" in name or "\r" in name:
                        continue
                    if os.path.splitext(name)[1].lower() not in config.AUDIO_EXTENSIONS:
                        continue
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, base).replace(os.sep, "/")
                    if os.path.islink(full):
                        continue
                    if os.access(full, os.R_OK):
                        files.append(rel)
                    else:
                        unreadable.append(rel)
        _cache.update(at=time.time(), root=base, files=files, unreadable=unreadable)
        return list(files), list(unreadable)


def pick():
    """A random emergency track, avoiding the most recently played ones."""
    files, _ = scan()
    if not files:
        return None
    memory = min(len(files) // 2, 100)
    fresh = [f for f in files if f not in _recent[-memory:]] if memory else files
    choice = _rng.choice(fresh or files)
    _recent.append(choice)
    del _recent[:-200]
    return choice


def art_key(rel):
    """Stable id for an emergency file; also the name of its cover in data/art."""
    return "em-" + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]


def liquidsoap_uri(rel):
    return f'annotate:air_id="{art_key(rel)}":{LIQ_PREFIX}/{rel}'


def status():
    files, unreadable = scan()
    return {"files": len(files), "unreadable": len(unreadable),
            "unreadable_examples": unreadable[:5], "path": str(root())}
