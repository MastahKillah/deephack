"""A little pixel pet that lives on the cooler.

Left alone, its mood follows your Claude Code plan limits: the fuller they are, the more worn out it gets.
It also reacts to what is going on, in an order you set: it works along while Claude Code is busy, dances
while music plays, sleeps at night and celebrates or sulks after your NBA team's games.

Every state is a short looping animation stored on the cooler, so it is rewritten only when the state,
a percentage or the score it shows changes. Music has to play for a few seconds before the pet dances
and stop for a while before it stops, so skipping or pausing a track does not cost a write.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from PIL import Image, ImageDraw

from .. import render
from ..config import config_dir
from ..render import H, W
from . import nba
from .base import Animation, Mode, Setting, parse_order
from .claude_usage import SNAPSHOT_NAME, Activity, read_limits
from .now_playing import Playback

FRAMES = 8
LOOP_MS = 2000

BG_TOP, BG_BOTTOM = (36, 29, 27), (18, 15, 15)
BODY, BODY_TIRED, EYE = (217, 119, 87), (150, 112, 100), (30, 22, 20)
TEXT, MUTED = (245, 240, 234), (150, 140, 132)
SPARK, SWEAT, ZED = (255, 226, 150), (130, 200, 255), (190, 180, 215)
ACCENT, AMBER, RED = (217, 119, 87), (240, 180, 70), (236, 96, 96)
MOON, CLOUD, SCREEN = (244, 234, 196), (98, 104, 122), (24, 21, 21)
BACKGROUNDS = {
    "sleeping": ((22, 26, 50), (9, 10, 22)),
    "sulking": ((34, 37, 46), (15, 16, 21)),
    "dancing": ((44, 27, 48), (19, 13, 22)),
}

REACTIONS = (("working", "Claude working"), ("music", "Music playing"), ("night", "Bedtime"), ("nba", "NBA result"))
DEFAULT_REACTIONS = "working,music,night,nba"
MUSIC_CHECK = 2.0                    # seconds between looks at the media session
MUSIC_SETTLE, MUSIC_LINGER = 8.0, 90.0
NBA_CHECK, NBA_RETRY = 20 * 60, 5 * 60
RESULT_HOURS = 12                    # how long the pet celebrates or sulks after the final

CELL = 20
BASE_Y = 150
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
NOTE = ("..X..", "..XX.", "..X.X", "..X..", "XXX..", "XXX..")

# 5 x 7 pixel font: digits, capitals and the few signs this screen needs
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
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "10001", "11001", "10101", "10011", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
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


def asleep(hour: int, bedtime: str) -> bool:
    """Whether `hour` falls in a bedtime like "23-7" (from 23:00 to 7:00)."""
    start, end = (int(h) for h in bedtime.split("-"))
    return start <= hour < end if start < end else hour >= start or hour < end


@dataclass(frozen=True)
class Result:
    won: bool
    line: str                          # "GS 118-110 MEM", the picked team first
    color: tuple[int, int, int]        # the team's colour, bright enough for a dark background


def team_result(team: str, past: list[nba.Game], now: datetime | None = None) -> Result | None:
    """The picked team's last game, while it is recent enough to have feelings about."""
    if team == "all" or not past:
        return None
    game = past[-1]
    now = now or datetime.now().astimezone()
    if now - game.when > timedelta(hours=nba.GAME_HOURS + RESULT_HOURS):
        return None
    ours, theirs = (game.home, game.away) if game.home.abbr == team else (game.away, game.home)
    if ours.abbr != team:
        return None
    won = ours.winner or (not theirs.winner and nba._points(ours.score) > nba._points(theirs.score))
    r, g, b = nba._rgb(ours.color, ACCENT)
    if r + g + b < 240:                                  # navy, black, ...: lift it towards white
        r, g, b = (round(c + (255 - c) * 0.55) for c in (r, g, b))
    return Result(won, f"{ours.abbr} {ours.score or 0}-{theirs.score or 0} {theirs.abbr}", (r, g, b))


def pick_state(reactions: list[str], flags: dict[str, bool], result: Result | None) -> str | None:
    """The first switched on reaction that applies, as a pet state; None leaves the pet to its mood."""
    for reaction in reactions:
        if flags.get(reaction):
            if reaction == "nba":
                return "cheering" if result and result.won else "sulking"
            return {"working": "working", "music": "dancing", "night": "sleeping"}[reaction]
    return None


