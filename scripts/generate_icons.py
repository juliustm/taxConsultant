#!/usr/bin/env python3
"""
Builds the PWA's home-screen icons from static/favicon.png.

Run once, and again whenever the favicon changes:

    python scripts/generate_icons.py

The outputs are committed rather than generated at request time - a phone installing
the app fetches these before it has a service worker, and an icon that 500s is an app
that installs without one. Pillow is already a dependency (utils/security uses it via
qrcode), so this adds nothing to requirements.txt.
"""
import pathlib
import sys

from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'static' / 'favicon.png'
OUT_DIR = ROOT / 'static' / 'icons'

# Android masks a maskable icon to whatever shape the launcher uses - a circle, a
# squircle, a rounded square - and clips up to 20% off each edge doing it. The art has
# to sit inside the middle 80% or the launcher crops it, so it is padded rather than
# scaled to the full canvas.
MASKABLE_SAFE_RATIO = 0.8
BACKGROUND = (255, 255, 255, 255)


def main():
    if not SOURCE.exists():
        sys.exit(f'No source icon at {SOURCE}')

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    source = Image.open(SOURCE).convert('RGBA')

    for size in (192, 512):
        out = OUT_DIR / f'icon-{size}.png'
        source.resize((size, size), Image.LANCZOS).save(out, 'PNG', optimize=True)
        print(f'wrote {out.relative_to(ROOT)} ({size}x{size})')

    size = 512
    inner = int(size * MASKABLE_SAFE_RATIO)
    canvas = Image.new('RGBA', (size, size), BACKGROUND)
    art = source.resize((inner, inner), Image.LANCZOS)
    offset = (size - inner) // 2
    canvas.paste(art, (offset, offset), art)

    out = OUT_DIR / 'icon-maskable-512.png'
    canvas.convert('RGB').save(out, 'PNG', optimize=True)
    print(f'wrote {out.relative_to(ROOT)} ({size}x{size}, maskable)')


if __name__ == '__main__':
    main()
