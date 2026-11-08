#!/usr/bin/env bash
# Back up everything that can't be re-downloaded: settings, secrets, database,
# rendered config. Music (NAS/cache), art and recordings are NOT included.
#   bash scripts/backup.sh [--with-emergency]
set -euo pipefail
export PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH"   # root via plain `su` lacks these
cd "$(dirname "$0")/.."
[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
out_dir=/root/air-backups
stamp=$(date +%Y%m%d-%H%M%S)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
install -d -m 700 "$out_dir"

# Consistent copy of the live SQLite database.
python3 - "$work/air.sqlite3" <<'EOF'
import sqlite3, sys
src = sqlite3.connect("file:data/db/air.sqlite3?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[1])
src.backup(dst)
dst.close(); src.close()
EOF

items=(.env secrets config data/generated)
[ "${1:-}" = "--with-emergency" ] && items+=(data/emergency)
[ -f /etc/wireguard/wg-air.conf ] && items+=(/etc/wireguard/wg-air.conf /etc/wireguard/air-private.key /etc/wireguard/air-public.key)
[ -f /root/.air-smbcredentials ] && items+=(/root/.air-smbcredentials)

# Community add-on: forum database, forum uploads, sign-on data.
if grep -Eq '^COMPOSE_PROFILES=.*community' .env 2>/dev/null; then
  if docker compose exec -T postgres pg_dump -U nodebb nodebb | gzip > "$work/forum-db.sql.gz"; then
    echo "Forum database dumped."
  else
    echo "WARNING: forum database dump failed (is postgres running?)"; rm -f "$work/forum-db.sql.gz"
  fi
  docker compose cp nodebb:/usr/src/app/public/uploads "$work/forum-uploads" >/dev/null 2>&1 \
    || echo "WARNING: could not copy forum uploads"
  [ -d data/community ] && items+=(data/community)
fi

tar -czf "$out_dir/air-$stamp.tar.gz" -C "$work" . -C "$PWD" "${items[@]}" \
  2> >(grep -v "Removing leading" >&2)
chmod 600 "$out_dir/air-$stamp.tar.gz"
ls -1t "$out_dir"/air-*.tar.gz | tail -n +15 | xargs -r rm -f   # keep 14
echo "Backup written: $out_dir/air-$stamp.tar.gz ($(du -h "$out_dir/air-$stamp.tar.gz" | cut -f1))"
echo "It contains passwords and keys: download it over SFTP and keep it private."
