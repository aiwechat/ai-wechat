"""Generate the PWA icon set for the web client.

Renders the same artwork as web/icons/icon.svg (green gradient tile, white
chat bubble, AI sparkle) into the PNG sizes the manifest and iOS need.

Usage: python3 scripts/generate_icons.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).resolve().parents[1] / "web" / "icons"

# Artwork is defined in a 512x512 coordinate space, like the SVG.
ART = 512
SUPERSAMPLE = 4
GRADIENT_TOP = (52, 52, 58)
GRADIENT_BOTTOM = (24, 24, 27)
SPARKLE = (24, 24, 27)
CORNER_RADIUS = 116


def quad_bezier(p0, p1, p2, steps=32):
    return [
        (
            (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t**2 * p2[0],
            (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t**2 * p2[1],
        )
        for t in (i / steps for i in range(steps + 1))
    ]


def sparkle_polygon(cx, cy, scale):
    tips = [(0, -88), (76, 0), (0, 88), (-76, 0)]
    controls = [(12, -24), (12, 24), (-12, 24), (-12, -24)]
    points = []
    for i, tip in enumerate(tips):
        nxt = tips[(i + 1) % 4]
        points.extend(quad_bezier(tip, controls[i], nxt))
    return [(cx + x * scale, cy + y * scale) for x, y in points]


def diagonal_gradient(size):
    axis = np.linspace(0.0, 1.0, size)
    t = (axis[None, :] + axis[:, None]) / 2.0
    top = np.array(GRADIENT_TOP, dtype=np.float64)
    bottom = np.array(GRADIENT_BOTTOM, dtype=np.float64)
    rgb = top[None, None, :] * (1 - t[:, :, None]) + bottom[None, None, :] * t[:, :, None]
    return Image.fromarray(rgb.astype(np.uint8), "RGB").convert("RGBA")


def draw_artwork(size, *, full_bleed, content_scale=1.0):
    """Render one icon.

    full_bleed paints the gradient edge to edge (maskable / apple-touch);
    otherwise the tile gets rounded transparent corners. content_scale
    shrinks the bubble into the maskable safe zone.
    """
    ss = size * SUPERSAMPLE
    unit = ss / ART
    image = diagonal_gradient(ss)

    if not full_bleed:
        mask = Image.new("L", (ss, ss), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, ss - 1, ss - 1), radius=CORNER_RADIUS * unit, fill=255)
        image.putalpha(mask)

    draw = ImageDraw.Draw(image)
    center = ss / 2
    scale = unit * content_scale

    def pt(x, y):
        return (center + (x - ART / 2) * scale, center + (y - ART / 2) * scale)

    draw.rounded_rectangle([pt(112, 128), pt(400, 352)], radius=64 * scale, fill=(255, 255, 255, 255))
    draw.polygon([pt(168, 330), pt(140, 420), pt(256, 352)], fill=(255, 255, 255, 255))
    draw.polygon(sparkle_polygon(center, center + (240 - ART / 2) * scale, scale), fill=(*SPARKLE, 255))

    return image.resize((size, size), Image.LANCZOS)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jobs = {
        "icon-192.png": draw_artwork(192, full_bleed=False),
        "icon-512.png": draw_artwork(512, full_bleed=False),
        "icon-maskable-192.png": draw_artwork(192, full_bleed=True, content_scale=0.78),
        "icon-maskable-512.png": draw_artwork(512, full_bleed=True, content_scale=0.78),
        "apple-touch-icon.png": draw_artwork(180, full_bleed=True, content_scale=0.9).convert("RGB"),
    }
    for name, image in jobs.items():
        path = OUT_DIR / name
        image.save(path)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
