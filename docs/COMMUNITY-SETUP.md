# Community add-on: forum + single sign-on

This adds two services to the VPS:

| | Address | What it is |
|---|---|---|
| **Rauthy** | `https://id.example.com` | AirStation accounts: sign up, email verification, password reset, passkeys. One account for everything. |
| **NodeBB** | `https://forum.example.com` | The forum: New Releases, General Discussion, Classifieds, plus chat rooms and private messages. Members log in with their AirStation account. |

The radio is untouched. Both new services run under a Docker Compose **profile**, so
they only start once you turn them on (step 2). Expect them to add about 0.5–0.7 GB
of RAM; NodeBB briefly uses more while it builds its theme the first time.

Allow about an hour. As in `docs/DEPLOY.md`, each step ends with a **Check**.

---

## 0. Before you start

1. **v2.2 or later installed.** To update an older install, follow `docs/UPDATING.md`.
2. **DNS:** add two A records pointing at the VPS (`203.0.113.10`):
   `id.example.com` and `forum.example.com`.
3. **An email-sending account.** Sign-ups get a verification email, so Rauthy needs
   an SMTP service. For example:
   - **Brevo**: free tier, 300 emails/day. SMTP server `smtp-relay.brevo.com`, port `587`.
   - Mailgun, Postmark and Amazon SES also work.

   In the provider, verify `example.com` as a sending domain (it gives you DNS
   records to add). Without that, the emails tend to land in spam. You'll need the
   SMTP **server, port, username, password** and a **from address** at
   `example.com`. (Many VPS providers, Vultr included, block normal email sending from
   the VPS itself, which is why a provider is needed.)

## 1. Update to v2.2

Skip this step if the station already runs v2.2 or later. Otherwise, as in
`docs/UPDATING.md`:

```bash
cd /opt/air-station && bash scripts/airctl backup && cp config/radio.liq /root/radio.liq.prev
cd / && unzip -o -q /root/air-station-v2.2.zip -d /opt && cd /opt/air-station
python3 scripts/init.py
docker compose build && bash scripts/airctl check-config && docker compose up -d
```

**Check:** `bash scripts/check.sh` passes. The radio behaves exactly as before.

## 2. Configure the add-on

```bash
cd /opt/air-station
python3 scripts/community.py init
```

It asks for:
- the two addresses (press Enter to accept `id.example.com` / `forum.example.com`);
- **your email**, which becomes the Rauthy admin login and gets alerts;
- the SMTP server, port, username, from-address and password. The password is stored
  in `secrets/smtp_password`, readable only by root and the container.

It then:
- generates every other password and key;
- writes the Rauthy config;
- turns on the community profile (`COMPOSE_PROFILES=community` in `.env`);
- adds the two sites to Caddy and reloads it (the forum shows "being set up" for now);
- checks that the DNS records point here.

**Check:** both domains show `OK` at the end. If one says `FIX`, wait for DNS and run
`init` again (it keeps everything you entered).

## 3. Start the services

```bash
docker compose pull rauthy nodebb postgres
docker compose up -d
sleep 60
python3 scripts/community.py status
```

**Check:**
- `Rauthy (inside): OK`
- `NodeBB (inside): web installer waiting`. That's expected until step 5.
- `Sign-on (public): 200`. Caddy gets the HTTPS certificate on first use, so give
  it a minute and rerun `status` if needed.
- The forum line says `503` with mode `setup placeholder`. That's expected at this
  point.

If something is wrong, `status` says what to do under the line. For example, it
shows Rauthy's last log lines if Rauthy keeps restarting, or tells you to run
`python3 scripts/community.py reload` if Caddy hasn't loaded the new sites.

## 4. Rauthy: your admin login and the forum's "client"

**4.1 First login.**

```bash
python3 scripts/community.py admin-password
```

It shows the one-time password from Rauthy's first start. Open
**https://id.example.com/auth/v1/admin** and log in with your email and that password.
Rauthy asks you to set a new password. Then add a **passkey** (Account > MFA) so the
admin account is properly protected.

**4.2 Create the forum client** (Admin > **Clients** > **Add New Client**):

