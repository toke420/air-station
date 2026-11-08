#!/usr/bin/env bash
# Diagnose the AirStation WireGuard link from the VPS side. Changes nothing
# (except installing tcpdump if it is missing).
#   bash scripts/wg-doctor.sh
set -uo pipefail
export PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH"
[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }

IF=wg-air
CONF=/etc/wireguard/$IF.conf
PUB=/etc/wireguard/air-public.key
RSC=/root/air-mikrotik.rsc
hr() { printf '\n== %s\n' "$*"; }

hr "1. Tunnel service on the VPS"
if systemctl is-active --quiet "wg-quick@$IF"; then
  echo "wg-quick@$IF is running."
else
  echo "PROBLEM: wg-quick@$IF is NOT running. Last log lines:"
  journalctl -u "wg-quick@$IF" -n 15 --no-pager -o cat
  echo "Fix: bash scripts/wg-setup.sh peer ROUTER_PUBLIC_KEY"
fi

hr "2. VPS WireGuard state"
wg show "$IF" 2>&1 || true
port=$(wg show "$IF" listen-port 2>/dev/null)
vps_key=$(wg show "$IF" public-key 2>/dev/null)
peer_key=$(wg show "$IF" peers 2>/dev/null | head -1)
if [ -s "$PUB" ] && [ -n "$vps_key" ] && [ "$vps_key" != "$(cat "$PUB")" ]; then
  echo "PROBLEM: the running key differs from $PUB. Restart: systemctl restart wg-quick@$IF"
fi

hr "3. Listening and host firewall"
ss -Hlun | grep -q ":${port:-51820} " && echo "UDP ${port:-51820} is listening." \
  || echo "PROBLEM: nothing listens on UDP ${port:-51820}."
ufw_blocks=0
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  if ufw status | grep -q "${port:-51820}"; then echo "ufw allows ${port:-51820}."
  else ufw_blocks=1; echo "PROBLEM: ufw is active without a ${port:-51820} rule. Fix: ufw allow ${port:-51820}/udp"; fi
fi
if nft list ruleset 2>/dev/null | grep -Eq 'hook input .*policy drop'; then
  echo "NOTE: an nftables input chain drops by default; make sure UDP 51820 is accepted:"
  nft list ruleset | grep -n "51820" || echo "  (no rule mentioning 51820 found)"
fi

hr "4. Addresses"
ip=$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || echo "?")
echo "VPS public IPv4:       $ip"
if [ -f "$RSC" ]; then
  rsc_ip=$(grep -o 'endpoint-address=[0-9.]*' "$RSC" | head -1 | cut -d= -f2)
  echo "Router was told to use: $rsc_ip"
  [ "$rsc_ip" = "$ip" ] || echo "PROBLEM: those differ. Rerun: bash scripts/wg-setup.sh keys"
fi

hr "5. Listening for the router's packets for 30 seconds"
command -v tcpdump >/dev/null || apt-get install -y -qq tcpdump >/dev/null 2>&1
if command -v tcpdump >/dev/null; then
  cap=$(timeout 30 tcpdump -nni any -c 8 "udp port ${port:-51820}" 2>/dev/null)
  echo "${cap:-  (no packets)}"
  inbound=$(echo "$cap" | grep -c "> [0-9.]*\.${port:-51820}:" || true)
  outbound=$(echo "$cap" | grep -c "\.${port:-51820} > " || true)
  echo
  if [ "$inbound" -eq 0 ]; then
    cat <<EOF
RESULT: nothing from home reached the VPS.
  -> Vultr firewall: allow UDP ${port:-51820} inbound AND make sure the firewall group is
     linked to this instance (Products > Network > Firewall > Linked Instances).
  -> Router peer must have endpoint-address=$ip endpoint-port=${port:-51820}
     and must NOT have responder=yes (a responder never dials out).
  -> On the router, '/interface wireguard peers print detail' should show tx increasing.
EOF
  elif [ "$outbound" -eq 0 ] && [ "$ufw_blocks" = 1 ]; then
    cat <<EOF
RESULT: the router's packets ARRIVE, but the VPS firewall (ufw) drops them before
WireGuard sees them. (tcpdump captures packets before the firewall.)
  -> Fix: ufw allow ${port:-51820}/udp comment 'AirStation WireGuard'
     then wait 30 s and run: bash scripts/wg-setup.sh status
EOF
  elif [ "$outbound" -eq 0 ]; then
    cat <<EOF
RESULT: the router's packets ARRIVE, but the VPS does not answer.
  -> WireGuard silently ignores a peer whose key it does not know. The VPS expects the
     router key: ${peer_key:-none}
     Compare with the router: /interface wireguard print detail where name=wg-air
     If different: bash scripts/wg-setup.sh peer 'ROUTER_PUBLIC_KEY'
  -> Or the router's peer holds the wrong VPS key. It must be: $vps_key
EOF
  else
    cat <<EOF
RESULT: packets flow both ways.
  If there is still no handshake, the router's peer public-key is not the VPS key.
  It must be: $vps_key   (re-add the peer from $RSC)
  If there IS a recent handshake above, test: ping -c3 10.77.88.2
EOF
  fi
else
  echo "tcpdump unavailable; install it with: apt install -y tcpdump"
fi

hr "6. Check on the router (Winbox > New Terminal)"
cat <<EOF
/interface wireguard print detail where name=wg-air
    public-key must equal what the VPS expects: ${peer_key:-'(no peer set yet)'}
/interface wireguard peers print detail where interface=wg-air
    public-key       = $vps_key
    endpoint-address = $ip   endpoint-port = ${port:-51820}
    allowed-address  = 10.77.88.1/32   persistent-keepalive = 25s
    responder        = no (or absent)  - and NO private-key set on this peer
    last-handshake should be under 2m; tx should grow every 25 s
EOF
