"""Station look: images, page texts, colours and custom CSS/JS.

Everything here is editable in Admin > Appearance and can be moved between
installs as a *theme pack* (a folder with theme.json + images), see
docs/THEMING.md and `airctl branding import|export`.
"""
import base64
import json
import re
import shutil
import time
from pathlib import Path

from . import config, db
from .tags import image_kind, image_size

# ------------------------------------------------------------------- images
# min = smallest accepted size; size = what to export (shown in admin).
# aspect = recommended width/height, used for a friendly warning only.
SLOTS = {
    "logo": {
        "title": "Header logo",
        "where": "Top bar and footer of every page. Without one, a small icon and the "
                 "station name are shown.",
        "size": "600 × 140 px (wide, about 4:1)", "aspect": 600 / 140,
        "min": (200, 40), "formats": ("png", "webp", "svg", "jpeg"), "max_mb": 1,
        "tip": "Transparent PNG, WebP or SVG. It sits on a dark bar and is shown 44 px tall.",
        "default": None,
    },
    "hero": {
        "title": "Homepage banner",
        "where": "The big banner at the top of the homepage and the Live Video page. "
                 "Without one, the station name is shown in large letters.",
        "size": "1400 × 330 px (wide, about 4:1)", "aspect": 1400 / 330,
        "min": (600, 120), "formats": ("png", "webp", "svg", "jpeg"), "max_mb": 2,
        "tip": "Transparent PNG/WebP or SVG of your wordmark or logo. Shown up to 720 px wide.",
        "default": None,
    },
    "hero_bg": {
        "title": "Banner background",
        "where": "Decoration behind the banner (bottom-aligned, faded).",
        "size": "1600 × 400 px (4:1)", "aspect": 4.0,
        "min": (800, 150), "formats": ("png", "webp", "svg", "jpeg"), "max_mb": 2,
        "tip": "A skyline, crowd or pattern. Keep the top half fairly empty.",
        "default": "/assets/img/skyline.svg",
    },
    "feature": {
        "title": "Feature picture",
        "where": "Left side of the first card under the player.",
        "size": "800 × 600 px (4:3)", "aspect": 4 / 3,
        "min": (400, 260), "formats": ("jpeg", "webp", "png"), "max_mb": 2,
        "tip": "Photo or artwork. It is cropped to fill, so keep the subject centred.",
        "default": "/assets/img/feature.jpg",
    },
    "follow": {
        "title": "Follow card picture",
        "where": "Top of the \"Follow\" card with the social links.",
        "size": "600 × 400 px (3:2)", "aspect": 1.5,
        "min": (300, 170), "formats": ("jpeg", "webp", "png"), "max_mb": 2,
        "tip": "Cropped to fill; keep the subject centred.",
        "default": "/assets/img/follow.jpg",
    },
    "video": {
        "title": "Video chat picture",
        "where": "Background of the Video Chat card in the community section.",
        "size": "800 × 340 px (about 2.4:1)", "aspect": 800 / 340,
        "min": (400, 170), "formats": ("jpeg", "webp", "png"), "max_mb": 2,
        "tip": "The Join button sits top-left, so keep that corner calm.",
        "default": "/assets/img/video.jpg",
    },
    "cover": {
        "title": "Default cover art",
        "where": "Shown in the player and track lists when a song has no cover of its own.",
        "size": "512 × 512 px (square)", "aspect": 1.0,
        "min": (300, 300), "formats": ("png", "jpeg", "webp"), "max_mb": 2, "square": True,
        "tip": "Square. Shown whole (never cropped) on a dark background.",
        "default": config.DEFAULT_ART_URL,
    },
    "icon": {
        "title": "Site icon",
        "where": "Browser tab, bookmarks and phone home screen.",
        "size": "512 × 512 px (square)", "aspect": 1.0,
        "min": (192, 192), "formats": ("png",), "max_mb": 1, "square": True,
        "tip": "Square PNG. Keep it bold and simple; it is shown as small as 16 px.",
        "default": "/assets/img/icon-512.png",
    },
    "share": {
        "title": "Link preview image",
        "where": "Shown when the site is shared on social media or in chat apps.",
        "size": "1200 × 630 px (1.91:1)", "aspect": 1200 / 630,
        "min": (600, 315), "formats": ("jpeg", "png"), "max_mb": 2,
        "tip": "Put your logo and station name in the middle; edges may be cropped.",
        "default": "/assets/img/share.jpg",
    },
}
MIME = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp", "svg": "image/svg+xml"}
EXT = {"png": "png", "jpeg": "jpg", "webp": "webp", "svg": "svg"}
MAX_PIXELS = 4000


