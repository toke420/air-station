"""Public pages with the station's own title, description and texts in the HTML.

Search engines and link previews read the HTML the server sends. The page templates in
web/ only carry generic defaults (brand.js fills in the real ones in the browser), so
this module writes finished copies to data/site/ whenever the look changes. Caddy
serves those copies and falls back to web/ if they don't exist yet.

    page title        Appearance > Texts > "Browser tab title"  ({station} is filled in)
    description       Appearance > Texts > "Search description", else "<station>: <tagline>"
    headings / texts  every element with data-t, data-station or data-tagline
"""
import html
import os
import re
import tempfile
import time

from . import branding, config, db

PAGES = {"index.html": "home", "live/index.html": "live"}
DEFAULT_DESCRIPTION = "Internet radio: live DJs, listener requests and non-stop music."


def _plain(text, station):
    return (str(text or "").replace("{station}", station).replace("{year}", time.strftime("%Y"))
            .replace("*", "").replace("\n", " ").strip())


def _rich(text, station):
    """Same output as brand.js render(): escaped, *word* bold, new lines as <br>."""
    out = html.escape(str(text or ""), quote=True)
    out = out.replace("{station}", html.escape(station)).replace("{year}", time.strftime("%Y"))
    out = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", out)
    return out.replace("\n", "<br>")


def seo(settings):
    """{"title", "live_title", "description", "station"} as shown to search engines."""
    station = str(settings.get("station_name") or "").strip() or "AirStation Radio"
    texts = branding.texts(settings)
    description = (settings.get("texts") or {}).get("page_description", "").strip()
    if description:
        description = _plain(description, station)
    elif str(settings.get("tagline") or "").strip():
        description = f"{station}: {settings['tagline'].strip()}"
    else:
        description = DEFAULT_DESCRIPTION
    return {"station": station, "title": _plain(texts["page_title"], station),
            "live_title": f"Live Video | {station}", "description": description[:300]}


def _set_meta(page, attr, name, content):
    """Replace the content of <meta attr="name">, or add the tag before </head>."""
    tag = f'<meta {attr}="{name}" content="{html.escape(content, quote=True)}">'
    pattern = re.compile(rf'<meta\s+{attr}="{re.escape(name)}"[^>]*>', re.I)
    if pattern.search(page):
        return pattern.sub(lambda _m: tag, page, count=1)
    return page.replace("</head>", f"  {tag}\n</head>", 1)


def _fill(page, attr, value_for):
    """Replace the inner HTML of every element carrying attr (data-t="key", data-station...)."""
    pattern = re.compile(rf'(<([a-z0-9]+)\b[^>]*\s{attr}(?:="([a-z0-9_]+)")?(?=[\s/>])[^>]*>)(.*?)(</\2>)',
                         re.I | re.S)

    def swap(m):
        value = value_for(m.group(3))
        return m.group(1) + value + m.group(5) if value is not None else m.group(0)
    return pattern.sub(swap, page)


def render(template, settings, kind="home", domain=""):
    info = seo(settings)
    station, texts = info["station"], branding.texts(settings)
    title = info["live_title"] if kind == "live" else info["title"]
    page = re.sub(r"<title>.*?</title>", lambda _m: f"<title>{html.escape(title)}</title>", template,
                  count=1, flags=re.S)
    page = _set_meta(page, "name", "description", info["description"])
    page = _set_meta(page, "property", "og:title", title)
    page = _set_meta(page, "property", "og:description", info["description"])
    page = _set_meta(page, "property", "og:site_name", station)
    page = _set_meta(page, "property", "og:type", "website")
    page = _set_meta(page, "name", "twitter:card", "summary_large_image")
    if domain:
        url = f"https://{domain}/" + ("live/" if kind == "live" else "")
        page = _set_meta(page, "property", "og:url", url)
        page = _set_meta(page, "property", "og:image", f"https://{domain}/api/brand/share")
        canonical = f'<link rel="canonical" href="{html.escape(url, quote=True)}">'
        page = re.sub(r'<link rel="canonical"[^>]*>\n?', "", page)
        page = page.replace("</head>", f"  {canonical}\n</head>", 1)
    page = _fill(page, "data-t", lambda key: _rich(texts[key], station) if key in texts else None)
    page = _fill(page, "data-station", lambda _k: html.escape(station))
    tagline = str(settings.get("tagline") or "").strip()
    if tagline:
        page = _fill(page, "data-tagline", lambda _k: html.escape(tagline))
    return page


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".page-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True


def write_all(conn, web_dir=None, site_dir=None, domain=None):
    """Write the finished pages. Returns the files that changed. Never raises for I/O."""
    web_dir = web_dir or config.WEB_DIR
    site_dir = site_dir or config.SITE_DIR
    domain = config.DOMAIN if domain is None else domain
    settings = db.all_settings(conn)
    changed = []
    for name, kind in PAGES.items():
        try:
            template = (web_dir / name).read_text(encoding="utf-8")
            if _write(site_dir / name, render(template, settings, kind, domain)):
                changed.append(name)
        except OSError:
            continue        # read-only or missing folder: Caddy keeps serving web/
    return changed
