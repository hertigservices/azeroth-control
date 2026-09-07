# -*- coding: utf-8 -*-
"""Generate AzerothControl.ico for the desktop launcher.

The icon is the rail crest from launcher.html rendered on its own: a dark tile
with the hub's gold keyline and a serif 'A'.  Kept as a script rather than a
committed binary so the palette stays in one place -- if the launcher's accent
colour changes, re-run this instead of hand-editing an .ico.
"""
import os

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:                       # the only non-stdlib import in the repo,
    raise SystemExit(                     # and only this one script needs it
        'This icon generator needs Pillow:  pip install Pillow\n'
        'Nothing else in Azeroth Control does - the panel is stdlib only.')

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'AzerothControl.ico')

TILE = (0x14, 0x14, 0x16, 255)      # --background-alt
GOLD = (0xc9, 0xaa, 0x71, 255)      # --primary
SIZES = [16, 24, 32, 48, 64, 128, 256]

# Georgia carries the launcher's display face better than Arial; fall back
# rather than fail on a box that trimmed its fonts.
FONTS = [r'C:\Windows\Fonts\georgiab.ttf',
         r'C:\Windows\Fonts\timesbd.ttf',
         r'C:\Windows\Fonts\arialbd.ttf']


def font_at(px):
    for path in FONTS:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, px)
            except Exception:
                continue
    return ImageFont.load_default()


def tile(size):
    """Draw one square at 4x and downsample, so the small sizes stay clean."""
    s = size * 4
    img = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = max(1, int(s * 0.04))
    radius = int(s * 0.22)
    d.rounded_rectangle([pad, pad, s - pad - 1, s - pad - 1],
                        radius=radius, fill=TILE,
                        outline=GOLD, width=max(2, int(s * 0.035)))

    f = font_at(int(s * 0.62))
    box = d.textbbox((0, 0), 'A', font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    # textbbox offsets are not zero for most faces; subtract them or the glyph
    # sits low and right of centre.
    d.text(((s - w) / 2 - box[0], (s - h) / 2 - box[1]), 'A', font=f, fill=GOLD)

    return img.resize((size, size), Image.LANCZOS)


def main():
    frames = [tile(n) for n in SIZES]
    # Pillow drops any requested size larger than the base image, so the biggest
    # frame has to be the one save() is called on; the rest ride along and are
    # matched by exact size rather than being resampled from it.
    frames.sort(key=lambda f: f.size[0], reverse=True)
    frames[0].save(OUT, format='ICO',
                   sizes=[(n, n) for n in SIZES],
                   append_images=frames[1:])
    print('wrote %s  (%d bytes, sizes %s)'
          % (OUT, os.path.getsize(OUT), ','.join(str(n) for n in SIZES)))


if __name__ == '__main__':
    main()