class BrandError(ValueError):
    pass


def _prefix(slot):
    return "default-art" if slot == "cover" else f"brand-{slot.replace('_', '-')}"


def _is_svg(data):
    head = data[:2048].lstrip().lower()
    return (head.startswith(b"<svg") or head.startswith(b"<?xml")) and b"<svg" in data[:8192].lower()


def _check_svg(data):
    text = data.decode("utf-8", "replace").lower()
    # Defence in depth: Caddy also serves /branding with a no-script CSP.
    if "<script" in text or "javascript:" in text or re.search(r"\son[a-z]+\s*=", text) \
            or "<foreignobject" in text:
        raise BrandError("This SVG contains scripts or event handlers. Export a plain SVG.")


def decode_upload(raw):
    raw = str(raw or "")
    if raw.startswith("data:") and "," in raw[:200]:
        raw = raw.split(",", 1)[1]
    try:
        return base64.b64decode(raw, validate=True)
    except (ValueError, TypeError):
        raise BrandError("Could not read the image upload.")


def validate(slot, data):
    """Check an upload against the slot rules. Returns (kind, (w, h) or None, warning)."""
    spec = SLOTS.get(slot)
    if not spec:
        raise BrandError(f"Unknown image: {slot}")
    kind = "svg" if _is_svg(data) else image_kind(data)
    allowed = spec["formats"]
    names = "/".join("JPG" if f == "jpeg" else f.upper() for f in allowed)
    if kind not in allowed:
        raise BrandError(f"Use a {names} image for the {spec['title'].lower()}.")
    if len(data) > spec["max_mb"] * 1024 * 1024:
        raise BrandError(f"Image is larger than {spec['max_mb']} MB. Export it smaller "
                         f"({spec['size'].split(' (')[0]} is plenty).")
    if kind == "svg":
        _check_svg(data)
        return kind, None, ""
    size = image_size(data)
    if not size:
        raise BrandError("Could not read the image dimensions.")
    width, height = size
    min_w, min_h = spec["min"]
    if width < min_w or height < min_h:
        raise BrandError(f"Image is {width}×{height}; it must be at least {min_w}×{min_h} "
                         f"({spec['size']} recommended).")
    if max(width, height) > MAX_PIXELS:
        raise BrandError(f"Image is {width}×{height}; keep it under {MAX_PIXELS} px "
                         f"({spec['size']} is ideal).")
    ratio = width / height
    if spec.get("square") and not 0.9 <= ratio <= 1.1:
        raise BrandError(f"Image is {width}×{height}; it must be square ({spec['size']}).")
    warning = ""
    if not spec.get("square") and abs(ratio - spec["aspect"]) / spec["aspect"] > 0.25:
        warning = (f"Saved. Your image is {width}×{height}; {spec['size']} fits this spot "
                   f"best, so it may be cropped or letterboxed.")
    return kind, size, warning


def _setting_key(slot):
    return "default_art" if slot == "cover" else None


def custom_url(settings, slot):
    """Uploaded image URL for a slot if its file still exists, else None."""
    if slot == "cover":
        value = settings.get("default_art") or ""
    else:
        value = (settings.get("brand_images") or {}).get(slot) or ""
    if value.startswith("/branding/"):
        name = value[len("/branding/"):]
        if "/" not in name and (config.BRANDING_DIR / name).is_file():
            return value
    return None


