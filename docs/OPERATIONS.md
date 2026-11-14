# Running AirStation

All commands run as root on the VPS from `/opt/air-station` (or anywhere, if you
linked `airctl` into `/usr/local/bin`).

## Everyday commands

| Command | What it does |
|---|---|
| `airctl status` | Containers, both streams decoded, HTTPS, now playing, NAS sync, disk |
| `airctl logs liquidsoap` | Follow a service's log (`api`, `icecast`, `caddy`, `mediamtx`) |
| `airctl skip` | Skip the current AutoDJ track |
| `airctl live off` / `on` | Block or allow live takeover (off = kick the DJ back to AutoDJ) |
| `airctl record off\|live\|all` | Recording mode |
| `airctl queue` | Upcoming AutoDJ tracks and whether they're cached yet |
| `airctl sync` / `airctl sync --index` | Run the NAS job now / force a full rescan |
| `airctl art-check "song"` | Explain why a track does or doesn't show cover art |
| `airctl art-retry` | Re-check covers for every cached track without art |
| `airctl art-doctor` | Check everything covers depend on: ffmpeg, the art folder, missing files, the default cover, and what the website serves |
| `airctl live-check` | Mixxx/OBS takeover: what the station reports, live-related log lines, the settings Mixxx needs |
| `airctl creds` | Admin password, Mixxx and OBS settings |
| `airctl geoip update` | Download or refresh the free location database for the listener map (`docs/STATISTICS.md`) |
| `airctl check-config` | Validate `config/radio.liq` after editing it |
| `airctl backup` | Back up settings, passwords, database and link credentials |
| `airctl update` | Pull and build new images, validate, restart, health check |

Most of these are also buttons in the admin dashboard.

## How the music gets chosen

1. **Requests** and admin **"Play next"** tracks go first, once they're cached on the VPS.
2. Then the **queue**, which the API keeps about two hours deep from the playlist of
   whichever show is scheduled at that time (or the default playlist).
3. Selection within a playlist is a **fair shuffle**: tracks that have gone longest
   without playing go first (never-played first), picked at random among them, with an
   artist gap (default 4 tracks) and a no-repeat window (default 24 h). Over a whole
   library, everything plays before anything repeats.
4. If a queued track isn't cached yet (home offline), it's skipped for now and a cached
   track from the same playlist plays instead.
5. If nothing is cached at all, the **emergency folder** plays. If that's empty too,
   the stream carries silence rather than disconnecting listeners.

## Going live

With `AIR_LIVE_SWITCH=track` (the default), connecting Mixxx or OBS puts you **next**:
the station finishes the current song, then switches to you. While you wait, the
admin dashboard shows *DJ connected* with a **Go live now** button, and the site shows
*LIVE DJ UP NEXT*. Liquidsoap keeps up to about 10 s of your audio buffered, so start
your first track shortly before the AutoDJ song ends. With `AIR_LIVE_SWITCH=immediate`
the station cuts to you as soon as you send audio. Apply a change with
`docker compose up -d liquidsoap`.

## Talking on air from the browser (studio desk)

Studio > Dashboard > **Studio desk** is a small mixing desk for voice-overs and talk
breaks, without Mixxx:

- **Connect microphone** asks the browser for the microphone and connects it to the
  station, muted. The mic button then switches you on and off air. Your voice is mixed
  on top of whatever is playing (AutoDJ or a live DJ).
- **Music volume** sets how loud the programme is under your voice. Put it back to
  *Full* when you're done; it stays where you leave it.
- **Pause music after this track** lets the current song finish and then keeps AutoDJ
  quiet until you press **Resume music**. With the mic off, listeners hear silence.
- Listeners hear you a few seconds late (longer on HLS). Wear headphones and don't
  monitor the stream while you talk.
- Closing or reloading the page disconnects the microphone. Volume and pause reset to
  100% / off when Liquidsoap restarts.

## Home outages

Nothing to do. The dashboard shows *NAS unreachable* and the sync job (every 5 min)
reports it without failing. The cache pool (`AIR_POOL_GB`, default 6 GB, roughly 70
hours at 192 kbps) keeps the rotation varied. When home is back, the next run catches
up by itself.

To check the tunnel: `bash scripts/wg-setup.sh status`.

## Recordings

Mode *Live sets only* (the default) writes each Mixxx/OBS set to
`data/recordings/YYYY-MM-DD/air-YYYYmmdd-HHMMSS.mp3` (192 kbps, about 86 MB/hour). Mode
*Everything* records the whole programme into one file until the mode changes.
Recording switches itself off if free disk drops below `AIR_DISK_RESERVE_GB` (10 GB).
Old recordings are never deleted automatically: download them over SFTP and delete
them when you're done.

## Changing settings

- `.env`: domain, cache sizes, NAS path, image versions. Apply with
  `docker compose up -d`. NAS settings apply on the next sync run.
