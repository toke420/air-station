#!/usr/bin/env python3
"""Prepare /opt/air-station on the VPS. Safe to re-run: existing passwords,
settings and data are kept.

    python3 scripts/init.py --domain radio.example.com --email you@example.com
"""
import argparse
import grp
import os
import pwd
import re
import secrets
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ROOT = Path("/opt/air-station")
UID = GID = 10001

# `su` without "-" leaves root without the sbin directories (groupadd, useradd...).
os.environ["PATH"] = "/usr/local/sbin:/usr/sbin:/sbin:" + os.environ.get("PATH", "/usr/bin:/bin")

# name -> generator. Hex/URL-safe values avoid quoting problems in URLs and Mixxx.
SECRETS = {
    "source_password": lambda: secrets.token_hex(24),
    "relay_password": lambda: secrets.token_hex(24),
    "admin_password": lambda: secrets.token_hex(24),        # Icecast admin (not public)
    "dj_password": lambda: secrets.token_hex(10),           # typed into Mixxx
    "obs_harbor_password": lambda: secrets.token_hex(24),   # MediaMTX -> Liquidsoap
    "obs_publish_password": lambda: secrets.token_hex(12),  # OBS -> MediaMTX
    "internal_token": lambda: secrets.token_hex(32),        # Liquidsoap -> API
    "admin_ui_password": lambda: secrets.token_urlsafe(12),  # website admin login
    "session_secret": lambda: secrets.token_hex(32),
    "forum_db_password": lambda: secrets.token_hex(16),     # community add-on (NodeBB)
}


def ensure_user():
    try:
        grp.getgrgid(GID)
    except KeyError:
        subprocess.run(["groupadd", "--gid", str(GID), "air"], check=True)
    try:
        pwd.getpwuid(UID)
    except KeyError:
        subprocess.run(["useradd", "--uid", str(UID), "--gid", str(GID),
                        "--no-create-home", "--home-dir", "/nonexistent",
                        "--shell", "/usr/sbin/nologin", "air"], check=True)


def ensure_dirs():
    for name in ("db", "cache", "art", "emergency", "recordings", "generated", "branding", "geoip", "site"):
        path = ROOT / "data" / name
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, UID, GID)
        path.chmod(0o755)
    # Cover files must stay readable by Caddy and replaceable by the sync job
    # (uid 10001), even if something once wrote them as root.
    for name in ("art", "branding"):
        for item in (ROOT / "data" / name).iterdir():
            if item.is_file() and not item.is_symlink():
                os.chown(item, UID, GID)
                item.chmod(0o644)
    os.chown(ROOT / "data", 0, 0)
    (ROOT / "data").chmod(0o755)


def ensure_secrets():
    directory = ROOT / "secrets"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    created = []
    for name, make in SECRETS.items():
        path = directory / name
        if not path.exists() or not path.read_text().strip():
            path.write_text(make())  # no trailing newline
            created.append(name)
        os.chown(path, UID, GID)
        path.chmod(0o400)
    return created


def set_env_value(text, key, value):
    line = f"{key}={value}"
    if re.search(rf"^{re.escape(key)}=.*$", text, flags=re.M):
        return re.sub(rf"^{re.escape(key)}=.*$", line, text, flags=re.M)
    return text.rstrip("\n") + "\n" + line + "\n"


def ensure_env(domain, email):
    env = ROOT / ".env"
    if not env.exists():
        env.write_text((ROOT / ".env.example").read_text())
    text = env.read_text()
    # Add settings introduced by newer versions, keeping existing values.
    added = []
    for line in (ROOT / ".env.example").read_text().splitlines():
        match = re.match(r"^([A-Z_]+)=", line)
        if match and not re.search(rf"^{match.group(1)}=", text, flags=re.M):
            text = text.rstrip("\n") + "\n" + line + "\n"
            added.append(match.group(1))
    if added:
        print("  .env: added new settings", ", ".join(added))
    if domain:
        text = set_env_value(text, "AIR_DOMAIN", domain)
    if email:
        text = set_env_value(text, "AIR_ACME_EMAIL", email)
    env.write_text(text)
    env.chmod(0o644)
    values = dict(re.findall(r"^([A-Z_]+)=(.*)$", text, flags=re.M))
    return values


def render_mediamtx():
    template = (ROOT / "config" / "mediamtx.yml.template").read_text()
    for placeholder, name in (("@@OBS_PUBLISH_PASSWORD@@", "obs_publish_password"),
                              ("@@OBS_HARBOR_PASSWORD@@", "obs_harbor_password")):
        template = template.replace(placeholder, (ROOT / "secrets" / name).read_text().strip())
    output = ROOT / "data" / "generated" / "mediamtx.yml"
    temporary = output.with_suffix(".tmp")
    if temporary.exists():
        temporary.chmod(0o600)
        temporary.unlink()
    temporary.write_text(template)
    os.chown(temporary, UID, GID)
    temporary.chmod(0o400)
    if output.exists():
        output.chmod(0o600)
    temporary.replace(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", help="public hostname, e.g. radio.example.com")
    parser.add_argument("--email", help="Let's Encrypt contact address")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Run as root: python3 scripts/init.py")
    if ROOT != EXPECTED_ROOT and not os.environ.get("AIR_ALLOW_ANY_PATH"):
        raise SystemExit(
            f"This copy is at {ROOT}, but AirStation must live in {EXPECTED_ROOT}\n"
            "(the sync service and scripts expect that path). Usually this means the\n"
            "shell is still inside a folder that was renamed. Run:\n"
            "    cd / && ls -d /opt/air-station*\n"
            "and see 'If init.py says the copy is in the wrong place' in docs/DEPLOY.md.")
    ensure_user()
    ensure_dirs()
    created = ensure_secrets()
    values = ensure_env(args.domain, args.email)
    render_mediamtx()
    for script in (ROOT / "scripts").iterdir():
        if script.suffix in (".sh", ".py") or script.name == "airctl":
            script.chmod(0o755)
    print("AirStation initialised in", ROOT)
    print("  new secrets:", ", ".join(created) if created else "none (existing ones kept)")
    print("  domain:     ", values.get("AIR_DOMAIN"))
    print("  ACME email: ", values.get("AIR_ACME_EMAIL"))
    if values.get("AIR_ACME_EMAIL", "").endswith("@example.com"):
        print("\n  ! Set a real email: python3 scripts/init.py --email you@yourdomain")
    print("\nAdmin login for https://%s/admin/ :" % values.get("AIR_DOMAIN"))
    print("  password:", (ROOT / "secrets" / "admin_ui_password").read_text().strip())
    print("\nNext: bash scripts/preflight.sh")


if __name__ == "__main__":
    main()
