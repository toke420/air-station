#!/usr/bin/env bash
# WireGuard link between the VPS and home, VPS side.
#
#   bash scripts/wg-setup.sh keys                 # step 1: VPS key + MikroTik commands
#   bash scripts/wg-setup.sh peer HOME_PUBLIC_KEY [NAS_LAN_IP]
#                                                 # step 2: install and start the tunnel
#   bash scripts/wg-setup.sh status [NAS_IP]      # handshake, ping and SMB port check
#
# Defaults: tunnel 10.77.88.0/30, VPS .1, home .2, VPS UDP port 51820.
# Override with WG_NET=10.99.1 WG_PORT=51821 if these clash with your networks.
# Home always dials out to the VPS, so it works behind CGNAT and needs no
# port forwarding at home.
set -euo pipefail
export PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH"   # root via plain `su` lacks these

NET=${WG_NET:-10.77.88}
PORT=${WG_PORT:-51820}
VPS_ADDR="$NET.1"
HOME_ADDR="$NET.2"
DIR=/etc/wireguard
KEY="$DIR/air-private.key"
PUB="$DIR/air-public.key"
CONF="$DIR/wg-air.conf"
RSC=/root/air-mikrotik.rsc

[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
command -v wg >/dev/null || { echo "Install wireguard-tools first: apt install -y wireguard-tools"; exit 1; }

public_ip() {
  curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null \
    || ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}'
}

make_keys() {
  install -d -m 700 "$DIR"
  if [ ! -s "$KEY" ]; then
    (umask 077; wg genkey > "$KEY")
    wg pubkey < "$KEY" > "$PUB"
    echo "Created a new VPS WireGuard key."
  fi
  [ -s "$PUB" ] || wg pubkey < "$KEY" > "$PUB"
}

case "${1:-}" in
  keys)
    make_keys
    ip=$(public_ip)
    vps_pub=$(cat "$PUB")
    cat > "$RSC" <<EOF
# AirStation - MikroTik RouterOS 7 commands (generated $(date -u +%F))
# Paste into Winbox > New Terminal, one block at a time. Nothing here touches
# your existing WireGuard, LAN or Internet settings.
/interface wireguard add name=wg-air listen-port=13232 mtu=1420 comment="AirStation VPS link"
/ip address add address=$HOME_ADDR/30 interface=wg-air comment="AirStation VPS link"
/interface wireguard peers add interface=wg-air public-key="$vps_pub" endpoint-address=$ip endpoint-port=$PORT allowed-address=$VPS_ADDR/32 persistent-keepalive=25s comment="AirStation VPS"
:if ([:len [/ip firewall filter find where dynamic=no]] > 0) do={ /ip firewall filter add chain=input in-interface=wg-air src-address=$VPS_ADDR protocol=tcp dst-port=445 action=accept comment="AirStation: VPS reads SMB share" place-before=[:pick [/ip firewall filter find where dynamic=no] 0] } else={ /ip firewall filter add chain=input in-interface=wg-air src-address=$VPS_ADDR protocol=tcp dst-port=445 action=accept comment="AirStation: VPS reads SMB share" }
:if ([:len [/ip firewall filter find where dynamic=no]] > 0) do={ /ip firewall filter add chain=input in-interface=wg-air src-address=$VPS_ADDR protocol=icmp action=accept comment="AirStation: VPS ping" place-before=[:pick [/ip firewall filter find where dynamic=no] 0] }
# Show the router's public key -> copy the long "public-key" value:
/interface wireguard print detail where name=wg-air
EOF
    echo
    echo "VPS public IP:          $ip"
    echo "VPS WireGuard key:      $vps_pub"
    echo "VPS tunnel address:     $VPS_ADDR   (router will be $HOME_ADDR)"
    echo
    echo "MikroTik commands saved to $RSC:"
    echo "----------------------------------------------------------------------"
    cat "$RSC"
    echo "----------------------------------------------------------------------"
    echo "Then run:  bash scripts/wg-setup.sh peer <router public-key>"
    ;;

  peer)
    home_pub=${2:-}
    nas_ip=${3:-}
    [ -n "$home_pub" ] || { echo "Usage: $0 peer HOME_PUBLIC_KEY [NAS_LAN_IP]"; exit 1; }
    if ! [[ "$home_pub" =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$ ]]; then
      echo "That does not look like a WireGuard public key (44 characters ending in =)."; exit 1
    fi
    [ "$home_pub" != "$(cat "$PUB" 2>/dev/null)" ] || { echo "That is the VPS key; use the ROUTER's public key."; exit 1; }
    make_keys
    allowed="$HOME_ADDR/32"
    [ -z "$nas_ip" ] || allowed="$allowed, $nas_ip/32"
    if [ -e "$CONF" ]; then cp -a "$CONF" "$CONF.bak.$(date +%s)"; fi
    (umask 077; cat > "$CONF" <<EOF
# AirStation tunnel to home. Managed by scripts/wg-setup.sh.
[Interface]
Address = $VPS_ADDR/30
ListenPort = $PORT
MTU = 1420
# The private key stays in its own file.
PostUp = wg set %i private-key $KEY

[Peer]
# Home (MikroTik or Linux device). Only home addresses are routed here;
# the VPS keeps its normal Internet route.
PublicKey = $home_pub
AllowedIPs = $allowed
EOF
)
    systemctl enable wg-quick@wg-air >/dev/null 2>&1 || true
    if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
      ufw allow "$PORT/udp" comment 'AirStation WireGuard' >/dev/null && echo "ufw: allowed UDP $PORT."
    fi
    systemctl restart wg-quick@wg-air
    echo "Tunnel wg-air is up on the VPS. Waiting for home to connect..."
    for _ in $(seq 1 12); do
      hs=$(wg show wg-air latest-handshakes | awk '{print $2}')
      if [ -n "$hs" ] && [ "$hs" != 0 ]; then echo "Handshake OK."; break; fi
      sleep 5
    done
    wg show wg-air
    ping -c 3 -W 2 "$HOME_ADDR" && echo "Home router answers on $HOME_ADDR." \
      || echo "No ping reply yet: see docs/MIKROTIK.md troubleshooting."
    ;;

  status)
    wg show wg-air || exit 1
    hs=$(wg show wg-air latest-handshakes | awk '{print $2}')
    if [ -n "$hs" ] && [ "$hs" != 0 ]; then
      echo "Last handshake $(( $(date +%s) - hs )) s ago (should be under ~3 minutes)."
    else
      echo "No handshake yet."
    fi
    ping -c 2 -W 2 "$HOME_ADDR" >/dev/null && echo "Ping $HOME_ADDR OK" || echo "Ping $HOME_ADDR FAILED"
    target=${2:-$HOME_ADDR}
    if timeout 5 bash -c "</dev/tcp/$target/445" 2>/dev/null; then
      echo "SMB port 445 on $target reachable"
    else
      echo "SMB port 445 on $target NOT reachable (router firewall or SMB service)"
    fi
    ;;

  *)
    sed -n '2,12p' "$0"; exit 1 ;;
esac
