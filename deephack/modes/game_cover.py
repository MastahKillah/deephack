"""The cover art of the game you are playing, for games started through Steam.

Steam records the running game in the registry (HKCU\\Software\\Valve\\Steam\\RunningAppID), so detecting
it is a registry read. The portrait cover comes from Steam's image server (cached in
%APPDATA%\\deephack\\cache\\steam), with Steam's own local library cache as a fallback when offline.

On DeepCool's own firmware the cover is drawn once per game: a picture is a write to the cooler's flash.
With the direct drawing firmware (`direct`, set by the engine) a picture costs nothing, and while the game
runs a strip at the bottom shows the CPU's and the GPU's temperature and load, live, once a second (issue
#37): the cooler is the one screen still in view during a full-screen game. The readings are the System
mode's (system.READINGS, DeepCool's sensor service). Only the strip is sent then (base.Part), 64 of the
640 rows. With firmware patch 16 the pump head's lights can be the same two temperatures, as meters: the
CPU's up the left side, the GPU's up the right. Both end when the game does: the cover alone is left.
"""
from __future__ import annotations

import asyncio
import io
import json
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from .. import light, render
from ..config import config_dir
from ..render import H, W
from .base import Mode, Part, Setting
from .system import CPU_C, CPU_LIMITS, GPU_C, GPU_LIMITS, MEM_C, READINGS, TEXT, Reading, temp_colour

STEAM_KEY = r"Software\Valve\Steam"
CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps/{app}/{name}"
COVERS = ("library_600x900_2x.jpg", "library_600x900.jpg")
STORE_API = "https://store.steampowered.com/api/appdetails?appids={app}&filters=basic"
POLL_SECONDS = 2.0
SETTLE_SECONDS = 6.0      # launchers flash through app ids; only draw a game that stays running this long
RETRY_SECONDS = 300
MAX_BYTES = 8_000_000

STRIP_H = 64                       # the readings' strip, along the bottom, as wide as the screen: both multiples
STRIP_TOP = H - STRIP_H            # of 16, so that a cooler that can is sent these rows alone (base.Part)
STRIP_SMALL = (196, 201, 216)
LIGHT_COOL = 30.0                  # °C at which a side's meter is down to its first light; at "hot" it is full
LIGHT_POWER = 0.7                  # how bright the meters are, of what the lights can
LIGHT_FADE = 0.8                   # seconds: a temperature moves slowly, and so do its lights
LIGHT_SETTLE = 0.3                 # ... which take this much of the way to each new reading: a CPU's temperature
                                   # jumps some degrees up and down every second, and a meter's top light with it
LIGHT_ALARM = (255, 0, 0)          # a side whose temperature is hot: all of it this colour, brighter and dimmer
LIGHT_SCALE = ((0.0, (40, 110, 255)), (0.3, (0, 200, 255)), (0.5, (0, 230, 120)), (0.7, (240, 220, 0)),
               (0.85, (255, 120, 0)), (1.0, (255, 30, 0)))     # a meter's colour by how full it is: cool to hot


def _registry(path: str, value: str):
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return winreg.QueryValueEx(key, value)[0]
    except OSError:
        return None


def running_app() -> int:
    """Steam app id of the running game, 0 when none (or Steam is not installed)."""
    try:
        return int(_registry(STEAM_KEY, "RunningAppID") or 0)
    except (TypeError, ValueError):
        return 0


def app_name(app: int) -> str | None:
    name = _registry(rf"{STEAM_KEY}\Apps\{app}", "Name")
    return name if isinstance(name, str) and name.strip() else None


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "deephack"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return r.read(MAX_BYTES)


def _local_cover(app: int) -> Path | None:
    """Steam's own library cache: flat files in older clients, a folder per app (sometimes nested) in newer ones."""
    steam = _registry(STEAM_KEY, "SteamPath")
    if not isinstance(steam, str):
        return None
    cache = Path(steam) / "appcache" / "librarycache"
    found = sorted(cache.glob(f"{app}/**/library_600x900*.jpg")) + sorted(cache.glob(f"{app}_library_600x900*.jpg"))
    return max(found, key=lambda p: p.stat().st_size, default=None)


