# Architecture

```
                         Internet listeners
                                │ https://radio.example.com
                                ▼
┌─────────────────────────── VPS (Debian 13, Docker) ────────────────────────────┐
│                                                                                │
│  Caddy :80/:443 ── /            → web/ (site + /admin)                         │
│   (auto HTTPS)  ── /api/*       → api:8080                                     │
│                 ── /art/*       → data/art (cover images)                      │
│                 ── /radio.mp3, /radio-low.mp3 → icecast:8000                   │
│                                                                                │
│  Icecast ◄──── Liquidsoap ─────────────► data/recordings (live sets)           │
│  2 mounts       ▲  priority: Mixxx > OBS > AutoDJ > emergency > silence        │
│                 │                                                              │
│   Mixxx ──:8005─┘ /live     MediaMTX :8890 SRT / :1935 RTMP ◄── OBS            │
│                   /obs  ◄── (ffmpeg pulls the audio, pushes it to /obs)        │
│                                                                                │
│  Liquidsoap ──"next track?"──► API ──► SQLite (library, playlists, schedule,   │
│  telnet :1234 ◄──state/skip── API          queue, history, settings)          │
│                                  ▲                                             │
│                                  │ same database                               │
│  air-sync (host systemd timer, every 5 min, user "air")                        │
│     index NAS tags → plan → copy queued + pool tracks → data/cache → evict     │
│         │ read-only CIFS automount /mnt/air-nas                                │
└─────────┼──────────────────────────────────────────────────────────────────────┘
          │ WireGuard wg-air (home dials out; VPS 10.77.88.1 ↔ router 10.77.88.2)
          ▼
   MikroTik router ── SMB share ── USB music drive
```

The diagram shows the reference setup: a MikroTik router's USB share as the music
library. Any SMB server reachable over WireGuard works. The tested VPS is 2 vCPU,
4 GB RAM, 80 GB disk, from any provider (e.g. Vultr, Hetzner, DigitalOcean).

## Design decisions

**The radio never touches the NAS.** Liquidsoap plays only files in `data/cache` and
`data/emergency`. The only thing that touches the network share is a separate host job
with a timeout. If the SMB mount hangs (and a stuck CIFS read can block a process in
the kernel), only that job waits.

**An API-driven AutoDJ instead of a random batch.** Copying a random batch of files
every few hours and letting Liquidsoap shuffle it gives no scheduling, no requests, no
fair rotation across the library, and repeats between batches. AirStation does what
AzuraCast does internally: Liquidsoap asks the API for each next track
(`request.dynamic`). The API decides from the schedule, the playlist rules, requests
and play history, and tells the sync job what to fetch ahead of time. This is where playlist management lives,
and it's plain Python you can change.

**Fair shuffle.** Least-recently-played first, random within that group, with an artist
gap and a no-repeat window. It's simple and explainable, and it plays the whole library
before repeating.

**Cache as the outage buffer.** The queue (~2 h ahead) is fetched first, then a pool of
least-recently-played music (default 6 GB) is kept topped up, with LRU eviction under a
hard cap (12 GB) and a free-disk reserve (10 GB). Tracks that are queued or played
within the last 3 hours are never evicted.

**Tag reading without downloading.** Indexing seeks through ID3v2 frame headers and
skips cover art, so a first scan of a big library over a home upload link reads a few
KB per file instead of whole files. Duration, cover art and tags for other formats are
filled in from the local copy once cached.

**Live takeover.** `fallback(track_sensitive=false, [live, obs, autodj])` switches
immediately. `blank.strip` drops a connected-but-silent source after 15 s. When a live
source starts, the interrupted AutoDJ track is skipped so the rotation resumes with a
fresh song. An admin toggle gates both live inputs, which effectively kicks a DJ.

**OBS bridge.** MediaMTX accepts SRT/RTMP from OBS. On `runOnReady` it runs ffmpeg,
which extracts the audio and pushes it into Liquidsoap's `/obs` harbor mount. When OBS
stops, ffmpeg exits and AutoDJ returns. Video is received but not republished (HLS or
WebRTC can be switched on in MediaMTX later if you want video on the site).

**Official images where they matter.** Liquidsoap uses the upstream
`savonet/liquidsoap:v2.4.5` image (every encoder included, current stable series) rather
than Debian's older 2.3.2 package. Icecast and the API share one small Debian 13 image.
Caddy does HTTPS with automatic certificates.

**Standard-library Python.** The API and sync job use only Python's standard library
and SQLite. There's nothing to `pip install`, and the same code runs in the container and
on the host.

**Search-friendly pages without a template engine.** The page templates in `web/` hold
generic defaults that `brand.js` replaces in the browser. For search engines and link
previews, the API also writes finished copies of the home and Live Video pages to
`data/site/` (`pages.py`), and Caddy serves those when they exist.

**HLS from the same audio.** An `hls` service (ffmpeg) listens to `/radio.mp3` like a
player and re-encodes it to AAC HLS segments in a memory-only volume. Liquidsoap and its
config are untouched, so HLS can never take the main stream down. Caddy serves the
segments; the playlist goes through the API so HLS listeners can be counted.

**Listener statistics without an analytics service.** Icecast 2.4 can't trust an
`X-Forwarded-For` header, so Caddy appends the listener's address to the User-Agent it
forwards, and the API polls Icecast's `listclients` every 15 s to build sessions. The
GeoIP reader (`airstation/geoip.py`) reads MaxMind DB files with the standard library,
and the Studio map (`web/admin/worldmap.js`) is a small built-in tile map, so no
library is downloaded at run time. Details: `docs/STATISTICS.md`.

## Security model

- Public ports: 80/443 (Caddy), 8005 (Mixxx, password), 8890/udp and 1935 (OBS,
  password), 51820/udp (WireGuard, key-based). Icecast (8000) and the API (8080) bind to
  127.0.0.1 on the host; Liquidsoap telnet (1234) isn't published at all.
- All containers run as UID 10001, drop all Linux capabilities (Caddy keeps only
  `NET_BIND_SERVICE`), use `no-new-privileges`, and Icecast/API have read-only root
  filesystems.
- Passwords are random, generated once by `init.py`, stored in `secrets/` (mode 0400)
  and passed as Docker secrets, never as environment variables.
- `/internal/next` (Liquidsoap → API) is unreachable through Caddy and also requires a
  shared token.
- Admin: one password, rate-limited login, an HMAC-signed `HttpOnly; Secure;
  SameSite=Strict` session cookie scoped to `/api/admin`, plus a required custom
  header on state-changing calls (CSRF guard).
- Listener requests are rate-limited per client (salted hash of the IP; raw IPs aren't
  stored).
- NAS: read-only SMB user, read-only mount (`nosuid,nodev,noexec`), readable only by
  the `air` user. The tunnel carries only VPS ↔ router traffic, and the router accepts
  only TCP 445/ICMP from the VPS tunnel address.

## Files

```
app/airstation/     config, db (SQLite schema), tags (ID3 reader), director (schedule +
                    selection), sync (NAS job), server (HTTP API), liquidsoap (telnet),
                    icecast_start, cli
config/             radio.liq, icecast.xml, Caddyfile, mediamtx.yml.template,
                    wireguard/ + mikrotik/ examples
docker/Dockerfile   Icecast + API image
web/                public site, admin (/admin), assets
scripts/            init, preflight, wg-setup, nas-setup, check, airctl, backup
systemd/            air-sync.service + .timer
tests/              unit + integration tests, fake Liquidsoap, preview server
data/  (created)    db, cache, art, emergency, recordings, generated
secrets/ (created)  generated passwords
```