def url(settings, slot):
    return custom_url(settings, slot) or SLOTS[slot]["default"]


def images(settings):
    """{slot: {"url": effective or None, "custom": bool}} for the public site and admin."""
    return {slot: {"url": url(settings, slot), "custom": bool(custom_url(settings, slot))}
            for slot in SLOTS}


def save(conn, slot, data):
    kind, size, warning = validate(slot, data)
    config.BRANDING_DIR.mkdir(parents=True, exist_ok=True)
    prefix = _prefix(slot)
    for old in config.BRANDING_DIR.glob(f"{prefix}-*"):
        old.unlink(missing_ok=True)
    name = f"{prefix}-{int(time.time() * 1000)}.{EXT[kind]}"
    path = config.BRANDING_DIR / name
    path.write_bytes(data)
    path.chmod(0o644)
    value = f"/branding/{name}"
    if slot == "cover":
        db.set_setting(conn, "default_art", value)
    else:
        current = dict(db.get_setting(conn, "brand_images") or {})
        current[slot] = value
        db.set_setting(conn, "brand_images", current)
    conn.commit()
    return {"url": value, "width": size[0] if size else None,
            "height": size[1] if size else None, "warning": warning}


def reset(conn, slot):
    if slot not in SLOTS:
        raise BrandError(f"Unknown image: {slot}")
    for old in config.BRANDING_DIR.glob(f"{_prefix(slot)}-*"):
        old.unlink(missing_ok=True)
    if slot == "cover":
        db.set_setting(conn, "default_art", "")
    else:
        current = dict(db.get_setting(conn, "brand_images") or {})
        current.pop(slot, None)
        db.set_setting(conn, "brand_images", current)
    conn.commit()
    return {"url": SLOTS[slot]["default"]}


# -------------------------------------------------------------------- texts
# (key, label, default, multiline, hint). {station} and {year} are filled in;
# *word* is highlighted; a new line in a multi-line field is a line break.
TEXTS = [
    ("brand_tag", "Header label", "INTERNET RADIO", False, "Small text next to the logo."),
    ("hero_band", "Banner strap", "INTERNET RADIO", False, "The boxed line under the banner."),
    ("hero_right", "Banner side text", "Music\nPeople\nCulture\n*Worldwide*", True,
     "Right of the banner (hidden on phones). The left side shows Genres (Site settings)."),
    ("feature_title", "Feature card heading", "Your music, live", False, ""),
    ("pillar_1", "Feature icon 1", "Worldwide\nListeners", True, ""),
    ("pillar_2", "Feature icon 2", "Independent\nArtists", True, ""),
    ("pillar_3", "Feature icon 3", "Music\nCommunity", True, ""),
    ("pillar_4", "Feature icon 4", "24/7\nStreaming", True, ""),
    ("follow_title", "Follow card heading", "Follow {station}", False, ""),
    ("follow_text", "Follow card text",
     "Stay connected for updates, new shows and exclusive content.", False, ""),
    ("slogan", "Slogan between sections", "Your music. *Your* station.", False, ""),
    ("music_title", "Music section heading", "Explore the music", False, ""),
    ("music_text", "Music section text",
     "Search the {station} library and request a track. Requests join the queue and play "
     "after the current song, subject to fair-play limits.", True, ""),
    ("community_title", "Community card heading", "Community Access", False, ""),
    ("community_text", "Community card text",
     "Join the conversation with fellow music lovers. Create an account to access the forum "
     "and video chat.", True, ""),
    ("video_title", "Video chat heading", "Video Chat", False, ""),
    ("video_text", "Video chat text", "Jump into a live group chat with the {station} community.",
     True, ""),
    ("video_foot_title", "Video chat footer heading", "Chat • Meet • Share • Collaborate", False, ""),
    ("video_foot", "Video chat footer text",
     "Be respectful. Keep it positive. Music brings us together.", False, ""),
    ("about_title", "About heading", "About {station}", False, ""),
    ("about_side", "About side text", "Made by\n*music* lovers", True, ""),
    ("footer_slogan", "Footer slogan", "Listen • Request • *Connect*", False, ""),
    ("copyright", "Copyright line", "© {year} {station}. All rights reserved.", False, ""),
    ("page_title", "Browser tab title (and search result title)", "{station} | Internet Radio", False,
     "Keep it under about 60 characters, e.g. {station} | Independent Hip Hop & Rap Radio. "
     "It stays the same while songs change."),
    ("page_description", "Search description", "", False,
     "The snippet under your title in search results and link previews (about 150 characters). "
     "Empty = station name and tagline."),
    ("live_band", "Live Video page strap", "LIVE VIDEO", False, ""),
]
TEXT_DEFAULTS = {key: default for key, _, default, _, _ in TEXTS}
TEXT_MAX = 600