def cover(app: int) -> Image.Image | None:
    """Portrait cover of a Steam app, from the download cache, Steam's image server or Steam's local cache."""
    cached = config_dir() / "cache" / "steam" / f"{app}.jpg"
    if not cached.exists():
        for name in COVERS:
            try:
                data = _download(CDN.format(app=app, name=name))
                Image.open(io.BytesIO(data)).verify()
            except (OSError, ValueError, SyntaxError):
                continue
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(data)
            break
    for path in (cached, _local_cover(app)):
        if path is not None and path.exists():
            try:
                return Image.open(path).convert("RGB")
            except OSError:
                continue
    return None


def store_name(app: int) -> str | None:
    try:
        data = json.loads(_download(STORE_API.format(app=app)))
        return data[str(app)]["data"]["name"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _name_card(name: str) -> Image.Image:
    """Stand-in cover for games without art: the name on a plain card."""
    card = render.vertical_gradient((52, 58, 92), (18, 20, 32), (600, 900))
    d = ImageDraw.Draw(card)
    lines = render.wrap(d, name, render.font(render.BOLD, 64), 520, 5)
    y = 450 - len(lines) * 40
    for line in lines:
        d.text((300, y), line, font=render.font(render.BOLD, 64), fill=(236, 238, 246), anchor="ma")
        y += 80
    return card


def _band(canvas: Image.Image) -> None:
    """The readings' strip, empty: what is under it blurred and darkened, so that numbers read on any cover."""
    box = (0, STRIP_TOP, W, H)
    under = canvas.crop(box).filter(ImageFilter.GaussianBlur(8))
    canvas.paste(Image.blend(under, Image.new("RGB", under.size, (6, 7, 12)), 0.66), box)
    ImageDraw.Draw(canvas, "RGBA").line([(0, STRIP_TOP), (W, STRIP_TOP)], fill=(255, 255, 255, 34))


def render_cover(name: str, art: Image.Image | None, layout: str = "full", strip: bool = False) -> Image.Image:
    """`strip`: with room for the readings along the bottom (draw_strip fills it in)."""
    if art is None:
        art = _name_card(name)
    bg = ImageOps.fit(art, (W // 4, H // 4), Image.BILINEAR).filter(ImageFilter.GaussianBlur(7)).resize((W, H), Image.BILINEAR)
    canvas = Image.blend(bg, Image.new("RGB", (W, H)), 0.5)

    if layout == "art":
        fg = ImageOps.contain(art, (W, H), Image.LANCZOS)
        canvas.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
        if strip:
            _band(canvas)
        return canvas

    if strip:                      # the cover a little smaller, the name under it, and the strip in the last line's place
        cw, ch = 320, 480
        x, top = (W - cw) // 2, 20
        _band(canvas)
    else:
        cw, ch = 340, 510
        x, top = (W - cw) // 2, 26
    fg = ImageOps.fit(art, (cw, ch), Image.LANCZOS)
    mask = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, cw - 1, ch - 1], radius=18, fill=255)
    shadow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shadow).rounded_rectangle([x, top + 8, x + cw, top + ch + 8], radius=18, fill=170)
    canvas.paste((0, 0, 0), (0, 0), shadow.filter(ImageFilter.GaussianBlur(14)))
    canvas.paste(fg, (x, top), mask)

    d = ImageDraw.Draw(canvas)
    title = render.font(render.BOLD, 30)
    if strip:
        d.text((W / 2, top + ch + 14), render.fit(d, name, title, W - 48), font=title, fill=(255, 255, 255), anchor="ma")
        return canvas
    d.text((W / 2, top + ch + 22), render.fit(d, name, title, W - 48), font=title, fill=(255, 255, 255), anchor="ma")
    d.text((W / 2, top + ch + 66), "Playing on Steam", font=render.font(render.REGULAR, 19), fill=(200, 204, 216), anchor="ma")
    return canvas


def strip_cells(r: Reading) -> list[tuple]:
    """What the strip shows, left to right: (name, its colour, the large number, that one's colour, the
    small one, how full the bar under them is, 0 to 100). The CPU and the GPU, each with its temperature
    and its load; without DeepCool's sensor service there are no temperatures, and Windows knows of no
    GPU: the CPU's load then, and the memory's."""
    cells = []
    for name, colour, temp, load, limits in (("CPU", CPU_C, r.cpu_temp, r.cpu_load, CPU_LIMITS),
                                             ("GPU", GPU_C, r.gpu_temp, r.gpu_load, GPU_LIMITS)):
        if temp is not None:
            cells.append((name, colour, f"{temp:.0f}°", temp_colour(temp, *limits), "" if load is None else f"{load:.0f}%", load))
        elif load is not None:
            cells.append((name, colour, f"{load:.0f}%", TEXT, "load", load))
    if len(cells) < 2 and r.mem_load is not None:
        cells.append(("RAM", MEM_C, f"{r.mem_load:.0f}%", TEXT, "used", r.mem_load))
    return cells


