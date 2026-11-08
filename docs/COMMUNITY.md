# Community: forum, accounts, chat and video (options and plan)

The goal: a **small, simple forum** (New Releases, General Discussion, Classifieds for
records, studio gear, CDs and merch) where anyone can post freely but moderators can
remove anything inappropriate. It should look like the main site, use **one account for
everything** (forum, chat, video), and be lightweight and secure.

The AirStation site already has the Community section. Its **Sign Up / Log In / Video chat**
buttons take their links from *Admin > Site settings > Community*, so whatever is
chosen plugs in without code changes.

---

## The short answer

| Piece | Recommendation | Why |
|---|---|---|
| **One account for everything (SSO)** | **Rauthy** | A small single sign-on server (OpenID Connect). Open self-registration, email verification, passkeys/magic links, per-app login branding. About 60 MB of RAM with its built-in database. Independently security-audited. |
| **Forum + chat** | **NodeBB** | Categories, moderation queue, post reporting, an admin "delete/purge" for posts, **built-in chat rooms and private messages**, image uploads for classifieds, a clean theme that's easy to restyle in AirStation colours, and OpenID Connect login through a plugin. About 300–500 MB of RAM with its database. |
| **Video** | **Jitsi on a separate server**, signing in with the same Rauthy account | Jitsi rooms open for logged-in members only (JWT sign-in). Jitsi's chat lives *inside* a meeting; the always-on community chat is NodeBB's. |
| **Live streams** | **Owncast** (optional, runs separately) | Linked from the *Live Video* page. Owncast keeps its own chat for viewers. |

The forum and sign-on programs are open source, and everything is self-hosted. Each piece gets its own subdomain behind
the same Caddy:

```
radio.example.com     radio + website (this package)
id.example.com         Rauthy: sign up / log in / password reset, one account
forum.example.com      NodeBB: forum + chat (logs in with Rauthy)
meet.example.com       Jitsi (separate server; logs in with Rauthy)
cast.example.com       Owncast (optional, separate)
```

## Forum options compared

| | **NodeBB** (recommended) | **Flarum** (lightest) | **Discourse** (most polished) |
|---|---|---|---|
| Licence | GPLv3 | MIT | GPLv2 |
| Stack | Node.js + PostgreSQL, MongoDB or Redis | PHP + MySQL/MariaDB | Ruby + PostgreSQL + Redis |
| RAM (small forum) | ~300–500 MB | ~150–300 MB | 2 GB+ (own server) |
| Built-in chat | **Yes** (rooms + DMs) | No (extensions only) | Yes |
| Moderation | Post queue, reports, delete/purge, bans | Flags, hide/delete, suspend | Excellent |
| Theming to match AirStation | Theme settings + custom CSS (Harmony theme) | Custom LESS/CSS in admin | Theme components |
| SSO with Rauthy | OIDC plugin | OAuth/OIDC extension | Built-in OIDC plugin |
| Status | Mature, actively released (v4.x) | 1.8 stable; **2.0 still in release candidates** | Mature |
| Fit here | Forum *and* chat in one, fits the VPS | Fine if forum-only is enough | Best on its own server |

**Why not the others:** phpBB and Simple Machines are light but dated and hard to
theme. Lemmy and Misago are more than a small community needs. Self-hosted Rocket.Chat and Zulip are
heavy chat servers, and NodeBB's built-in chat covers the "chat" need for a small
community.

## Single sign-on: how it would work

1. Visitors click **Sign Up** on radio.example.com, which opens Rauthy at
   `id.example.com` with AirStation branding. They register, verify their email, and are
   sent back.
2. NodeBB has a **"Log in with AirStation"** button (OIDC). The first login creates their
   forum profile automatically, with no second password.
3. Jitsi on its separate server checks the same login. A small token bridge issues the
   Jitsi room token after Rauthy login. The popular *jitsi-openid* adapter was archived
   in May 2025, so the plan is either a maintained fork or a small bridge in the
   AirStation API.
4. Later, the radio site itself can use the same login, for example for member-only
   requests or saving favourites.

Moderators work in NodeBB (delete, purge or move posts, ban users), and accounts can
be disabled centrally in Rauthy.

## Resource check (4 GB VPS)

| | RAM |
|---|---|
| Radio stack today | ~0.6–0.8 GB |
| Rauthy | ~0.06 GB |
| NodeBB + PostgreSQL | ~0.4–0.6 GB |
| **Total** | **~1.1–1.5 GB**, leaving plenty of headroom |

Jitsi belongs on a separate server: video bridging is CPU- and bandwidth-heavy and
shouldn't share a box with the stream.

## Things to sort out first

- **Email delivery** for verification and password resets: an SMTP provider such as
  Brevo, Mailgun, Postmark or Amazon SES on port 587. Many VPS providers
  (Vultr included) block outbound port 25 by default, so don't plan on sending mail
  straight from the VPS.
- **Forum rules / classifieds policy**: e.g. "no counterfeit goods, meet safely,
  sellers are responsible for their listings". NodeBB can pin it in each category.
- **DNS records** for `id.` and `forum.` pointing at the VPS.

## Status

**Decided and built (v2.2):** Rauthy at `id.example.com` + NodeBB at
`forum.example.com`. Setup: **[COMMUNITY-SETUP.md](COMMUNITY-SETUP.md)**. Jitsi with
the same sign-on is planned and not part of this release.
