"""AirStation licence: 30-day trial, online activation, offline certificates.

How it works
- The first time this version runs, a 30-day trial starts. Everything works.
- The owner enters their licence key in Studio > Licence. The API sends the key, this
  install's random id and the station's domain (AIR_DOMAIN) to the licence server, which
  returns a certificate signed with the seller's private key. It is checked here, offline,
  with PUBLIC_KEY below, and stored in the database.
- About once a week the install refreshes its certificate. If the server can't be
  reached nothing changes: a valid certificate never expires. Only an answer from the
  server that the licence was cancelled or moved removes it.
- Without a valid certificate after the trial, the station keeps broadcasting and the
  public site keeps working, but Studio only offers the dashboard and this licence page.

PUBLIC_KEY and LICENSE_SERVER are filled in by scripts/dev/release.py when a release is
built. An empty PUBLIC_KEY means a developer build: no trial and no lock.

    python3 -m airstation.licensing status | activate KEY | certificate CERT | deactivate | check
"""
import base64
import json
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request

from . import __version__, config, db, ed25519

# --- set by scripts/dev/release.py ------------------------------------------------
PUBLIC_KEY = ""
LICENSE_SERVER = ""
# -----------------------------------------------------------------------------------

TRIAL_DAYS = 30
CERT_PREFIX = "AIRCERT."
KEY_RE = re.compile(r"AIRS(-?[0-9A-Z]{5}){4}")
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PURCHASE_URL_DEFAULT = ""

# Studio pages that keep working without a licence (the station must stay manageable).
FREE_ADMIN = re.compile(r"/api/admin/(login|logout|me|status|queue|history|licence(/.*)?|skip|live)$")

_cache = {"at": 0.0, "state": None}
_lock = threading.Lock()


def public_key():
    return PUBLIC_KEY


def server_url():
    return (config.env("AIR_LICENSE_SERVER") or LICENSE_SERVER).rstrip("/")


def purchase_url():
    return config.env("AIR_LICENSE_PURCHASE_URL") or PURCHASE_URL_DEFAULT


class LicenceRefused(Exception):
    pass


# ------------------------------------------------------------------ helpers
def _checksum(body):
    import hashlib
    digest = int.from_bytes(hashlib.sha256(("airstation:" + body).encode()).digest()[:3], "big") >> 4
    return "".join(ALPHABET[(digest >> (5 * i)) & 31] for i in reversed(range(4)))


def normalise_key(text):
    raw = re.sub(r"[\s-]", "", str(text or "").upper())
    if raw.startswith("AIRS"):
        raw = raw[4:]
    raw = raw.translate(str.maketrans("ILO", "110"))
    if len(raw) != 20 or any(c not in ALPHABET for c in raw) or _checksum(raw[:16]) != raw[16:]:
        return None
    return "AIRS-" + "-".join(raw[i:i + 5] for i in range(0, 20, 5))


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def read_certificate(text, key_hex=None):
    """The certificate's payload if it was signed by the seller, else None."""
    key_hex = key_hex or public_key()
    text = str(text or "").strip()
    if not key_hex or not text.startswith(CERT_PREFIX):
        return None
    try:
        body64, sig64 = text[len(CERT_PREFIX):].split(".")
        body, sig = _unb64(body64), _unb64(sig64)
        if not ed25519.verify(bytes.fromhex(key_hex), body, sig):
            return None
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) and payload.get("v") == 1 else None


def install_id(conn):
    value = db.get_setting(conn, "_install_id")
    if not value or not re.fullmatch(r"[0-9a-f]{32}", str(value)):
        value = secrets.token_hex(16)
        db.set_setting(conn, "_install_id", value)
        conn.commit()
    return value


def domain():
    return str(config.DOMAIN or "").strip().lower().rstrip(".")


def trial_started(conn, now=None):
    value = db.get_setting(conn, "_trial_started")
    if value is None or value == "":
        value = int(time.time()) if now is None else int(now)
        db.set_setting(conn, "_trial_started", value)
        conn.commit()
    return int(value)


# -------------------------------------------------------------------- state
def state(conn, now=None, use_cache=False):
    """Everything Studio shows about the licence. status: developer|licensed|trial|expired."""
    if use_cache:
        with _lock:
            if _cache["state"] is not None and time.time() - _cache["at"] < 30:
                return _cache["state"]
    now = int(time.time()) if now is None else int(now)
    ident = install_id(conn)
    info = {"install_id": ident, "domain": domain(), "server": server_url(), "purchase_url": purchase_url(),
            "version": __version__, "trial_days": TRIAL_DAYS, "locked": False, "licence": None,
            "last_error": db.get_setting(conn, "_licence_error") or "",
            "checked_at": db.get_setting(conn, "_licence_checked") or 0}
    if not public_key():
        info.update(status="developer", message="Developer build: licensing is not configured.")
        return _remember(info)
    cert = db.get_setting(conn, "_licence_cert") or ""
    payload = read_certificate(cert) if cert else None
    mismatch = ""
    if payload:
        if payload.get("install") != ident:
            mismatch = "This certificate belongs to another install (was the database copied from another server?)."
        elif payload.get("domain") != domain():
            mismatch = (f"This licence was activated for {payload.get('domain')}, but this station is {domain()}. "
                        "Activate it again to move it here.")
        else:
            info.update(status="licensed", licence={
                "id": payload.get("lic"), "name": payload.get("name", ""), "edition": payload.get("edition", ""),
                "hint": payload.get("hint", ""), "kind": payload.get("kind", "online"),
                "issued": payload.get("issued"), "domain": payload.get("domain")},
                message=f"Licensed to {payload.get('name') or payload.get('lic')}.")
            return _remember(info)
    started = trial_started(conn, now)
    left = TRIAL_DAYS - (now - started) / 86400
    info["trial_ends"] = started + TRIAL_DAYS * 86400
    if left > 0:
        days = int(left) + (1 if left % 1 else 0)
        info.update(status="trial", days_left=days,
                    message=mismatch or f"Trial: {days} day{'s' if days != 1 else ''} left.")
    else:
        info.update(status="expired", locked=True, days_left=0,
                    message=mismatch or "The 30-day trial has ended. Enter a licence key to keep using Studio. "
                                        "The station itself keeps broadcasting.")
    return _remember(info)


