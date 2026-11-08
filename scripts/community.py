#!/usr/bin/env python3
"""AirStation Community add-on: Rauthy single sign-on + NodeBB forum.

    python3 scripts/community.py init            # settings, passwords, configs, Caddy
    python3 scripts/community.py status          # what is running and reachable
    python3 scripts/community.py reload          # load the forum/sign-on sites into Caddy
    python3 scripts/community.py fix-url         # fix NodeBB's address and proxy setting
    python3 scripts/community.py admin-password  # Rauthy's first admin password
    python3 scripts/community.py db-password     # for NodeBB's web installer
    python3 scripts/community.py finish          # open the forum to the public
    python3 scripts/community.py categories --token TOKEN [--tidy]
    python3 scripts/community.py render          # rebuild Rauthy config after .env edits
    python3 scripts/community.py disable         # stop serving forum/sign-on (keeps data)

Full walkthrough: docs/COMMUNITY-SETUP.md
"""
import argparse
import base64
import getpass
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UID = GID = 10001
ENV = ROOT / ".env"
SECRETS = ROOT / "secrets"
RAUTHY_DIR = ROOT / "data" / "community" / "rauthy"
CADDY_D = ROOT / "config" / "Caddyfile.d"
TEMPLATES = ROOT / "config" / "community"
STRATEGY = "airstation"          # NodeBB OAuth2 strategy name (login button)

os.environ["PATH"] = "/usr/local/sbin:/usr/sbin:/sbin:" + os.environ.get("PATH", "/usr/bin:/bin")


# ------------------------------------------------------------------ helpers

def say(msg=""):
    print(msg, flush=True)


def die(msg):
    sys.exit(f"ERROR: {msg}")


def read_env():
    values = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            m = re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip())
            if m:
                values[m.group(1)] = m.group(2).strip().strip("'\"")
    return values


def set_env(key, value):
    text = ENV.read_text() if ENV.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    ENV.write_text(text)


def secret_file(name, make=None, value=None):
    """Return a secret, creating it (0400, owned by uid 10001) when missing."""
    SECRETS.mkdir(mode=0o700, exist_ok=True)
    path = SECRETS / name
    if value is not None:
        if path.exists():
            path.chmod(0o600)
        path.write_text(value)
    elif not path.exists() or not path.read_text().strip():
        if make is None:
            return ""
        path.write_text(make())
    os.chown(path, UID, GID)
    path.chmod(0o400)
    return path.read_text().strip()


def compose(*args, check=True, capture=False):
    cmd = ["docker", "compose", *args]
    return subprocess.run(cmd, cwd=ROOT, check=check, text=True,
                          capture_output=capture)


def ask(prompt, default="", validate=None):
    while True:
        shown = f" [{default}]" if default else ""
        answer = input(f"  {prompt}{shown}: ").strip() or default
        if not validate or validate(answer):
            return answer
        say("    That doesn't look right, try again.")


def toml_str(value):
    # JSON string escaping is valid TOML basic-string escaping.
    return json.dumps(str(value), ensure_ascii=False)


def resolves_here(domain):
    try:
        mine = urllib.request.urlopen("https://api.ipify.org", timeout=8).read().decode().strip()
    except OSError:
        mine = ""
    try:
        addr = socket.gethostbyname(domain)
    except OSError:
        return False, "does not resolve yet"
    if mine and addr != mine:
        return False, f"points at {addr}, not this VPS ({mine})"
    return True, f"points at {addr}"


# ------------------------------------------------------------------- render