def draw_strip(page: Image.Image, reading: Reading) -> Part:
    """The readings on a page that has room for them (render_cover with `strip`): the strip alone is new."""
    out = page.crop((0, STRIP_TOP, W, H))
    d = ImageDraw.Draw(out, "RGBA")
    cells = strip_cells(reading)
    name_font, big, small = render.font(render.BOLD, 16), render.font(render.BOLD, 32), render.font(render.SEMIBOLD, 19)
    for i, (name, colour, value, value_colour, more, level) in enumerate(cells):
        x0, x1 = round(W * i / len(cells)) + 22, round(W * (i + 1) / len(cells)) - 22
        d.text((x0, 25), name, font=name_font, fill=colour, anchor="lm")
        d.text((x0 + 48, 24), value, font=big, fill=value_colour, anchor="lm")
        d.text((x1, 26), more, font=small, fill=STRIP_SMALL, anchor="rm")
        d.rounded_rectangle([x0, 47, x1, 51], radius=2, fill=(255, 255, 255, 38))
        if level:
            d.rounded_rectangle([x0, 47, x0 + max(4, round((x1 - x0) * min(level, 100) / 100)), 51], radius=2, fill=colour)
    return Part(page, out, (0, STRIP_TOP))


def _scale(share: float) -> tuple[float, float, float]:
    """A meter's colour when it is this full (0 to 1): LIGHT_SCALE, with the steps between its stops."""
    share = max(0.0, min(1.0, share))
    for (a, low), (b, high) in zip(LIGHT_SCALE, LIGHT_SCALE[1:]):
        if share <= b:
            return tuple(x + (y - x) * (share - a) / (b - a) for x, y in zip(low, high))
    return LIGHT_SCALE[-1][1]


def temperature_lights(reading: Reading, beat: bool = False) -> list | None:
    """The pump head's lights as two temperatures (firmware patch 16): the CPU's up the left side, the GPU's
    up the right. A side is a meter, one light at LIGHT_COOL and all seven at the temperature the System
    page calls hot, and all of one colour, which says the same: blue, green, yellow, red. From hot on the
    whole side is red, brighter on every other reading (`beat`) and dimmer on the ones between. With one
    of the two temperatures missing both sides show the other; with neither, None: the cover's own edge."""
    sides = []
    for temp, (_, hot) in ((reading.cpu_temp, CPU_LIMITS), (reading.gpu_temp, GPU_LIMITS)):
        if temp is None:
            sides.append(None)
        elif temp >= hot:
            sides.append([light.led(LIGHT_ALARM, 1.0 if beat else 0.3)] * light.SIDE)
        else:
            share = max(1 / light.SIDE, (temp - LIGHT_COOL) / (hot - LIGHT_COOL))
            sides.append(light.meter(share, [_scale(share)] * light.SIDE, LIGHT_POWER))
    if sides[0] is None and sides[1] is None:
        return None
    return light.arrange(sides[0] or sides[1], sides[1] or sides[0])


def settled(last: Reading | None, reading: Reading) -> Reading:
    """The reading's two temperatures, each only LIGHT_SETTLE of the way from the last ones here: for the
    lights (the strip shows the numbers as they are)."""
    def step(old, new):
        return new if old is None or new is None else old + (new - old) * LIGHT_SETTLE

    if last is None:
        return reading
    return replace(reading, cpu_temp=step(last.cpu_temp, reading.cpu_temp), gpu_temp=step(last.gpu_temp, reading.gpu_temp))


def readings_line(r: Reading) -> str:
    """For the window's status line, after the game's name."""
    return " · ".join(f"{name} {value}" for name, _, value, _, _, _ in strip_cells(r))


