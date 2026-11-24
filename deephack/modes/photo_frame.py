"""Pictures from a folder, one at a time, changing every so often.

On DeepCool's own firmware every change is a write to the cooler's flash, so the shortest interval there
is 15 minutes, and a photo appears all at once. A cooler that draws from USB (Mode.direct) stores
nothing: there the interval may be seconds, one photo fades into the next (`_fade_step`), and a photo
that is wider or taller than the screen can fill it and move across it while it is up (`_pan_step`).

The current photo and when it went up are remembered in %APPDATA%\\deephack\\photo-frame.json, so
restarting deephack or changing a setting keeps the photo instead of skipping to the next one.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

from ..config import config_dir
from ..render import H, W
from . import video
from .base import Mode, Setting

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
MAX_FILES = 100_000
STATE_NAME = "photo-frame.json"
PORTRAIT_RATIO = 0.9      # "auto" fills the screen with photos narrower than this (width / height), shows wider ones whole
SHORTEST_STORED = 15 * 60 # seconds between two photos at the least, where each one is a write to the cooler's flash
TICK = 0.5                # seconds from one call of frame() to the next when a mode asks for nothing else (engine.TICK)
RESCAN = 60.0             # seconds for which the list of the folder's pictures is good: an interval may be shorter
FADE = 1.0                # seconds one photo takes to fade into the next
FADE_TICK = 0.036         # ... and how often a picture of that is made: every second turn of the cooler's panel, 27 a
                          # second, which leaves each one 30 KB (video.room_for)
PAN_STEP = 0.5            # screen pixels a photo that moves has to have moved before it is drawn again: the slower it
                          # goes, the fewer pictures. A 16:9 photo is 658 pixels wider than the screen: up for an
                          # hour, that is a picture every 2.7 seconds; up for a minute or less, FADE_TICK decides
MAKING = 0.006            # seconds of a tick that go into making a picture, and are not there for sending it
PAN_LEAST = 8             # pixels a photo has to be larger than the screen, scaled to cover it, to be moved at all
FADE_QUALITY = 70         # the JPEG quality a fade begins with. From there it goes by how large the pictures come out
                          # (video.quality_for). Ten of Windows' wallpapers, mixed in pairs: 7 to 24 KB at 70, and
                          # 17 to 48 KB at the 92 of a still. With grain put on them (stills of 73 to 106 KB) the
                          # mixes were 27 to 41 KB and the quality went down to 58; on the cooler either kind of
                          # fade took its second, 27 or 28 pictures (24 Nov 2026)


def scan(folder: Path) -> list[Path]:
    """Every picture under folder (subfolders included), sorted by path so the order is stable."""
    found = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        found.extend(Path(root) / f for f in files if Path(f).suffix.lower() in EXTENSIONS and not f.startswith("."))
        if len(found) >= MAX_FILES:
            break
    return sorted(found, key=lambda p: str(p).lower())


def next_in_order(photos: list[Path], current: Path | None) -> Path:
    """The photo after current in path order, wrapping around; works even if current was deleted."""
    if current is not None:
        key = str(current).lower()
        for p in photos:
            if str(p).lower() > key:
                return p
    return photos[0]


def load(path: Path) -> Image.Image:
    img = Image.open(path)
    img.draft("RGB", (W * 2, H * 2))          # big JPEGs decode at a fraction of their size
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def cover(photo: Image.Image) -> Image.Image:
    """The photo scaled so that it covers the screen, as wide or as high as it and larger the other way, at
    twice the screen's pixels: what `window` cuts its pictures from."""
    scale = max(2 * W / photo.width, 2 * H / photo.height)
    return photo.resize((max(2 * W, round(photo.width * scale)), max(2 * H, round(photo.height * scale))), Image.LANCZOS)


