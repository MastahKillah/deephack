"""Display modes. To add one: subclass Mode in a new module and append it to MODES."""
from .base import Mode, Setting
from .claude_usage import ClaudeUsage
from .now_playing import NowPlaying
from .weather import Weather

MODES: tuple[type[Mode], ...] = (NowPlaying, Weather, ClaudeUsage)
BY_ID = {m.id: m for m in MODES}

__all__ = ["Mode", "Setting", "MODES", "BY_ID"]
