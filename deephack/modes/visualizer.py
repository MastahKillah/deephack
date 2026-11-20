"""Visualizer: the music as a picture, without wearing the flash. Two of them:

**Spectrum** (needs the direct drawing firmware): a bar for each of 24 pitches, from the bass on the left
to the treble on the right, as loud as that pitch is right now, with a mark where it last peaked. Every
frame is drawn anew, twenty times a second, from what audio.Spectrum hears. A cooler that cannot be drawn
on gets the orb instead.

**Orb**: the louder (the bass counts most), the bigger and hotter the orb and the longer its rays. The mode
draws 24 frames, from silence to full blast, once, and from then on only says which one belongs on screen:
a Flipbook. What the engine does with it depends on the cooler's firmware:

- With the direct drawing firmware (tools/fw, see the README) the frame is drawn straight from USB,
  about twenty times a second and well under a tenth of a second behind the sound. Nothing is stored.
- DeepCool's own firmware cannot be drawn on: every picture has to be stored first. Showing a stored
  picture is only a read, and the firmware keeps up with about ten a second, so the engine stores as
  many of the frames as the cooler has room for (it keeps nine or so stills in all), once, and flips
  between those. That way the orb trails the music by 0.2 to 0.3 seconds, which is late for a beat.

The frames' edges glow in their colour, and the cooler's own light, when set to follow the screen,
takes its colour from them: it pulses along, with drawn frames as with stored ones (seen on the cooler).

The sound comes from what the PC is playing (see audio.py); nothing is recorded or sent anywhere.
"""
from __future__ import annotations

import asyncio
import colorsys
import math
import random
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter

from ..audio import BANDS, LISTENER
from ..render import H, W
from .base import Flipbook, Mode, Setting

LEVELS = 24
VERSION = 3                                    # bump when the frames change, so they are stored again
PALETTES = {   # hue (degrees) at the quietest and at the loudest bass level
    "aurora": (205, 320), "fire": (350, 48), "ocean": (150, 215), "mono": None,
}
CENTRE = (W // 2, 300)
BAR_W, BAR_GAP = 14, 5                         # the spectrum's bars: 24 of them fill the screen's width
BARS_LEFT = (W - BANDS * BAR_W - (BANDS - 1) * BAR_GAP) // 2
FLOOR, REACH = 452, 392                        # where the bars stand, and how tall the loudest is
MIRROR = 0.36                                  # their reflection under the floor: this much of the height
GLOWS = 8                                      # steps of the glow behind them, which follows the bass


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


@lru_cache(maxsize=2)
def bar_grounds(palette: str) -> tuple[Image.Image, ...]:
    """What the bars are drawn on, from quiet to loud: dark, with a glow at the floor and at the picture's
    edges (which the cooler's light picks up). Blurring is slow, so these are made once."""
    out = []
    for step in range(GLOWS):
        energy = step / (GLOWS - 1)
        colour = _colour(palette, round(energy * (LEVELS - 1)))
        glow = Image.new("RGB", (W, H), (0, 0, 0))
        g = ImageDraw.Draw(glow)
        edge = tuple(round(c * (0.08 + 0.5 * energy)) for c in colour)
        for box in ([0, 0, W, 50], [0, H - 50, W, H], [0, 0, 50, H], [W - 50, 0, W, H]):
            g.rectangle(box, fill=edge)
        g.rectangle([0, FLOOR - 30, W, FLOOR + 40], fill=tuple(round(c * (0.10 + 0.25 * energy)) for c in colour))
        out.append(Image.blend(Image.new("RGB", (W, H), (6, 6, 12)), glow.filter(ImageFilter.GaussianBlur(40)), 0.9))
    return tuple(out)


@lru_cache(maxsize=8)
def bar_colours(palette: str) -> tuple[tuple, ...]:
    """Per bar: its colour, the dimmer one of its reflection, and the lighter one of its peak mark."""
    out = []
    for i in range(BANDS):
        colour = _colour(palette, round(i * (LEVELS - 1) / (BANDS - 1)))
        out.append((colour, tuple(round(c * 0.3) for c in colour), tuple(round(c + (255 - c) * 0.6) for c in colour)))
    return tuple(out)


def render_bars(ground: Image.Image, palette: str, heights, marks) -> Image.Image:
    """The spectrum: `heights` and `marks` are pixels above the floor, one per band."""
    img = ground.copy()
    d = ImageDraw.Draw(img)
    for i, (colour, dim, light) in enumerate(bar_colours(palette)):
        x = BARS_LEFT + i * (BAR_W + BAR_GAP)
        tall = max(2, heights[i])                                  # a quiet band is a line on the floor
        d.rectangle([x, FLOOR - tall, x + BAR_W - 1, FLOOR], fill=colour)
        d.rectangle([x, FLOOR + 5, x + BAR_W - 1, FLOOR + 5 + round(tall * MIRROR)], fill=dim)
        if marks[i] > tall + 4:
            d.rectangle([x, FLOOR - marks[i] - 3, x + BAR_W - 1, FLOOR - marks[i]], fill=light)
    return img


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
        Setting("style", "Picture", "choice", "bars",
                (("bars", "Spectrum: a bar for each pitch"), ("orb", "Orb: pulses with the bass")),
                hint="The spectrum is drawn anew twenty times a second, which needs the direct drawing firmware on "
                     "the cooler; without it you get the orb."),
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
        self._look = None                              # the spectrum last returned, as pixels
        self.placeholder = ("Visualizer", "Drawing the frames…")

    async def _bars(self):
        levels, peaks = self.listener.spectrum()
        if not self.listener.available:
            self.status = "No sound output to listen to"
        else:
            self.status = "Following the music" if any(levels) else "Nothing is playing"
        heights = tuple(round(v * REACH) for v in levels)
        marks = tuple(round(v * REACH) for v in peaks)
        glow = min(GLOWS - 1, int(sum(levels[:4]) / 4 * GLOWS))          # the bass
        if (heights, marks, glow) == self._look:
            return None
        self._look, self._index = (heights, marks, glow), None
        grounds = await asyncio.to_thread(bar_grounds, self.palette)
        return render_bars(grounds[glow], self.palette, heights, marks)

    async def frame(self):
        if self.direct and self.values["style"] != "orb":
            return await self._bars()
        self._look = None
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