def texts(settings):
    merged = dict(TEXT_DEFAULTS)
    for key, value in (settings.get("texts") or {}).items():
        if key in merged and isinstance(value, str) and value.strip():
            merged[key] = value
    return merged


def clean_texts(value):
    if not isinstance(value, dict):
        raise BrandError("texts must be an object")
    out = {}
    for key, text in value.items():
        if key not in TEXT_DEFAULTS:
            continue
        text = str(text or "").replace("\r\n", "\n").strip()
        if len(text) > TEXT_MAX:
            raise BrandError(f"{key} is longer than {TEXT_MAX} characters")
        if text and text != TEXT_DEFAULTS[key]:
            out[key] = text
    return out


# ------------------------------------------------------------- colours, CSS
THEME_DEFAULTS = {"accent": "#1f9bff", "highlight": "#ff2b2b"}
CUSTOM_MAX = 100 * 1024
_HEX = re.compile(r"#[0-9a-fA-F]{6}")


def theme(settings):
    out = dict(THEME_DEFAULTS)
    for key, value in (settings.get("theme") or {}).items():
        if key in out and isinstance(value, str) and _HEX.fullmatch(value):
            out[key] = value.lower()
    return out


def clean_theme(value):
    if not isinstance(value, dict):
        raise BrandError("theme must be an object")
    out = {}
    for key in THEME_DEFAULTS:
        if key in value and value[key]:
            if not _HEX.fullmatch(str(value[key])):
                raise BrandError(f"{key} must be a colour like #1f9bff")
            out[key] = str(value[key]).lower()
    return out


def clean_code(value, name):
    value = str(value or "").replace("\r\n", "\n")
    if len(value.encode()) > CUSTOM_MAX:
        raise BrandError(f"Custom {name} is larger than 100 KB")
    return value


def _rgb(hex_colour):
    return tuple(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))


def _shade(hex_colour, factor):
    return "#" + "".join(f"{max(0, min(255, int(c * factor))):02x}" for c in _rgb(hex_colour))


def custom_css(settings):
    colours = theme(settings)
    code = settings.get("custom_css") or ""
    # @import must come before any other rule, so lift it above the colours.
    imports = re.findall(r"^[ \t]*@import[^;]*;", code, flags=re.M)
    code = re.sub(r"^[ \t]*@import[^;]*;[ \t]*\n?", "", code, flags=re.M)
    parts = [*(i.strip() for i in imports), "/* Generated by AirStation: Admin > Appearance */"]
    if colours != THEME_DEFAULTS:
        a, h = colours["accent"], colours["highlight"]
        parts.append(":root {\n"
                     f"  --blue: {a}; --blue-2: {_shade(a, 0.72)}; "
                     f"--blue-glow: rgba({', '.join(map(str, _rgb(a)))}, .35);\n"
                     f"  --red: {h}; --red-2: {_shade(h, 0.84)}; "
                     f"--red-glow: rgba({', '.join(map(str, _rgb(h)))}, .35);\n"
                     "}")
    if code.strip():
        parts.append("/* Custom CSS */\n" + code)
    return "\n".join(parts) + "\n"


