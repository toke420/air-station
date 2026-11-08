# Theming: images, texts, colours, custom CSS and JS

Out of the box AirStation is brand-neutral: the station name is shown in large letters,
and the pictures are abstract defaults. Everything a visitor sees can be changed in
**Admin (Studio) > Appearance** without touching any files.

| What | Where in the admin | Notes |
|---|---|---|
| Station name, tagline, about text, banner genres | Site settings | Used across both public pages |
| Logo, banner, pictures, icon, default cover | Appearance > Images | Size and format rules below |
| Every other piece of text on the page | Appearance > Page texts | Empty box = default |
| Accent and highlight colours | Appearance > Colours | Generates CSS variables |
| Anything else | Appearance > Custom CSS / JavaScript | Public pages only |

## Images

Each image has a fixed spot on the site. Upload in the admin; the old file is replaced
straight away. Uploads are checked on the server: wrong format, too small, over 4000 px
or over the file-size limit are refused with a message. An image with a very different
shape is accepted with a warning, because it will be cropped or letterboxed.

| Image | Used for | Best size | Formats | Max |
|---|---|---|---|---|
| Header logo | Top bar and footer (shown 44 px tall). Without one: icon + station name | 600 × 140 (≈4:1) | PNG, WebP, SVG, JPG | 1 MB |
| Homepage banner | Large banner on the homepage and Live Video page (up to 720 px wide). Without one: the station name in large letters | 1400 × 330 (≈4:1) | PNG, WebP, SVG, JPG | 2 MB |
| Banner background | Faded decoration behind the banner, bottom-aligned | 1600 × 400 (4:1) | PNG, WebP, SVG, JPG | 2 MB |
| Feature picture | First card under the player; cropped to fill | 800 × 600 (4:3) | JPG, WebP, PNG | 2 MB |
| Follow card picture | Top of the "Follow" card; cropped to fill | 600 × 400 (3:2) | JPG, WebP, PNG | 2 MB |
| Video chat picture | Background of the Video Chat card; keep the top-left calm | 800 × 340 (≈2.4:1) | JPG, WebP, PNG | 2 MB |
| Default cover art | Player and track lists when a song has no cover; never cropped | 512 × 512 (square) | PNG, JPG, WebP | 2 MB |
| Site icon | Browser tab, bookmarks, phone home screen | 512 × 512 (square) | PNG | 1 MB |
| Link preview image | Shown when the site is shared on social media / chat apps | 1200 × 630 (1.91:1) | JPG, PNG | 2 MB |

Transparent PNG/WebP or SVG works best for the logo and banner, because they sit on the
dark page. Uploaded SVGs may not contain scripts or event handlers (they are refused),
and `/branding/*` is served with a no-script Content-Security-Policy as well.

Each image always has a stable address, `/api/brand/<name>` (`logo`, `hero`, `hero_bg`,
`feature`, `follow`, `video`, `cover`, `icon`, `share`), which redirects to the current
file or the bundled default. Use these addresses in your own CSS.

The bundled defaults in `web/assets/img` are drawn by `scripts/dev/make_default_images.py`
and can be released with the project.

## Page texts

Every heading and short text on the public pages has a box in **Appearance > Page
texts**. Leave a box empty to keep the default (shown in grey).

- `{station}` is replaced by the station name and `{year}` by the current year.
- `*word*` shows that word in the highlight colour (the default style for slogans).
- In the larger boxes, each line becomes a new line on the page.

Texts are plain text: HTML is escaped. For markup, use custom JavaScript.

### Title and description for search engines

- **Browser tab title (and search result title)** is the page title search engines
  and link previews show, for example `{station} | Independent Hip Hop & Rap Radio`.
  It stays the same while songs change (the song shows on the page and on the phone's
  lock screen). Keep it under about 60 characters.
- **Search description** is the snippet under the title in search results (about 150
  characters). Empty = "station name: tagline".

These, the station name and every page text are written into the HTML of the home and
Live Video pages on the server (`data/site/`, refreshed within a minute of a change and
immediately when you save), so search engines that don't run JavaScript see them too.

