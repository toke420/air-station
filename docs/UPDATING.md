# Updating an existing AirStation install

Updates only replace program files. Your `.env`, passwords (`secrets/`), database,
cache, cover art, emergency music and recordings live outside the zip and are kept.

Run every command as root (`su -` or `sudo -i`). Upload the release zip to `/root`
first. Version-specific notes follow the general recipe, newest first.

## General update recipe

```bash
cd /opt/air-station && bash scripts/airctl backup && cp config/radio.liq /root/radio.liq.prev
cd / && unzip -o -q /root/air-station-NEW.zip -d /opt && cd /opt/air-station
python3 scripts/init.py
docker compose pull --ignore-buildable && docker compose build
bash scripts/airctl check-config && docker compose up -d && sleep 20 && bash scripts/check.sh
```

If a release changes the systemd units, its notes say so and include the
`install -m 644 systemd/... /etc/systemd/system/` step.

If `check-config` prints an error, nothing is restarted. Contact support (SUPPORT.md) with
the full output.

## Upgrading to 2.7.0 (search-friendly title and pages)

The API and Caddy get new folders (`data/site`, `web` for the API), so run `init.py` and
`up -d`:

```bash
cd /opt/air-station && bash scripts/airctl backup
cd / && unzip -o -q /root/air-station-v2.7.0.zip -d /opt && cd /opt/air-station
python3 scripts/init.py
docker compose build api
docker compose up -d
sleep 20 && bash scripts/check.sh
```

Then in **Studio > Appearance > Page texts** set **Browser tab title**, e.g.
`{station} | Independent Hip Hop & Rap Radio`, and optionally **Search description**,
and save. Check what search engines see:

```bash
curl -s https://$(grep ^AIR_DOMAIN= .env | cut -d= -f2)/ | grep -m1 '<title>'
```

## Upgrading to 2.6.0 (HLS stream)

Adds a new `hls` service and a memory volume, and changes the Caddyfile, so start
everything with `up -d` (it only creates or recreates what changed):

```bash
cd /opt/air-station && bash scripts/airctl backup
cd / && unzip -o -q /root/air-station-v2.6.0.zip -d /opt && cd /opt/air-station
docker compose pull hls && docker compose build api
docker compose up -d
sleep 30 && bash scripts/check.sh          # look for: HLS /radio.m3u8   OK
```

Caddy restarts, so listeners reconnect once. Test in VLC: Media > Open Network Stream >
`https://<your domain>/radio.m3u8`. The addresses and a ready-made playlist entry are in
**Studio > Go live > Share your stream**.

## Upgrading to 2.5.0

2.5.0 adds the licence check (see `docs/LICENSING.md`). Only the API image and the
Studio pages change.

```bash
cd /opt/air-station && bash scripts/airctl backup
cd / && unzip -o -q /root/air-station-v2.5.0.zip -d /opt && cd /opt/air-station
docker compose build api
docker compose up -d --force-recreate api
bash scripts/check.sh
bash scripts/airctl licence           # shows trial / licence status
```

A release bought from the seller starts a 30-day trial on first start: enter your key
in **Studio > Licence**. (A developer build shows "Developer build" there and has no
trial.)

Coming from 2.3.x or older: follow "Upgrading to 2.4.0" first, then this.

## Upgrading to 2.4.1 (from 2.4.0)

Fixes the "API KEY REQUIRED" map. Only the API image changes.

```bash
cd /opt/air-station && bash scripts/airctl backup
cd / && unzip -o -q /root/air-station-v2.4.1.zip -d /opt && cd /opt/air-station
docker compose build api
docker compose up -d --force-recreate api
bash scripts/check.sh
```

Hard-refresh Studio (Ctrl+F5) and open **Listeners**: the map is OpenStreetMap,
darkened, with no key. To use CARTO instead, see **Site settings > Listener map**
and `docs/STATISTICS.md`. If you set `AIR_MAP_TILES` in `.env` for 2.4.0, remove it,
or it keeps overriding the setting.

Coming from 2.3.x or older: follow "Upgrading to 2.4.0" below with the 2.4.1 zip.

## Upgrading to 2.4.0

2.4.0 adds **Studio > Listeners**: a world map of where listeners and website visitors
are, how long people listen, listeners over time, countries, players and a CSV export.
Details and privacy notes: `docs/STATISTICS.md`. The Caddyfile and `compose.yaml`
change (Caddy passes the listener's address to Icecast; the API gets the Icecast admin
password and the location database), so recreate `api` and `caddy`.