| Field | Value |
|---|---|
| Client ID | `nodebb` |
| Name | `AirStation Forum` |
| Confidential | **On** (the forum keeps a client secret) |
| Redirect URI | `https://forum.example.com/auth/airstation/callback` |
| Post logout redirect URI | `https://forum.example.com` |
| Client URI | `https://forum.example.com` |

Save, then open the new client and set:
- **Flows:** `authorization_code` and `refresh_token`.
- **Scopes (allowed + default):** `openid`, `email`, `profile`.
- **PKCE challenges: untick both** (`S256` and `plain`). Rauthy turns PKCE on by
  default; the forum's login plugin uses the client secret instead, and with PKCE
  required its logins fail.
- **Secret:** generate one and copy it somewhere safe for step 6.

**4.3 Branding (optional, recommended).** Open the client's **Branding** tab and set
background `#07090d`, primary `#1f9bff`, accent `#ff2b2b`, text `#eef2f7`. Upload
`https://radio.example.com/assets/community/forum-icon.png` as the logo. Do the same
for the built-in **rauthy** client: that's the look of the **Sign Up** page.

**Check:** in a private browser window, open
**https://id.example.com/auth/v1/users/register** and register a test account with a
second email address. The verification email should arrive within a minute. If it
doesn't, see Troubleshooting.

## 5. Install NodeBB (private, through an SSH tunnel)

NodeBB's first-time installer must not be public, so it's only reachable from your PC
through SSH.

**5.1** Get the database details:

```bash
python3 scripts/community.py db-password
```

**5.2** On **your PC** (PowerShell, Terminal or similar), open a tunnel and leave it running:

```
ssh -N -L 4567:127.0.0.1:4567 root@203.0.113.10
```

After you enter your password (or straight away with an SSH key) the command shows
**nothing and gives no prompt**. That's normal: the tunnel is open while it sits there.
Ctrl+C closes it.

(In PuTTY: Connection > SSH > Tunnels, source port `4567`, destination `127.0.0.1:4567`, Add.)

**5.3** In your browser open **http://localhost:4567**. The NodeBB installer appears.

| Installer field | Value |
|---|---|
| Web Address (URL) | `https://forum.example.com` (the installer pre-fills `http://localhost:4567`; **replace it**) |
| Administrator username / email / password | your forum admin. Keep these: this local login always works, even if sign-on is down. |
| Database | **PostgreSQL** |
| Host / Port | `postgres` / `5432` |
| Username / Database | `nodebb` / `nodebb` |
| Password | from 5.1 |

Click **Install NodeBB**. It sets up the database and builds the site, which takes
3–6 minutes on the reference VPS (2 vCPU / 4 GB). When it offers to launch, the forum is installed. Close the
browser tab and stop the tunnel (Ctrl+C).

**5.4** Open the forum to the public:

```bash
python3 scripts/community.py finish
```

This switches Caddy to the real forum. It also fills the website's Community buttons
(Sign Up → AirStation registration, Log In → forum login, Forum link), but only where you
hadn't set them already.

**Check:** **https://forum.example.com** shows NodeBB, and you can log in with the
admin account from 5.3. If it shows *502* for more than a couple of minutes:
`docker compose logs --tail=50 nodebb`, or `docker compose restart nodebb`.

## 6. Connect the forum to AirStation sign-on

Logged in to the forum as admin, open the **Admin** panel (gear icon):

**6.1** **Extend > Plugins > Installed:** find **"OAuth2 Multiple Client SSO"**
(`nodebb-plugin-sso-oauth2-multiple`; it was installed for you) and click
**Activate**. Then **Rebuild & Restart** (top right of the admin panel). Wait about a
minute.

**6.2** **Plugins > OAuth2 Multiple Client SSO > add a strategy:**

