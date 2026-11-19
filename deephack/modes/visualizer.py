"""Visualizer: an orb that pulses with the music, without wearing the flash.

The louder (the bass counts most), the bigger and hotter the orb and the longer its rays. The mode draws
24 frames, from silence to full blast, once, and from then on only says which one belongs on screen: a
Flipbook. What the engine does with it depends on the cooler's firmware:

- With the direct drawing firmware (tools/fw, see the README) the frame is drawn straight from USB,
  about twenty times a second and well under a tenth of a second behind the sound. Nothing is stored.
- DeepCool's own firmware cannot be drawn on: every picture has to be stored first. Showing a stored
  picture is only a read, and the firmware keeps up with about ten a second, so the engine stores as
  many of the frames as the cooler has room for (it keeps nine or so stills in all), once, and flips
  between those. That way the orb trails the music by 0.2 to 0.3 seconds, which is late for a beat.

The frames' edges glow in their colour. The cooler's own light picks its colour from the edges of a
stored picture when it is shown, so it follows the beat with stored frames and not with drawn ones.

The sound comes from what the PC is playing (see audio.py); nothing is recorded or sent anywhere.
"""
from __future__ import annotations

import asyncio
import colorsys
import math
import random
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter

from ..audio import LISTENER
from ..render import H, W
from .base import Flipbook, Mode, Setting

LEVELS = 24
VERSION = 3                                    # bump when the frames change, so they are stored again
PALETTES = {   # hue (degrees) at the quietest and at the loudest bass level
    "aurora": (205, 320), "fire": (350, 48), "ocean": (150, 215), "mono": None,
}
CENTRE = (W // 2, 300)


def _colour(palette: str, level: int) -> tuple[int, int, int]:
    t = level / (LEVELS - 1)
    hues = PALETTES[palette]
    if hues is None:
        v = 150 + round(105 * t)
        return v, v, v
    lo, hi = hues
    hue = (lo + (hi - lo if hi >= lo else hi + 360 - lo) * t) % 360
    r, g, b_ = colorsys.hsv_to_rgb(hue / 360, 0.78 - 0.18 * t, 0.85 + 0.15 * t)
    return round(r * 255), round(g * 255), round(b_ * 255)


def render_frame(palette: str, level: int) -> Image.Image:
    """Frame for loudness step `level`, 0 (silence) to LEVELS - 1."""
    colour = _colour(palette, level)
    energy = level / (LEVELS - 1)
    s = 2                                              # drawn at twice the size for smooth edges
    cx, cy = CENTRE[0] * s, CENTRE[1] * s
    img = Image.new("RGB", (W * s, H * s), (6, 6, 12))

    # glow at the picture's edges, which the cooler's light picks up, and behind the orb
    glow = Image.new("RGB", img.size, (0, 0, 0))
    g = ImageDraw.Draw(glow)
    edge = tuple(round(c * (0.10 + 0.55 * energy)) for c in colour)
    band = 70 * s
    g.rectangle([0, 0, W * s, band], fill=edge)
    g.rectangle([0, H * s - band, W * s, H * s], fill=edge)
    g.rectangle([0, 0, band, H * s], fill=edge)
    g.rectangle([W * s - band, 0, W * s, H * s], fill=edge)
    radius = round(40 + 118 * energy) * s
    halo = radius + round(40 + 130 * energy) * s
    g.ellipse([cx - halo, cy - halo, cx + halo, cy + halo], fill=tuple(round(c * (0.25 + 0.3 * energy)) for c in colour))
    img = Image.blend(img, glow.filter(ImageFilter.GaussianBlur(46 * s)), 0.9)

    d = ImageDraw.Draw(img)
    rng = random.Random(7)
    rays = 44
    for i in range(rays):                              # the rest of the sound: rays around the orb
        angle = 2 * math.pi * i / rays
        jitter = 0.45 + 0.55 * rng.random()
        inner = radius + 16 * s
        length = (5 + (6 + 96 * energy) * jitter) * s
        x0, y0 = cx + math.cos(angle) * inner, cy + math.sin(angle) * inner
        x1, y1 = cx + math.cos(angle) * (inner + length), cy + math.sin(angle) * (inner + length)
        shade = tuple(round(c * (0.55 + 0.45 * energy)) for c in colour)
        d.line([(x0, y0), (x1, y1)], fill=shade, width=5 * s)
        d.ellipse([x1 - 3 * s, y1 - 3 * s, x1 + 3 * s, y1 + 3 * s], fill=shade)
    core = tuple(min(255, round(c + (255 - c) * (0.15 + 0.5 * energy))) for c in colour)
    d.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=colour)
    inner = round(radius * 0.72)
    d.ellipse([cx - inner, cy - inner, cx + inner, cy + inner], fill=core)
    return img.resize((W, H), Image.LANCZOS)


@lru_cache(maxsize=2)
def frames(palette: str) -> tuple[Image.Image, ...]:
    return tuple(render_frame(palette, level) for level in range(LEVELS))


def frame_index(bass: float, rest: float) -> int:
    """The loudness step for the current sound: mostly the bass, where the beat is."""
    loud = 0.75 * bass + 0.25 * rest
    return min(LEVELS - 1, int(loud ** 1.2 * LEVELS))


class Visualizer(Mode):
    id = "visualizer"
    title = "Visualizer"
    description = "Pulses with the music"
    glyph = ""
    tick = 0.05
    settings = (
        Setting("palette", "Colours", "choice", "aurora",
                (("aurora", "Aurora: blue to magenta"), ("fire", "Fire: red to yellow"), ("ocean", "Ocean: green to blue"),
                 ("mono", "White")),
                hint="Follows whatever the PC is playing. With the direct drawing firmware on the cooler the frames "
                     "are drawn straight from USB, on the beat, and nothing is stored. On DeepCool's own firmware "
                     "as many of the frames as there is room for, seven at most, are stored on it once (as many flash "
                     "writes, also when you pick other colours here) and the mode flips between them, a quarter "
                     "of a second behind the music."),
    )

    def __init__(self, values=None, listener=None):
        super().__init__(values)
        self.listener = listener or LISTENER
        self.palette = self.values["palette"] if self.values["palette"] in PALETTES else "aurora"
        self._index: int | None = None
        self.placeholder = ("Visualizer", "Drawing the frames…")

    async def frame(self):
        bass, rest = self.listener.levels()
        if not self.listener.available:
            self.status = "No sound output to listen to"
        else:
            self.status = "Following the music" if bass or rest else "Nothing is playing"
        index = frame_index(bass, rest)
        if index == self._index:
            return None
        self._index = index
        book = await asyncio.to_thread(frames, self.palette)
        return Flipbook(f"visualizer-{self.palette}-{VERSION}", list(book), index)
