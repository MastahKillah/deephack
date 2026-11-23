"""deephack's logo, as the picture the patched firmware shows while the cooler starts (patch.py, version 5).

    python tools/fw/logo.py [--show logo.png]        writes tools/fw/logo.jpg

The cooler's own start screen is a picture in a file on its serial flash that nothing reaches over USB.
The patch puts this one over it, and has to carry it: as a JPEG after the patch's code, in what is left
of the chip's flash behind DeepCool's firmware, which is less than 15 KB. So the picture is plain (flat
colours, no gradients), and the JPEG has Huffman tables made for it (Pillow's optimize), which the
cooler's decoder takes (seen on the cooler, 23 Nov 2026) and which makes this picture a fifth smaller
than the standard tables do.

logo.jpg is in the repository and is what patch.py reads: another Pillow may encode the same drawing
into other bytes, and the patch's bytes are compared with a table (deephack/firmware_patch.py). Run this
only to change the logo, and `python tools/fw/patch.py --table` after it.
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PIL import Image, ImageDraw  # noqa: E402

from deephack import render  # noqa: E402

OUT = Path(__file__).with_name("logo.jpg")
MOST = 11_000                               # bytes the JPEG may have: the rest of the room is for the patch's code
K = 4                                       # drawn this many times as large and scaled down, for smooth edges
BG, PURPLE, GREEN, WHITE, MUTED, GLASS = (10, 12, 20), (124, 108, 247), (61, 220, 151), (240, 242, 248), (120, 128, 156), (18, 20, 34)


def draw() -> Image.Image:
    """480 x 640: the cooler's upright screen with a prompt on it, and the name under it."""
    width, height = 480 * K, 640 * K
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    cx, top, w, h = width // 2, 150 * K, 150 * K, 200 * K
    d.rounded_rectangle([cx - w // 2, top, cx + w // 2, top + h], radius=30 * K, fill=GLASS, outline=PURPLE, width=9 * K)
    x0, y0, s, t = cx - 38 * K, top + h // 2 - 8 * K, 30 * K, 13 * K           # ">": two strokes with round ends
    for a, b in (((x0, y0 - s), (x0 + s, y0)), ((x0 + s, y0), (x0, y0 + s))):
        d.line([a, b], fill=GREEN, width=t)
    for px, py in ((x0, y0 - s), (x0 + s, y0), (x0, y0 + s)):
        d.ellipse([px - t // 2, py - t // 2, px + t // 2, py + t // 2], fill=GREEN)
    d.rounded_rectangle([cx + 6 * K, y0 + s - t // 2, cx + 44 * K, y0 + s + t // 2], radius=t // 2, fill=WHITE)   # the cursor
    light, bold = render.font(render.LIGHT, 60 * K), render.font(render.BOLD, 60 * K)
    a, b = d.textlength("deep", font=light), d.textlength("hack", font=bold)
    x, y = (width - a - b) / 2, 432 * K
    d.text((x, y), "deep", font=light, fill=WHITE, anchor="ls")
    d.text((x + a, y), "hack", font=bold, fill=GREEN, anchor="ls")
    d.text((width / 2, 476 * K), "CUSTOM FIRMWARE", font=render.font(render.SEMIBOLD, 15 * K), fill=MUTED, anchor="ms")
    return img.resize((480, 640), Image.LANCZOS)


def encode(picture: Image.Image, most: int = MOST) -> tuple[bytes, int]:
    """The picture as the JPEG the cooler decodes (baseline, 4:2:0, its own Huffman tables), at the best
    quality that fits in `most` bytes; and that quality."""
    for quality in range(92, 40, -1):
        out = io.BytesIO()
        picture.save(out, "JPEG", quality=quality, subsampling="4:2:0", optimize=True, progressive=False)
        if len(out.getvalue()) <= most:
            return out.getvalue(), quality
    raise ValueError(f"the picture does not fit in {most} bytes at any quality worth showing")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--show", help="also write the drawing as a PNG, to look at")
    a = ap.parse_args()
    picture = draw()
    jpeg, quality = encode(picture)
    OUT.write_bytes(jpeg)
    if a.show:
        picture.save(a.show)
    print(f"{OUT}: {len(jpeg)} bytes at quality {quality}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
