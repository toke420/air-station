# Deploying AirStation v2 on a VPS

This is the complete path from an empty `/opt/air-station` to a public station with
home-NAS music, Mixxx and OBS live takeover. Each step ends with a **Check**. Don't move
on until it passes; if one fails, the Troubleshooting notes in that step (or
`docs/OPERATIONS.md`) tell you where to look.

The radio is designed to be **on air before the home link exists** (step 6). The
WireGuard/NAS part (steps 7–9) adds your library; if it takes a few evenings to get
right, the station keeps playing its emergency music in the meantime.

Time needed: about 1 hour for steps 1–6, plus 30–60 minutes for the home link.

The tested reference is a VPS with 2 vCPU, 4 GB RAM and 80 GB disk running Debian 13
from any provider (e.g. Vultr, Hetzner, DigitalOcean). The reference setup for the home
library uses a MikroTik router's USB share; any SMB server reachable over WireGuard works.

---

## 0. Before you start

Have these ready:

| You need | Where to find it |
|---|---|
| VPS public IPv4 | your VPS provider's dashboard |
| SSH/SFTP access as root | your usual client (WinSCP, Termius, etc.) |
| DNS access for your domain (`example.com` here) | your DNS provider |
| An email address for Let's Encrypt | any address you read |
| Winbox or terminal access to the MikroTik (reference setup) | Winbox > New Terminal |
| The router's SMB share name and the music folder inside it | Winbox > IP > SMB > Shares |
| 20–50 MP3s for the emergency folder | your PC |

The home-link steps assume a MikroTik router running **RouterOS v7**. An **always-on
home Linux device** (e.g. a Raspberry Pi) is an optional fallback for the tunnel.

---

## 1. Prepare the VPS

Become root **with a login shell**: SSH in as root, or use `su -` (with the dash) or
`sudo -i`. A plain `su` keeps your user's PATH, which on Debian leaves out `/usr/sbin`,
so commands like `groupadd` and `mkswap` aren't found.

```bash
command -v groupadd || echo "Wrong shell: run 'su -' or 'sudo -i' and start again"
apt update && apt full-upgrade -y
apt install -y unzip python3 ffmpeg cifs-utils wireguard-tools curl dnsutils
docker --version && docker compose version
```

If `docker compose version` fails, install Docker's official packages:

```bash
curl -fsSL https://get.docker.com | sh
```

**Recommended: add 2 GB of swap.** A 4 GB shared VPS with no swap can have processes
killed during an image build or a memory spike:

```bash
[ -f /swapfile ] || { fallocate -l 2G /swapfile && chmod 600 /swapfile; }
swapon --show | grep -q /swapfile || { mkswap -f /swapfile && swapon /swapfile; }
grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
free -h
```

**Check:** `docker compose version` prints v2.x, and `free -h` shows about 2 GB of swap.

---

## 2. Upload and unpack

Upload `air-station-v2.zip` to `/root` with SFTP (replace any older copy of it).

Run this whole block **as written, starting with `cd /`**. It moves any earlier copy
(an older release, or a half-finished attempt) aside without deleting anything, removes
old containers that would hold ports 8000/8005, and unpacks a clean copy:

```bash
cd /
systemctl disable --now air-cache.timer 2>/dev/null || true
old=$(docker ps -aq --filter label=com.docker.compose.project=air-station)
[ -n "$old" ] && docker rm -f $old
[ -d /opt/air-station ] && mv /opt/air-station /opt/air-station.old-$(date +%Y%m%d-%H%M%S)
cd /opt && unzip -q /root/air-station-v2.zip && cd /opt/air-station
pwd && ls
```

**Check:** `pwd` prints exactly `/opt/air-station`, and you see
`README.md compose.yaml app config docs scripts systemd tests web`.

> **Why `cd /` first:** if your shell is *inside* a folder when it gets renamed, the
> shell follows the folder to its new name, even though the prompt still shows the old
> path. Commands then run against the wrong copy.

### If init.py says the copy is in the wrong place

`init.py` stops if it isn't running from `/opt/air-station`. Run the block above again
(it starts with `cd /`). Nothing is deleted: earlier copies stay in
`/opt/air-station.v1-*` or `/opt/air-station.old-*`. If you had uploaded emergency MP3s
into an earlier copy, bring them across after step 4:

```bash
cp -an /opt/air-station.v1-*/data/emergency/. /opt/air-station/data/emergency/ 2>/dev/null
cp -an /opt/air-station.old-*/data/emergency/. /opt/air-station/data/emergency/ 2>/dev/null
```

When everything works, you can delete the old copies with `rm -rf /opt/air-station.v1-* /opt/air-station.old-*`.

Optional but worthwhile: run the test suite (about 10 seconds, needs no Docker):

```bash
python3 -m unittest discover -s tests
```

It should end with `OK`.

---

## 3. DNS and the VPS firewall

**DNS:** create an **A record** `radio.example.com` → your VPS IPv4. Leave any other
records (for example an optional backup stream on another server) alone.

