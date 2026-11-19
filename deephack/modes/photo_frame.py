"""Pictures from a folder, one at a time, changing every so often.

Every change is a write to the cooler's flash, so the shortest interval is 15 minutes. The current photo
and when it went up are remembered in %APPDATA%\\deephack\\photo-frame.json, so restarting deephack or
changing a setting keeps the photo instead of skipping to the next one.
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
from .base import Mode, Setting

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
MAX_FILES = 100_000
STATE_NAME = "photo-frame.json"
PORTRAIT_RATIO = 0.9      # "auto" fills the screen with photos narrower than this (width / height), shows wider ones whole


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
                (("15", "15 minutes"), ("60", "1 hour"), ("360", "6 hours"), ("1440", "Day"))),
        Setting("order", "Order", "choice", "random", (("random", "Random"), ("name", "By file name"))),
        Setting("fit", "Fit", "choice", "auto",
                (("auto", "Auto"), ("fill", "Fill the screen"), ("whole", "Whole photo")),
                hint="Auto fills the screen with portrait photos and shows wider ones whole. "
                     "Every change is a write to the cooler's flash."),
    )

    def __init__(self, values=None):
        super().__init__(values)
        self.state_path = config_dir() / STATE_NAME
        self._current: Path | None = None
        self._since = 0.0                     # wall clock time the current photo went up
        self._bag: list[Path] = []            # random order: photos not shown yet this round
        self._rendered: tuple | None = None
        self._next_try = 0.0
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

    def _advance(self, folder: Path) -> tuple[Path, Image.Image] | None:
        """Pick and load the next photo, skipping files that do not open. None if the folder has none."""
        photos = scan(folder)
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

    async def frame(self):
        folder = self.folder
        if folder is None:
            self.status = "No folder set"
            return None
        if time.monotonic() < self._next_try:
            return None
        interval = int(self.values["interval"]) * 60
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
        return await asyncio.to_thread(render_photo, photo, self.values["fit"])