def render_rauthy(env):
    template = (TEMPLATES / "rauthy.toml.template").read_text()
    key_id = secret_file("rauthy_enc_key_id",
                         lambda: "".join(secrets.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
                                         for _ in range(16)))
    key = secret_file("rauthy_enc_key", lambda: base64.b64encode(secrets.token_bytes(32)).decode())
    values = {
        "ID_DOMAIN": toml_str(env["AIR_ID_DOMAIN"]),
        # Rauthy requires the port in the WebAuthn origin, even the default 443.
        "RP_ORIGIN": toml_str(f"https://{env['AIR_ID_DOMAIN']}:443"),
        "RP_NAME": toml_str(env.get("AIR_STATION_NAME") or "AirStation"),
        "SECRET_RAFT": toml_str(secret_file("rauthy_secret_raft", lambda: secrets.token_hex(32))),
        "SECRET_API": toml_str(secret_file("rauthy_secret_api", lambda: secrets.token_hex(32))),
        "ENC_KEY_ENTRY": toml_str(f"{key_id}/{key}"),
        "ENC_KEY_ID": toml_str(key_id),
        "ADMIN_EMAIL": toml_str(env["AIR_ADMIN_EMAIL"]),
        "SMTP_HOST": toml_str(env.get("AIR_SMTP_HOST") or "localhost"),
        "SMTP_PORT": str(int(env.get("AIR_SMTP_PORT") or 587)),
        "SMTP_USERNAME": toml_str(env.get("AIR_SMTP_USERNAME", "")),
        "SMTP_PASSWORD": toml_str(secret_file("smtp_password")),
        "SMTP_FROM": toml_str(env.get("AIR_SMTP_FROM") or f"AirStation <{env['AIR_ADMIN_EMAIL']}>"),
        "SMTP_TLS_MODE": toml_str(env.get("AIR_SMTP_TLS") or "starttls"),
    }
    for name, value in values.items():
        template = template.replace(f"@@{name}@@", value)
    left = re.findall(r"@@[A-Z_]+@@", template)
    if left:
        die(f"unfilled values in Rauthy template: {left}")
    (RAUTHY_DIR / "data").mkdir(parents=True, exist_ok=True)
    target = RAUTHY_DIR / "config.toml"
    if target.exists():
        target.chmod(0o600)
    target.write_text(template)
    for path, mode in ((RAUTHY_DIR, 0o700), (RAUTHY_DIR / "data", 0o700), (target, 0o600)):
        os.chown(path, UID, GID)
        path.chmod(mode)
    (ROOT / "data" / "community").chmod(0o755)
    say(f"  Rauthy config written: {target.relative_to(ROOT)}")


def install_snippet(source, target_name):
    CADDY_D.mkdir(exist_ok=True)
    shutil.copyfile(TEMPLATES / source, CADDY_D / target_name)
    (CADDY_D / target_name).chmod(0o644)


def reload_caddy():
    # Recreates Caddy only if its settings (e.g. the domains in .env) changed.
    compose("up", "-d", "--no-deps", "caddy", check=False, capture=True)
    result = compose("exec", "-T", "caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile",
                     "--adapter", "caddyfile", check=False, capture=True)
    if result.returncode != 0:
        say("  Caddy reload reported a problem, restarting it instead:\n    " + (result.stderr or "").strip()[-400:])
        compose("restart", "caddy", check=False)
    else:
        say("  Caddy reloaded.")


# ----------------------------------------------------------------- commands

