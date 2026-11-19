"""Auto: shows whichever mode has something worth showing right now.

Every mode in the rotation keeps running in the background, each with its own settings. The first one
in the user's order whose `active` flag is set gets the screen (music playing, a Steam game running,
the picked NBA team on court, ...); when none is, the fallback mode does.

Every switch can be a flash write, so a mode keeps the screen for LINGER seconds after it goes quiet:
pausing a song or a short break between games does not flip the display back and forth. Switching back
to an image the cooler already holds costs no write at all (the engine compares what it stored).
"""
from __future__ import annotations

import time
import traceback

from PIL import Image

from .base import Animation, Mode, Setting, parse_order
from .claude_usage import ClaudeUsage
from .countdown import Countdown
from .game_cover import GameCover
from .nba import Nba
from .now_playing import NowPlaying
from .photo_frame import PhotoFrame
from .tamagotchi import Tamagotchi
from .ticker import Ticker
from .weather import Weather

CANDIDATES: tuple[type[Mode], ...] = (GameCover, NowPlaying, Tamagotchi, Nba, Countdown, ClaudeUsage)
FALLBACKS: tuple[type[Mode], ...] = (Weather, PhotoFrame, Ticker, Countdown, ClaudeUsage, Tamagotchi, Nba, NowPlaying, GameCover)
DEFAULT_ORDER = "game_cover,now_playing,tamagotchi,nba,countdown,-claude_usage"
LINGER = 120.0


class Auto(Mode):
    id = "auto"
    title = "Auto"
    description = "Picks the mode for the moment"
    glyph = ""
    settings = (
        Setting("order", "Show first", "order", DEFAULT_ORDER, tuple((c.id, f"{c.title} · {c.claims}") for c in CANDIDATES),
                hint="Drag to reorder, untick to leave a mode out. A mode keeps the screen for two minutes after it "
                     "has nothing left to show. Each mode uses the settings on its own page."),
        Setting("fallback", "Otherwise show", "choice", "weather", tuple((c.id, c.title) for c in FALLBACKS)),
    )

    def __init__(self, values=None, child_settings=None):
        super().__init__(values)
        child_settings = child_settings or (lambda mode_id: {})
        by_id = {c.id: c for c in CANDIDATES + FALLBACKS}
        self.order = [mode_id for mode_id, on in parse_order(self.values["order"], [c.id for c in CANDIDATES]) if on]
        self.fallback = self.values["fallback"] if self.values["fallback"] in by_id else Weather.id
        self.children: dict[str, Mode] = {i: by_id[i](child_settings(i)) for i in dict.fromkeys(self.order + [self.fallback])}
        self._latest: dict[str, tuple[int, Image.Image | Animation]] = {}   # mode id -> (frame number, last frame)
        self._seen: dict[str, float] = {}                                   # mode id -> when it was last active
        self._sent: tuple[str, int] | None = None
        self.chosen: str | None = None
        self.placeholder = ("Auto", "Looking for something to show…")

    @classmethod
    def build(cls, config) -> Auto:
        return cls(config.mode_settings(cls.id), config.mode_settings)

    def pick(self, now: float) -> str | None:
        """First mode in the order that is (or was, within LINGER) active and has a frame; else the fallback."""
        for mode_id in self.order:
            if mode_id in self._latest and now - self._seen.get(mode_id, -LINGER - 1) <= LINGER:
                return mode_id
        return self.fallback if self.fallback in self._latest else None

    async def frame(self):
        now = time.monotonic()
        for mode_id, child in self.children.items():
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
        if self.chosen is None:
            fallback = self.children[self.fallback]
            self.status = f"{fallback.title}: {fallback.status}" if fallback.status else "Looking for something to show"
            self.placeholder = fallback.placeholder or self.placeholder
            return None
        child = self.children[self.chosen]
        self.status = f"{child.title} · {child.status}" if child.status else child.title
        number, img = self._latest[self.chosen]
        if (self.chosen, number) == self._sent:
            return None
        self._sent = (self.chosen, number)
        return img

    async def close(self) -> None:
        for child in self.children.values():
            await child.close()
