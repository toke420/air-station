#!/usr/bin/env python3
"""Regenerate the bundled, brand-neutral default images in web/assets/img.

They are drawn from code (no photos, no fonts with licence questions), so the
whole set can be released with the project. Needs Pillow:
    python3 -m pip install pillow
    python3 scripts/dev/make_default_images.py
"""
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parents[2] / "web" / "assets" / "img"
BG = (7, 9, 13)
BLUE = (31, 155, 255)
RED = (255, 43, 43)
WHITE = (238, 242, 247)


def glow(size, spots):
    """Dark canvas with soft coloured light spots: [(x, y, radius, colour, alpha)]."""
    w, h = size
    base = Image.new("RGB", size, BG)
    for x, y, r, colour, alpha in spots:
        layer = Image.new("RGB", size, BG)
        d = ImageDraw.Draw(layer)
        d.ellipse((x - r, y - r, x + r, y + r), fill=colour)
        layer = layer.filter(ImageFilter.GaussianBlur(r * 0.6))
        base = Image.blend(base, Image.composite(layer, base, Image.new("L", size, 255)), alpha)
    return base


def bars(draw, x0, y_base, count, width, gap, max_h, colour, seed=1):
    rnd = random.Random(seed)
    for i in range(count):
        h = max_h * (0.25 + 0.75 * abs(math.sin(i * 0.55 + rnd.random())))
        x = x0 + i * (width + gap)
        draw.rounded_rectangle((x, y_base - h, x + width, y_base), radius=width // 2, fill=colour)


def radio_mark(draw, cx, cy, scale, colour, stroke):
    """The radio-waves mark used across the default set (matches the site icon)."""
    draw.ellipse((cx - 2.2 * scale, cy - 2.2 * scale, cx + 2.2 * scale, cy + 2.2 * scale), fill=colour)
    for radius in (6.5, 11):
        r = radius * scale
        box = (cx - r, cy - r, cx + r, cy + r)
        draw.arc(box, 315, 45, fill=colour, width=stroke)
        draw.arc(box, 135, 225, fill=colour, width=stroke)


def vinyl(draw, cx, cy, r, accent):
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(14, 17, 23), outline=(40, 48, 60), width=3)
    for i in range(6, int(r * 0.92), 9):
        draw.ellipse((cx - i, cy - i, cx + i, cy + i), outline=(24, 29, 38), width=1)
    lr = r * 0.32
    draw.ellipse((cx - lr, cy - lr, cx + lr, cy + lr), fill=accent)
    draw.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), fill=BG)


def feature():
    img = glow((800, 600), [(250, 330, 260, BLUE, .55), (640, 180, 200, RED, .35)])
    d = ImageDraw.Draw(img)
    vinyl(d, 330, 320, 230, BLUE)
    bars(d, 560, 520, 12, 14, 8, 300, (255, 255, 255), seed=4)
    img.save(OUT / "feature.jpg", quality=86)


def follow():
    img = glow((600, 400), [(150, 120, 200, BLUE, .5), (470, 300, 180, RED, .4)])
    d = ImageDraw.Draw(img)
    for k, colour in enumerate((BLUE, WHITE, RED)):
        pts = [(x, 200 + 60 * math.sin(x / 55 + k * 1.3) * (0.6 + 0.4 * math.cos(x / 140)))
               for x in range(-10, 611, 6)]
        d.line(pts, fill=colour, width=5 - k)
    img.save(OUT / "follow.jpg", quality=86)


def video():
    img = glow((800, 340), [(620, 170, 190, BLUE, .5), (720, 80, 120, RED, .35)])
    d = ImageDraw.Draw(img)
    rnd = random.Random(7)
    for _ in range(40):                  # bokeh, kept to the right (button sits top-left)
        x, y, r = rnd.randint(380, 800), rnd.randint(0, 340), rnd.randint(6, 26)
        tone = rnd.choice((BLUE, RED, WHITE))
        d.ellipse((x - r, y - r, x + r, y + r), outline=tone, width=2)
    radio_mark(d, 620, 190, 9, WHITE, 10)
    img.save(OUT / "video.jpg", quality=86)


def cover():
    img = glow((512, 512), [(256, 256, 230, BLUE, .45)])
    d = ImageDraw.Draw(img)
    vinyl(d, 256, 256, 190, BLUE)
    img.save(OUT / "default-art.png", optimize=True)


def icon(size, name, radius_frac=0.22):
    scale = size / 512
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * radius_frac), fill=(16, 21, 29, 255))
    radio_mark(d, size / 2, size / 2, 18 * scale, BLUE, max(2, int(34 * scale)))
    img.save(OUT / name, optimize=True)


def share():
    img = glow((1200, 630), [(380, 315, 330, BLUE, .5), (860, 240, 260, RED, .35)])
    d = ImageDraw.Draw(img)
    radio_mark(d, 600, 300, 16, WHITE, 18)
    bars(d, 360, 560, 30, 10, 6, 70, BLUE, seed=9)
    img.save(OUT / "share.jpg", quality=86)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    feature()
    follow()
    video()
    cover()
    share()
    icon(512, "icon-512.png")
    icon(192, "icon-192.png")
    icon(180, "apple-touch-icon.png", radius_frac=0)
    icon(64, "favicon.png")
    print("Default images written to", OUT)
