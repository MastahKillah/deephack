"""Auto: shows whichever mode has something worth showing right now.

Every mode in the rotation keeps running in the background, each with its own settings. The first one
in the user's order whose `active` flag is set gets the screen (music playing, a Steam game running,
the picked NBA team on court, ...); when none is, the fallback mode does.

On DeepCool's own firmware every switch can be a flash write, so a mode keeps the screen for LINGER
seconds after it goes quiet: pausing a song or a short break between games does not flip the display
back and forth. Switching back to an image the cooler already holds costs no write at all (the engine
compares what it stored).

With the direct drawing firmware (`direct`, set by the engine) a switch costs nothing. A mode gives the
screen up LINGER_DRAWN seconds after it went quiet, which is only there against flicker, and one mode's
picture fades into the next one's. Not to or from the cooler's own sensor screens (Live): that is a
change of screen on the cooler, with no picture of ours to fade.
"""
from __future__ import annotations

import time
import traceback

from PIL import Image

from .agenda import Calendar
from .base import Animation, Mode, Setting, parse_order
from .claude_usage import ClaudeUsage
from .countdown import Countdown
from .live import Live
from .game_cover import GameCover
from .github import GitHub
from .nba import Nba
from .now_playing import NowPlaying
from .photo_frame import PhotoFrame
from .scene import Scene
from .system import SystemMonitor
from .tamagotchi import Tamagotchi
from .ticker import Ticker
from .weather import Weather

CANDIDATES: tuple[type[Mode], ...] = (Calendar, GameCover, NowPlaying, Tamagotchi, Nba, Countdown, ClaudeUsage, SystemMonitor, GitHub)
FALLBACKS: tuple[type[Mode], ...] = (Weather, Scene, Live, PhotoFrame, Ticker, SystemMonitor, Countdown, ClaudeUsage, Tamagotchi, Nba, NowPlaying, GameCover, GitHub, Calendar)
DEFAULT_ORDER = "calendar,game_cover,now_playing,tamagotchi,nba,countdown,-claude_usage,-system,-github"
LINGER = 120.0
LINGER_DRAWN = 5.0     # ... when pictures are drawn instead of stored
FADE = 0.45            # seconds one mode's picture takes to fade into the next one's (drawn pictures only)
FADE_TICK = 0.05       # how often frame() is wanted while it fades


class Auto(Mode):
    id = "auto"
    title = "Auto"
    description = "Picks the mode for the moment"
    reads = tuple(dict.fromkeys(i for c in CANDIDATES + FALLBACKS for i in (c.id, *c.reads)))
    glyph = ""
    settings = (
        Setting("order", "Show first", "order", DEFAULT_ORDER, tuple((c.id, f"{c.title} · {c.claims}") for c in CANDIDATES),
                hint="Drag to reorder, untick to leave a mode out. A mode keeps the screen for two minutes after it "
                     "has nothing left to show; with the direct drawing firmware, where a switch costs nothing, for "
                     "five seconds, and one picture fades into the next. Each mode uses the settings on its own page."),
        Setting("fallback", "Otherwise show", "choice", "weather", tuple((c.id, c.title) for c in FALLBACKS)),
    )

    def __init__(self, values=None, child_settings=None):
        super().__init__(values)
        child_settings = child_settings or (lambda mode_id: {})
        by_id = {c.id: c for c in CANDIDATES + FALLBACKS}
        self.order = [mode_id for mode_id, on in parse_order(self.values["order"], [c.id for c in CANDIDATES]) if on]
        self.fallback = self.values["fallback"] if self.values["fallback"] in by_id else Weather.id
        self.children: dict[str, Mode] = {i: by_id[i].build(child_settings) for i in dict.fromkeys(self.order + [self.fallback])}
        self._latest: dict[str, tuple[int, Image.Image | Animation]] = {}   # mode id -> (frame number, last frame)
        self._seen: dict[str, float] = {}                                   # mode id -> when it was last active
        self._sent: tuple[str, int] | None = None
        self._last: Image.Image | None = None          # drawn pictures: the one returned last, to fade from
        self._fade: tuple[Image.Image, float] | None = None    # ... what it fades from, and since when
        self._showing: str | None = None               # ... and whose it was
        self.chosen: str | None = None
        self.placeholder = ("Auto", "Looking for something to show…")

    @classmethod
    def build(cls, settings) -> Auto:
        return cls(settings(cls.id), settings)

    def pick(self, now: float) -> str | None:
        """First mode in the order that is (or was, a moment ago) active and has a frame; else the fallback."""
        linger = LINGER_DRAWN if self.direct else LINGER
        for mode_id in self.order:
            if mode_id in self._latest and now - self._seen.get(mode_id, -linger - 1) <= linger:
                return mode_id
        return self.fallback if self.fallback in self._latest else None

    def _faded(self, img, now: float):
        """`img`, the chosen mode's newest picture, or while another mode's is still fading out, the two mixed.
        The mode fading in keeps moving meanwhile; the one fading out is the picture it left."""
        if not (self.direct and isinstance(img, Image.Image)):
            self._last = self._fade = None             # stored pictures, or a sensor screen: no fades
            return img
        if self._showing not in (None, self.chosen) and self._last is not None and self._last.size == img.size:
            self._fade = (self._last, now - FADE_TICK)     # (so that the first picture is a step on already)
        if self._fade is not None:
            done = (now - self._fade[1]) / FADE
            if done < 1:
                self.tick = min(FADE_TICK, self.tick or FADE_TICK)
                img = Image.blend(self._fade[0], img, done)
            else:
                self._fade = None
        self._last = img
        return img

    async def frame(self):
        now = time.monotonic()
        for mode_id, child in self.children.items():
            child.direct = self.direct
            try:
                img = await child.frame()
            except Exception:
                traceback.print_exc()
                child.status, img = "Mode error, see console", None
            if img is not None:
                self._latest[mode_id] = (self._latest.get(mode_id, (0, None))[0] + 1, img)
            if child.active and mode_id in self.order:
                self._seen[mode_id] = now
        self.chosen = self.pick(now)
        self.tick = self.children[self.chosen].tick if self.chosen else None   # the mode on screen may want
                                                                               # its frames faster for a while
        if self.chosen is None:
            fallback = self.children[self.fallback]
            self.status = f"{fallback.title}: {fallback.status}" if fallback.status else "Looking for something to show"
            self.placeholder = fallback.placeholder or self.placeholder
            return None
        child = self.children[self.chosen]
        self.status = f"{child.title} · {child.status}" if child.status else child.title
        number, img = self._latest[self.chosen]
        fading = self._fade is not None
        if (self.chosen, number) == self._sent and not fading:
            return None
        self._sent = (self.chosen, number)
        img = self._faded(img, now)
        self._showing = self.chosen
        return img

    async def close(self) -> None:
        for child in self.children.values():
            await child.close()
