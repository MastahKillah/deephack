"""A little pixel pet that lives on the cooler and feels your Claude Code plan limits.

The fuller the limits, the more worn out it gets. Only the limit percentages are shown (the windows
your plan has), read from the same claude-hud snapshot as the Claude Code mode. The pet is a short
looping animation stored on the cooler, re-uploaded only when a percentage or its mood changes.
"""
from __future__ import annotations

import asyncio
import time

from PIL import Image, ImageDraw

from .. import render
from ..config import config_dir
from ..render import H, W
from .base import Animation, Mode, Setting
from .claude_usage import SNAPSHOT_NAME, Activity, read_limits

FRAMES = 8
LOOP_MS = 2000

BG_TOP, BG_BOTTOM = (36, 29, 27), (18, 15, 15)
BODY, BODY_TIRED, EYE = (217, 119, 87), (150, 112, 100), (30, 22, 20)
TEXT, MUTED = (245, 240, 234), (150, 140, 132)
SPARK, SWEAT, ZED = (255, 226, 150), (130, 200, 255), (190, 180, 215)
ACCENT, AMBER, RED = (217, 119, 87), (240, 180, 70), (236, 96, 96)

CELL = 20
# 16 x 10 cells. X body, E eye, A arm, L leg.
SPRITE = (
    "..XXXXXXXXXXXX..",
    "..XXXXXXXXXXXX..",
    "..XXEXXXXXXEXX..",
    "..XXEXXXXXXEXX..",
    "AAXXXXXXXXXXXXAA",
    "AAXXXXXXXXXXXXAA",
    "..XXXXXXXXXXXX..",
    "..XXXXXXXXXXXX..",
    "..L.L......L.L..",
    "..L.L......L.L..",
)
SPRITE_W, SPRITE_H = len(SPRITE[0]) * CELL, len(SPRITE) * CELL

# 5 x 7 pixel font, just the characters this screen needs
FONT = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "%": ("11001", "11001", "00010", "00100", "01000", "10011", "10011"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    " ": ("00000",) * 7,
}


def mood_for(percent: int | None) -> str:
    if percent is None or percent < 50:
        return "happy"
    if percent < 80:
        return "fine"
    if percent < 95:
        return "stressed"
    return "exhausted"


def pixel_text(d: ImageDraw.ImageDraw, text: str, x: float, y: float, cell: int, fill, anchor: str = "l") -> None:
    width = len(text) * 6 * cell - cell
    if anchor == "m":
        x -= width / 2
    elif anchor == "r":
        x -= width
    for ch in text:
        for r, row in enumerate(FONT.get(ch, FONT[" "])):
            for c, bit in enumerate(row):
                if bit == "1":
                    d.rectangle([x + c * cell, y + r * cell, x + (c + 1) * cell - 1, y + (r + 1) * cell - 1], fill=fill)
        x += 6 * cell


def _draw_pet(d: ImageDraw.ImageDraw, x: int, y: int, frame: int, mood: str) -> None:
    body = BODY_TIRED if mood == "exhausted" else BODY
    eyes_closed = mood == "exhausted" or frame == 4
    wave = mood == "happy" and frame in (5, 6)
    step = mood != "exhausted" and frame in (1, 2, 5, 6)      # legs shuffle while it bobs
    for r, row in enumerate(SPRITE):
        for c, ch in enumerate(row):
            if ch == ".":
                continue
            cx, cy, colour = c, r, body
            if ch == "E":
                if eyes_closed and r == 2:
                    colour = body               # closed eyes: only the lower pixel stays dark
                else:
                    colour = EYE
            elif ch == "A" and wave and c >= 14:
                cy -= 2                         # right arm up
            elif ch == "L" and step and r == 9 and c in (2, 11):
                continue                        # lift alternating feet
            elif ch == "L" and not step and mood != "exhausted" and r == 9 and c in (4, 13) and frame in (3, 7):
                continue
            d.rectangle([x + cx * CELL, y + cy * CELL, x + (cx + 1) * CELL - 1, y + (cy + 1) * CELL - 1], fill=colour)


