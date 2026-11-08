#!/usr/bin/env bash
# Health check for the running station. Changes nothing.
set -uo pipefail
export PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH"   # root via plain `su` lacks these
cd "$(dirname "$0")/.."
domain=$(grep -E '^AIR_DOMAIN=' .env | tail -1 | cut -d= -f2-)
fail=0
say() { printf '%-28s %s\n' "$1" "$2"; }

echo "== Containers"
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Health}}'
echo

echo "== Services"
curl -fsS --max-time 5 http://127.0.0.1:8080/api/health >/dev/null \
  && say "API" "OK" || { say "API" "NOT RESPONDING"; fail=1; }
if curl -fsS --max-time 5 http://127.0.0.1:8000/status-json.xsl -o /tmp/air-ice.json; then
  mounts=$(python3 -c "import json;d=json.load(open('/tmp/air-ice.json'))['icestats'].get('source',[]);d=d if isinstance(d,list) else [d];print(' '.join(s.get('listenurl','').rsplit('/',1)[-1]+'('+str(s.get('listeners',0))+')' for s in d) or 'none')")
  say "Icecast mounts (listeners)" "$mounts"
  echo "$mounts" | grep -q "radio.mp3" || { echo "  ! /radio.mp3 missing: docker compose logs liquidsoap"; fail=1; }
else
  say "Icecast" "NOT RESPONDING"; fail=1
fi

for mount in radio.mp3 radio-low.mp3; do
  if ffmpeg -nostdin -hide_banner -loglevel error -rw_timeout 15000000 \
       -i "http://127.0.0.1:8000/$mount" -t 8 -f null - 2>/tmp/air-decode.log; then
    say "Decode /$mount (8 s)" "OK"
  else
    say "Decode /$mount" "FAILED ($(tail -1 /tmp/air-decode.log))"; fail=1
  fi
done

if [ -n "$domain" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$domain/api/health" || true)
  [ "$code" = 200 ] && say "HTTPS https://$domain" "OK" || { say "HTTPS https://$domain" "HTTP $code"; fail=1; }
  code=$(curl -s -o /tmp/air-hls.m3u8 -w '%{http_code}' --max-time 10 "https://$domain/radio.m3u8" || true)
  segment=$(grep -m1 '^radio_[0-9]*\.ts' /tmp/air-hls.m3u8 2>/dev/null || true)
  if [ "$code" = 200 ] && [ -n "$segment" ] && \
     [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$domain/$segment" || true)" = 200 ]; then
    say "HLS /radio.m3u8" "OK"
  else
    say "HLS /radio.m3u8" "NOT READY (HTTP $code): docker compose logs --tail=20 hls"; fail=1
  fi
fi

echo
echo "== On air"
curl -fsS --max-time 5 http://127.0.0.1:8080/api/nowplaying | python3 -c "
import json,sys
d=json.load(sys.stdin); n=d['now']
print('  source   :', d['source'])
print('  playing  :', (n['artist']+' - ' if n['artist'] else '')+(n['title'] or '(no metadata)'))
print('  show     :', d['on_air']['name'])
print('  listeners:', d['listeners'])" 2>/dev/null || echo "  (unavailable)"

echo
echo "== NAS sync"
systemctl list-timers air-sync.timer --no-pager 2>/dev/null | sed -n '1,2p'
journalctl -u air-sync.service -n 3 --no-pager -o cat 2>/dev/null | sed 's/^/  /'
echo
echo "== Disk"
df -h /opt/air-station | tail -1
echo
[ "$fail" = 0 ] && echo "All checks passed. Listen too: a decode check can't hear silence." \
  || { echo "Some checks failed (see above)."; exit 1; }
