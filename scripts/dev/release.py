#!/usr/bin/env python3
"""Build a sellable AirStation release zip with your licence server built in.

    python3 scripts/dev/release.py --server https://license.example.com \\
        [--public-key HEX] [--purchase-url https://shop.example.com] [--out DIR] [--skip-tests]

- Fetches the public key from <server>/v1/health (or uses --public-key, and checks that
  they match when both are available).
- Copies the project to a temporary folder, writes the key, server and purchase address
  into app/airstation/licensing.py, runs the tests there, and zips it as
  air-station-v<version>.zip (unzips to air-station/), without development leftovers.

The repository itself is left unchanged: it stays a developer build (no trial, no lock).
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXCLUDE_DIRS = {".git", "__pycache__", ".github", "node_modules", ".pytest_cache"}
EXCLUDE_FILES = {".env", ".DS_Store"}
EXCLUDE_TOP = {"data", "secrets"}            # never ship a station's own data


def fetch_key(server):
    with urllib.request.urlopen(server.rstrip("/") + "/v1/health", timeout=20) as r:
        return json.loads(r.read())["public_key"]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", required=True, help="licence server address, https://...")
    p.add_argument("--public-key", help="64 hex characters (from: python3 -m airlicense public-key)")
    p.add_argument("--purchase-url", default="", help="where Studio's 'Buy a licence' button points")
    p.add_argument("--out", default=str(ROOT.parent))
    p.add_argument("--skip-tests", action="store_true")
    a = p.parse_args()

    server = a.server.rstrip("/")
    if not re.fullmatch(r"(https://[A-Za-z0-9.-]+|http://(127\.0\.0\.1|localhost))(:\d+)?(/[\w./-]*)?", server):
        p.error("--server must be an https:// address (http:// only for 127.0.0.1 testing)")
    key = (a.public_key or "").strip().lower()
    try:
        live = fetch_key(server).lower()
    except Exception as error:  # the server may not be public yet
        live = ""
        print(f"Note: could not read {server}/v1/health ({error}).")
    if key and live and key != live:
        sys.exit(f"The public key you gave doesn't match the one {server} uses. Refusing to build.")
    key = key or live
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        sys.exit("Need the licence server's public key: --public-key HEX, or a reachable --server.")
    if a.purchase_url and not a.purchase_url.startswith("https://"):
        p.error("--purchase-url must start with https://")

    version = re.search(r'__version__ = "([^"]+)"', (ROOT / "app/airstation/__init__.py").read_text()).group(1)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "air-station"

        def ignore(folder, names):
            skip = {n for n in names if n in EXCLUDE_DIRS or n in EXCLUDE_FILES or n.endswith(".pyc")}
            if Path(folder) == ROOT:
                skip |= {n for n in names if n in EXCLUDE_TOP}
            return skip
        shutil.copytree(ROOT, stage, ignore=ignore)
        lic = stage / "app/airstation/licensing.py"
        text = lic.read_text()
        for name, value in (("PUBLIC_KEY", key), ("LICENSE_SERVER", server), ("PURCHASE_URL_DEFAULT", a.purchase_url)):
            text, n = re.subn(rf'^{name} = ".*"$', f'{name} = "{value}"', text, count=1, flags=re.M)
            if n != 1:
                sys.exit(f"Could not set {name} in licensing.py")
        lic.write_text(text)
        if not a.skip_tests:
            print("Running tests on the release copy…")
            result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=stage,
                                    capture_output=True, text=True)
            if result.returncode != 0:
                print(result.stdout[-3000:], result.stderr[-3000:])
                sys.exit("Tests failed; no release built.")
        out = Path(a.out) / f"air-station-v{version}.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for path in sorted(stage.rglob("*")):
                if "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                z.write(path, path.relative_to(stage.parent))
    print(f"Built {out}\n  licence server: {server}\n  public key:     {key}")


if __name__ == "__main__":
    main()
