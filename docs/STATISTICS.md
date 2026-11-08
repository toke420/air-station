# Listener statistics and the audience map

**Studio > Listeners** shows who listens, from where, for how long, and how the
website is used. It is similar to AzuraCast's listener reports and map, and it runs
entirely on your server: no analytics service, no cookies, and no third-party scripts.

## What you see

| Part | What it shows |
|---|---|
| Period | 24 hours, 7 days, 30 days, 90 days or 12 months. Everything on the page follows it. |
| Tiles | Listening now, unique listeners, listening hours and sessions, average and longest session, most listeners at once (and when), countries, website visitors, and whether location data is on. |
| Map | One circle per city. Its size is the listening time from that place. Places with someone listening now have a white ring and a pulse. Tick **Website visits** to add the website's visitors in orange. Drag to move, scroll or use + / − to zoom, ⤢ to fit everything; tap a circle on a phone. |
| Listeners over time | The most listeners at once per hour (24 h), per 6 hours (7 days), per day (30 / 90 days) or per week (12 months). Hover a column for the average too; **Show as a table** lists the numbers. |
| Countries | Listeners, sessions and listening time per country. |
| Listening now | Each open connection: place, how long it has been connected, player, stream. |
| How long people listen | Sessions grouped by length (under 1 min … 3 hours +). |
| Players and apps | VLC, browsers by system, foobar2000, Winamp, iTunes, FFmpeg-based apps, and so on. |
| Streams | Main (192 kbps) against Data saver (96 kbps). |
| Website | Page views and visitors, pages, the sites that linked to you, phone or desktop, and countries. |

**Download sessions (CSV)** exports every listening session in the chosen period
(start, end, minutes, stream, player, country, region, city) for a spreadsheet.

The page refreshes itself every minute while it is open.

## How it works

- **Listening sessions.** Every 15 seconds the API asks Icecast which listeners are
  connected to each stream (`/admin/listclients`, with the Icecast admin password).
  A listener's session runs from the moment they connect until they leave. Connections
  shorter than 10 seconds are ignored in the reports, because those are usually players
  checking the stream rather than people listening.
- **The listener's address.** Icecast only sees Caddy, so Caddy appends the real
  address to the User-Agent it passes on (`[airstation-ip:…]`, see `config/Caddyfile`).
  The API reads it, looks up the location, and stores only a one-way hash of the address.
  Connections that don't come through Caddy (Liquidsoap, `check.sh`, a relay on the
  server) carry no address and are left out of every report.
- **HLS listeners** (`/radio.m3u8`, used by IPTV apps and TVs) don't connect to
  Icecast. Their players fetch the playlist every few seconds through the API; each
  address + player that keeps doing so is one listener, and stops being one 30 seconds
  after its last request. They show as the stream "HLS / IPTV apps".
- **Most at once.** Each minute the API keeps the highest number of listeners seen.
- **Website visits.** Each public page sends one small, cookieless request to `/api/hit`
  when it opens. Bots and scripts are skipped. The same visitor opening the same page
  again within 30 minutes counts once. Links from your own domain are not counted as
  "came from".
- **Locations** come from an offline database on your server (see below). Addresses
  that aren't on the public internet show as "Local network".

## Location database (GeoIP)

The map needs the free **DB-IP "IP to City Lite"** database. Download it once:

```bash
cd /opt/air-station && bash scripts/airctl geoip update
```

After that, the NAS sync job checks once a day and downloads the new monthly file when
yours is more than 32 days old. The file is just over 100 MB and lives in `data/geoip/`.

DB-IP's data is licensed under **CC BY 4.0**, which asks for attribution. Studio shows
it under the map; if you publish maps or numbers from it, credit
"IP geolocation by DB-IP (db-ip.com)".

Prefer MaxMind? Put your own `GeoLite2-City.mmdb` in `data/geoip/`. It is used ahead of
DB-IP whenever it is present (MaxMind's licence requires a free account to download it).

Without any database, everything except the map and the country/city columns still
works.

## Privacy

- No IP address is stored. Each visitor is stored as an HMAC hash keyed with your
  station's own secret (`secrets/session_secret`), so it can't be reversed by looking up
  a list of addresses, and it means nothing on another install.
- Locations are kept at city level and rounded to two decimals (about 1 km).
- No cookies and no tracking across sites. The admin itself is never counted.
- Statistics are deleted after **365 days** by default. Change this, or switch
  statistics off completely, in **Site settings > Listener statistics**. Switching off
  stops collecting; existing data stays until it ages out.

Depending on where your listeners live, you may still need to mention the
statistics in a privacy notice. You can use the "About" text or a custom page for that.

## Map tiles

Choose the map's background in **Studio > Site settings > Listener map**. Only your
browser loads it, when you open Studio > Listeners; your listeners never do.

| Style | Key | Notes |
|---|---|---|
| **OpenStreetMap, dark** (default) | none | OpenStreetMap's standard tiles, darkened in the browser to match Studio. |
| OpenStreetMap, standard colours | none | The same tiles, as they come. |
| CARTO Dark Matter / Voyager | free CARTO key | Since August 2026 CARTO's tiles show "API KEY REQUIRED" without a key. Request one at [carto.com/basemaps/apikey](https://carto.com/basemaps/apikey): no account is needed, and it arrives by e-mail. It is free for non-commercial use up to 5 million tiles a month (1 million for commercial use). Paste it into the **CARTO key** field. In CARTO's dashboard you can restrict the key to your domain. |
| Other tile server | depends | Enter its address with `{z}`, `{x}` and `{y}` (and `{s}` for a/b/c sub-domains), plus the credit it asks you to show. |

The OpenStreetMap tiles are used under the
[OSMF tile usage policy](https://operations.osmfoundation.org/policies/tiles/). A Studio
page that one or two people open is well within it: the map only loads the tiles you are
looking at and always shows the OpenStreetMap credit.

The CARTO key is stored in the station database and only sent to Studio, never to the
public site. It does appear in the tile addresses your browser requests, so a domain
restriction is worth setting.

`AIR_MAP_TILES` and `AIR_MAP_ATTRIBUTION` in `.env` still work and override the Studio
setting (then run `docker compose up -d api`).

If the tiles can't be loaded, the circles are still drawn on a latitude/longitude grid.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Notice "No location database yet" | Run `bash scripts/airctl geoip update`. |
| Listeners are counted but every place is "Local network" | The stream isn't reaching Caddy directly from the internet. Common causes: listeners use `http://IP:8000/...` straight to Icecast (use the `https://` stream addresses), or Docker sees IPv6 visitors through its proxy as an internal address. Check with `docker compose logs caddy` that client addresses are public. |
| Nobody shows as listening, but people are | The API can't ask Icecast. Check `docker compose logs api` and that `secrets/admin_password` matches Icecast's admin password (both come from `init.py`). |
| The map says "API KEY REQUIRED" | You are using CARTO without a key (2.4.0's default). Update to 2.4.1, which uses OpenStreetMap by default, or add a free CARTO key in Site settings > Listener map. |
| Numbers start from the update day | Statistics only exist from when 2.4.0 was installed; nothing older is recorded. |
| Website visits stay at zero | A browser extension may block `/api/hit`, or **Site settings > Listener statistics** is off. |
