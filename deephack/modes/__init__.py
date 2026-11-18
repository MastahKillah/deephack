"""Display modes. To add one: subclass Mode in a new module and append it to MODES."""
from .base import Animation, Mode, Setting
from .claude_usage import ClaudeUsage
from .nba import Nba
from .now_playing import NowPlaying
from .tamagotchi import Tamagotchi
from .weather import Weather

MODES: tuple[type[Mode], ...] = (NowPlaying, Weather, Nba, ClaudeUsage, Tamagotchi)
BY_ID = {m.id: m for m in MODES}

__all__ = ["Animation", "Mode", "Setting", "MODES", "BY_ID"]