class GameCover(Mode):
    id = "game_cover"
    title = "Game Cover"
    description = "Art of the Steam game you play"
    glyph = ""
    claims = "game running"
    settings = (
        Setting("layout", "Layout", "choice", "full", (("full", "Cover with the game's name"), ("art", "Cover only")),
                hint="Detects games started through Steam. Covers come from Steam's image server and are cached."),
        Setting("readings", "While you play", "choice", "on",
                (("on", "Temperatures and load, live"), ("off", "The cover alone")),
                hint="A strip along the bottom of the cover with the CPU's and the GPU's temperature and load, new "
                     "every second while the game runs. Needs the direct drawing firmware. The readings come from "
                     "DeepCool's sensor service, like the System mode's; without it the strip shows the CPU's load "
                     "and the memory in use."),
        Setting("lights", "Lights while you play", "choice", "temperature",
                (("temperature", "Two temperature meters"), ("cover", "The cover's colours")),
                hint="The CPU's temperature up the left side of the pump head, the GPU's up the right: one light at "
                     "30 °C and all seven when it is hot, from blue through green and yellow to red, and red that "
                     "pulses from there. Needs the direct drawing firmware from 16 on, and the cooler's light set to "
                     "follow the screen."),
    )

    def __init__(self, values=None, feed=None):
        super().__init__(values)
        self.feed = feed or READINGS
        self._next_poll = 0.0
        self._pending, self._pending_since = 0, 0.0
        self._shown = None
        self._failed: dict[int, float] = {}      # app id -> when to try fetching its cover again
        self._cover: tuple[str, Image.Image | None] | None = None      # the game on screen: its name and its art
        self._title = ""                         # ... and what the status line says of it
        self._playing = False                    # that game runs right now
        self._page: Image.Image | None = None    # the picture last drawn of it, without readings
        self._stripped = False                   # ... which has room for them
        self._read_at: float | None = None       # time of the reading that the strip was last drawn from
        self._settled: Reading | None = None     # the temperatures as the lights have them (settled)
        self._beat = False
        self.lights_fade = LIGHT_FADE
        self.placeholder = ("No game running", "Start a game from Steam and its cover shows up here.")

    def _load(self, app: int) -> tuple[str, Image.Image | None]:
        art = cover(app)
        name = app_name(app) or store_name(app) or f"Steam app {app}"
        return name, art

    async def _look(self, now: float) -> bool:
        """Which game runs. True when there is a cover to draw: another game's, or this one's, found at last."""
        app = await asyncio.to_thread(running_app)
        self._playing = False
        if not app:
            self._pending = 0
            self.active = False
            if self._shown is None:
                self.status = "No game running"
            return False
        if app != self._pending:
            self._pending, self._pending_since = app, now
        self.active = now - self._pending_since >= SETTLE_SECONDS
        key = (app, self.values["layout"])
        if self._shown and self._shown[:2] == key and (self._shown[2] or now < self._failed.get(app, 0.0)):
            self._playing = True
            return False                                 # drawn already (without a cover: until the next retry)
        if now - self._pending_since < SETTLE_SECONDS:
            return False
        name, art = await asyncio.to_thread(self._load, app)
        self._title = name + ("" if art is not None else " · no cover found")
        self._playing = True
        if art is None:
            self._failed[app] = now + RETRY_SECONDS      # draw the name for now, look for a cover again later
            if self._shown == key + (False,):
                return False                             # still no cover: the same picture is on screen already
        self._shown, self._cover = key + (art is not None,), (name, art)
        return True

    async def frame(self):
        now = time.monotonic()
        fresh = False
        if now >= self._next_poll:
            self._next_poll = now + POLL_SECONDS
            fresh = await self._look(now)
        if self._cover is None:
            return None
        live = self.direct and self._playing             # pictures cost nothing, and there is a game to read for
        strip = live and self.values["readings"] == "on"
        meters = live and self.values["lights"] == "temperature"
        reading, new = None, False
        if strip or meters:
            got = self.feed.latest()
            if got is not None:
                reading, new = got[1], got[0] != self._read_at
                self._read_at = got[0]
                self._beat ^= new
                if new or self._settled is None:
                    self._settled = settled(self._settled, reading)
        if reading is None:
            self._settled = None                         # the next game's lights start from its own first reading
        self.lights = temperature_lights(self._settled, self._beat) if meters and reading is not None else None
        self.status = self._title + (f" · {readings_line(reading)}" if reading is not None else "")
        if fresh or strip != self._stripped:             # another cover, or the same with or without the strip
            self._page = await asyncio.to_thread(render_cover, *self._cover, self.values["layout"], strip)
            self._stripped = strip
            return draw_strip(self._page, reading) if strip and reading is not None else self._page
        if strip and new:
            return draw_strip(self._page, reading)
        return None
