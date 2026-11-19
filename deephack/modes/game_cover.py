"""The cover art of the game you are playing, for games started through Steam.

Steam records the running game in the registry (HKCU\\Software\\Valve\\Steam\\RunningAppID), so detecting
it is a registry read. The portrait cover comes from Steam's image server (cached in
%APPDATA%\\deephack\\cache\\steam), with Steam's own local library cache as a fallback when offline.
"""
from __future__ import annotations

import asyncio
import io
import json
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from .. import render
from ..config import config_dir
from ..render import H, W
from .base import Mode, Setting

STEAM_KEY = r"Software\Valve\Steam"
CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps/{app}/{name}"
COVERS = ("library_600x900_2x.jpg", "library_600x900.jpg")
STORE_API = "https://store.steampowered.com/api/appdetails?appids={app}&filters=basic"
POLL_SECONDS = 2.0
SETTLE_SECONDS = 6.0      # launchers flash through app ids; only draw a game that stays running this long
RETRY_SECONDS = 300
MAX_BYTES = 8_000_000


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


def render_cover(name: str, art: Image.Image | None, layout: str = "full") -> Image.Image:
    if art is None:
        art = _name_card(name)
    bg = ImageOps.fit(art, (W // 4, H // 4), Image.BILINEAR).filter(ImageFilter.GaussianBlur(7)).resize((W, H), Image.BILINEAR)
    canvas = Image.blend(bg, Image.new("RGB", (W, H)), 0.5)

    if layout == "art":
        fg = ImageOps.contain(art, (W, H), Image.LANCZOS)
        canvas.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
        return canvas

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
    d.text((W / 2, top + ch + 22), render.fit(d, name, title, W - 48), font=title, fill=(255, 255, 255), anchor="ma")
    d.text((W / 2, top + ch + 66), "Playing on Steam", font=render.font(render.REGULAR, 19), fill=(200, 204, 216), anchor="ma")
    return canvas


class GameCover(Mode):
    id = "game_cover"
    title = "Game Cover"
    description = "Art of the Steam game you play"
    glyph = ""
    claims = "game running"
    settings = (
        Setting("layout", "Layout", "choice", "full", (("full", "Cover with the game's name"), ("art", "Cover only")),
                hint="Detects games started through Steam. Covers come from Steam's image server and are cached."),
    )

    def __init__(self, values=None):
        super().__init__(values)
        self._next_poll = 0.0
        self._pending, self._pending_since = 0, 0.0
        self._shown = None
        self._failed: dict[int, float] = {}      # app id -> when to try fetching its cover again
        self.placeholder = ("No game running", "Start a game from Steam and its cover shows up here.")

    def _load(self, app: int) -> tuple[str, Image.Image | None]:
        art = cover(app)
        name = app_name(app) or store_name(app) or f"Steam app {app}"
        return name, art

    async def frame(self):
        now = time.monotonic()
        if now < self._next_poll:
            return None
        self._next_poll = now + POLL_SECONDS
        app = await asyncio.to_thread(running_app)
        if not app:
            self._pending = 0
            self.active = False
            if self._shown is None:
                self.status = "No game running"
            return None
        if app != self._pending:
            self._pending, self._pending_since = app, now
        self.active = now - self._pending_since >= SETTLE_SECONDS
        key = (app, self.values["layout"])
        if self._shown and self._shown[:2] == key and (self._shown[2] or now < self._failed.get(app, 0.0)):
            return None                                  # drawn already (without a cover: until the next retry)
        if now - self._pending_since < SETTLE_SECONDS:
            return None
        name, art = await asyncio.to_thread(self._load, app)
        self.status = name + ("" if art is not None else " · no cover found")
        if art is None:
            self._failed[app] = now + RETRY_SECONDS      # draw the name for now, look for a cover again later
            if self._shown == key + (False,):
                return None                              # still no cover: the same picture is on screen already
        self._shown = key + (art is not None,)
        return render_cover(name, art, self.values["layout"])