**Your VPS provider's firewall** (on Vultr: Products > Network > Firewall; attach the
group to this VPS). Allow inbound:

| Protocol | Port | Purpose |
|---|---|---|
| TCP | 22 | SSH (ideally limited to your home IP) |
| TCP | 80, 443 | Website + HTTPS certificate |
| UDP | 443 | HTTP/3 (optional) |
| TCP | 8005 | Mixxx live input |
| UDP | 8890 | OBS over SRT |
| TCP | 1935 | OBS over RTMP (optional) |
| UDP | 51820 | WireGuard from home |

Don't open 8000, 8080 or 445. The stack keeps those local.

**Check:** `dig +short radio.example.com` on the VPS prints the VPS IP. DNS can take
a few minutes.

---

## 4. Initialise and preflight

```bash
cd /opt/air-station
python3 scripts/init.py --domain radio.example.com --email YOUR_REAL_EMAIL
bash scripts/preflight.sh
```

`init.py` creates the `air` system user (UID 10001), the data folders, every password
(kept on re-runs), `.env`, and the MediaMTX config. **Write down the admin password it
prints.** You can show it again at any time with `bash scripts/airctl creds`.

**Check:** preflight ends with `0 failures`. Common failures:

- *port 80/443 already in use*: something else (maybe another site) is running a web
  server on this VPS. Stop it, or see "Sharing ports 80/443" in `docs/OPERATIONS.md`.
- *domain does not resolve to this VPS*: wait for DNS, or fix the A record.

Optional: make `airctl` available everywhere:

```bash
ln -sf /opt/air-station/scripts/airctl /usr/local/bin/airctl
```

---

## 5. Emergency music

Upload 20–50 tagged tracks (MP3 is safest) into `/opt/air-station/data/emergency/`
with SFTP. These play whenever the library cache has nothing to give, including on
the first start, so pick tracks you're happy to hear repeated. Keep the folder under
about 1 GB.

```bash
chmod -R a+rX /opt/air-station/data/emergency
ls /opt/air-station/data/emergency | head
```

---

## 6. Build, validate and go on air

```bash
cd /opt/air-station
docker compose pull --ignore-buildable
docker compose build
bash scripts/airctl check-config
```

`check-config` runs Liquidsoap's own checker on `config/radio.liq`. **It must print
`radio.liq OK`.** If it prints an error, stop and contact support (SUPPORT.md) with the full
output.

Start everything:

```bash
docker compose up -d
sleep 20
docker compose ps
bash scripts/check.sh
```

**Check:**

- `docker compose ps`: all five services `running`, with icecast and api `healthy`.
- `check.sh` decodes both `/radio.mp3` and `/radio-low.mp3` and reaches
  `https://radio.example.com`. The first HTTPS certificate can take a minute: rerun
  it if the HTTPS line fails right after starting.
- Open **https://radio.example.com**, press play and hear your emergency music.
- Open **https://radio.example.com/admin/** and log in. The dashboard shows
  `AUTODJ` and "NAS sync has not run yet". Both are expected at this point.

If there's no audio: `docker compose logs --tail=80 liquidsoap`.

The station is live. Everything after this adds your library and live shows.

---

## 7. Home link: WireGuard to the MikroTik

This step describes the reference setup (a MikroTik router sharing a USB disk). Full
detail and troubleshooting: **`docs/MIKROTIK.md`**. Short version:

```bash
cd /opt/air-station
bash scripts/wg-setup.sh keys
```

This prints (and saves to `/root/air-mikrotik.rsc`) a block of RouterOS commands with
your VPS key and IP already filled in. In **Winbox > New Terminal**, paste the lines
one at a time. The last command shows the router's WireGuard **public-key**. Copy it,
then on the VPS:

```bash
bash scripts/wg-setup.sh peer 'ROUTER_PUBLIC_KEY_HERE'
```

**Check:** it prints `Handshake OK` and `Home router answers on 10.77.88.2`.

> If the router route doesn't work, use an always-on home Linux device instead
> (Option B in `docs/MIKROTIK.md`). The rest of this guide is the same, except that the
> SMB server address becomes the router's LAN IP.

---

## 8. Mount the music share (read-only)

On the router, create a read-only SMB user for the VPS and allow SMB on `wg-air`
(`docs/MIKROTIK.md`, step 4). Then on the VPS, with your real share name:

```bash
bash scripts/nas-setup.sh test 10.77.88.2 'YourShareName'
```

It asks for the SMB username/password once (saved to `/root/.air-smbcredentials`,
root-only), then tries SMB 3.1.1, 3.0, 2.1 and 2.0 and reports which works and
what's at the top of the share. When it succeeds:

```bash
bash scripts/nas-setup.sh install 10.77.88.2 'YourShareName'
```

This backs up `/etc/fstab`, adds a single read-only, on-demand mount at
`/mnt/air-nas`, and lists the folders. Pick the folder that holds your music and
put it in `.env`:

```bash
nano /opt/air-station/.env      # AIR_NAS_PATH=/mnt/air-nas/DATA/Music (for example)
```