## Colours

The two colours in **Appearance > Colours** set these CSS variables (darker shades and
glows are derived automatically):

| Colour | Variables | Used for |
|---|---|---|
| Accent | `--blue`, `--blue-2`, `--blue-glow` | Links, buttons, icons, player, progress bar |
| Highlight | `--red`, `--red-2`, `--red-glow` | Live badges, Listen button, highlighted words, banner strap |

## Custom CSS

Custom CSS is served at `/api/custom.css` and loaded after the site stylesheet, so it
wins. Useful variables (defaults in `web/assets/css/site.css`):

| Variable | Default | Meaning |
|---|---|---|
| `--bg`, `--bg-2` | `#07090d`, `#0c1016` | Page background |
| `--panel`, `--panel-2` | `#10151d`, `#151b25` | Card backgrounds |
| `--line`, `--line-2` | `#222b38`, `#2d3848` | Borders |
| `--text`, `--muted`, `--dim` | `#eef2f7`, `#9aa6b6`, `#6c7889` | Text colours |
| `--radius` | `10px` | Card corner radius |
| `--maxw` | `1180px` | Page width |
| `--head` | Oswald | Headings, banner strap |
| `--body` | Inter | Body text |
| `--marker` | Permanent Marker | Side texts and slogans |

Useful class names: `.topbar`, `.brand-logo`, `.brand-text`, `.brand-tag`, `.hero`,
`.hero-name`, `.hero-img`, `.hero-band`, `.hero-tag`, `.hero-left`, `.hero-right`,
`.player`, `.feature`, `.follow`, `.slogan`, `.schedule`, `.music`, `.community`,
`.about`, `footer`. The `<body>` has `data-page="home"` or `data-page="live"`.

Example:

```css
@import url("https://fonts.googleapis.com/css2?family=Bebas+Neue&display=swap");
:root { --head: "Bebas Neue", Impact, sans-serif; --radius: 4px; }
.hero-name { letter-spacing: .08em; }
.hero-left, .hero-right { display: none; }   /* hide the side texts */
```

## Custom JavaScript

Custom JavaScript is served at `/api/custom.js` and added to the homepage and the Live
Video page after the site's own scripts (analytics, a chat widget, extra markup…). It
runs in every visitor's browser, so only add code you trust. `await AirBrand.ready`
gives you the same data as `/api/site` (station name, texts, image addresses).

Custom CSS and JS are **never** loaded in the admin, so a mistake can't lock you out.
If a public page breaks, open it with `?nocustom` (e.g. `https://radio.example.com/?nocustom`)
to see it without your code, then fix or clear it in the admin. Each box holds up to 100 KB.

## Theme packs

A theme pack is a folder (or .zip) with a `theme.json` and the image files. It lets you
back up a look, move it to another install, or share it.

```bash
bash scripts/airctl branding export                  # -> /root/airstation-theme-DATE.zip
bash scripts/airctl branding import /root/theme.zip  # apply a pack
```

`theme.json`:

```json
{
  "format": "airstation-theme/1",
  "settings": { "station_name": "My Station", "tagline": "…", "about": "…",
                "genres_banner": ["House", "Techno"], "rotation_name": "…",
                "rotation_description": "…", "social": {} },
  "texts":  { "slogan": "Turn it *up*.", "about_side": "Made by\n*music* lovers" },
  "theme":  { "accent": "#00c2a8", "highlight": "#ff3d7f" },
  "custom_css": "…CSS, or the name of a .css file in the pack…",
  "custom_js": "",
  "images": { "logo": "logo.png", "hero": "hero.png", "feature": "feature.jpg" }
}
```

- Image files are checked with the same rules as uploads. A file named after its slot
  (`logo.png`, `hero.svg`, `feature.jpg`…) is picked up even without an `images` entry.
- `settings` only fill in values this install hasn't set yet: importing a theme never
  renames an existing station. Texts, colours, custom code and images are applied.
- All keys are optional; leave out what the theme doesn't change.
