"""Display modes. To add one: subclass Mode in a new module and append it to MODES."""
from .auto import Auto
from .base import Animation, Mode, Setting
from .claude_usage import ClaudeUsage
from .countdown import Countdown
from .game_cover import GameCover
from .nba import Nba
from .now_playing import NowPlaying
from .photo_frame import PhotoFrame
from .tamagotchi import Tamagotchi
from .ticker import Ticker
from .weather import Weather

MODES: tuple[type[Mode], ...] = (Auto, NowPlaying, Weather, Nba, ClaudeUsage, Tamagotchi, Countdown, PhotoFrame, GameCover, Ticker)
BY_ID = {m.id: m for m in MODES}

__all__ = ["Animation", "Mode", "Setting", "MODES", "BY_ID"]