```bash
cd /opt/air-station && bash scripts/airctl backup
cd / && unzip -o -q /root/air-station-v2.4.0.zip -d /opt && cd /opt/air-station
python3 scripts/init.py
docker compose build
bash scripts/airctl check-config        # must print: radio.liq OK
docker compose up -d --force-recreate api caddy
bash scripts/airctl geoip update        # free DB-IP city database for the map
bash scripts/check.sh
```

Restarting Caddy drops current listeners for a moment; their players reconnect. Then
open **Studio > Listeners** (hard-refresh with Ctrl+F5 first). Statistics start from
now: there is no history from before the update. Listeners connected before Caddy was
restarted will appear once they reconnect.

To check that it works: play the station on your phone over mobile data, wait 30
seconds, and refresh the Listeners tab. You should appear under **Listening now** with
your city.

## Upgrading to 2.3.0

2.3.0 makes the site brand-neutral. A station's look is now set in the new
**Appearance** tab in the admin:

- images, each with its recommended size;
- page texts;
- colours;
- custom CSS and JS.

A whole look can also be moved between installs as a theme pack with
`airctl branding import` and `airctl branding export`. See `docs/THEMING.md`.

**Existing stations: plan to re-apply your look.** Versions before 2.3.0 have no
`airctl branding export`, so the current look can't be exported before updating.
After updating, either re-upload your images in **Admin > Appearance**, or import a
theme pack (see below).

```bash
cd /opt/air-station && bash scripts/airctl backup
cd / && unzip -o -q /root/air-station-v2.3.0.zip -d /opt && cd /opt/air-station
python3 scripts/init.py
docker compose build
bash scripts/airctl check-config        # must print: radio.liq OK
docker compose up -d --force-recreate api caddy
bash scripts/check.sh
```

Then hard-refresh the site (Ctrl+F5, or pull down to refresh on a phone).

To apply a theme pack once the update is running:

```bash
bash scripts/airctl branding import /root/my-theme.zip
```

If custom CSS or JS breaks a page, add `?nocustom` to the address (for example
`https://radio.example.com/?nocustom`) to load it without them, then fix or remove the
code in Admin > Appearance.

## Upgrading to 2.2.5 – 2.2.8

Use the general recipe, then run `docker compose up -d caddy`. 2.2.8 lets containers
reach the station's public names inside Docker, which forum sign-on needs. If the
community add-on is on, also run `python3 scripts/community.py fix-url`, which sets
NodeBB's address and its `trust_proxy` option, then `python3 scripts/community.py status`.

## Upgrading to 2.2.4 (Rauthy start-up fix)

```bash
cd / && unzip -o -q /root/air-station-v2.2.4.zip -d /opt && cd /opt/air-station
python3 scripts/community.py render
docker compose up -d --force-recreate rauthy
sleep 30 && python3 scripts/community.py status
```

Then continue with docs/COMMUNITY-SETUP.md step 4 (`python3 scripts/community.py admin-password`).

## Upgrading to 2.2.3 (community set-up fixes)

Only the community add-on changes; the radio isn't restarted.

```bash
cd /opt/air-station && bash scripts/airctl backup
cd / && unzip -o -q /root/air-station-v2.2.3.zip -d /opt && cd /opt/air-station
python3 scripts/init.py
python3 scripts/community.py render
docker compose stop rauthy && rm -rf data/community/rauthy/data/*
docker compose up -d --force-recreate rauthy
python3 scripts/community.py reload
sleep 45 && python3 scripts/community.py status
```

The `rm` clears Rauthy's half-created database, so it starts fresh and prints the
first admin password again. Only do this while Rauthy has never run successfully.

## Upgrading to 2.2.2 (from 2.2.x or 2.1)

2.2.2 fixes the Mixxx/OBS takeover and brings back the progress bar and covers in the player.

```bash
cd /opt/air-station && bash scripts/airctl backup && cp config/radio.liq /root/radio.liq.prev
cd / && unzip -o -q /root/air-station-v2.2.2.zip -d /opt && cd /opt/air-station
python3 scripts/init.py
docker compose build
bash scripts/airctl check-config        # must print: radio.liq OK
docker compose up -d --force-recreate liquidsoap api caddy
sleep 20 && bash scripts/check.sh
```

`--force-recreate` matters: the audio config is a single mounted file, and a
running Liquidsoap keeps the old one until its container is recreated. Listeners
drop for a few seconds. Then hard-refresh the site (Ctrl+F5).

**If `check-config` prints an error**, don't restart anything. Use the v2.0 audio
config instead (instant takeover), and contact support (SUPPORT.md) with the error:

```bash
cp config/radio-safe.liq config/radio.liq
bash scripts/airctl check-config && docker compose up -d --force-recreate liquidsoap
```