| Field | Value |
|---|---|
| Name | `airstation` (lowercase; it's part of the callback address) |
| Client ID | `nodebb` |
| Client Secret | the secret from 4.2 |
| Authorization / Token / User info URLs | see below |
| Scope | `openid email profile` |
| User ID property | `sub` |

For the three URLs, open
**https://id.example.com/auth/v1/.well-known/openid-configuration** in your browser and
copy:
- `authorization_endpoint` → Authorization URL
- `token_endpoint` → Token URL
- `userinfo_endpoint` → User info URL

(They should be `https://id.example.com/auth/v1/oidc/authorize`, `/oidc/token` and
`/oidc/userinfo`.)

Everything in this strategy points at the **sign-on server, id.example.com**; never
at the forum or radio address. If the plugin also shows one of these optional fields:
- *Discovery / OpenID configuration URL*:
  `https://id.example.com/auth/v1/.well-known/openid-configuration`
- *Domain / Issuer*: `https://id.example.com/auth/v1/`

If it shows neither, skip them. The forum's own address only appears in the
**callback URL** the plugin displays (it ends in `/callback`). That callback must be
entered exactly as the Redirect URI of the `nodebb` client in Rauthy (step 4.2).

Save. **If the plugin shows a callback URL, make sure it matches the Redirect URI in
the Rauthy client exactly.** Then **Rebuild & Restart** again.

**Check:** in a private window, open the forum, click **Login**, choose the AirStation
button, sign in with your test account from step 4, and you land back on the forum,
logged in. Your forum profile was created automatically.

**6.3 Make AirStation sign-on the way in (recommended):**
- **Admin > Manage > Privileges > Global:** for **registered-users**, untick
  **Local login**. Members then use their AirStation account only. Your admin account keeps
  local login.
- **Admin > Settings > User > Registration Type:** try **No registration** (sign-ups
  happen at Rauthy instead). Then test once more with a *new* AirStation account. If the
  forum refuses to create it, set it back to **Normal**.

## 7. AirStation look

In the forum admin:

1. **Appearance > Skins:** pick **Darkly** (or **Cyborg**) and save.
2. **Appearance > Custom Content:**
   - **Custom CSS/LESS:** open
     **https://radio.example.com/assets/community/nodebb-theme.css**, copy everything,
     paste, tick **Enable**, save.
   - **Custom Header:** paste the contents of
     **https://radio.example.com/assets/community/nodebb-header.txt**, tick
     **Enable**, save.
3. **Settings > General:**
   - Site title `AirStation Community`.
   - Site logo URL `https://radio.example.com/assets/img/logo-small.png`, with the logo
     link pointing to `https://radio.example.com`.
   - Favicon and touch icon: upload `forum-icon.png` (download it from the
     `/assets/community/` address above).

**Check:** the forum is dark with AirStation blue links, red buttons and Oswald headings.

## 8. Categories, sub-categories and the Classifieds rules

Create a one-off API token: **Admin > Settings > API Access > Create token**. Set the
user ID to **1** (your admin) and copy the token. Then on the VPS:

```bash
python3 scripts/community.py categories --token PASTE_TOKEN --tidy
```

This creates, or updates if they already exist:
- **New Releases**
- **General Discussion**
- **Classifieds**, with sub-categories **Records & Vinyl**, **Studio & DJ Gear**,
  **CDs & Tapes** and **Merch**

`--tidy` hides NodeBB's default *Blogs* and *Comments & Feedback* categories.
The script also posts the Classifieds rules as a pinned, locked topic. The text is in
`web/assets/community/classifieds-rules.md`; edit that file first if you want
different rules.

Afterwards, **delete the token** in API Access.

**Check:** `https://forum.example.com/categories` shows the three sections, and
Classifieds has its four sub-categories with the rules pinned on top.

## 9. Forum email (notifications, digests)

**Admin > Settings > Email:** use the same SMTP details as in step 2, with the
from-address on `example.com`. Then **Send test email**.

## 10. Moderating

| Task | Where |
|---|---|
| Remove a post | On the post: **⋮ > Delete** (hidden, can be restored) or **Purge** (gone for good) |
| Lock, move, pin a topic | **Topic tools** on the topic |
| Reported posts | **Admin > Manage > Flags** (members can flag posts) |
| Hold new members' posts for review | **Admin > Settings > Posts > Post Queue** (e.g. for users with fewer than 3 posts) |
| Ban or mute someone | Their profile **⋮ > Ban / Mute**. In Rauthy you can also disable the account for all services. |
| Add moderators | **Admin > Manage > Groups > Global Moderators**, or per-category **Privileges** |

## 11. On the website

- **Sign Up** → AirStation registration (Rauthy). **Log In** → forum login.
- *Latest from the community* on the homepage now shows the newest forum topics
  automatically (updated every minute). If the forum is unreachable, it falls back to
  your highlight lines from Site settings.
- Video chat isn't part of this release. The plan is Jitsi on a separate server, using
  the same AirStation accounts.

---

## Day-to-day

| | |
|---|---|
| Status | `python3 scripts/community.py status` |
| Logs | `docker compose logs -f rauthy` / `nodebb` / `postgres` |
| Backups | `bash scripts/airctl backup` now also saves the forum database, forum uploads and Rauthy data |
| Update the forum/sign-on | `docker compose pull rauthy nodebb && docker compose up -d`. Read the Rauthy release notes first: some releases change settings. Keep PostgreSQL on major version 17; moving to 18 needs a dump and restore. |
| Turn it all off | `python3 scripts/community.py disable` (keeps the data; `init` brings it back) |

## Troubleshooting

| Symptom | Fix |
|---|---|
| Sign-up email never arrives | `docker compose logs rauthy \| grep -i -E "smtp\|mail"`. Usually the from-address isn't a verified sender at your provider, or port 587 needs `AIR_SMTP_TLS=starttls` (465 needs `tls`). After fixing `.env` or `secrets/smtp_password`: `python3 scripts/community.py render && docker compose up -d --force-recreate rauthy`. |
| "invalid redirect_uri" after clicking the AirStation button | The Rauthy client's Redirect URI must match the plugin's callback exactly (including `https://` and no trailing slash). |
| Error about `code_challenge` / PKCE | Untick both PKCE challenges on the Rauthy `nodebb` client (step 4.2). |
| Logged in, but the forum asks for an email or username | Make sure the strategy's scope includes `email profile`, and that the account has its email verified in Rauthy. |
| Forum shows "being set up" after installing | Run `python3 scripts/community.py finish`. |
| AirStation sign-in succeeds but you land back on the sign-in page, or never get logged in to the forum | The forum can't reach id.example.com from inside Docker to finish the login. `python3 scripts/community.py status` shows `NodeBB -> sign-on`. Fix: `docker compose up -d caddy` (v2.2.8+), or `ufw allow 80/tcp && ufw allow 443/tcp`. |
| Passkey sign-in says "Unknown Resident Key" (in `docker compose logs rauthy`) | Your browser offered a passkey from an earlier, wiped Rauthy install. Delete the old id.example.com passkeys in your browser/password manager and keep only the newest. |
| Forgot the forum admin password | `docker compose exec nodebb ./nodebb --config=/opt/config/config.json user reset 1 --password 'NEW-PASSWORD'` (user 1 is the installer's admin). Without `--config`, NodeBB thinks it isn't installed and tries to start the installer. |
| Forum login fails ("invalid csrf token" in `docker compose logs nodebb`), or a pop-up says "Unable to reach the server at this time" | NodeBB's address or proxy setting is wrong. Run `python3 scripts/community.py fix-url` (it sets `url` and `trust_proxy` in NodeBB's config.json), then clear the site's cookies or use a private window. |
| Forum 502 | NodeBB is building or restarting: `docker compose logs -f nodebb`. A first build can take several minutes and needs swap on a 4 GB VPS. |
| `unreachable (... TLSV1_ALERT_INTERNAL_ERROR ...)` for id./forum. | Caddy has no certificate for that name. `status` says why: DNS not pointing here yet, Caddy hasn't loaded the site (`python3 scripts/community.py reload`), or Let's Encrypt is still issuing it (`docker compose logs caddy \| grep -i id.example.com`). |
| Rauthy `Restarting (139)` | It crashed at start-up; `status` prints its last log lines. If the cause isn't clear, contact support (SUPPORT.md) with them. |
| The well-known page shows `http://` addresses | Rauthy isn't treating itself as HTTPS behind Caddy. Contact support (SUPPORT.md) with the first lines of that page and the output of `docker compose logs --tail=30 rauthy`. |
| `status` shows Rauthy errors about untrusted proxies | The Docker network uses an address range outside `trusted_proxies`. Check with `docker network inspect air-station_default`, add the range to `config/community/rauthy.toml.template`, then `render` and restart. |
| Installer page won't load on localhost:4567 | The SSH tunnel must stay open. Check NodeBB is waiting for setup: `docker compose logs nodebb` should mention the installer. |