def window(wide: Image.Image, x: float, y: float) -> Image.Image:
    """The screen's worth of a covering photo from (x, y), in screen pixels and halves of them.

    A crop of the photo at twice the size, made half as large: every picture goes through the same
    filter, wherever it is cut. Moving a photo of the screen's own size by parts of a pixel makes it
    sharp at whole pixels and soft in between, and a slow pan would pulse; it also took 12 ms a picture
    (bicubic) where this takes one."""
    left, top = round(2 * x), round(2 * y)
    return wide.crop((left, top, left + 2 * W, top + 2 * H)).reduce(2)


def render_photo(photo: Image.Image, fit: str = "auto") -> Image.Image:
    fill = fit in ("fill", "pan") or (fit == "auto" and photo.width / photo.height < PORTRAIT_RATIO)
    if fill:
        return ImageOps.fit(photo, (W, H), Image.LANCZOS)
    # the whole photo, over a blurred and darkened copy of itself
    bg = ImageOps.fit(photo, (W // 4, H // 4), Image.BILINEAR).filter(ImageFilter.GaussianBlur(6)).resize((W, H), Image.BILINEAR)
    canvas = Image.blend(bg, Image.new("RGB", (W, H)), 0.5)
    fg = ImageOps.contain(photo, (W, H), Image.LANCZOS)
    canvas.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    return canvas


class PhotoFrame(Mode):
    id = "photo_frame"
    title = "Photo Frame"
    description = "Pictures from a folder"
    glyph = ""
    settings = (
        Setting("folder", "Folder", "folder", "", placeholder="Pick a folder with pictures", hint="Subfolders are included."),
        Setting("interval", "Change every", "choice", "60",
                (("0.25", "15 seconds"), ("1", "1 minute"), ("5", "5 minutes"), ("15", "15 minutes"), ("60", "1 hour"),
                 ("360", "6 hours"), ("1440", "Day")),
                hint="On DeepCool's own firmware every change is a write to the cooler's flash: the shortest there "
                     "is 15 minutes."),
        Setting("transition", "Change with", "choice", "fade", (("fade", "A crossfade"), ("cut", "A cut")),
                hint="The crossfade needs the direct drawing firmware."),
        Setting("order", "Order", "choice", "random", (("random", "Random"), ("name", "By file name"))),
        Setting("fit", "Fit", "choice", "auto",
                (("auto", "Auto"), ("fill", "Fill the screen"), ("whole", "Whole photo"), ("pan", "Fill and move across")),
                hint="Auto fills the screen with portrait photos and shows wider ones whole. Fill and move across: a "
                     "photo that is wider or taller than the screen moves slowly from one side to the other while it "
                     "is up (with the direct drawing firmware; without it the photo fills the screen)."),
    )

    def __init__(self, values=None):
        super().__init__(values)
        self.state_path = config_dir() / STATE_NAME
        self._current: Path | None = None
        self._since = 0.0                     # wall clock time the current photo went up
        self._bag: list[Path] = []            # random order: photos not shown yet this round
        self._rendered: tuple | None = None
        self._next_try = 0.0
        self._scanned: tuple[Path, float, list[Path]] | None = None      # the folder, when it was read, its pictures
        self._shown: tuple[Path, Image.Image] | None = None    # the photo last returned, as it is on screen
        self._fade: tuple[Image.Image, Image.Image, float] | None = None     # from, to, and when it began (_clock)
        self._clock = time.monotonic
        self._wall = time.time                # the clock a photo's time on screen goes by: it outlasts deephack
        self._pan: tuple[Image.Image, float, float] | None = None      # the photo covering the screen (`cover`), and by
                                                               # how many screen pixels it is wider and higher: it moves
        self._pan_at: tuple[float, float] | None = None        # ... and from where in it the picture on screen is
        self._reverse = False                 # ... from the far side to the near one: every other photo
        self.placeholder = ("Photo frame", "Pick a folder in the settings on the right.")
        self._restore()

    @property
    def folder(self) -> Path | None:
        text = self.values["folder"].strip().strip('"')
        return Path(os.path.expandvars(os.path.expanduser(text))) if text else None

    def _restore(self) -> None:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf8"))
            current, since = Path(state["path"]), float(state["since"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        folder = self.folder
        if folder is not None and current.is_relative_to(folder):
            self._current, self._since, self._reverse = current, since, bool(state.get("reverse"))

    def _save(self) -> None:
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"path": str(self._current), "since": self._since, "reverse": self._reverse}),
                           encoding="utf8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    def _pick(self, photos: list[Path]) -> Path:
        if self.values["order"] == "name":
            return next_in_order(photos, self._current)
        present = set(photos)
        self._bag = [p for p in self._bag if p in present]
        if not self._bag:
            self._bag = [p for p in photos if p != self._current] or list(photos)
            random.shuffle(self._bag)
        return self._bag.pop()

    def _photos(self, folder: Path) -> list[Path]:
        """The folder's pictures. Read anew at most every RESCAN seconds: with an interval of seconds a large
        folder would be walked all the time."""
        if self._scanned is None or self._scanned[0] != folder or time.monotonic() - self._scanned[1] >= RESCAN:
            self._scanned = (folder, time.monotonic(), scan(folder))
        return list(self._scanned[2])

    def _advance(self, folder: Path) -> tuple[Path, Image.Image] | None:
        """Pick and load the next photo, skipping files that do not open. None if the folder has none."""
        photos = self._photos(folder)
        for _ in range(min(len(photos), 20)):
            path = self._pick(photos)
            try:
                return path, load(path)
            except (OSError, ValueError, Image.DecompressionBombError):
                photos = [p for p in photos if p != path]
                if not photos:
                    break
        return None

    def _show(self, folder: Path, due: bool) -> tuple[Path, Image.Image] | None:
        if not due and self._current is not None:
            try:
                return self._current, load(self._current)
            except (OSError, ValueError, Image.DecompressionBombError):
                pass
        return self._advance(folder)

    def _fade_step(self) -> Image.Image:
        """The next picture of a fade from one photo into the other, by the time that has passed: a picture
        the cooler was too busy for is left out, and the fade is not longer for it. While it runs the mode
        asks to be called every FADE_TICK, and its pictures are JPEGs small enough for the cooler to take
        one in that time. The last one is the new photo itself, as sharp as a still is."""
        before, after, began = self._fade
        done = (self._clock() - began) / FADE
        if done >= 1 or not self.direct:
            self._fade, self.tick, self.quality = None, None, None
            if self._pan is not None and self.direct:
                self.tick = FADE_TICK         # (called again at once, not in half a second: the photo begins to move)
            return after
        self.tick = FADE_TICK
        self.quality = self._quality()
        return Image.blend(before, after, done * done * (3 - 2 * done))      # slow, fast, slow

    def _quality(self) -> int:
        """The JPEG quality of the next of the pictures that come one every `tick`: by how large the last one
        came out, so that the cooler has it in the time that is left of a tick once the picture is made."""
        return video.quality_for(self.quality or FADE_QUALITY, self.sent_bytes, 1 / (self.tick - MAKING), self.fast, self.turn)

    def _pan_place(self, interval: float) -> tuple[tuple[float, float], float | None]:
        """Where in the photo the screen is now (x, y), in steps of PAN_STEP, and in how many seconds it
        will be a step further (None: it has arrived). One pass in the time the photo is up, slow at both
        ends; it begins when the fade into the photo is over. By the wall clock since the photo went up,
        so a restart of deephack goes on where the photo would be by now."""
        _, over_x, over_y = self._pan
        over, span = max(over_x, over_y), max(1.0, interval - FADE)
        done = min(1.0, max(0.0, (self._wall() - self._since - FADE) / span))

        def way(part: float) -> float:        # slow, fast, slow
            return part * part * (3 - 2 * part)

        steps = round(over * way(done) / PAN_STEP)
        gone, wait = min(over, steps * PAN_STEP), None
        then = (steps + 0.5) * PAN_STEP / over            # the part of the way at which the next step shows
        if then < 1:
            low, high = done, 1.0
            for _ in range(24):
                middle = (low + high) / 2
                low, high = (middle, high) if way(middle) < then else (low, middle)
            wait = (high - done) * span
        at = over - gone if self._reverse else gone
        return (at if over_x else 0.0, at if over_y else 0.0), wait

    def _pan_step(self, interval: float) -> Image.Image | None:
        """The photo a little further across the screen, or None while it has not moved by PAN_STEP. The mode
        asks to be called as often as that takes, FADE_TICK at the soonest; pictures that come many a
        second are JPEGs small enough for that."""
        if self._pan is None or self._shown is None:
            return None
        if not self.direct:
            self.tick, self.quality = None, None
            return None
        (x, y), wait = self._pan_place(interval)
        self.tick = None if wait is None or wait >= TICK else max(FADE_TICK, wait)
        if self.tick is None:
            self.quality = None               # a picture now and then: as sharp as a still
        else:                                 # several a second: what the bus carries in the time between two
            if self.quality is None:
                self.sent_bytes = 0           # (the size of a still: nothing to go by)
            self.quality = self._quality()
        if (x, y) == self._pan_at:
            return None
        self._pan_at = (x, y)
        picture = window(self._pan[0], x, y)
        self._shown = (self._shown[0], picture)
        return picture

    async def frame(self):
        if self._fade is not None:
            return self._fade_step()
        folder = self.folder
        if folder is None:
            self.status = "No folder set"
            return None
        if time.monotonic() < self._next_try:
            return None
        interval = float(self.values["interval"]) * 60
        if not self.direct:
            interval = max(interval, SHORTEST_STORED)
        due = self._current is None or self._wall() - self._since >= interval
        fit, moving = self.values["fit"], self.values["fit"] == "pan" and self.direct
        if not due and self._rendered is not None and self._rendered[2] == moving:
            return self._pan_step(interval)
        if not folder.is_dir():
            self.status = "Folder not found"
            self.placeholder = ("Folder not found", str(folder))
            self._next_try = time.monotonic() + 60
            return None
        picked = await asyncio.to_thread(self._show, folder, due)
        if picked is None:
            self.status = "No pictures in the folder"
            self.placeholder = ("No pictures found", "JPG, PNG, WebP, BMP, GIF and TIFF files are shown.")
            self._next_try = time.monotonic() + 60
            return None
        path, photo = picked
        if path != self._current or due:
            self._current, self._since, self._reverse = path, self._wall(), not self._reverse
            self._save()
        key = (str(path), fit, moving)        # (a cooler that begins to draw: the photo is made again, to move)
        self.status = path.name
        if key == self._rendered:
            return self._pan_step(interval)   # the only photo there is: it goes back the way it came
        self._rendered = key
        self._pan = self._pan_at = None
        self.tick, self.quality = None, None  # (a photo that moved may have left them set, and this one may stand)
        if moving:
            wide = await asyncio.to_thread(cover, photo)
            over = (wide.width / 2 - W, wide.height / 2 - H)
            if max(over) >= PAN_LEAST:
                self._pan = (wide, *over)
        if self._pan is not None:
            self._pan_at = self._pan_place(interval)[0]
            picture = window(self._pan[0], *self._pan_at)
        else:
            picture = await asyncio.to_thread(render_photo, photo, fit)
        last, self._shown = self._shown, (path, picture)
        if last is not None and last[0] != path and self.direct and self.values["transition"] == "fade":
            self._fade = (last[1], picture, self._clock())
            self.sent_bytes = 0               # (the size of the photo that is up, a still: nothing for a fade to go by)
            return self._fade_step()
        return picture
