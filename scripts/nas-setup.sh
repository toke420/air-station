#!/usr/bin/env bash
# Find a working SMB dialect for the home share and (optionally) install the
# read-only automount at /mnt/air-nas.
#
#   bash scripts/nas-setup.sh test    SERVER SHARE    # try SMB 3.1.1 / 3.0 / 2.1 / 2.0
#   bash scripts/nas-setup.sh install SERVER SHARE    # test, then add to /etc/fstab
#
# SERVER is the address reached through the tunnel: 10.77.88.2 when the
# MikroTik itself runs WireGuard, or the router's LAN IP when a home Linux
# device runs it. Credentials live in /root/.air-smbcredentials (created here).
set -euo pipefail
export PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH"   # root via plain `su` lacks these

MODE=${1:-}; SERVER=${2:-}; SHARE=${3:-}
MNT=/mnt/air-nas
TEST=/mnt/air-nas-test
CRED=/root/.air-smbcredentials
OPTS_BASE="ro,credentials=$CRED,uid=10001,gid=10001,file_mode=0440,dir_mode=0550,iocharset=utf8,soft,echo_interval=15,actimeo=60,nosuid,nodev,noexec"

[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
if [ -z "$SERVER" ] || [ -z "$SHARE" ] || [[ "$MODE" != test && "$MODE" != install ]]; then
  sed -n '2,12p' "$0"; exit 1
fi
command -v mount.cifs >/dev/null || { echo "apt install -y cifs-utils"; exit 1; }

if [ ! -s "$CRED" ]; then
  echo "SMB account on the router (a read-only user is best):"
  read -r -p "  username: " smb_user
  read -r -s -p "  password: " smb_pass; echo
  (umask 077; printf 'username=%s\npassword=%s\n' "$smb_user" "$smb_pass" > "$CRED")
  echo "Saved to $CRED (root only)."
fi
chmod 600 "$CRED"

if ! timeout 5 bash -c "</dev/tcp/$SERVER/445" 2>/dev/null; then
  echo "Cannot reach $SERVER on TCP 445."
  echo "  - Is the tunnel up?  bash scripts/wg-setup.sh status"
  echo "  - Does the router firewall allow 445 from the VPS, and is SMB enabled on wg-air?"
  exit 1
fi

share_esc=${SHARE// /\\040}
mkdir -p "$TEST"
found=""
for vers in 3.1.1 3.0 2.1 2.0; do
  if mount -t cifs "//$SERVER/$SHARE" "$TEST" -o "$OPTS_BASE,vers=$vers" 2>/tmp/air-cifs-err; then
    found=$vers
    echo "SMB $vers works."
    echo "Top of the share:"
    ls -1 "$TEST" | head -30 | sed 's/^/    /'
    umount "$TEST"
    break
  else
    echo "SMB $vers: $(tail -1 /tmp/air-cifs-err)"
  fi
done
rmdir "$TEST" 2>/dev/null || true

if [ -z "$found" ]; then
  echo
  echo "No dialect worked. Check the share name (case matters), the user/password,"
  echo "and 'dmesg | tail -20'. See docs/MIKROTIK.md > SMB troubleshooting."
  exit 1
fi

[ "$MODE" = install ] || { echo; echo "Test only. Run again with 'install' to add the mount."; exit 0; }

line="//$SERVER/$share_esc $MNT cifs $OPTS_BASE,vers=$found,_netdev,nofail,x-systemd.automount,x-systemd.idle-timeout=10min,x-systemd.mount-timeout=30s 0 0"
cp -a /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d%H%M%S)"
# Replace any previous AirStation line, keep everything else untouched.
grep -v " $MNT cifs " /etc/fstab > /etc/fstab.air-new || true
printf '# AirStation home music share (read-only, mounted on demand)\n%s\n' "$line" >> /etc/fstab.air-new
mv /etc/fstab.air-new /etc/fstab
install -d -m 755 "$MNT"
systemctl daemon-reload
systemctl restart 'mnt-air\x2dnas.automount'
sleep 1
ls "$MNT" >/dev/null
findmnt -T "$MNT" -o TARGET,SOURCE,FSTYPE,OPTIONS | cat
echo
echo "Mounted. Folders you can use for AIR_NAS_PATH in .env:"
find "$MNT" -mindepth 1 -maxdepth 2 -type d 2>/dev/null | head -40 | sed 's/^/    /'