**Check:** `ls "$(grep ^AIR_NAS_PATH /opt/air-station/.env | cut -d= -f2)" | head` shows
your artist/album folders.

---

## 9. Start the library sync

```bash
cd /opt/air-station
install -m 644 systemd/air-sync.service systemd/air-sync.timer /etc/systemd/system/
systemctl daemon-reload
bash scripts/airctl sync --index
```

The first full scan reads only the tags from each file (it seeks past cover art), so
it's much lighter than copying. Expect roughly 1–3 minutes per 1,000 tracks over a
typical home upload. Then turn on the timer:

```bash
systemctl enable --now air-sync.timer
systemctl list-timers air-sync.timer
```

Every 5 minutes the job copies the upcoming queue, then tops up a pool of about 6 GB
(`AIR_POOL_GB`) of least-recently-played music, at most 600 MB per run. Over the first
few hours the pool fills, and from then on the station can ride out a home outage for
days.

**Check:** the admin dashboard shows **NAS reachable**, the Library count, and a growing
Cached count. Within about 10 minutes, *Coming up* shows tracks marked `ready`, and
*Recently played* shows `AUTODJ` tracks from your library with cover art.

---

## 10. Playlists and schedule (admin)

- **Playlists**: *All Music* is the default rotation. Add playlists by **NAS folder**
  (sub-folders included), by **genre tag**, or **hand-picked** (tick tracks in Library,
  then "Add to manual playlist").
- **Schedule**: create shows with days and times (station time: `AIR_TZ` in `.env`,
  default UTC). Give a show a playlist to change what AutoDJ plays during it. Tick
  "Live DJ show" for live sets so they show in red on the site.
- **Site settings**: tagline, about text, social links, backup stream URL, community
  links. The public page picks these up within a minute.

---

## 11. Go live from Mixxx

`bash scripts/airctl creds` (or the admin **Go live** tab) shows the details:

| Mixxx field | Value |
|---|---|
| Type | Icecast 2 |
| Host | `radio.example.com` |
| Port | `8005` |
| Mount | `/live` |
| Login | `source` |
| Password | from `airctl creds` |
| Encoding | MP3, 192 kbps, 44.1 kHz, stereo |

Enable Live Broadcasting and play something. Within about 5 seconds the stream
switches to you, and the site shows **LIVE**. Your Mixxx track metadata becomes the
now-playing title. Stop broadcasting (or go silent for 15 s) and AutoDJ returns with
a fresh track.

With the default **Record: Live sets only**, each set is saved to
`data/recordings/YYYY-MM-DD/`.

---

## 12. Go live from OBS

OBS > Settings > Stream > Service **Custom**. Use SRT if possible:

- **Server**: the `SRT server` line from `airctl creds` (starts with `srt://`)
- **Stream key**: leave empty

or RTMP: server `rtmp://radio.example.com`, key `obs?user=obs&pass=…`.

Set OBS audio to 48 kHz. Start Streaming; the radio switches to OBS's audio within a
few seconds. Mixxx has priority over OBS if both are connected.

---

## 13. Acceptance tests

| Test | How | Expected |
|---|---|---|
| Stream plays | site, press play | music, title and art update |
| Data saver | switch "96 kbps" on the player | keeps playing |
| Track changes | wait 3 songs | *Recently played* updates |
| Skip | admin > Skip track | next song within ~2 s |
| Request | site > Request a Track | plays after the current song if cached, otherwise within minutes |
| Mixxx takeover | enable broadcasting | on air in ≤ 5 s, LIVE badge |
| Mixxx silence | connected but silent 15 s | AutoDJ resumes |
| Kick DJ | admin > untick "Allow live takeover" | AutoDJ resumes |
| OBS takeover | Start Streaming | on air in ≤ 5 s |
| Home outage | `systemctl stop wg-quick@wg-air` for 30 min | music continues from cache; dashboard says NAS unreachable |
| Recovery | `systemctl start wg-quick@wg-air` | next sync run says NAS reachable |
| Icecast restart | `docker compose restart icecast` | listeners may need to press play again |
| Reboot | `reboot` (home link down or up) | everything returns by itself |
| Recording | after a Mixxx set | MP3 in `data/recordings/` plays |

`bash scripts/check.sh` any time for a quick health report.

---

## 14. Afterwards

- **Licence**: the install runs as a 30-day trial. Enter your key in
  **Studio > Licence** (or `bash scripts/airctl licence activate AIRS-…`). See
  `docs/LICENSING.md`.
- **Backups**: `bash scripts/airctl backup` writes `/root/air-backups/air-*.tar.gz`
  (settings, passwords, database, WireGuard + SMB credentials). Download one over SFTP
  now and after big changes.
- **Pin versions** once everything works: put the exact MediaMTX version in `.env`
  (`docker compose images mediamtx` shows it).
- **Backup stream** (optional): set the URL of a backup stream, e.g. on another server
  (`backup.example.com`), in Site settings so listeners have somewhere to go if this
  VPS is down.
- Community (forum + video chat): see `docs/COMMUNITY.md`.