def cmd_init(args):
    env = read_env()
    say("AirStation Community setup: Rauthy (accounts) + NodeBB (forum)\n")
    say("Addresses (press Enter to accept):")
    id_domain = ask("Sign-on address", env.get("AIR_ID_DOMAIN") or "id.example.com",
                    lambda v: re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", v))
    forum_domain = ask("Forum address", env.get("AIR_FORUM_DOMAIN") or "forum.example.com",
                       lambda v: re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", v))
    admin_email = ask("Your email (Rauthy admin login, alerts)", env.get("AIR_ADMIN_EMAIL") or "",
                      lambda v: re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v))
    say("\nEmail sending (needed for sign-up verification and password resets).")
    say("Use your provider's SMTP settings, e.g. Brevo: smtp-relay.brevo.com, port 587.")
    smtp_host = ask("SMTP server", env.get("AIR_SMTP_HOST") or "")
    smtp_port = ask("SMTP port (587 or 465)", env.get("AIR_SMTP_PORT") or "587", lambda v: v.isdigit())
    smtp_user = ask("SMTP username", env.get("AIR_SMTP_USERNAME") or "")
    smtp_from = ask("Send emails from (a verified sender at your provider)",
                    env.get("AIR_SMTP_FROM") or f"AirStation <{admin_email}>")
    have_pw = bool(secret_file("smtp_password"))
    pw = getpass.getpass(f"  SMTP password{' (Enter keeps the saved one)' if have_pw else ''}: ")
    if pw:
        secret_file("smtp_password", value=pw)
    elif not have_pw:
        secret_file("smtp_password", value="")
        say("  ! No SMTP password: sign-ups can't receive their verification email until you add one.")
    for key, value in (("AIR_ID_DOMAIN", id_domain), ("AIR_FORUM_DOMAIN", forum_domain),
                       ("AIR_ADMIN_EMAIL", admin_email), ("AIR_SMTP_HOST", smtp_host),
                       ("AIR_SMTP_PORT", smtp_port), ("AIR_SMTP_USERNAME", smtp_user),
                       ("AIR_SMTP_FROM", smtp_from),
                       ("AIR_SMTP_TLS", "tls" if smtp_port == "465" else "starttls"),
                       ("COMPOSE_PROFILES", "community")):
        set_env(key, value)
    env = read_env()
    say("\nPasswords and configuration:")
    secret_file("forum_db_password", lambda: secrets.token_hex(16))
    render_rauthy(env)
    install_snippet("id.caddy", "10-id.caddy")
    if not (CADDY_D / "20-forum.caddy").exists():
        install_snippet("forum-setup.caddy", "20-forum.caddy")
    say("  Caddy sites added (forum shows 'being set up' until you run finish).")
    reload_caddy()
    say("\nDNS:")
    for domain in (id_domain, forum_domain):
        ok, msg = resolves_here(domain)
        say(f"  {'OK  ' if ok else 'FIX '} {domain} {msg}")
    say("\nNext (docs/COMMUNITY-SETUP.md step 3):")
    say("  docker compose up -d")
    say("  python3 scripts/community.py status")


def cmd_render(args):
    env = read_env()
    for key in ("AIR_ID_DOMAIN", "AIR_ADMIN_EMAIL"):
        if not env.get(key):
            die(f"{key} missing in .env: run python3 scripts/community.py init")
    render_rauthy(env)
    say("Now: docker compose up -d --force-recreate rauthy")


def api_container_get(url, timeout=6):
    """Fetch a URL from inside the api container (same Docker network)."""
    code = ("import urllib.request,sys\n"
            f"r=urllib.request.urlopen({url!r},timeout={timeout})\n"
            "sys.stdout.write(str(r.status))")
    result = compose("exec", "-T", "api", "python3", "-c", code, check=False, capture=True)
    return result.stdout.strip() if result.returncode == 0 else (result.stderr.strip().splitlines() or ["error"])[-1]


def container_state(service):
    """(state, exit_code) of a compose service's container, or ("missing", None)."""
    result = compose("ps", "-a", "--format", "json", service, check=False, capture=True)
    for line in result.stdout.splitlines():
        try:
            info = json.loads(line)
        except ValueError:
            continue
        if isinstance(info, list):
            info = info[0] if info else {}
        code = info.get("ExitCode")
        # While Docker restarts a crashing container, ExitCode reads 0; the real
        # code is in Status, e.g. "Restarting (139) 5 seconds ago".
        found = re.search(r"\((\d+)\)", str(info.get("Status", "")))
        if found:
            code = int(found.group(1))
        return str(info.get("State", "?")), code
    return "missing", None


def caddy_sites():
    """Host names in Caddy's running config ('' if it can't be read)."""
    result = compose("exec", "-T", "caddy", "wget", "-qO-", "http://localhost:2019/config/apps/http/servers",
                     check=False, capture=True)
    return result.stdout if result.returncode == 0 else ""


def cmd_status(args):
    env = read_env()
    compose("ps", "postgres", "rauthy", "nodebb", check=False)
    say("")
    problems = []

    state, code = container_state("rauthy")
    rauthy = api_container_get("http://rauthy:8080/auth/v1/health")
    if state == "restarting" or (state == "exited" and code):
        why = {139: "crashed", 137: "killed: out of memory",
               101: "stopped on a configuration error", 134: "aborted"}.get(code, f"exit code {code}")
        say(f"Rauthy (inside):  NOT RUNNING, {why}. Its last log lines:")
        logs = compose("logs", "--no-color", "--no-log-prefix", "--tail", "25", "rauthy",
                       check=False, capture=True).stdout.strip()
        # Show each distinct message once (a restart loop repeats the same lines).
        seen, lines = set(), []
        for line in logs.splitlines():
            if line.strip() and line not in seen:
                seen.add(line)
                lines.append(line)
        say("    " + ("\n    ".join(lines[-12:]) if lines else "(no output)"))
        problems.append("rauthy")
    else:
        say(f"Rauthy (inside):  {'OK' if rauthy == '200' else rauthy}")

    nodebb = api_container_get("http://nodebb:4567/api/config")
    if nodebb != "200" and "404" in nodebb and api_container_get("http://nodebb:4567/") == "200":
        say("NodeBB (inside):  web installer waiting (normal until step 5)")
    else:
        say(f"NodeBB (inside):  {'OK' if nodebb == '200' else nodebb}")
        conf = nodebb_config() if nodebb == "200" else None
        want = f"https://{env.get('AIR_FORUM_DOMAIN')}"
        if conf and (str(conf.get("url", "")).rstrip("/") != want or conf.get("trust_proxy") is not True):
            say(f"    NodeBB isn't set up for {want} behind the proxy (url / trust_proxy):")
            say("    logins fail. Fix: python3 scripts/community.py fix-url")
            problems.append("nodebb config")

    if nodebb == "200":
        probe = ("fetch(process.argv[1]).then(r=>console.log(r.status))"
                 ".catch(e=>console.log('ERROR '+((e.cause&&e.cause.code)||e.message)))")
        url = f"https://{env.get('AIR_ID_DOMAIN')}/auth/v1/.well-known/openid-configuration"
        result = compose("exec", "-T", "nodebb", "node", "-e", probe, url, check=False, capture=True)
        reach = (result.stdout or result.stderr).strip()
        if reach == "200":
            say("NodeBB -> sign-on: OK")
        else:
            say(f"NodeBB -> sign-on: {reach or 'no answer'}. Forum logins with AirStation accounts")
            say("    can't finish. Fix: docker compose up -d caddy (v2.2.8 adds the internal")
            say("    route), or: ufw allow 80/tcp && ufw allow 443/tcp")
            problems.append("nodebb to sign-on")

    sites = caddy_sites()
    for name, domain, path in (("Sign-on (public)", env.get("AIR_ID_DOMAIN"), "/auth/v1/health"),
                               ("Forum (public)  ", env.get("AIR_FORUM_DOMAIN"), "/")):
        try:
            result = urllib.request.urlopen(f"https://{domain}{path}", timeout=10).status
        except urllib.error.HTTPError as error:
            result = error.code
        except OSError as error:
            result = f"unreachable ({getattr(error, 'reason', error)})"
        say(f"{name}: {result}")
        if isinstance(result, str):
            ok, dns = resolves_here(domain)
            if not ok:
                say(f"    DNS: {domain} {dns}. Add/fix the A record (203.0.113.10), then wait a few minutes.")
            elif sites and domain not in sites:
                say(f"    Caddy hasn't loaded {domain}. Run: python3 scripts/community.py reload")
            else:
                say("    DNS is fine and Caddy has the site, so the certificate isn't issued yet.")
                say(f"    Caddy's messages: docker compose logs caddy | grep -i '{domain}' | tail -20")
            problems.append(domain)
    live = (CADDY_D / "20-forum.caddy").exists() and "reverse_proxy" in (CADDY_D / "20-forum.caddy").read_text()
    say(f"Forum mode: {'live' if live else 'setup placeholder (run finish after the installer)'}")
    if not problems:
        say("\nAll good.")


def cmd_reload(args):
    reload_caddy()


def cmd_admin_password(args):
    logs = compose("logs", "--no-color", "--no-log-prefix", "rauthy", check=False, capture=True).stdout
    lines = logs.splitlines()
    # Rauthy prints "First-Time setup - new random password for '<email>':" and
    # the password itself on the following line.
    found = None
    for i, line in enumerate(lines):
        if "new random password" in line:
            for after in lines[i + 1:i + 5]:
                candidate = after.strip()
                if candidate and not set(candidate) <= set("-=*#_ ") and " INFO " not in candidate:
                    found = candidate
                    break
    if not found:
        say("No first-time password in Rauthy's log. It's only printed on the very first start,")
        say("and the log is lost when the container is recreated. Show the raw log with:")
        say("  docker compose logs --no-log-prefix rauthy | grep -A4 'new random password'")
        return
    env = read_env()
    say(f"Rauthy admin:  {env.get('AIR_ADMIN_EMAIL')}")
    say(f"Password:      {found}")
    say(f"\nLog in at https://{env.get('AIR_ID_DOMAIN')}/auth/v1/admin")
    say("and change the password (or add a passkey) straight away.")


def cmd_db_password(args):
    say("NodeBB web installer, database section:")
    say("  Database: PostgreSQL | Host: postgres | Port: 5432")
    say("  Username: nodebb | Database name: nodebb")
    say(f"  Password: {secret_file('forum_db_password')}")


def set_site_links(env, force=False):
    forum = f"https://{env['AIR_FORUM_DOMAIN']}"
    links = {"forum_url": forum,
             "signup_url": f"https://{env['AIR_ID_DOMAIN']}/auth/v1/users/register",
             "login_url": f"{forum}/login"}
    code = (
        "import json,sys\n"
        "from airstation import db\n"
        "c=db.open_db(); s=db.get_setting(c,'community') or {}\n"
        f"links=json.loads({json.dumps(json.dumps(links))}); force={force!r}\n"
        "changed=[k for k,v in links.items() if force or not s.get(k)]\n"
        "for k in changed: s[k]=links[k]\n"
        "db.set_setting(c,'community',s); c.commit(); print(','.join(changed) or 'none')\n")
    result = compose("exec", "-T", "api", "python3", "-c", code, check=False, capture=True)
    if result.returncode == 0:
        say(f"  Website Community buttons set: {result.stdout.strip()} (existing links kept)")
    else:
        say("  Could not update the website links automatically; set them in Admin > Site settings.")


NODEBB_CONFIG = "/opt/config/config.json"


def nodebb_config():
    """NodeBB's config.json as a dict, or None if it can't be read."""
    result = compose("exec", "-T", "nodebb", "cat", NODEBB_CONFIG, check=False, capture=True)
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def nodebb_url():
    """The "url" in NodeBB's config.json, or None if it can't be read."""
    return (nodebb_config() or {}).get("url")


def fix_nodebb_url(env):
    """Make NodeBB's config.json fit running behind Caddy:
    - url: the installer pre-fills its own address (http://localhost:4567 through
      the tunnel); if that's kept, logins and live updates fail;
    - trust_proxy: without it NodeBB thinks requests are plain HTTP, so its secure
      session cookie is never set and every login fails with "invalid csrf token".
      Safe here: Caddy replaces X-Forwarded-For from clients.
    """
    want = f"https://{env['AIR_FORUM_DOMAIN']}"
    conf = nodebb_config()
    if conf is None:
        say("  Could not read NodeBB's config.json (is the installer finished?)")
        return False
    changes = {}
    if str(conf.get("url", "")).rstrip("/") != want:
        changes["url"] = want
    if conf.get("trust_proxy") is not True:
        changes["trust_proxy"] = True
    if not changes:
        say(f"  NodeBB settings OK: {want}, behind the proxy")
        return True
    script = ("const fs=require('fs'),f=process.argv[1],c=JSON.parse(fs.readFileSync(f));"
              f"Object.assign(c,{json.dumps(changes)});fs.writeFileSync(f,JSON.stringify(c,null,4));")
    result = compose("exec", "-T", "nodebb", "node", "-e", script, NODEBB_CONFIG, check=False, capture=True)
    if result.returncode != 0:
        say(f"  ! Could not update NodeBB's config.json: {result.stderr.strip()[-200:]}")
        return False
    say(f"  NodeBB settings changed: {', '.join(f'{k}={v}' for k, v in changes.items())}. Restarting NodeBB...")
    compose("restart", "nodebb", check=False, capture=True)
    return True


def cmd_finish(args):
    env = read_env()
    fix_nodebb_url(env)
    install_snippet("forum-live.caddy", "20-forum.caddy")
    say("  Forum site switched to live.")
    reload_caddy()
    set_site_links(env, force=args.force_links)
    say(f"\nForum: https://{env['AIR_FORUM_DOMAIN']}")
    say("Next: docs/COMMUNITY-SETUP.md step 6 (connect the forum to AirStation sign-on).")


def nodebb_api(method, path, token, body=None, base="http://127.0.0.1:4567"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode() or "{}")


CATEGORIES = [
    {"name": "New Releases", "icon": "fa-compact-disc", "bgColor": "#1f9bff", "color": "#ffffff",
     "description": "Fresh drops, premieres and tracks you can't stop playing. Share links and tell us why."},
    {"name": "General Discussion", "icon": "fa-comments", "bgColor": "#2d3848", "color": "#ffffff",
     "description": "Hip Hop, Rap, Trap, Grime, R&B and everything in between. Shows, artists, the station."},
    {"name": "Classifieds", "icon": "fa-tags", "bgColor": "#ff2b2b", "color": "#ffffff",
     "description": "Buy, sell and swap: records, studio and DJ gear, CDs and merch. Read the pinned rules first.",
     "children": [
         {"name": "Records & Vinyl", "icon": "fa-record-vinyl", "bgColor": "#d61616", "color": "#ffffff",
          "description": "LPs, 12\"s, 7\"s and box sets."},
         {"name": "Studio & DJ Gear", "icon": "fa-sliders", "bgColor": "#d61616", "color": "#ffffff",
          "description": "Decks, controllers, mixers, monitors, mics, interfaces, software licences."},
         {"name": "CDs & Tapes", "icon": "fa-compact-disc", "bgColor": "#d61616", "color": "#ffffff",
          "description": "CDs, cassettes, mixtapes and promos."},
         {"name": "Merch", "icon": "fa-shirt", "bgColor": "#d61616", "color": "#ffffff",
          "description": "Tees, hoodies, posters and artist merch."},
     ]},
]


def cmd_categories(args):
    token = args.token
    try:
        existing = nodebb_api("GET", "/api/categories", token).get("categories", [])
    except (OSError, ValueError) as error:
        die(f"Could not reach NodeBB on 127.0.0.1:4567 with that token ({error}).")
    by_name = {}

    def walk(items):
        for c in items:
            by_name[c["name"].lower()] = c
            walk(c.get("children") or [])
    walk(existing)

    def ensure(spec, parent=None):
        fields = {k: v for k, v in spec.items() if k != "children"}
        if parent:
            fields["parentCid"] = parent
        found = by_name.get(spec["name"].lower())
        if found:
            nodebb_api("PUT", f"/api/v3/categories/{found['cid']}", token, fields)
            cid = found["cid"]
            say(f"  updated  {spec['name']}")
        else:
            reply = nodebb_api("POST", "/api/v3/categories", token, fields)
            cid = reply.get("response", {}).get("cid")
            say(f"  created  {spec['name']}")
        for child in spec.get("children", []):
            ensure(child, cid)
        return cid

    say("Forum categories:")
    classifieds = None
    for spec in CATEGORIES:
        try:
            cid = ensure(spec)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            die(f"NodeBB refused '{spec['name']}' (HTTP {error.code}): {detail}\n"
                "Is the token for the admin user (uid 1)? Create it under Admin > Settings > API Access.")
        except (OSError, ValueError) as error:
            die(f"Could not create '{spec['name']}': {error}")
        if spec["name"] == "Classifieds":
            classifieds = cid
    if args.tidy:
        for name in ("Blogs", "Comments & Feedback"):
            found = by_name.get(name.lower())
            if found:
                try:
                    nodebb_api("PUT", f"/api/v3/categories/{found['cid']}", token, {"disabled": 1})
                    say(f"  hidden   {name}")
                except (OSError, ValueError) as error:
                    say(f"  could not hide {name}: {error}")
    rules = (ROOT / "web" / "assets" / "community" / "classifieds-rules.md").read_text()
    if classifieds and not args.no_rules:
        try:
            reply = nodebb_api("POST", "/api/v3/topics", token,
                               {"cid": classifieds, "title": "Classifieds: read before you post",
                                "content": rules})
            tid = reply.get("response", {}).get("tid")
            if tid:
                nodebb_api("PUT", f"/api/v3/topics/{tid}/pin", token)
                nodebb_api("PUT", f"/api/v3/topics/{tid}/lock", token)
            say("  posted and pinned the Classifieds rules")
        except (OSError, ValueError) as error:
            say(f"  could not post the rules topic ({error}); paste web/assets/community/classifieds-rules.md yourself")
    say("Done. Check them at /categories and adjust anything in Admin > Manage > Categories.")


def cmd_disable(args):
    for name in ("10-id.caddy", "20-forum.caddy"):
        (CADDY_D / name).unlink(missing_ok=True)
    set_env("COMPOSE_PROFILES", "")
    compose("stop", "rauthy", "nodebb", "postgres", check=False)
    reload_caddy()
    say("Forum and sign-on are offline. Data is kept; run init again to bring them back.")


def main():
    parser = argparse.ArgumentParser(description="AirStation Community add-on",
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("render")
    sub.add_parser("status")
    sub.add_parser("reload")
    sub.add_parser("fix-url")
    sub.add_parser("admin-password")
    sub.add_parser("db-password")
    p = sub.add_parser("finish")
    p.add_argument("--force-links", action="store_true", help="overwrite existing website links")
    p = sub.add_parser("categories")
    p.add_argument("--token", required=True, help="NodeBB API token (Admin > Settings > API Access)")
    p.add_argument("--tidy", action="store_true", help="hide the default Blogs and Comments categories")
    p.add_argument("--no-rules", action="store_true", help="don't post the Classifieds rules topic")
    sub.add_parser("disable")
    args = parser.parse_args()
    if os.geteuid() != 0:
        die("run as root (su - or sudo -i)")
    if ROOT != Path("/opt/air-station") and not os.environ.get("AIR_ALLOW_ANY_PATH"):
        die(f"this copy is at {ROOT}; run it from /opt/air-station")
    {"init": cmd_init, "render": cmd_render, "status": cmd_status,
     "admin-password": cmd_admin_password, "db-password": cmd_db_password,
     "finish": cmd_finish, "categories": cmd_categories, "disable": cmd_disable,
     "reload": cmd_reload, "fix-url": lambda a: fix_nodebb_url(read_env())}[args.cmd](args)


if __name__ == "__main__":
    main()
