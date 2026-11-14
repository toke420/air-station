# Changelog

## 2.8.0 (14 Nov 2026): studio desk (microphone, music volume, talk breaks)

- **Studio > Dashboard > Studio desk.** Talk on air from the browser, no Mixxx needed:
  - **Microphone:** *Connect microphone*, then the mic button puts your voice on top of
    whatever is playing. Level slider and input meter. The audio goes from the browser
    over a WebSocket (through Caddy) to a new Liquidsoap input, `/mic`.
  - **Music volume:** slider plus *Full / Voice-over (25%) / Silent* buttons for the
    programme under your voice. Changes are a short fade.
  - **Pause music after this track:** the current song plays to the end, then AutoDJ
    stays quiet for a talk break until you press *Resume music*. The site shows
    "Talk break" meanwhile. A DJ who connects during a talk break goes on air at once.
- The microphone input has no fixed password: a logged-in admin gets a one-time token
  per connection. Only `/admin` may ask the browser for the microphone.
- Music volume and pause go back to 100% / off when Liquidsoap restarts.
- New telnet commands: `air.volume`, `air.pause`, `air.mic_token`. `config/radio.liq`
  changed (run `airctl check-config`); `radio-safe.liq` has no studio desk.

## 2.7.0 (6 Nov 2026): search-friendly pages

- **Fixed page title.** The browser tab no longer changes to the playing song; it always
  shows Appearance > Texts > **Browser tab title** (e.g. `{station} | Independent Hip
  Hop & Rap Radio`). The song still shows on the page and the phone's lock screen.
- **The title, description and texts are in the HTML.** The API writes finished copies
  of the home and Live Video pages to `data/site/` (title, meta description, Open Graph
  and Twitter tags, canonical address, station name, headings and every page text).
  Caddy serves them and falls back to the plain pages in `web/` if they don't exist.
- New text: **Search description** (Appearance > Texts).
- `/api/site` includes `seo` (title, description), used by brand.js.

## 2.6.0 (28 Oct 2026): HLS stream for IPTV apps and playlists

- **`https://<your domain>/radio.m3u8`**: the main stream as HLS (AAC 128 kbps,
  6-second segments), the format IPTV apps, smart TVs, Kodi, TiviMate and M3U playlist
  collections expect. A new `hls` service (ffmpeg) listens to `/radio.mp3` and writes
  segments to a memory-only volume; Caddy serves them; the API serves the playlist.
