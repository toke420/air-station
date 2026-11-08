# Licence, trial and activation

## For station owners

### The trial
A new install runs as a full **30-day trial**. Studio > Licence shows how many days are
left, and a reminder bar appears at the top in the last 7 days.

When the trial ends without a licence:
- the station **keeps broadcasting**: AutoDJ, live takeover, the website, the player,
  requests and statistics all carry on;
- Studio keeps the **Dashboard** (now playing, skip, allow live takeover, health) and
  the **Licence** page; the other pages ask for a licence key.

### Activating
1. Buy a licence. Your key arrives by e-mail and looks like
   `AIRS-7KQ2M-9XD4P-H3T8W-RC5NB`.
2. Open **Studio > Licence**, paste the key, and press **Activate**.

Or on the server: `bash scripts/airctl licence activate AIRS-…`

Activation sends the licence server four things: the key, your station's domain
(`AIR_DOMAIN`), a random install id and the AirStation version. About once a week the
station checks in with the same details. If the licence server can't be reached, nothing
changes: an activated station never locks because of an outage on the seller's side.

### One station per licence
A licence covers one station (one domain) at a time.

- **Moving to a new server:** in Studio > Licence on the old server press
  **Deactivate on this server**, then activate the same key on the new one.
- **Old server already gone?** Activate on the new server. If the old one is still
  counted, the message says when it frees itself: a server that hasn't checked in for
  30 days is released automatically. Or contact support to release it straight away.
- **Changing the domain** (`AIR_DOMAIN`): activate the key again after the change.
- **Restoring a backup** (`airctl backup`) keeps the licence, because the database
  carries the install id. Run only one copy of a station at a time: a licence covers
  one station.

### No internet on the server?
Under **Studio > Licence > No internet on this server?** you'll find what to send the
seller: your key, the domain and the install id (both shown on that page, with Copy
buttons). You get back a certificate (`AIRCERT.…`) to paste there. It works without
ever contacting the licence server.

### Commands
```bash
bash scripts/airctl licence                      # status, domain, install id
bash scripts/airctl licence activate AIRS-…      # activate
bash scripts/airctl licence certificate AIRCERT.…
bash scripts/airctl licence check                # check in now
bash scripts/airctl licence deactivate           # before moving server
```

## For the seller

You run the **AirStation licence server** (a separate package, `airstation-licensing`,
which buyers never get). It issues keys, activates installs and signs certificates with
your private key. AirStation releases contain only the matching **public key**, so
nobody else can create keys or certificates they accept.

### Build a release
The repository is a developer build (no trial, no lock). To make the zip you sell:

```bash
python3 scripts/dev/release.py --server https://license.example.com \
    --purchase-url https://shop.example.com/airstation
```

It reads the public key from your licence server (or `--public-key`), writes it with
the server and shop addresses into a copy of the project, runs the tests, and builds
`air-station-v<version>.zip` without `.github/` and development leftovers.
Releases built with the same key accept the same licences, so buyers can update freely.

### Limits of any licence check
AirStation is delivered as readable source, so a determined person could edit the
check out. The licence check keeps honest people honest and makes sharing keys
visible (one active server per key). The real protection is the licence agreement in
`LICENSE`, which forbids removing the check or redistributing the software.

### Your own station
Issue yourself a licence and use an **offline certificate**
(`python3 -m airlicense offline …`). If the licence server runs on the same VPS as
your station, the station can't reach it through its own public address (the same
"hairpin" limit as the forum sign-on), and an offline certificate doesn't need to.
