"""Drawing helpers shared by the display modes. Everything renders to a 480x640 PIL image."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .device import HEIGHT, WIDTH

W, H = WIDTH, HEIGHT
FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
REGULAR, SEMIBOLD, BOLD, LIGHT = "segoeui.ttf", "seguisb.ttf", "segoeuib.ttf", "segoeuil.ttf"


@lru_cache(maxsize=64)
def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for candidate in (name, REGULAR, "arial.ttf"):
        try:
            return ImageFont.truetype(str(FONT_DIR / candidate), size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def fit(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: int) -> str:
    """Truncate text with an ellipsis so it fits max_w pixels."""
    if draw.textlength(text, font=fnt) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=fnt) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: int, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for i, word in enumerate(words):
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=fnt) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        cur = word
        if len(lines) == max_lines - 1:
            cur = " ".join(words[i:])
            break
    lines.append(cur)
    lines = lines[:max_lines]
    lines[-1] = fit(draw, lines[-1], fnt, max_w)
    return lines


def vertical_gradient(top: tuple[int, int, int], bottom: tuple[int, int, int], size=(W, H)) -> Image.Image:
    w, h = size
    column = Image.new("RGB", (1, h))
    column.putdata([tuple(round(top[c] + (bottom[c] - top[c]) * y / (h - 1)) for c in range(3)) for y in range(h)])
    return column.resize((w, h), Image.BILINEAR)


def message(title: str, subtitle: str = "") -> Image.Image:
    """Neutral placeholder frame (used for the in-app preview, e.g. 'nothing is playing')."""
    img = vertical_gradient((30, 32, 44), (14, 15, 22))
    d = ImageDraw.Draw(img)
    d.text((W / 2, H / 2 - 24), fit(d, title, font(SEMIBOLD, 30), W - 60), font=font(SEMIBOLD, 30), fill=(235, 236, 242), anchor="mm")
    if subtitle:
        y = H / 2 + 22
        for line in wrap(d, subtitle, font(REGULAR, 21), W - 80, 3):
            d.text((W / 2, y), line, font=font(REGULAR, 21), fill=(150, 153, 168), anchor="mm")
            y += 30
    return img