def _sparkle(d: ImageDraw.ImageDraw, x: int, y: int, size: int) -> None:
    d.rectangle([x - size * 2, y - size // 2, x + size * 2, y + size // 2], fill=SPARK)
    d.rectangle([x - size // 2, y - size * 2, x + size // 2, y + size * 2], fill=SPARK)


def render_frame(frame: int, limits: dict | None) -> Image.Image:
    windows = [(label, limits[key][0]) for key, label in (("five_hour", "5H"), ("seven_day", "WEEK")) if limits and key in limits]
    worst = max((p for _, p in windows), default=None)
    mood = mood_for(worst)
    img = render.vertical_gradient(BG_TOP, BG_BOTTOM)
    d = ImageDraw.Draw(img)

    bob = 0 if mood == "exhausted" else (0, -8, -14, -8, 0, -8, -14, -8)[frame]
    px, py = (W - SPRITE_W) // 2, 150 + bob
    ground = 150 + SPRITE_H + 14
    shrink = -bob                               # the shadow tightens as the pet lifts off
    d.ellipse([px + 40 + shrink, ground, px + SPRITE_W - 40 - shrink, ground + 16], fill=(12, 10, 10))
    _draw_pet(d, px, py, frame, mood)

    if mood == "happy":
        for i, (sx, sy) in enumerate(((70, 120), (410, 150), (96, 330), (398, 300))):
            if (frame + i * 2) % 8 < 3:
                _sparkle(d, sx, sy, 4 if (frame + i) % 2 else 3)
    elif mood == "stressed":
        drop = (frame % 4) * 12
        sx, sy = px + SPRITE_W - 16, py - 6 + drop
        d.polygon([(sx, sy - 14), (sx - 8, sy + 2), (sx + 8, sy + 2)], fill=SWEAT)
        d.ellipse([sx - 8, sy - 6, sx + 8, sy + 10], fill=SWEAT)
    elif mood == "exhausted":
        rise = frame * 5
        pixel_text(d, "Z", px + SPRITE_W - 30, py - 30 - rise, 4, ZED)
        pixel_text(d, "Z", px + SPRITE_W + 6, py - 74 - rise, 3, ZED)

    # the limits, in chunky pixels
    top = 470
    if not windows:
        pixel_text(d, "--%", W / 2, top + 30, 9, MUTED, "m")
    for i, (label, pct) in enumerate(windows):
        cx = W / 2 if len(windows) == 1 else W * (0.27 + 0.46 * i)
        colour = RED if pct >= 85 else AMBER if pct >= 60 else TEXT
        pixel_text(d, label, cx, top, 4, MUTED, "m")
        pixel_text(d, f"{pct}%", cx, top + 44, 10 if len(windows) == 1 else 8, colour, "m")
    return img


def render_animation(limits: dict | None) -> Animation:
    return Animation([render_frame(i, limits) for i in range(FRAMES)], LOOP_MS)


class Tamagotchi(Mode):
    id = "tamagotchi"
    title = "Claude Pet"
    description = "A pixel pet that feels your limits"
    glyph = ""
    claims = "Claude busy"
    settings = (
        Setting("refresh", "Check limits every", "choice", "15", (("5", "5 minutes"), ("15", "15 minutes"), ("30", "30 minutes"), ("60", "1 hour")),
                hint="The pet is an 8-frame animation stored on the cooler. It is rewritten only when a percentage "
                     "changes. Limits come from the claude-hud usage snapshot, like the Claude Code mode."),
    )

    def __init__(self, values=None):
        super().__init__(values)
        self._next = 0.0
        self._shown = None
        self.snapshot = config_dir() / SNAPSHOT_NAME
        self.placeholder = ("Claude Pet", "Waking up…")
        self._activity = Activity()

    async def frame(self):
        self.active = await self._activity.busy()
        if time.monotonic() < self._next:
            return None
        self._next = time.monotonic() + int(self.values["refresh"]) * 60
        limits = await asyncio.to_thread(read_limits, self.snapshot)
        shown = {k: v[0] for k, v in (limits or {}).items()}
        parts = [f"{name} {shown[key]}%" for key, name in (("five_hour", "5-hour"), ("seven_day", "weekly")) if key in shown]
        mood = mood_for(max(shown.values(), default=None))
        self.status = f"Feeling {mood}" + (" · " + ", ".join(parts) if parts else " · no limits reported")
        key = tuple(sorted(shown.items()))
        if key == self._shown:
            return None
        self._shown = key
        return await asyncio.to_thread(render_animation, limits)