def custom_js(settings):
    code = settings.get("custom_js") or ""
    return ("// Custom JS from Admin > Appearance\n" + code + "\n") if code.strip() else ""


# --------------------------------------------------------------- theme packs
PACK_SETTINGS = ("station_name", "tagline", "about", "genres_banner", "rotation_name",
                 "rotation_description", "social")


def export_pack(conn, target):
    """Write the current look to a folder: theme.json + one file per custom image."""
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    settings = db.all_settings(conn)
    manifest = {"format": "airstation-theme/1", "exported_at": int(time.time()),
                "settings": {k: settings.get(k) for k in PACK_SETTINGS},
                "texts": settings.get("texts") or {}, "theme": settings.get("theme") or {},
                "custom_css": settings.get("custom_css") or "",
                "custom_js": settings.get("custom_js") or "", "images": {}}
    for slot in SLOTS:
        value = custom_url(settings, slot)
        if value:
            source = config.BRANDING_DIR / value[len("/branding/"):]
            name = f"{slot}{source.suffix}"
            shutil.copyfile(source, target / name)
            manifest["images"][slot] = name
    (target / "theme.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    return manifest


def import_pack(conn, source):
    """Apply a theme pack folder. Returns a list of human-readable results."""
    source = Path(source)
    manifest = json.loads((source / "theme.json").read_text(encoding="utf-8"))
    done = []
    # A theme never renames a station: its station settings only fill in what
    # this install hasn't set yet (a fresh install takes them all).
    for key, value in (manifest.get("settings") or {}).items():
        if key not in PACK_SETTINGS or value is None:
            continue
        if conn.execute("SELECT 1 FROM settings WHERE key = ?", (key,)).fetchone():
            done.append(f"kept your {key}")
            continue
        db.set_setting(conn, key, value)
        done.append(f"setting {key}")
    if "texts" in manifest:
        db.set_setting(conn, "texts", clean_texts(manifest["texts"] or {}))
        done.append("page texts")
    if "theme" in manifest:
        db.set_setting(conn, "theme", clean_theme(manifest["theme"] or {}))
        done.append("colours")
    for key, name in (("custom_css", "CSS"), ("custom_js", "JS")):
        value = manifest.get(key)
        if isinstance(value, str) and value.endswith((".css", ".js")) and (source / value).is_file():
            value = (source / value).read_text(encoding="utf-8")
        if value is not None:
            db.set_setting(conn, key, clean_code(value, name))
            done.append(f"custom {name}")
    conn.commit()
    images = dict(manifest.get("images") or {})
    for slot in SLOTS:                      # also pick up files named like the slot
        if slot not in images:
            for ext in ("png", "jpg", "jpeg", "webp", "svg"):
                if (source / f"{slot}.{ext}").is_file():
                    images[slot] = f"{slot}.{ext}"
                    break
    for slot, name in images.items():
        path = source / Path(name).name
        if slot not in SLOTS or not path.is_file():
            done.append(f"! image {slot}: {name} not found")
            continue
        try:
            result = save(conn, slot, path.read_bytes())
            done.append(f"image {slot}" + (f" ({result['warning']})" if result["warning"] else ""))
        except BrandError as error:
            done.append(f"! image {slot}: {error}")
    return done


def main(argv=None):
    """python3 -m airstation.branding import|export DIR (inside the api container)."""
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] not in ("import", "export"):
        print("usage: python3 -m airstation.branding import|export DIR")
        return 2
    conn = db.open_db()
    if args[0] == "export":
        manifest = export_pack(conn, args[1])
        print(f"Exported {len(manifest['images'])} images and the page settings to {args[1]}")
    else:
        for line in import_pack(conn, args[1]):
            print(("  " if not line.startswith("!") else "  ") + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
