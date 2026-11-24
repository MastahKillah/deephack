"""Pictures from a folder, one at a time, changing every so often.

On DeepCool's own firmware every change is a write to the cooler's flash, so the shortest interval there
is 15 minutes, and a photo appears all at once. A cooler that draws from USB (Mode.direct) stores
nothing: there the interval may be seconds, one photo fades into the next (`_fade_step`), and a photo
that is wider or taller than the screen can fill it and move across it while it is up (`_pan_step`).

The current photo and when it went up are remembered in %APPDATA%\\deephack\\photo-frame.json, so
restarting deephack or changing a setting keeps the photo instead of skipping to the next one.

Next and Previous (`act`), from the app's buttons or from keys that work in any window (hotkeys.py):
the photos that were up are remembered, so Previous goes back through them, and Next comes forward
again before it picks new ones.

The next photo is loaded a few seconds before its time, in a thread (`Coming`), and the mode asks to be
called at that time and not within half a second of it: the photo changes when it is due.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import threading
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
AHEAD = 3.0               # seconds before a photo's time is up that the next one is loaded and made ready
KEPT = 50                 # photos remembered behind the one that is up, for Previous
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


def previous_in_order(photos: list[Path], current: Path | None) -> Path:
    """The photo before current in path order, wrapping around."""
    if current is not None:
        key = str(current).lower()
        for p in reversed(photos):
            if str(p).lower() < key:
                return p
    return photos[-1]


class Coming:
    """A photo being loaded and made ready in a thread of its own, while another one is still up."""

    def __init__(self, work, key):
        self.key, self._got = key, None
        self._thread = threading.Thread(target=self._run, args=(work,), name="photo", daemon=True)
        self._thread.start()

    def _run(self, work) -> None:
        self._got = work()

    def wait(self):
        self._thread.join()
        return self._got


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
    actions = (("previous", "Previous"), ("next", "Next"))
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
        Setting("next_key", "Key for the next photo", "shortcut", "Ctrl+Alt+PgDown", action="next",
                hint="Works whatever window is in front, while the Photo Frame is on the cooler. Empty: no key."),
        Setting("previous_key", "Key for the one before", "shortcut", "Ctrl+Alt+PgUp", action="previous"),
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
        self._before: list[Path] = []         # the photos that were up before this one, the last of them last
        self._after: list[Path] = []          # ... and the ones Previous went back from: they come next, the last first
        self._step = 0                        # Next (+1) or Previous (-1) was asked for (`act`)
        self._coming: Coming | None = None    # the next photo, being made ready before its time
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
            before = state.get("before")
            self._before = [Path(p) for p in before if isinstance(p, str)][-KEPT:] if isinstance(before, list) else []

    def _save(self) -> None:
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"path": str(self._current), "since": self._since, "reverse": self._reverse,
                                       "before": [str(p) for p in self._before]}), encoding="utf8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    def _pick(self, photos: list[Path]) -> Path:
        present = set(photos)
        while self._after:                    # Previous went back from these: they come before any other
            path = self._after.pop()
            if path in present:
                return path
        if self.values["order"] == "name":
            return next_in_order(photos, self._current)
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

    def _back(self, photos: list[Path]) -> Path | None:
        """The photo that was up before this one. By file name that is the one before it in the folder, also
        where nothing is remembered; in random order nothing remembered means there is none."""
        present = set(photos)
        while self._before:
            path = self._before.pop()
            if path in present and path != self._current:
                return path
        if self.values["order"] == "name" and photos:
            path = previous_in_order(photos, self._current)
            return None if path == self._current else path
        return None

    def _show(self, folder: Path, how: str) -> tuple[Path, Image.Image] | None:
        """The photo to show, loaded: the "next" one, the "previous" one, or the "same" one again (deephack
        was started, a setting was changed). Where there is no previous one, the same."""
        if how == "previous":
            photos = self._photos(folder)
            while (path := self._back(photos)) is not None:
                try:
                    return path, load(path)
                except (OSError, ValueError, Image.DecompressionBombError):
                    photos = [p for p in photos if p != path]
            how = "same"
        if how == "same" and self._current is not None:
            try:
                return self._current, load(self._current)
            except (OSError, ValueError, Image.DecompressionBombError):
                pass
        return self._advance(folder)

    def _prepare(self, folder: Path, how: str, fit: str, moving: bool):
        """A photo picked, loaded and made ready to show (in a thread): (its path, the picture, None), or for
        one that will move (its path, None, what `_pan` holds). None if the folder has no picture."""
        picked = self._show(folder, how)
        if picked is None:
            return None
        path, photo = picked
        if moving:
            wide = cover(photo)
            over = (wide.width / 2 - W, wide.height / 2 - H)
            if max(over) >= PAN_LEAST:
                return path, None, (wide, *over)
        return path, render_photo(photo, fit), None

    def act(self, action: str) -> None:
        """Next or Previous, from the app's buttons or a key: the photo changes at the next frame()."""
        if action in ("next", "previous"):
            self._step = 1 if action == "next" else -1

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
        if self._pan is None or self._shown is None or not self.direct:
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
        step, self._step = self._step, 0
        if step and self._fade is not None:   # in the middle of a fade: that one is over, the photo it led to is up
            self._fade, self.tick, self.quality = None, None, None
        if self._fade is not None:
            return self._fade_step()
        folder = self.folder
        if folder is None:
            self.status = "No folder set"
            return None
        if time.monotonic() < self._next_try and not step:
            return None
        interval = float(self.values["interval"]) * 60
        if not self.direct:
            interval = max(interval, SHORTEST_STORED)
        left = interval - (self._wall() - self._since)
        due = self._current is None or left <= 0
        fit, moving = self.values["fit"], self.values["fit"] == "pan" and self.direct
        if not due and not step and self._rendered is not None and self._rendered[2] == moving:
            if left < AHEAD and self._coming is None and folder.is_dir():
                self._coming = Coming(lambda: self._prepare(folder, "next", fit, moving), (fit, moving))
            picture = self._pan_step(interval)
            if left < (self.tick or TICK):
                self.tick = max(0.005, left)  # called again when the photo's time is up, not up to half a second later
            return picture
        if not folder.is_dir():
            self.status = "Folder not found"
            self.placeholder = ("Folder not found", str(folder))
            self._next_try = time.monotonic() + 60
            return None
        how = "previous" if step < 0 else "next" if due or step else "same"
        coming, self._coming, got = self._coming, None, None
        if coming is not None:                # the next photo was made ready (or nearly: then it is waited for)
            got = await asyncio.to_thread(coming.wait)
            if got is not None and (how != "next" or coming.key != (fit, moving)):
                self._after.append(got[0])    # not what is wanted now: it comes after
                got = None
        if got is None:
            got = await asyncio.to_thread(self._prepare, folder, how, fit, moving)
        if got is None:
            self.status = "No pictures in the folder"
            self.placeholder = ("No pictures found", "JPG, PNG, WebP, BMP, GIF and TIFF files are shown.")
            self._next_try = time.monotonic() + 60
            return None
        path, picture, pan = got
        if path != self._current or due or step > 0:
            if self._current is not None and path != self._current:
                (self._after if step < 0 else self._before).append(self._current)
                del self._before[:-KEPT]
            self._current, self._since, self._reverse = path, self._wall(), not self._reverse
            self._save()
        key = (str(path), fit, moving)        # (a cooler that begins to draw: the photo is made again, to move)
        self.status = path.name
        if key == self._rendered:
            return self._pan_step(interval)   # the only photo there is: it goes back the way it came
        self._rendered = key
        self._pan, self._pan_at = pan, None
        self.tick, self.quality = None, None  # (a photo that moved may have left them set, and this one may stand)
        if pan is not None:
            self._pan_at = self._pan_place(interval)[0]
            picture = window(pan[0], *self._pan_at)
        last, self._shown = self._shown, (path, picture)
        if last is not None and last[0] != path and self.direct and self.values["transition"] == "fade":
            self._fade = (last[1], picture, self._clock())
            self.sent_bytes = 0               # (the size of the photo that is up, a still: nothing for a fade to go by)
            return self._fade_step()
        return picture