**Test Mixxx:** connect, play a deck, and watch Admin > Dashboard. It shows "DJ
connected" and you go on air when the song ends (or press **Go live now**). If not,
run `bash scripts/airctl live-check` and contact support (SUPPORT.md) with the output.

## Upgrading to 2.2.1 (from 2.2 or 2.1)

2.2.1 fixes cover art and the default cover going missing again after an update.

```bash
cd /opt/air-station && bash scripts/airctl backup
cd / && unzip -o -q /root/air-station-v2.2.1.zip -d /opt && cd /opt/air-station
python3 scripts/init.py                 # also repairs ownership of data/art and data/branding
docker compose build
docker compose up -d && sleep 20 && bash scripts/check.sh
bash scripts/airctl art-retry           # restores every cover now (otherwise 200 per sync run)
bash scripts/airctl art-doctor          # every line should start with "ok"
```

Then hard-refresh the site (Ctrl+F5, or pull down to refresh on a phone).
`radio.liq` is unchanged, so `check-config` isn't needed. The update doesn't change
the community add-on.

If `art-doctor` still shows a line starting with `!!`, contact support (SUPPORT.md) with
its full output.

## Upgrading to 2.2 (from 2.1)

2.2 adds the optional **community add-on** (forum + single sign-on). Updating
changes nothing on the radio, and the add-on stays off until it is set up with
`docs/COMMUNITY-SETUP.md`.

```bash
cd /opt/air-station && bash scripts/airctl backup && cp config/radio.liq /root/radio.liq.prev
cd / && unzip -o -q /root/air-station-v2.2.zip -d /opt && cd /opt/air-station
python3 scripts/init.py                 # required: creates the new forum DB password
docker compose build
bash scripts/airctl check-config        # radio.liq OK (unchanged since 2.1)
docker compose up -d && sleep 20 && bash scripts/check.sh
```

Caddy restarts once to pick up the new `config/Caddyfile.d` folder. Then hard-refresh
the site. If `init.py` is skipped, `docker compose up` stops with an error about
`forum_db_password`; run `init.py` and try again.

## Upgrading to 2.1 (from any 2.0.x)

Run as root (`su -` or `sudo -i`).

**1. Back up, and keep a copy of the current working audio config**

```bash
cd /opt/air-station
bash scripts/airctl backup
cp config/radio.liq /root/radio.liq.v2.0
```

**2. Upload `air-station-v2.1.zip` to `/root`, then unpack over the install**

```bash
cd / && unzip -o -q /root/air-station-v2.1.zip -d /opt
cd /opt/air-station
python3 scripts/init.py
```

`init.py` keeps every password and setting. It creates the new `data/branding` folder
and adds the new `.env` settings (`AIR_LIVE_SWITCH`, `AIR_OWNCAST_URL`) with their
defaults; it prints which ones it added.

**3. Rebuild, check the audio config, restart**

```bash
docker compose pull --ignore-buildable
docker compose build
bash scripts/airctl check-config        # must print: radio.liq OK
docker compose up -d
sleep 20 && bash scripts/check.sh
```

The restart drops listeners for a few seconds. The database upgrades itself
(schema v2) the first time the API starts.

**4. Fetch cover art for tracks already cached**

```bash
bash scripts/airctl sync
```

Each sync run (every 5 minutes) re-checks up to 200 cached tracks for embedded covers,
and covers the emergency folder too. For a big cache, `bash scripts/airctl art-retry`
does all of them in one go.

**5. Hard-refresh the site** (Ctrl+F5, or pull down to refresh on a phone) so the
browser loads the new page files.

### If `check-config` reports an error

Put the previous audio config back. Everything else in 2.1 works with it; only the
new "wait for the end of the song" takeover is missing:

```bash
cp /root/radio.liq.v2.0 config/radio.liq
docker compose up -d
```

Then contact support (SUPPORT.md) with the full `check-config` output.

### What to check after updating to 2.1

| Feature | Where |
|---|---|
| Mixxx takeover waits for the song to end | Connect Mixxx. The dashboard shows **"DJ connected: goes on air when the current song ends"** and the site says **LIVE DJ UP NEXT**. **Go live now** cuts in straight away. |
| Cover art | Player and *Recently played* show covers once the sync has run. Diagnose with `bash scripts/airctl art-doctor`, or one track with `bash scripts/airctl art-check "song name"`. |
| Default cover | Admin > Site settings > **Default cover art**: upload, preview, restore |
| Duplicates | Admin > **Duplicates** |
| Live Video page | Site nav > **Live Video** (`/live/`). Owncast address and on/off are in Admin > Site settings. |

## Switching back to instant takeover

In `.env` set `AIR_LIVE_SWITCH=immediate`, then `docker compose up -d liquidsoap`.
