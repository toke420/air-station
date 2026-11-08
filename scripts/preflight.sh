#!/usr/bin/env bash
# Checks the VPS before the first start. Changes nothing.
set -uo pipefail
export PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH"   # root via plain `su` lacks these
cd "$(dirname "$0")/.."

pass=0; warn=0; fail=0
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; pass=$((pass+1)); }
wn()   { printf '  \033[33mWARN\033[0m  %s\n' "$*"; warn=$((warn+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; fail=$((fail+1)); }

env_get() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2-; }

echo "AirStation preflight"
[ "$(id -u)" = 0 ] && ok "running as root" || bad "run as root"

if [ -r /etc/os-release ]; then
  . /etc/os-release
  case "${VERSION_ID:-}" in
    13) ok "Debian 13 (${PRETTY_NAME})" ;;
    12) wn "Debian 12 detected; tested for Debian 13 but should work" ;;
    *)  wn "OS is ${PRETTY_NAME:-unknown}; this guide targets Debian 13" ;;
  esac
fi

arch=$(uname -m)
case "$arch" in
  x86_64|aarch64) ok "CPU architecture $arch" ;;
  *) bad "CPU architecture $arch is not supported by the upstream images" ;;
esac

mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
[ "$mem_mb" -ge 1800 ] && ok "RAM ${mem_mb} MB" || wn "RAM ${mem_mb} MB is tight (2 GB+ recommended)"
swap_mb=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
[ "$swap_mb" -gt 0 ] && ok "swap ${swap_mb} MB" || wn "no swap; consider a 2 GB swap file (docs/OPERATIONS.md)"

free_gb=$(df -BG --output=avail . | tail -1 | tr -dc 0-9)
[ "$free_gb" -ge 30 ] && ok "free disk ${free_gb} GB" || wn "free disk ${free_gb} GB (cache 12 + reserve 10 + recordings)"

if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  ok "$(docker --version | cut -d, -f1), compose $(docker compose version --short)"
else
  bad "Docker Engine with the compose plugin is missing (docs/DEPLOY.md step 1)"
fi

for tool in python3 ffprobe mount.cifs wg curl; do
  command -v "$tool" >/dev/null && ok "$tool installed" || bad "$tool missing: apt install -y python3 ffmpeg cifs-utils wireguard-tools curl"
done

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw status | grep -q "51820" && ok "ufw active, WireGuard 51820/udp allowed" \
    || wn "ufw is active: it doesn't affect Docker ports, but WireGuard needs 'ufw allow 51820/udp' (wg-setup.sh adds it)"
fi

here=$(pwd -P)
[ "$here" = /opt/air-station ] && ok "installed at /opt/air-station" \
  || bad "this copy is at $here, not /opt/air-station (see DEPLOY.md step 2)"

# Containers left over from another copy (e.g. the v1 bundle) hold ports 8000/8005.
if command -v docker >/dev/null; then
  others=$(docker ps -a --filter label=com.docker.compose.project=air-station \
    --format '{{.Names}} {{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null \
    | awk -v here="$here" '$2 != here {print $1}' | xargs)
  [ -z "$others" ] && ok "no leftover air-station containers" \
    || bad "old containers from another folder: $others (remove: docker rm -f $others)"
fi

[ -f .env ] && ok ".env present" || bad ".env missing: run python3 scripts/init.py"
[ -s secrets/internal_token ] && ok "secrets generated" || bad "secrets missing: run python3 scripts/init.py"
[ -s data/generated/mediamtx.yml ] && ok "MediaMTX config rendered" || bad "MediaMTX config missing: run python3 scripts/init.py"

domain=$(env_get AIR_DOMAIN)
email=$(env_get AIR_ACME_EMAIL)
[ -n "$email" ] && [ "${email#*@example.com}" = "$email" ] && ok "ACME email $email" || bad "set AIR_ACME_EMAIL in .env (python3 scripts/init.py --email ...)"

public_ip=$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)
[ -n "$public_ip" ] && ok "public IPv4 $public_ip" || wn "could not detect the public IP"
if [ -n "$domain" ]; then
  resolved=$(getent ahostsv4 "$domain" | awk 'NR==1 {print $1}')
  if [ -z "$resolved" ]; then
    bad "$domain does not resolve yet: add an A record -> ${public_ip:-VPS_IP}"
  elif [ -n "$public_ip" ] && [ "$resolved" != "$public_ip" ]; then
    bad "$domain resolves to $resolved, not this VPS ($public_ip)"
  else
    ok "$domain resolves to $resolved"
  fi
fi

# Ports this stack publishes. Our own containers are fine; anything else clashes.
for spec in tcp:80 tcp:443 udp:443 tcp:8005 tcp:1935 udp:8890 tcp:8000 tcp:8080; do
  proto=${spec%%:*}; port=${spec##*:}
  if [ "$proto" = tcp ]; then line=$(ss -Hltnp "sport = :$port" 2>/dev/null); else line=$(ss -Hlunp "sport = :$port" 2>/dev/null); fi
  if [ -z "$line" ]; then ok "$proto/$port free"
  elif echo "$line" | grep -q docker-proxy; then ok "$proto/$port used by Docker (this stack?)"
  else bad "$proto/$port already in use: $(echo "$line" | grep -o 'users:(("[^"]*' | cut -d'"' -f2 | head -1)"
  fi
done

if systemctl is-active --quiet systemd-timesyncd 2>/dev/null || timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
  ok "clock synchronised"
else
  wn "clock sync not confirmed (timedatectl)"
fi

echo
echo "Remember the Vultr firewall group: TCP 22 (your IP), 80, 443, 8005, 1935; UDP 443, 8890, 51820."
echo "Summary: $pass ok, $warn warnings, $fail failures"
[ "$fail" -eq 0 ]