- `config/radio.liq`: the audio chain. After editing, run `airctl check-config`, then
  `docker compose restart liquidsoap`. A restart drops listeners for a few seconds.
- `config/mediamtx.yml.template`: rerun `python3 scripts/init.py`, then
  `docker compose up -d mediamtx`.
- Site text, links, playlists and schedule: the admin page. No restart needed.

To **change a password**, delete its file in `secrets/`, run `python3 scripts/init.py`,
then `docker compose up -d --force-recreate`. The admin password is
`secrets/admin_ui_password`; changing it logs everyone out.

## Updates

```bash
airctl backup
airctl update
```

`update` refuses to restart if `radio.liq` fails the Liquidsoap check. For OS updates:
`apt update && apt full-upgrade`, then reboot when the kernel changes. Everything
comes back by itself (containers use `restart: unless-stopped`, and the sync timer and
WireGuard are enabled at boot).

**Rollback:** image versions are set in `.env` (`AIR_LIQUIDSOAP_IMAGE` and so on). Put
the previous tag back and run `docker compose up -d`. Keep the last good bundle zip, so
you can unpack it over the code (your `.env`, `secrets/` and `data/` are separate and
stay put).

## Backups and restore

`airctl backup` writes `/root/air-backups/air-YYYYmmdd-HHMMSS.tar.gz` (the last 14 are
kept). It contains passwords and keys, so download it and store it privately.

To restore on a new VPS: follow DEPLOY steps 1–2, then:

```bash
cd /opt/air-station
tar -xzf /root/air-YYYYmmdd-HHMMSS.tar.gz        # .env, secrets, config, data/generated, air.sqlite3
install -o 10001 -g 10001 -m 644 air.sqlite3 data/db/air.sqlite3 && rm air.sqlite3
python3 scripts/init.py                          # fixes ownership, keeps restored secrets
# WireGuard/SMB files were saved under etc/ and root/ inside the archive: copy them back
# to /etc/wireguard and /root, then continue from DEPLOY step 6.
```

Music, art and cache rebuild themselves from the NAS.

## Sharing ports 80/443 with another site

Caddy needs 80/443. If this VPS also has to serve another site (for example a backup
stream site at `backup.example.com`), let Caddy serve both: add a block to
`config/Caddyfile`, for example

```
backup.example.com {
	reverse_proxy host.docker.internal:8081
}
```

Move the other site's web server to port 8081 on the host, add
`extra_hosts: ["host.docker.internal:host-gateway"]` under the `caddy` service in
`compose.yaml`, and run `docker compose up -d caddy`.

## Troubleshooting

| Symptom | Look at |
|---|---|
| Site says *Station offline* | `airctl logs liquidsoap`: Liquidsoap is down or can't reach Icecast |
| Stream plays silence | Emergency folder empty and nothing cached: `airctl queue`, dashboard NAS status |
| `check-config` error | The line number points into `config/radio.liq`. If the cause isn't clear, contact support (SUPPORT.md) with the full output |
| HTTPS certificate not issued | `airctl logs caddy`: DNS not pointing here, or TCP 80 blocked in your VPS provider's firewall |
| Mixxx "can't connect" | VPS provider's firewall TCP 8005; host `radio.example.com`, port 8005, mount `/live`, login `source`; password from `airctl creds` |
| Mixxx connects but you're not on air | With `track` mode you go on air when the current song ends (or press **Go live now**). Check Admin > "Allow live takeover" and that Mixxx is sending audio (15 s of silence is ignored). `airctl live-check` shows what Liquidsoap sees. |
| OBS on air but no sound | OBS audio 48 kHz, an audio track selected; `airctl logs mediamtx` |
| Library count 0 | `journalctl -u air-sync -n 50`; `AIR_NAS_PATH` in `.env` must be the music folder inside `/mnt/air-nas` |
| Tracks "Unplayable" | ffprobe couldn't decode them; check the files on the NAS |
| Admin login loops | Use `https://` (the session cookie is secure-only). Password: `airctl creds` |

Logs are capped (3 × 10 MB per container). The NAS job logs to the journal:
`journalctl -u air-sync.service --since today`.

## Resource budget (4 GB VPS)

| Service | Typical RAM | Limit |
|---|---|---|
| Liquidsoap (2 MP3 encodes + recording) | 150–300 MB | 768 MB |
| Icecast | 10–30 MB | 256 MB |
| API | 30–60 MB | 256 MB |
| Caddy | 20–50 MB | 256 MB |
| MediaMTX (+ ffmpeg while OBS is live) | 20–120 MB | 512 MB |

CPU: about 10–20% of one vCPU for the two MP3 encodes, plus a little for ffmpeg while
OBS is live. That leaves room for the optional community add-on.

Bandwidth: each listener costs about 86 MB/hour at 192 kbps (43 MB/hour on data
saver). Check your VPS plan's transfer allowance against your expected audience.
