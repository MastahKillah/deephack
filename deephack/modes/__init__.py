"""Display modes. To add one: subclass Mode in a new module and append it to MODES."""
from .agenda import Calendar
from .auto import Auto
from .base import Animation, Dashboard, Mode, Setting
from .claude_usage import ClaudeUsage
from .live import Live
from .countdown import Countdown
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

MODES: tuple[type[Mode], ...] = (Auto, NowPlaying, Weather, Calendar, Nba, ClaudeUsage, Tamagotchi, Countdown, PhotoFrame, GameCover, Ticker, SystemMonitor, Live, GitHub, Scene)
BY_ID = {m.id: m for m in MODES}

__all__ = ["Animation", "Mode", "Setting", "MODES", "BY_ID"]

# Not in the list: visualizer.Visualizer. It works, but a stored picture needs about 0.1 s to appear and
# the whole chain ends up 0.2 to 0.3 s behind the music, which looks wrong on the cooler. It waits for a
# way to draw directly (see the firmware issues).