class Debounce:
    """A flag that turns on once seen for `settle` seconds and off after `linger` seconds unseen."""

    def __init__(self, settle: float, linger: float):
        self.settle, self.linger = settle, linger
        self._since: float | None = None
        self._last: float | None = None
        self.on = False

    def update(self, seen: bool, now: float) -> bool:
        if seen:
            if self._last is None or now - self._last > self.linger:
                self._since = now
            self._last = now
        lingering = self._last is not None and now - self._last <= self.linger
        self.on = lingering and (self.on or self._last - self._since >= self.settle)
        return self.on


# ------------------------------------------------------------------ drawing
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


def _pixels(d: ImageDraw.ImageDraw, pattern: tuple[str, ...], x: float, y: float, cell: int, fill) -> None:
    for r, row in enumerate(pattern):
        for c, ch in enumerate(row):
            if ch != ".":
                d.rectangle([x + c * cell, y + r * cell, x + (c + 1) * cell - 1, y + (r + 1) * cell - 1], fill=fill)


@dataclass(frozen=True)
class Pose:
    dx: int = 0
    dy: int = 0
    eyes: str = "open"                         # open | closed | up | right
    arms: tuple[int, int] = (0, 0)             # rows the left / right arm is raised (negative: lowered)
    lift: tuple[int, ...] = ()                 # columns of the feet that are off the ground
    squash: bool = False                       # top row missing: breathing out, slumped
    body: tuple[int, int, int] = field(default=BODY)


def _draw_pet(d: ImageDraw.ImageDraw, x: int, y: int, pose: Pose) -> None:
    def cell(c, r, colour):
        d.rectangle([x + c * CELL, y + r * CELL, x + (c + 1) * CELL - 1, y + (r + 1) * CELL - 1], fill=colour)

    eyes = []
    for r, row in enumerate(SPRITE):
        if pose.squash and r == 0:
            continue
        for c, ch in enumerate(row):
            if ch == "." or (ch == "L" and r == 9 and c in pose.lift):
                continue
            if ch == "E":
                eyes.append((c, r))
            cell(c, r - (pose.arms[0 if c < 8 else 1] if ch == "A" else 0), pose.body)
    for c, r in eyes:
        if (pose.eyes == "closed" and r == 2) or (pose.eyes == "up" and r == 3):
            continue                            # closed: only the lower pixel stays dark; up: only the upper
        cell(c + (pose.eyes == "right"), r, EYE)


def _pose(state: str, f: int, body: tuple[int, int, int]) -> Pose:
    if state == "exhausted":
        return Pose(eyes="closed", body=BODY_TIRED)
    if state == "sleeping":
        return Pose(eyes="closed", squash=f >= 4, arms=(-1, -1), body=tuple(round(c * 0.8) for c in body))
    if state == "sulking":
        return Pose(dy=(0, 0, 2, 2, 4, 4, 2, 2)[f], eyes="closed", squash=True, arms=(-1, -1), body=body)
    if state == "working":                     # typing: the arms take turns, the eyes are on the terminal
        return Pose(dy=(0, -3)[f % 2], eyes="closed" if f == 6 else "up", arms=(1, 0) if f % 2 else (0, 1), body=body)
    if state == "dancing":
        dx = (0, -10, -16, -10, 0, 10, 16, 10)[f]
        arms = (2, 0) if f % 4 < 2 else (0, 2)
        return Pose(dx=dx, dy=(0, -12)[f % 2], eyes="closed" if f in (3, 7) else "open", arms=arms,
                    lift=(2, 11) if f % 2 else (4, 13), body=body)
    if state == "cheering":
        dy = (0, -24, -44, -52, -44, -24, 0, 0)[f]
        return Pose(dy=dy, eyes="closed" if dy <= -44 else "open", arms=(2, 2) if dy else (1, 1), body=body)
    # the moods: a gentle bob, a blink, and a wave when happy
    step = f in (1, 2, 5, 6)
    return Pose(dy=(0, -8, -14, -8, 0, -8, -14, -8)[f], eyes="closed" if f == 4 else "open",
                arms=(0, 2) if state == "happy" and f in (5, 6) else (0, 0),
                lift=(2, 11) if step else (4, 13) if f in (3, 7) else (), body=body)


