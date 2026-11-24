"""Pictures from a folder, one at a time, changing every so often.

On DeepCool's own firmware every change is a write to the cooler's flash, so the shortest interval there
is 15 minutes, and a photo appears all at once. A cooler that draws from USB (Mode.direct) stores
nothing: there the interval may be seconds, and one photo fades into the next (`_fade_step`).

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
RESCAN = 60.0             # seconds for which the list of the folder's pictures is good: an interval may be shorter
FADE = 1.0                # seconds one photo takes to fade into the next
FADE_TICK = 0.036         # ... and how often a picture of that is made: every second turn of the cooler's panel, 27 a
                          # second, which leaves each one 30 KB (video.room_for)
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


def render_photo(photo: Image.Image, fit: str = "auto") -> Image.Image:
    fill = fit == "fill" or (fit == "auto" and photo.width / photo.height < PORTRAIT_RATIO)
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
                (("auto", "Auto"), ("fill", "Fill the screen"), ("whole", "Whole photo")),
                hint="Auto fills the screen with portrait photos and shows wider ones whole."),
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
            self._current, self._since = current, since

    def _save(self) -> None:
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"path": str(self._current), "since": self._since}), encoding="utf8")
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
            return after
        self.tick = FADE_TICK
        self.quality = video.quality_for(self.quality or FADE_QUALITY, self.sent_bytes, 1 / FADE_TICK, self.fast, self.turn)
        return Image.blend(before, after, done * done * (3 - 2 * done))      # slow, fast, slow

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
        due = self._current is None or time.time() - self._since >= interval
        if not due and self._rendered is not None:
            return None
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
            self._current, self._since = path, time.time()
            self._save()
        key = (str(path), self.values["fit"])
        self.status = path.name
        if key == self._rendered:
            return None
        self._rendered = key
        picture = await asyncio.to_thread(render_photo, photo, self.values["fit"])
        last, self._shown = self._shown, (path, picture)
        if last is not None and last[0] != path and self.direct and self.values["transition"] == "fade":
            self._fade = (last[1], picture, self._clock())
            self.sent_bytes = 0               # (the size of the photo that is up, a still: nothing for a fade to go by)
            return self._fade_step()
        return picture
