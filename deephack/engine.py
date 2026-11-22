"""Background engine: runs the active mode and keeps the cooler's display in sync with it.

Lives in its own thread with an asyncio loop (the Windows media APIs are async, USB calls are short and
blocking). The UI talks to it through thread-safe methods and listens to its signals. While the UI shows
the page of another mode, the engine runs that one too, as a preview that never reaches the cooler.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import queue
import subprocess
import time
import traceback
from pathlib import Path

import usb.core
from PIL import Image
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from . import device, firmware, render, ytdlp
from .config import GIF_SLOT_ID, SLOT_ID, Config, book_id
from .modes import BY_ID, MODES, Mode
from .modes.base import Animation, Dashboard, Flipbook

LIGHTS = {"screen": device.LIGHT_SCREEN, "motherboard": device.LIGHT_MOTHERBOARD, "warning": device.LIGHT_WARNING}
RESHOW_DELAY = 1.5           # the firmware picks up the edges of an image shown a moment after a light change
MIN_UPLOAD_INTERVAL = 8.0     # seconds between flash writes, whatever the mode asks for
REOPEN_INTERVAL = 3.0
TICK = 0.5
CLOCK_SYNC = 3600.0         # the sensor screens' clock is set when shown and then hourly
FLIP_INTERVAL = 0.1         # stored pictures: the firmware shows ten a second and drops what comes faster
DRAW_QUALITY = 85           # JPEG quality of frames that come many a second (a flipbook's, or the pictures of a
                            # mode with a short tick): the smaller the frame, the sooner it is on screen
FAST_TICK = 0.1             # a mode that asks to be called this often or more is such a mode
STILL_QUALITIES = (92, 85, 75, 60, 40, 20)   # a drawn picture: the best of these that fits the firmware's 128 KiB
PLAY_QUALITY = 88           # an animation's frames, drawn one after the other: what they are stored with
DRAW_RETRY = 0.05           # the device step is run again this soon while a picture is waiting to be drawn
DRAW_SETTLE = 0.3           # a stored still is on screen 0.1 s after "show"; a frame drawn sooner is replaced by it
DRAW_PATIENCE = 0.2         # seconds to wait while the cooler is busy with the last frame; then the step is skipped
REDRAW = 1.0                # an unchanged drawn frame is sent again this often, in case the screen lost it
REDRAW_STILL = 10.0         # ... and a picture that may stay for hours this often (the stored still under it could
                            # have loaded late and taken its place)
BACK_PAUSE = 0.15           # on the sensor screen, when the image screen has to be left and entered again
DRAW_STUCK = 2.0            # frames refused for this long: start over (see _draw)
FLASH_SETTLE = 3.0          # after a firmware update the cooler is on the bus some seconds before it answers
FLASH_ANSWER = 60.0         # ... and if it has not answered by then, the update is called failed
DEEPCOOL_EXE = "DeepCool.exe"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def deepcool_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {DEEPCOOL_EXE}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return DEEPCOOL_EXE.lower() in out.lower()


def quit_deepcool() -> None:
    subprocess.run(["taskkill", "/IM", DEEPCOOL_EXE, "/T", "/F"], capture_output=True, timeout=10, creationflags=_NO_WINDOW)


def _stored_count(kind: str) -> int:
    """How many files of a kind ("jpg" / "gif") the official app uploaded; ours goes after them.

    The device has no known 'list files' command; the official app tracks its uploads in this folder.
    """
    base = Path(os.environ.get("APPDATA", "")) / "DeepCool" / "Pictures"
    return sum(1 for p in base.glob(f"*/{kind}/*") if p.is_dir())


def digest(frame: Image.Image | Animation) -> bytes:
    """Fingerprint of what a frame looks like, to skip uploads of an image the cooler already holds."""
    h = hashlib.sha1()
    for img in frame.frames if isinstance(frame, Animation) else [frame]:
        h.update(f"{img.mode}{img.size}".encode())
        h.update(img.tobytes())
    if isinstance(frame, Animation):
        h.update(str(frame.duration_ms).encode())
    return h.digest()


def frame_jpeg(img: Image.Image, best: int = STILL_QUALITIES[0]) -> bytes:
    """A picture as the JPEG to draw from USB: as good as what would be stored, unless that is too large."""
    for quality in sorted({best, *STILL_QUALITIES}, reverse=True):
        if quality > best:
            continue
        jpeg = device.encode_jpeg(img, quality=quality)
        if len(jpeg) <= device.DIRECT_MAX:
            break
    return jpeg


def to_qimage(img: Image.Image) -> QImage:
    """A mode's picture for the window. Straight from the RGB bytes: a mode may hand over twenty a second,
    and going through RGBA first took three times as long."""
    rgb = img if img.mode == "RGB" else img.convert("RGB")
    return QImage(rgb.tobytes(), rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888).copy()


def _qt_frames(img: Image.Image | Animation | Dashboard | Flipbook) -> tuple[list[QImage], int]:
    if isinstance(img, Dashboard):
        return [to_qimage(img.preview)], 0
    if isinstance(img, Flipbook):
        return [to_qimage(img.frames[img.index])], 0
    if isinstance(img, Animation):
        return [to_qimage(f) for f in img.frames], img.duration_ms
    return [to_qimage(img)], 0


def _placeholder_frames(mode: Mode) -> tuple[list[QImage], int]:
    title, subtitle = mode.placeholder or (mode.title, "")
    return [to_qimage(render.message(title, subtitle))], 0


class Engine(QThread):
    frameChanged = Signal(object, int, bool)   # list[QImage], loop duration in ms (0 for a still), is_placeholder
    previewChanged = Signal(str, object, int, bool)   # mode id, then as frameChanged
    stateChanged = Signal(dict)

    def __init__(self, config: Config, use_device: bool = True, parent=None):
        super().__init__(parent)
        self.config = config
        self.use_device = use_device
        self._commands: queue.Queue = queue.Queue()
        self._stop = False
        self._mode: Mode | None = None
        self._frame: Image.Image | Animation | None = None
        self._ours: tuple[int, int] | None = None     # (slot type, index) of what we last uploaded
        self._frame_seq = 0
        self._uploaded_seq = -1
        self._last_upload = 0.0
        self._last_upload_wall: float | None = None
        self._slot_digest: dict[int, bytes] = {}      # slot type -> digest of what we last stored in it
        self._frame_digest: tuple[int, bytes] | None = None   # (frame seq, digest), computed once per frame
        self._dev: device.Mystique | None = None
        self._dev_state = "missing" if use_device else "preview"   # connected | busy | missing | preview
        self._next_open = 0.0
        self._on_screen = None            # "ours" | "restore" | (screen, layout) of a sensor screen | None (unknown)
        self._display = None              # the device's display mode, device.MODE_*
        self._clock_set = 0.0
        self._slideshow_applied: bool | None = None
        self._options_applied: tuple | None = None
        self._reshow_at: float | None = None     # after a light change: show the picture again then
        self._book_storing: list | None = None   # [key, index of frame 0, frames stored so far, frames to store]
        self._book_room = True                   # False: the cooler is too full for a flipbook
        self._flipped = 0.0                      # when the last stored frame of a flipbook was shown
        self._can_draw = False                   # the cooler's firmware draws frames straight from USB (tools/fw)
        self._firmware: dict | None = None       # what the cooler runs: {"bcd", "version", "patch"}; None: not connected
        self._flash: dict | None = None          # the last firmware update: {"which", "stage", ...} (see _flash_firmware)
        self._flash_since = 0.0                  # when its wait for the cooler's answer began
        self._drawing = False                    # ... and that is how pictures get there now (not during a slide show)
        self._still: tuple[int, bytes] = (-1, b"")   # frame seq of the picture last drawn, and its JPEG
        self._play: tuple[int, list[bytes], float] = (-1, [], 0.0)   # frame seq of the animation played from here,
                                                                     # its frames as JPEGs, and when it started
        self._again: float | None = None         # the device step wants to run again in this many seconds
        self._veiled = False                     # a frame drawn from USB may be hiding the stored still on screen
        self._drawn = None                       # what was drawn last: (flipbook key, index) or ("still", frame seq)
        self._drawn_at = 0.0
        self._draw_from = 0.0
        self._refused: float | None = None       # since when the cooler has taken no frame
        self._jpegs: tuple[str | None, dict[int, bytes]] = (None, {})   # flipbook key, its frames as drawn
        self._last_state: dict | None = None
        self._placeholder_shown = None
        self._preview: Mode | None = None
        ytdlp.wanted = lambda: bool(config.get("ytdlp_current"))
        self._closing: list[Mode] = []    # modes that were replaced and have not been told yet
        self._preview_frame: Image.Image | Animation | None = None
        self._preview_shown = None        # placeholder of the preview last emitted

    # ---- thread-safe API for the UI ------------------------------------
    def reload_mode(self) -> None:
        """Call after the selected mode or its settings changed in the config."""
        self._commands.put(self._make_mode)

    def set_preview(self, mode_id: str | None) -> None:
        """Run this mode next to the active one for the UI only (None: stop). Its frames never reach the cooler."""
        self._commands.put(lambda: self._make_preview(mode_id))

    def reload_preview(self) -> None:
        """Call after the previewed mode's settings changed."""
        self._commands.put(lambda: self._make_preview(self._preview.id if self._preview else None))

    def mode_action(self, mode_id: str, action: str) -> None:
        """One of a mode's `actions` ("next"): for the mode on the cooler, and for its preview if that is it."""
        def act() -> None:
            for mode in (self._mode, self._preview):
                if mode is not None and mode.id == mode_id:
                    mode.act(action)

        self._commands.put(act)

    def mode_value(self, mode_id: str, key: str, value) -> None:
        """A setting of the `live` kind was changed: the running mode gets the new value as it is."""
        def put() -> None:
            for mode in (self._mode, self._preview):
                if mode is not None and mode.id == mode_id and key in mode.values:
                    mode.values[key] = value

        self._commands.put(put)

    def retry_device(self) -> None:
        self._commands.put(lambda: setattr(self, "_next_open", 0.0))

    def flash_firmware(self, which: str, stock_path: str | None = None) -> None:
        """Send the cooler another firmware: "direct" (draws from USB) or "stock" (DeepCool's own). This can
        kill its screen: see deephack.firmware. The state's "flash" says how it goes."""
        self._commands.put(lambda: self._flash_firmware(which, stock_path))

    def stop(self) -> None:
        self._stop = True

    # ---- internals -----------------------------------------------------
    def run(self) -> None:
        try:
            from winrt.runtime import ApartmentType, init_apartment

            init_apartment(ApartmentType.MULTI_THREADED)
        except Exception:
            pass
        asyncio.run(self._main())

    def _make_mode(self) -> None:
        cls = BY_ID.get(self.config.get("mode"), MODES[0])
        if self._mode is not None:
            self._closing.append(self._mode)
        if self._preview is not None and self._preview.id == cls.id:
            # the previewed mode goes on the cooler: keep it running, with what it has drawn so far
            self._mode, frame, self._preview, self._preview_frame = self._preview, self._preview_frame, None, None
            if frame is not None:
                self._set_frame(frame)
                return
        else:
            self._mode = cls.build(self.config.mode_settings)
        self._frame = None
        self._emit_placeholder()

    def _set_frame(self, img: Image.Image | Animation) -> None:
        self._frame = img
        self._frame_seq += 1
        self.frameChanged.emit(*_qt_frames(img), False)

    def _emit_placeholder(self) -> None:
        self._placeholder_shown = self._mode.placeholder
        self.frameChanged.emit(*_placeholder_frames(self._mode), True)

    def _make_preview(self, mode_id: str | None) -> None:
        if self._preview is not None:
            self._closing.append(self._preview)
        self._preview = BY_ID[mode_id].build(self.config.mode_settings) if mode_id in BY_ID else None
        self._preview_frame = None
        if self._preview is not None:
            self._preview_shown = self._preview.placeholder
            self.previewChanged.emit(self._preview.id, *_placeholder_frames(self._preview), True)

    async def _close_modes(self, *more) -> None:
        """Tell the modes that were replaced that they are done (a video stops its sound there)."""
        closing, self._closing = self._closing, []
        closing = [mode for mode in closing if mode is not self._mode and mode is not self._preview]     # still in use
        for mode in (*closing, *more):
            if mode is not None:
                try:
                    await mode.close()
                except Exception:
                    traceback.print_exc()

    async def _preview_step(self) -> None:
        mode = self._preview
        mode.direct, mode.audible = self._drawing, False
        try:
            img = await mode.frame()
        except Exception:
            traceback.print_exc()
            mode.status, img = "Mode error, see console", None
        if img is not None:
            self._preview_frame = img
            self.previewChanged.emit(mode.id, *_qt_frames(img), False)
        elif self._preview_frame is None and mode.placeholder != self._preview_shown:
            self._preview_shown = mode.placeholder
            self.previewChanged.emit(mode.id, *_placeholder_frames(mode), True)

    async def _main(self) -> None:
        self._make_mode()
        modes_due = 0.0                       # when the modes are asked for a picture again (perf_counter)
        while not self._stop:
            started = time.perf_counter()
            commanded = not self._commands.empty()
            while True:
                try:
                    self._commands.get_nowait()()
                except queue.Empty:
                    break
            if self._closing:
                await self._close_modes()
            enabled = self.config.get("enabled")
            if commanded or started >= modes_due:
                if enabled:
                    self._mode.direct, self._mode.audible = self._drawing, True
                    try:
                        img = await self._mode.frame()
                    except Exception:
                        traceback.print_exc()
                        self._mode.status = "Mode error, see console"
                        img = None
                    if img is not None:
                        self._set_frame(img)
                    elif self._frame is None and self._mode.placeholder != self._placeholder_shown:
                        self._emit_placeholder()   # a mode may refine its placeholder (e.g. "city not found")
                if self._preview is not None:
                    await self._preview_step()
                # A mode's tick is from one step's start to the next, so a step's own work does not add to it.
                modes_due = started + min(m.tick or TICK for m in (self._mode, self._preview) if m is not None)
            if self.use_device:
                self._device_step(enabled)
            self._emit_state(enabled)
            # The cooler may be due before the modes are: the next frame of an animation played from here, or
            # a picture that could not be drawn yet. Timed with perf_counter and slept in a thread: on
            # Windows time.monotonic() moves in steps of 15.6 ms, and asyncio's sleep goes by it. With those
            # a 50 ms tick came out as 62.5 (measured).
            deadline = modes_due if self._again is None else min(modes_due, started + self._again)
            while (left := deadline - time.perf_counter()) > 0 and not self._stop and self._commands.empty():
                await asyncio.to_thread(time.sleep, min(0.05, left))
            await asyncio.sleep(0)
        await self._close_modes(self._mode, self._preview)
        self._shutdown()

    def _device_step(self, enabled: bool) -> None:
        now = time.monotonic()
        self._again = None
        if self._dev is None:
            if now < self._next_open:
                return
            self._next_open = now + REOPEN_INTERVAL
            try:
                self._dev = device.Mystique()
                self._dev.set_mode(device.MODE_MOTION)
                self._display = device.MODE_MOTION
                patch, bcd = self._dev.draw_version(), getattr(self._dev.dev, "bcdDevice", 0)
                self._firmware = {"bcd": bcd, "version": firmware.version(bcd), "patch": patch}
                self._can_draw = patch > 0
                self._veiled = self._can_draw     # a deephack that was killed may have left a drawn frame up
            except device.DeviceBusy:
                self._dev, self._dev_state = None, "busy"
                return
            except (RuntimeError, usb.core.USBError):
                self._drop_device()
                self._flash_checked(now)
                return
            self._flash_checked(now)
            self._dev_state = "connected"
            self._on_screen = None
            self._slideshow_applied = None
            self._options_applied = None
            self._uploaded_seq = -1
            self._slot_digest = {}            # unknown what the slots hold after a reconnect
        try:
            slideshow = bool(self.config.get("slideshow"))
            if slideshow != self._slideshow_applied and self._display == device.MODE_MOTION:
                self._dev.set_slideshow(slideshow)
                self._slideshow_applied = slideshow
            drawing = self._can_draw and not slideshow     # a slide show is of stored pictures: ours is stored then
            if drawing != self._drawing:
                self._drawing, self._on_screen, self._uploaded_seq = drawing, None, -1
            options = (int(self.config.get("brightness")), LIGHTS.get(self.config.get("light"), device.LIGHT_SCREEN))
            if options != self._options_applied:
                self._dev.set_options(*options)
                if self._options_applied is not None:
                    self._reshow_at = now + RESHOW_DELAY
                self._options_applied = options
            if self._reshow_at is not None and now >= self._reshow_at:
                self._reshow_at = None
                if self._on_screen == "ours" or (isinstance(self._on_screen, tuple) and self._on_screen[0] == "book"):
                    self._on_screen = None        # show it again, so the light picks up its edges
            if not enabled:
                if self._on_screen != "restore":
                    self._show_stored(self.config.get("restore_index"), device.TYPE_JPG)
                    self._on_screen = "restore"
                return
            if self._frame is None:
                return
            if isinstance(self._frame, Dashboard):
                self._dashboard_step(self._frame, now)
                return
            if isinstance(self._frame, Flipbook):
                if self._drawing:
                    self._draw_book(self._frame, now)
                else:
                    self._flipbook_step(self._frame, now)
                return
            if self._drawing:
                if isinstance(self._frame, Animation):
                    self._draw_animation(self._frame, now)
                else:
                    self._draw_still(self._frame, now)
                return
            if self._frame_seq != self._uploaded_seq:
                animated = isinstance(self._frame, Animation)
                slot = device.TYPE_GIF if animated else device.TYPE_JPG
                if self._frame_digest is None or self._frame_digest[0] != self._frame_seq:
                    self._frame_digest = (self._frame_seq, digest(self._frame))
                stored = self._slot_digest.get(slot) == self._frame_digest[1]
                if not stored and now - self._last_upload < MIN_UPLOAD_INTERVAL:
                    return
                key, kind = ("gif_slot_index", "gif") if animated else ("slot_index", "jpg")
                index = self.config.get(key)
                if index is None:
                    index = _stored_count(kind)
                    self.config.set(key, index)
                if not stored:            # e.g. Auto switching back to an unchanged image: just show it again
                    if animated:
                        frames = [device.encode_jpeg(f, quality=88) for f in self._frame.frames]
                        self._dev.upload_gif(GIF_SLOT_ID, frames, self._frame.duration_ms)
                    else:
                        self._dev.upload_jpeg(SLOT_ID, device.encode_jpeg(self._frame))
                        self.config.set("ground", False)
                    self._slot_digest[slot] = self._frame_digest[1]
                    self._last_upload = time.monotonic()
                    self._veiled = False          # after an upload the firmware loads the stored picture afresh
                self._ours = (slot, index)
                self._show_stored(index, slot)
                self._uploaded_seq = self._frame_seq
                self._last_upload_wall = time.time()
                self._on_screen = "ours"
            elif self._on_screen != "ours" and self._ours is not None:
                self._show_stored(self._ours[1], self._ours[0])
                self._on_screen = "ours"
        except (usb.core.USBError, RuntimeError):
            traceback.print_exc()
            self._drop_device()

    def _show_stored(self, index: int, slot: int, afresh: bool = False) -> None:
        """Show a stored image or animation, first leaving a sensor screen if one is up.

        After a frame drawn from USB the firmware ignores "show" for the still it has selected, and the
        drawn frame stays up (seen on the cooler). Leaving the image screen and coming back makes it load
        the stored picture again; `afresh` asks for that detour in any case."""
        if (afresh or self._veiled and slot == device.TYPE_JPG) and self._display == device.MODE_MOTION:
            self._dev.set_mode(device.MODE_SITUATIONAL)
            self._display = device.MODE_SITUATIONAL
            time.sleep(BACK_PAUSE)
        if self._display != device.MODE_MOTION:
            self._dev.set_mode(device.MODE_MOTION)
            self._display = device.MODE_MOTION
            slideshow = bool(self.config.get("slideshow"))
            self._dev.set_slideshow(slideshow)
            self._slideshow_applied = slideshow
        self._dev.show_slot(index, slot)
        self._veiled = False

    def _own_still(self) -> int:
        """Index of our own still on the cooler, stored now if it never was (one flash write, once)."""
        main = self.config.get("slot_index")
        if main is None:
            main = _stored_count("jpg")
            self._dev.upload_jpeg(SLOT_ID, device.encode_jpeg(render.message("deephack")))
            self._last_upload = time.monotonic()
            self.config.set("slot_index", main)
            self._slot_digest.pop(device.TYPE_JPG, None)
        return main

    def _ground(self) -> int:
        """Index of our own still as the ground for drawn frames: a plain black picture, stored once.

        Frames are drawn over a stored still, and all that is seen of it is a moment before the first
        frame. Whatever picture was last stored there would flash up each time, so it is made plain."""
        main = self.config.get("slot_index")
        if main is None or not self.config.get("ground"):
            if main is None:
                main = _stored_count("jpg")
            self._dev.upload_jpeg(SLOT_ID, device.encode_jpeg(Image.new("RGB", (device.WIDTH, device.HEIGHT))))
            self._last_upload = time.monotonic()
            self.config.set("slot_index", main)
            self.config.set("ground", True)
            self._slot_digest.pop(device.TYPE_JPG, None)
        return main

    def _draw(self, what, jpeg, now: float, again: float) -> bool:
        """Put a picture on screen straight from USB: nothing is stored. True once `what` is on screen.

        `what` names the picture, `jpeg()` makes its data when it has to be sent, and an unchanged one is
        sent again every `again` seconds. Needs the direct drawing firmware (tools/fw). It draws over a
        stored still that is on show (an animation would take the screen back with its next frame), so
        ours is shown first. And the image screen is entered afresh for it: once an animation has been on
        that screen, the first patch takes one frame and answers "later" to every one after it, also over
        a still (issue #22; the patch's version 2 does not). Should it stop taking frames all the same, the
        whole start is done again."""
        if self._on_screen != "drawn":
            self._show_stored(self._ground(), device.TYPE_JPG, afresh=True)
            self._on_screen, self._drawn, self._draw_from, self._refused = "drawn", None, now + DRAW_SETTLE, None
            self._again = DRAW_RETRY
            return False
        if now < self._draw_from:
            self._again = DRAW_RETRY
            return False
        if self._drawn != what or now - self._drawn_at >= again:
            try:
                self._dev.draw_jpeg(jpeg(), patience=DRAW_PATIENCE)
            except TimeoutError:              # busy with the last frame: the next step tries again
                if self._refused is None:
                    self._refused = now
                elif now - self._refused > DRAW_STUCK:
                    self._on_screen = None
                self._again = DRAW_RETRY
                return False
            self._drawn, self._drawn_at, self._veiled, self._refused = what, now, True, None
        return True

    def _draw_book(self, book: Flipbook, now: float) -> None:
        """Draw the book's current frame: any number of frames will do, and none is stored."""
        if self._jpegs[0] != book.key:
            self._jpegs = (book.key, {})
        frames = self._jpegs[1]

        def jpeg() -> bytes:
            if book.index not in frames:
                frames[book.index] = device.encode_jpeg(book.frames[book.index], quality=DRAW_QUALITY)
            return frames[book.index]

        if self._draw((book.key, book.index), jpeg, now, REDRAW):
            self._uploaded_seq = self._frame_seq

    def _draw_animation(self, animation: Animation, now: float) -> None:
        """Play a mode's animation from here: the frame that is due is drawn, and none is stored.

        The cooler would loop a stored animation by itself, at the price of a flash write per frame every
        time the animation changes. Two frames in a row that are the same picture cost one draw."""
        if self._play[0] != self._frame_seq:
            self._play = (self._frame_seq, [frame_jpeg(f, PLAY_QUALITY) for f in animation.frames], now)
        seq, jpegs, started = self._play
        period = max(0.02, animation.duration_ms / 1000 / len(jpegs))
        at = (now - started) / period
        jpeg = jpegs[int(at) % len(jpegs)]
        if self._draw((seq, jpeg), lambda: jpeg, now, REDRAW_STILL):
            self._again = max(0.01, (int(at) + 1 - at) * period)       # until the next frame is due
            if seq != self._uploaded_seq:
                self._uploaded_seq = seq
                self._last_upload_wall = time.time()

    def _draw_still(self, picture: Image.Image, now: float) -> None:
        """Draw a mode's picture instead of storing it: no flash write, and no wait between two of them."""
        def jpeg() -> bytes:
            if self._still[0] != self._frame_seq:
                fast = self._mode is not None and (self._mode.tick or TICK) <= FAST_TICK
                quality = self._mode is not None and self._mode.quality or (DRAW_QUALITY if fast else STILL_QUALITIES[0])
                self._still = (self._frame_seq, frame_jpeg(picture, quality))
            return self._still[1]

        if self._draw(("still", self._frame_seq), jpeg, now, REDRAW_STILL) and self._frame_seq != self._uploaded_seq:
            self._uploaded_seq = self._frame_seq
            self._last_upload_wall = time.time()

    def _flipbook_step(self, book: Flipbook, now: float) -> None:
        """Store the book's frames once (one per step, each a flash write), then just show the wanted one.

        The cooler keeps device.MAX_STILLS stills in all. The book goes after the user's own and ours, so
        with little room left only some of its frames are stored, picked evenly from first to last."""
        stored = self.config.get("book") or {}
        main = self.config.get("slot_index")
        first = stored.get("first")               # frames of an earlier book are replaced in place
        if first is None:
            first = max(main or 0, _stored_count("jpg")) + 1     # after the official app's stills and our own
        count = min(len(book.frames), device.MAX_STILLS - first)
        self._book_room = count >= 2
        if not self._book_room:
            return
        if stored.get("key") != book.key or stored.get("count") != count:
            if self._book_storing is None or self._book_storing[0] != book.key:
                self._own_still()                 # ours comes first in the cooler's list: make sure it exists
                self._book_storing = [book.key, first, 0, count]
            done = self._book_storing[2]
            source = round(done * (len(book.frames) - 1) / (count - 1))
            self._dev.upload_jpeg(book_id(done), device.encode_jpeg(book.frames[source]))
            self._last_upload = time.monotonic()
            self._book_storing[2] = done + 1
            if done + 1 == count:
                self.config.set("book", {"key": book.key, "first": first, "count": count})
                self._book_storing = None
            return
        step = round(book.index * (count - 1) / max(1, len(book.frames) - 1))
        target = ("book", book.key, step)
        if self._on_screen != target:
            if now - self._flipped < FLIP_INTERVAL:
                return
            self._show_stored(first + step, device.TYPE_JPG)
            self._on_screen, self._flipped = target, now
        self._uploaded_seq = self._frame_seq

    def _dashboard_step(self, dash: Dashboard, now: float) -> None:
        """Bring up the firmware's sensor screen and feed it. None of this is stored: no flash writes."""
        target = (dash.screen, tuple(dash.layout))
        if self._on_screen != target or now - self._clock_set >= CLOCK_SYNC:
            self._dev.set_time()
            self._clock_set = now
        if self._on_screen != target:
            mode = device.MODE_RECORDER if dash.screen == "recorder" else device.MODE_SITUATIONAL
            if self._display != mode:
                self._dev.set_mode(mode)
                self._display = mode
            if dash.screen == "recorder":
                self._dev.set_recorder(*dash.layout)
            else:
                self._dev.set_dashboard(*dash.layout)
            self._on_screen = target
            self._uploaded_seq = -1
        if self._frame_seq != self._uploaded_seq:
            self._dev.send_sensors(**dash.values)
            self._uploaded_seq = self._frame_seq

    def _drop_device(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:
                pass
        self._dev, self._dev_state, self._firmware = None, "missing", None

    # ---- firmware updates (deephack.firmware)
    def _flash_stage(self, which: str, stage: str, **more) -> None:
        self._flash = {"which": which, "stage": stage, **more}
        self._emit_state(bool(self.config.get("enabled")))

    def _flash_firmware(self, which: str, stock_path: str | None) -> None:
        """Send a firmware and see the cooler through its restart. Blocks the engine until the cooler is
        back on the bus (or has not come back): nothing else may talk to it meanwhile.

        Stages, in the state's "flash": "sending" (with "progress", 0 to 1), "restarting" (with "seconds"
        and "off": the cooler has left the bus), "checking" (it is back; _flash_checked asks what it runs),
        then "done" or "failed" with a "message". "failed" with "sent": the whole file went out, so the
        cooler may have been flashed all the same."""
        if self._dev is None or self._firmware is None:
            return self._flash_stage(which, "failed", message="The cooler is not connected. Nothing was sent.")
        bcd = self._firmware["bcd"]
        if bcd not in firmware.KNOWN:
            return self._flash_stage(which, "failed", message=f"The cooler reports firmware {firmware.version(bcd)}, which "
                                     "deephack does not know. Nothing was sent.")
        try:
            image = firmware.image_for(which, firmware.read_stock(stock_path))
            firmware.check_image(image)
        except ValueError as e:
            return self._flash_stage(which, "failed", message=f"{e} Nothing was sent.")

        def progress(sent: int) -> None:
            if self._stop:
                raise InterruptedError("deephack is closing")
            self._flash_stage(which, "sending", progress=sent / len(image))

        self._flash_stage(which, "sending", progress=0.0)
        try:
            firmware.send_update(self._dev, image, progress)
        except (firmware.SendFailed, usb.core.USBError, RuntimeError) as e:
            traceback.print_exc()
            ended = getattr(e, "ended", True)
            self._drop_device()
            self._next_open = time.monotonic() + REOPEN_INTERVAL
            return self._flash_stage(which, "failed", message="Sending stopped before the end, so nothing was flashed. "
                                     + ("The cooler may show \"Loading\" for a while." if ended else
                                        "Shut the PC down and switch it on again before anything else is sent to the cooler."))
        self._drop_device()

        def report(seconds: float, state: str) -> None:
            if self._stop:
                raise InterruptedError("deephack is closing")
            self._flash_stage(which, "restarting", seconds=int(seconds), off=state != "waiting")

        self._flash_stage(which, "restarting", seconds=0, off=False)
        try:
            outcome, _ = firmware.wait_for_restart(report=report)
        except InterruptedError:
            return                             # the cooler goes on by itself; the next start finds what it runs
        if outcome == "gone":
            return self._flash_stage(which, "failed", sent=True, message="The cooler has not come back. Leave the PC on for "
                                     "a few more minutes; then shut it down, switch it on again and start deephack.")
        self._flash_since = time.monotonic()
        self._next_open = self._flash_since + FLASH_SETTLE
        self._flash_stage(which, "checking", restarted=outcome == "back")

    def _flash_checked(self, now: float) -> None:
        """After a firmware update, once the cooler has been opened (or could not be): what does it run?"""
        if self._flash is None or self._flash["stage"] != "checking":
            return
        which, restarted = self._flash["which"], self._flash.get("restarted")
        if self._firmware is None:
            if now - self._flash_since > FLASH_ANSWER:
                self._flash = {"which": which, "stage": "failed", "sent": True, "message": "The cooler is on USB again but "
                               "does not answer. Shut the PC down, switch it on again and start deephack."}
            return
        patch = self._firmware["patch"]
        if patch == (firmware.PATCH_VERSION if which == "direct" else 0):
            self._flash = {"which": which, "stage": "done", "message": "The cooler draws pictures straight from USB now."
                           if which == "direct" else "The cooler runs DeepCool's own firmware again."}
        else:
            self._flash = {"which": which, "stage": "failed", "sent": True, "message":
                           ("The cooler restarted, but it runs the firmware it had before." if restarted else
                            "The cooler did not restart: it has not taken the file.") + " Nothing was changed."}

    def _emit_state(self, enabled: bool) -> None:
        state = {
            "device": self._dev_state,
            "deepcool_running": deepcool_running() if self._dev_state == "busy" else False,
            "enabled": enabled,
            "mode": self._mode.id,
            "mode_status": self._mode.status,
            "preview": self._preview.id if self._preview else None,
            "preview_status": self._preview.status if self._preview else "",
            "last_upload": self._last_upload_wall,
            "live": isinstance(self._frame, (Dashboard, Flipbook)) and self._book_storing is None,
            "mode_live": "dashboard" if not isinstance(self._frame, Flipbook) else "drawn" if self._drawing else "flipbook",
            "drawn": self._drawing and isinstance(self._frame, (Image.Image, Animation)),
            "storing": (self._book_storing[2], self._book_storing[3])
                       if self._book_storing and isinstance(self._frame, Flipbook) else None,
            "no_room": isinstance(self._frame, Flipbook) and not self._book_room,
            "pending": enabled and self._frame is not None and not isinstance(self._frame, (Dashboard, Flipbook))
                       and self._frame_seq != self._uploaded_seq,
            "firmware": self._firmware,
            "flash": self._flash,
        }
        if state != self._last_state:
            self._last_state = state
            self.stateChanged.emit(state)

    def _shutdown(self) -> None:
        if self._dev is None:
            return
        try:
            if self._on_screen not in (None, "restore"):     # ours, or a sensor screen
                self._show_stored(self.config.get("restore_index"), device.TYPE_JPG)
        except (usb.core.USBError, RuntimeError):
            pass
        self._drop_device()