- HLS listeners are counted: they appear in Studio > Listeners (stream "HLS / IPTV
  apps") with location and player, and in the public listener count. The packager
  itself is not counted.
- **Studio > Go live > Share your stream:** every stream address with Copy buttons,
  plus a ready-made `#EXTINF` entry (with logo, group and `radio="true"`) to send to
  playlist maintainers.
- `check.sh` checks the HLS playlist and a segment over HTTPS. The API answers HEAD
  requests (stream checkers use them).
- `AIR_HLS_BITRATE` (default `128k`) and `AIR_FFMPEG_IMAGE` in `.env`.

## 2.5.0 (28 Oct 2026): licensing for sale

- **Commercial licence** replaces the AGPL notice (`LICENSE`, a draft to have reviewed),
  with `THIRD-PARTY-NOTICES.md` for the open-source programs AirStation runs with, plus
  `SUPPORT.md`. `CONTRIBUTING.md` became `docs/DEVELOPMENT.md`.
- **30-day trial, then a licence key.** After the trial Studio keeps the Dashboard and
  the new **Licence** page; everything else asks for a key. Broadcasting, the website,
  AutoDJ and live takeover are never affected.
- **Online activation** in Studio > Licence (or `airctl licence activate KEY`): one
  station per licence. Deactivate to move server; a server that stops checking in
  frees its place after 30 days. Weekly check-ins pick up refunds and moves; a
  licence-server outage never unlicenses a station.
- **Offline certificates** for servers without internet access.
- Certificates are Ed25519-signed (built-in, standard library) and checked offline.
- `scripts/dev/release.py` builds sellable zips with your licence server's public key;
  the repository itself stays a developer build without trial or lock.
- The seller's **licence server** is a separate package (`airstation-licensing`): key
  issuing, activation API, admin API for a shop, revoke/restore/reset, backups.

## 2.4.1 (27 Oct 2026): map tiles without a key

- **Fix:** the Listeners map showed "API KEY REQUIRED" tiles. CARTO started requiring
  an API key for its basemaps in August 2026. The default is now **OpenStreetMap,
  darkened** to match Studio, which needs no key.
- **Site settings > Listener map:** choose OpenStreetMap (dark or standard colours),
  CARTO Dark Matter or Voyager with your free CARTO key, or any other tile server with
  its own credit. The key is checked, stored in the database, and never sent to the
  public site. Changes apply the next time the Listeners tab refreshes.
- Studio: elements marked hidden now stay hidden (`[hidden]` wins over layout styles),
  and the map no longer loads tiles while its tab is hidden.

## 2.4.0 (27 Oct 2026): listener statistics and audience map

- **Studio > Listeners**, a new tab:
  - **World map** of where listeners are (circle size = listening time, live listeners
    ringed and pulsing), with an optional layer for website visitors. Built in: no
    map library or third-party script; tiles were CARTO dark by default (see 2.4.1).
  - Tiles for listening now, unique listeners, listening hours, sessions, average and
    longest session, most at once, countries and website visitors.
  - Listeners-over-time chart (with a table view), countries, everyone listening now,
    session lengths, players and apps, streams, and the website's pages, referrers,
    devices and countries.
  - Periods: 24 hours, 7, 30 and 90 days, 12 months. CSV export of sessions.
- **How it is collected:** the API polls Icecast every 15 s for each stream's
  listeners. Caddy passes the listener's real address to Icecast inside the User-Agent,
  so internal connections (Liquidsoap, checks) are left out. Public pages send one
  cookieless page-view beacon (`/api/hit`); bots are skipped and floods are capped.
- **Location data:** offline lookups with a built-in MaxMind DB reader. Uses the free
  DB-IP City Lite database (CC BY 4.0) or your own GeoLite2-City. New
  `airctl geoip update`; the sync job refreshes it monthly.
- **Privacy:** no IP addresses are stored (keyed hash only), city-level locations,
  no cookies, 365-day retention (Site settings > Listener statistics, which can also
  switch it off).
- Database schema 3 (listener_sessions, listener_samples, page_hits), migrated
  automatically.
- New: `docs/STATISTICS.md`.

## 2.3.0 (24 Oct 2026): first public pre-release

- **Brand-neutral by default.** The bundled site no longer carries one station's
  artwork. Without uploads it shows the station name in large letters, a small radio
  icon, and abstract pictures generated by `scripts/dev/make_default_images.py`.
  Defaults are generic (station "AirStation Radio", time zone UTC, no Owncast address).
- **Studio > Appearance**:
  - **Images:** logo, homepage banner, banner background, feature, follow and video-chat
    pictures, default cover, site icon and link-preview image. Each comes with what it
    is used for, the best size, the accepted formats, a minimum size and a file-size
    limit. Uploads are checked on the server, and SVGs with scripts are refused.
  - **Page texts:** every heading and short text, with `{station}`, `{year}` and
    `*highlight*`.
  - **Colours:** accent and highlight colours.
  - **Custom CSS and JavaScript** on every public page, as in AzuraCast; never on the
    admin. `?nocustom` loads a page without them.
- **Theme packs:** `airctl branding export|import` moves a whole look (images, texts,
  colours, code, station settings) between installs. Importing never renames an
  existing station. Format in `docs/THEMING.md`.
- Stable image addresses `/api/brand/<name>`. A dynamic web-app manifest uses the
  station name and icon.
- `/branding/*` is served with a no-script Content-Security-Policy.
- GitHub-ready: README, THEMING guide, CONTRIBUTING, SECURITY, licence notice
  (AGPL-3.0-or-later), issue template, and CI (tests, script/JS syntax, compose
  validation). Docs use placeholder domains.
- The default cover-art upload now lives in Appearance (old endpoints still work).

## 2.2.8 (8 Oct 2026)

- **Forum sign-on can finish.** To complete an AirStation login, NodeBB calls
  `https://id.example.com` from inside Docker. That address is the VPS's own public
  IP, and ufw dropped the call, so it timed out. Caddy now answers the station's
  public addresses inside Docker too, so the call stays internal.
  `community.py status` tests this (`NodeBB -> sign-on`).
- Guide: troubleshooting for this, and for stale passkeys ("Unknown Resident Key").
- Guide: the OAuth2 step says clearly that every address points at id.example.com.

## 2.2.7 (7 Oct 2026)

- **Forum logins behind Caddy.** NodeBB 4.x no longer trusts the reverse proxy by
  default, so it saw every request as plain HTTP. Its secure login cookie was never
  set, and logins failed with "invalid csrf token". `community.py finish`,
  `fix-url` and `status` now also set or check `trust_proxy` in NodeBB's config.json.
- The guide shows how to reset the forum admin password (`--config` is required).

## 2.2.6 (7 Oct 2026)

- **Forum address check.** Through the SSH tunnel, NodeBB's installer pre-fills
  `http://localhost:4567` as the forum address. If you keep it, logins fail and the
  forum says "Unable to reach the server". Now:
  - `community.py finish` corrects the address;
  - `community.py status` warns about it;
  - new `community.py fix-url` corrects it on its own.
- The setup guide says the SSH tunnel command shows no prompt while it is open.

## 2.2.5 (7 Oct 2026)

- `community.py admin-password` prints the password itself. Before, it showed the
  "new random password for ..." line, but the password is on the line after it.

## 2.2.4 (7 Oct 2026)

- **Rauthy starts.** It stopped on "Invalid value for `webauthn.rp_origin` port":
  Rauthy needs the port written out, even the default one. The sign-on origin is
  now `https://id.example.com:443`.
- `community.py status` shows the real exit code of a container stuck restarting,
  and prints each log message once.

## 2.2.3 (7 Oct 2026)

- **Community set-up fixes** (found on the first real run):
  - `community.py init` now loads the new sign-on and forum sites into Caddy. Before,
    Caddy kept running without them, so id./forum. failed with a TLS "internal error".
  - New `community.py reload` loads them by hand.
  - Rauthy kept restarting (exit code 139). It now runs with Rauthy's small-instance
    memory profile and sized password hashing (64 MB, one at a time instead of
    128 MB × 2). The memory limit goes from 256 MB to 512 MB, and the extra
    capability lockdown is dropped (Rauthy's own setup doesn't use it).
  - `community.py status` explains problems:
    - for a crashed Rauthy, the reason and its last log lines;
    - "web installer waiting" for NodeBB before step 5;
    - for each unreachable domain, whether it's DNS, Caddy not having the site, or
      a certificate still being issued.

## 2.2.2 (7 Oct 2026)

- **Mixxx/OBS takeover fixed.** v2.1 changed the takeover to "after the current
  song" using Liquidsoap fallback *transitions*. In testing on the VPS, Mixxx
  connected but its audio never reached the air. The same design could also leave
  the station reporting "live" afterwards. The audio chain is back to the v2.0 setup that
  worked on this VPS (instant fallback, no transitions). The waiting is now a gate
  that opens when the AutoDJ song ends (or on **Go live now**).
  - "Live" or "AutoDJ" is again read from the sources themselves, so the player
    can't get stuck in live mode.
  - `config/radio-safe.liq` is the exact v2.0 audio config, as a fallback.
- **Player: progress bar and covers.** Both need the station to know which library
  song is playing. If a song arrives without its id, the API now matches it by
  artist and title, so the progress bar and cover still show.
- New `airctl live-check`: what Liquidsoap reports, live-related log lines and the
  settings Mixxx needs.

## 2.2.1 (6 Oct 2026)

- **Cover art repairs itself.** v2.1/2.2 trusted the database's record of whether a track
  had a cover, so after an update covers could disappear permanently. That happened
  when cover files went missing from `data/art`, or when a cover check failed once
  (ffmpeg busy, killed, missing, or a permission error).
  - Every sync now matches the database against the files in `data/art`. Missing
    covers are re-extracted and forgotten files are re-linked.
  - A temporary failure is retried on the next run instead of being recorded as
    "no cover". Tracks without a cover are re-checked every 14 days, and every such
    track is re-checked once after this update.
  - The website only links to covers that exist. Cover URLs carry a version (`?v=`),
    so browsers pick up re-extracted covers.
- **ID3 reader handles more taggers.** It now reads:
  - ID3v2.4 tags with non-standard frame sizes;
  - a second ID3 tag in front of the original;
  - compressed picture frames;
  - stray bytes before the image data.
- **Default cover always shows, uncropped.** Missing cover → station default → bundled AirStation
  cover. If the uploaded default file is missing, the site uses the bundled cover, and
  Admin > Site settings says so.
- New `airctl art-doctor` checks everything covers depend on: ffmpeg, folder ownership,
  missing files, the default cover, and what Caddy serves for the playing track.
- `init.py` repairs ownership of the files in `data/art` and `data/branding`.
- A file that changes on the drive gets its cover re-read.

## 2.2.0 (6 Oct 2026)

- **Community add-on** (optional; nothing changes until you run `community.py init`):
  - **Rauthy** single sign-on at `id.example.com`: open registration with email
    verification, passkeys and AirStation branding.
  - **NodeBB** forum at `forum.example.com`: PostgreSQL, built-in chat, login with AirStation
    accounts through `nodebb-plugin-sso-oauth2-multiple` (installed automatically).
  - `scripts/community.py` handles init, status, admin password, DB password,
    finish, categories and disable:
    - generates all secrets and renders the Rauthy config;
    - adds the Caddy sites (the forum stays private until the installer is done);
    - fills in the website's Community links.
  - Categories script: New Releases, General Discussion, Classifieds (with Records &
    Vinyl, Studio & DJ Gear, CDs & Tapes and Merch), plus a pinned rules topic.
  - AirStation theme for NodeBB at `/assets/community/` (CSS, header, icon).
  - The homepage's *Latest from the community* shows real forum topics.
  - Backups include the forum database, forum uploads and Rauthy data.
- Caddy now loads extra sites from `config/Caddyfile.d/`.
- New guide: `docs/COMMUNITY-SETUP.md`.

## 2.1.0 (6 Oct 2026)

- **Live takeover waits for the end of the song** (default). Mixxx/OBS go on air when
  the current AutoDJ track finishes. The admin shows "DJ connected: waiting" with a
  **Go live now** button, and the site shows *LIVE DJ UP NEXT*. `AIR_LIVE_SWITCH=immediate`
  restores instant takeover. Ending a set still returns to AutoDJ straight away.
- **Cover art fixed and broadened**:
  - Covers are now read from any embedded picture, including WebP/PNG ID3 covers
    that ffmpeg can't map (common in YouTube-to-MP3 conversions).
  - Tracks cached before this version are re-checked automatically.
  - Emergency-folder tracks get covers too.
  - New `airctl art-check "text"` explains a track's cover, and `airctl art-retry`
    re-checks everything.
- **Default cover art** can be changed in Admin > Site settings (square PNG/JPG/WebP,
  at least 300 px, 512×512 recommended, up to 2 MB). The new bundled default is the
  full AirStation brain mark, which is no longer cropped. Site icons were regenerated from it.
- **Duplicate finder** (Admin > Duplicates) matches same artist + title (ignoring
  "Official Video", "[HD]", "feat.", "Remastered", etc.) or identical files. It shows the
  drive paths and has one-click "Keep this" (other copies are disabled, not deleted).
- **Live Video page** (`/live/`) embeds the Owncast video and chat in the site theme,
  with a live indicator in the nav and an offline screen that points to the radio.
  The Owncast address is configurable in admin.
- **Community plan**: `docs/COMMUNITY.md` rewritten, recommending Rauthy (single
  sign-on) + NodeBB (forum + chat) + Jitsi via the same login.
- The emergency folder is now picked by the API (any sub-folder depth), and the
  dashboard warns when the folder is empty or unreadable.
- Database schema v2 (automatic migration). Static files are versioned so browsers
  pick up updates.

## 2.0.x (5 Oct 2026)

- 2.0.4: NAS mount check accepts the autofs + cifs stacking of the systemd automount.
- 2.0.3: `wg-setup.sh` opens UDP 51820 in ufw; `wg-doctor.sh` and `preflight.sh` detect it.
- 2.0.2: `wg-doctor.sh`; MikroTik notes on `responder=yes`, peer `private-key` and QR tests.
- 2.0.1: Works when root comes from plain `su`; refuses to run outside `/opt/air-station`.
- 2.0.0: First v2 release.