def _sparkle(d: ImageDraw.ImageDraw, x: int, y: int, size: int, colour=SPARK) -> None:
    d.rectangle([x - size * 2, y - size // 2, x + size * 2, y + size // 2], fill=colour)
    d.rectangle([x - size // 2, y - size * 2, x + size // 2, y + size * 2], fill=colour)


def _bg_at(state: str, y: int) -> tuple[int, int, int]:
    top, bottom = BACKGROUNDS.get(state, (BG_TOP, BG_BOTTOM))
    return tuple(round(top[c] + (bottom[c] - top[c]) * y / (H - 1)) for c in range(3))


def _night_sky(d: ImageDraw.ImageDraw, f: int) -> None:
    mx, my, r = 92, 86, 36
    d.ellipse([mx - r, my - r, mx + r, my + r], fill=MOON)
    d.ellipse([mx - r + 18, my - r - 10, mx + r + 18, my + r - 10], fill=_bg_at("sleeping", my))
    for i, (sx, sy) in enumerate(((190, 44), (262, 104), (330, 36), (446, 82), (40, 196), (452, 214), (30, 372), (446, 400))):
        if (f + i * 3) % 8 < 2:
            _sparkle(d, sx, sy, 2, (236, 232, 255))
        else:
            d.rectangle([sx - 2, sy - 2, sx + 1, sy + 1], fill=(150, 156, 196))


def _zeds(d: ImageDraw.ImageDraw, px: int, py: int, f: int) -> None:
    for i in range(3):
        phase = (f + i * 3) % 8                  # three Zs drifting up and to the right, growing
        pixel_text(d, "Z", px + SPRITE_W - 44 + phase * 7, py - 16 - phase * 14, 2 + phase // 3, ZED)


def _terminal(d: ImageDraw.ImageDraw, f: int) -> None:
    x0, y0, x1, y1 = 120, 18, 360, 126
    d.rounded_rectangle([x0, y0, x1, y1], radius=10, fill=SCREEN, outline=(70, 60, 56), width=2)
    for i, colour in enumerate(((236, 96, 96), (240, 180, 70), (110, 196, 120))):
        d.ellipse([x0 + 12 + i * 16, y0 + 9, x0 + 21 + i * 16, y0 + 18], fill=colour)
    # Claude's spinner, then lines of "text" typed out over the loop
    sx, sy = x0 + 22, y0 + 44
    if f % 2:
        d.rectangle([sx - 7, sy - 1, sx + 7, sy + 1], fill=ACCENT)
        d.rectangle([sx - 1, sy - 7, sx + 1, sy + 7], fill=ACCENT)
    else:
        d.line([sx - 6, sy - 6, sx + 6, sy + 6], fill=ACCENT, width=3)
        d.line([sx - 6, sy + 6, sx + 6, sy - 6], fill=ACCENT, width=3)
    tokens = ((0, 0, 70), (0, 80, 54), (1, 0, 40), (1, 50, 96), (2, 0, 84), (2, 94, 30), (3, 0, 56), (3, 66, 60))
    cursor = (0, 0)
    for line, start, width in tokens[: f + 1]:
        tx, ty = x0 + 40 + start, y0 + 40 + line * 18
        d.rectangle([tx, ty, tx + width, ty + 7], fill=TEXT if line == 0 else MUTED)
        cursor = (tx + width + 8, ty)
    if f % 2 == 0:
        d.rectangle([cursor[0], cursor[1] - 3, cursor[0] + 7, cursor[1] + 10], fill=ACCENT)


def _notes(d: ImageDraw.ImageDraw, f: int) -> None:
    span = 280                                   # risen per loop, so the loop repeats seamlessly
    for x, colour, phase in ((14, SPARK, 0), (428, SWEAT, 70), (14, (240, 140, 200), 140), (428, SPARK, 210)):
        rise = (f * span // 8 + phase) % span
        _pixels(d, NOTE, x + (6 if (f * 2 + phase // 70) % 4 < 2 else 0), 390 - rise, 7, colour)


def _confetti(d: ImageDraw.ImageDraw, f: int, team: tuple[int, int, int]) -> None:
    rng = random.Random(9)
    for _ in range(30):
        span = rng.choice((360, 270, 180))            # falls a whole span per loop, so it repeats seamlessly
        x, y0 = rng.randrange(10, W - 10), rng.randrange(0, span)
        colour = rng.choice((team, team, TEXT, SPARK, ACCENT))
        y = 10 + (y0 + f * span / 8) % span           # stays above the score line
        w, h = (10, 6) if (f + x) % 2 else (6, 10)
        d.rectangle([x, y, x + w, y + h], fill=colour)


def _rain(d: ImageDraw.ImageDraw, f: int) -> None:
    cx = W // 2
    for dx, dy, r in ((-58, 70, 30), (0, 54, 42), (56, 72, 30)):
        d.ellipse([cx + dx - r, dy - r, cx + dx + r, dy + r], fill=CLOUD)
    d.rounded_rectangle([cx - 88, 64, cx + 86, 102], radius=16, fill=CLOUD)
    for i, x in enumerate(range(cx - 66, cx + 67, 33)):
        y = 112 + (f * 10 + i * 17) % 50
        d.rectangle([x, y, x + 3, y + 11], fill=SWEAT)


def render_frame(frame: int, limits: dict | None, state: str | None = None, result: Result | None = None) -> Image.Image:
    """One frame of the pet. `state` is an activity (working, dancing, sleeping, cheering, sulking);
    without one the pet shows the mood of the fullest plan limit."""
    windows = [(label, limits[key][0]) for key, label in (("five_hour", "5H"), ("seven_day", "WEEK")) if limits and key in limits]
    worst = max((p for _, p in windows), default=None)
    mood = mood_for(worst)
    state = state or mood
    img = render.vertical_gradient(*BACKGROUNDS.get(state, (BG_TOP, BG_BOTTOM)))
    d = ImageDraw.Draw(img)

    pose = _pose(state, frame, BODY_TIRED if mood == "exhausted" else BODY)
    px, py = (W - SPRITE_W) // 2 + pose.dx, BASE_Y + pose.dy
    ground = BASE_Y + SPRITE_H + 14
    shrink = min(-pose.dy, 50) if pose.dy < 0 else 0      # the shadow tightens as the pet lifts off
    d.ellipse([px + 40 + shrink, ground, px + SPRITE_W - 40 - shrink, ground + 16], fill=(12, 10, 10))

    if state == "sleeping":
        _night_sky(d, frame)
    elif state == "working":
        _terminal(d, frame)
    elif state == "sulking":
        _rain(d, frame)
    _draw_pet(d, px, py, pose)

    if state == "happy":
        for i, (sx, sy) in enumerate(((70, 120), (410, 150), (96, 330), (398, 300))):
            if (frame + i * 2) % 8 < 3:
                _sparkle(d, sx, sy, 4 if (frame + i) % 2 else 3)
    elif state == "stressed" or (state == "working" and mood in ("stressed", "exhausted")):
        drop = (frame % 4) * 12
        sx, sy = px + SPRITE_W - 16, py - 6 + drop
        d.polygon([(sx, sy - 14), (sx - 8, sy + 2), (sx + 8, sy + 2)], fill=SWEAT)
        d.ellipse([sx - 8, sy - 6, sx + 8, sy + 10], fill=SWEAT)
    elif state == "exhausted":
        rise = frame * 5
        pixel_text(d, "Z", px + SPRITE_W - 30, py - 30 - rise, 4, ZED)
        pixel_text(d, "Z", px + SPRITE_W + 6, py - 74 - rise, 3, ZED)
    elif state == "sleeping":
        _zeds(d, px, py, frame)
    elif state == "dancing":
        _notes(d, frame)
    elif state == "cheering" and result:
        _confetti(d, frame, result.color)

    if result and state in ("cheering", "sulking"):
        cell = 4 if len(result.line) * 24 <= W - 30 else 3
        pixel_text(d, result.line, W / 2, 398, cell, result.color if result.won else MUTED, "m")

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


def render_animation(limits: dict | None, state: str | None = None, result: Result | None = None) -> Animation:
    return Animation([render_frame(i, limits, state, result) for i in range(FRAMES)], LOOP_MS)


DOING = {"working": "Working along", "dancing": "Dancing", "sleeping": "Asleep"}


class Tamagotchi(Mode):
    id = "tamagotchi"
    title = "Claude Pet"
    description = "A pixel pet with moods"
    glyph = ""
    claims = "Claude busy"
    reads = ("nba",)               # the team to cheer for
    settings = (
        Setting("reacts", "Reacts to", "order", DEFAULT_REACTIONS, REACTIONS,
                hint="Drag to reorder, untick to ignore. The first one that applies decides what the pet does; "
                     "otherwise its mood follows your plan limits. NBA results are for the team picked on the NBA "
                     "page and last 12 hours after the final."),
        Setting("bedtime", "Bedtime", "choice", "23-7",
                (("22-6", "22:00 to 6:00"), ("23-7", "23:00 to 7:00"), ("0-8", "0:00 to 8:00"), ("1-9", "1:00 to 9:00"))),
        Setting("refresh", "Check limits every", "choice", "15", (("5", "5 minutes"), ("15", "15 minutes"), ("30", "30 minutes"), ("60", "1 hour")),
                hint="Each state is an 8-frame animation stored on the cooler, rewritten only when the state, a "
                     "percentage or a score changes. Limits come from the claude-hud usage snapshot, like the "
                     "Claude Code mode."),
    )

    def __init__(self, values=None, team: str = "all"):
        super().__init__(values)
        self.reactions = [r for r, on in parse_order(self.values["reacts"], [r for r, _ in REACTIONS]) if on]
        self.team = team
        self.snapshot = config_dir() / SNAPSHOT_NAME
        self.placeholder = ("Claude Pet", "Waking up…")
        self._activity = Activity()
        self._playback = Playback()
        self._music = Debounce(MUSIC_SETTLE, MUSIC_LINGER)
        self._next_limits = self._next_music = self._next_nba = 0.0
        self._limits: dict | None = None
        self._past: list[nba.Game] = []
        self._shown = None

    @classmethod
    def build(cls, settings) -> Tamagotchi:
        return cls(settings(cls.id), team=settings(nba.Nba.id).get("team", "all"))

    async def _music_on(self, now: float) -> bool:
        if now >= self._next_music:
            self._next_music = now + MUSIC_CHECK
            self._music.update(await self._playback.playing(), now)
        return self._music.on

    async def _result(self, now: float) -> Result | None:
        if self.team == "all":
            return None
        if now >= self._next_nba:
            try:
                self._past, _ = await asyncio.to_thread(lambda: nba.pick(nba.fetch_games(self.team)))
                self._next_nba = now + NBA_CHECK
            except (OSError, ValueError):
                self._next_nba = now + NBA_RETRY
        return team_result(self.team, self._past)

    async def frame(self):
        now = time.monotonic()
        busy = await self._activity.busy()
        self.active = busy
        if now >= self._next_limits:
            self._next_limits = now + int(self.values["refresh"]) * 60
            self._limits = await asyncio.to_thread(read_limits, self.snapshot)
        result = await self._result(now) if "nba" in self.reactions else None
        flags = {
            "working": busy,
            "music": "music" in self.reactions and await self._music_on(now),
            "night": asleep(datetime.now().hour, self.values["bedtime"]),
            "nba": result is not None,
        }
        state = pick_state(self.reactions, flags, result)

        shown = {k: v[0] for k, v in (self._limits or {}).items()}
        parts = [f"{name} {shown[key]}%" for key, name in (("five_hour", "5-hour"), ("seven_day", "weekly")) if key in shown]
        if state in DOING:
            doing = DOING[state]
        elif state:
            doing = f"Celebrating {result.line}" if state == "cheering" else f"Sulking after {result.line}"
        else:
            doing = f"Feeling {mood_for(max(shown.values(), default=None))}"
        self.status = doing + (" · " + ", ".join(parts) if parts else " · no limits reported")

        key = (state, result.line if state in ("cheering", "sulking") else None, tuple(sorted(shown.items())))
        if key == self._shown:
            return None
        self._shown = key
        return await asyncio.to_thread(render_animation, self._limits, state, result)