def _remember(info):
    with _lock:
        _cache.update(at=time.time(), state=info)
    return info


def forget():
    with _lock:
        _cache.update(at=0.0, state=None)


def locked(conn):
    return state(conn, use_cache=True)["locked"]


def allowed_when_locked(path):
    return bool(FREE_ADMIN.match(path))


# --------------------------------------------------------- licence server
def _post(path, body, timeout=15):
    url = server_url()
    if not url:
        raise LicenceRefused("This build has no licence server address. Use a certificate instead, "
                             "or contact the seller.")
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "User-Agent": f"AirStation/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as error:
        try:
            data = json.loads(error.read())
        except ValueError:
            data = {}
        if error.code in (400, 403, 404, 409) and data.get("error"):
            raise LicenceRefused(data.get("message") or data["error"]) from None
        raise ConnectionError(f"Licence server answered {error.code}. Try again later.") from None
    except (OSError, ValueError) as error:
        raise ConnectionError(f"Can't reach the licence server ({error}). Check the server's internet "
                              "connection, or ask the seller for an offline certificate.") from None


def _store(conn, cert):
    payload = read_certificate(cert)
    if payload is None:
        raise LicenceRefused("The licence server sent a certificate this build doesn't accept. "
                             "Make sure you are running an official release.")
    db.set_setting(conn, "_licence_cert", cert)
    db.set_setting(conn, "_licence_checked", int(time.time()))
    db.set_setting(conn, "_licence_error", "")
    conn.commit()
    forget()
    return payload


def activate(conn, key):
    canonical = normalise_key(key)
    if not canonical:
        raise LicenceRefused("That doesn't look like a licence key (AIRS-XXXXX-XXXXX-XXXXX-XXXXX). "
                             "Check it against your purchase e-mail.")
    reply = _post("/v1/activate", {"key": canonical, "domain": domain(), "install_id": install_id(conn),
                                   "version": __version__})
    return _store(conn, reply.get("certificate", ""))


def use_certificate(conn, text):
    """An offline certificate from the seller (for stations that can't reach the licence server)."""
    text = re.sub(r"\s+", "", str(text or ""))
    payload = read_certificate(text)
    if payload is None:
        raise LicenceRefused("That certificate isn't valid for this build. Copy it again, in one piece.")
    if payload.get("install") != install_id(conn):
        raise LicenceRefused(f"That certificate was made for another install. This install's id is "
                             f"{install_id(conn)}.")
    if payload.get("domain") != domain():
        raise LicenceRefused(f"That certificate is for {payload.get('domain')}, but this station is {domain()}.")
    return _store(conn, text)


def deactivate(conn):
    """Release this server's activation so the key can be used on a new server."""
    cert = db.get_setting(conn, "_licence_cert") or ""
    if cert and server_url():
        _post("/v1/deactivate", {"certificate": cert})
    db.set_setting(conn, "_licence_cert", "")
    conn.commit()
    forget()


def checkin(conn, force=False, now=None):
    """Refresh the certificate about weekly. Returns a short result text."""
    now = int(time.time()) if now is None else int(now)
    cert = db.get_setting(conn, "_licence_cert") or ""
    payload = read_certificate(cert) if cert else None
    if not payload or not server_url():
        return "nothing to check"
    if not force and (payload.get("kind") == "offline" or now < int(payload.get("checkin_after") or 0)):
        return "not due"
    if not force and now - int(db.get_setting(conn, "_licence_attempt") or 0) < 6 * 3600:
        return "tried recently"
    db.set_setting(conn, "_licence_attempt", now)
    conn.commit()
    try:
        reply = _post("/v1/checkin", {"certificate": cert, "version": __version__})
    except LicenceRefused as refused:
        # The seller cancelled the licence or it was moved to another server.
        db.set_setting(conn, "_licence_cert", "")
        db.set_setting(conn, "_licence_error", str(refused))
        conn.commit()
        forget()
        return f"removed: {refused}"
    except ConnectionError as error:
        db.set_setting(conn, "_licence_error", str(error))
        conn.commit()
        return "server unreachable (licence unchanged)"
    _store(conn, reply.get("certificate", ""))
    return "refreshed"


# ------------------------------------------------------------------ CLI
def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    conn = db.open_db()
    try:
        if args[:1] == ["activate"] and len(args) == 2:
            activate(conn, args[1])
        elif args[:1] == ["certificate"] and len(args) == 2:
            use_certificate(conn, args[1])
        elif args[:1] == ["deactivate"]:
            deactivate(conn)
        elif args[:1] == ["check"]:
            print(checkin(conn, force=True))
        elif args[:1] not in (["status"], []):
            print("usage: python3 -m airstation.licensing status | activate KEY | certificate CERT | deactivate | check")
            return 2
    except (LicenceRefused, ConnectionError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    s = state(conn)
    print(f"Status:     {s['status']}  {s['message']}")
    print(f"Domain:     {s['domain']}")
    print(f"Install id: {s['install_id']}")
    if s["licence"]:
        print(f"Licence:    {s['licence']['id']} (key …{s['licence']['hint']}), {s['licence']['kind']}")
    if s["last_error"]:
        print(f"Last check: {s['last_error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
