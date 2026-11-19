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

from . import device, render
from .config import GIF_SLOT_ID, SLOT_ID, Config
from .modes import BY_ID, MODES, Mode
from .modes.base import Animation, Dashboard

MIN_UPLOAD_INTERVAL = 8.0     # seconds between flash writes, whatever the mode asks for
REOPEN_INTERVAL = 3.0
TICK = 0.5
CLOCK_SYNC = 3600.0         # the sensor screens' clock is set when shown and then hourly
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


def to_qimage(img: Image.Image) -> QImage:
    rgba = img.convert("RGBA")
    return QImage(rgba.tobytes(), rgba.width, rgba.height, QImage.Format.Format_RGBA8888).copy()


def _qt_frames(img: Image.Image | Animation | Dashboard) -> tuple[list[QImage], int]:
    if isinstance(img, Dashboard):
        return [to_qimage(img.preview)], 0
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
        self._last_state: dict | None = None
        self._placeholder_shown = None
        self._preview: Mode | None = None
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

    def retry_device(self) -> None:
        self._commands.put(lambda: setattr(self, "_next_open", 0.0))

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
        self._preview = BY_ID[mode_id].build(self.config.mode_settings) if mode_id in BY_ID else None
        self._preview_frame = None
        if self._preview is not None:
            self._preview_shown = self._preview.placeholder
            self.previewChanged.emit(self._preview.id, *_placeholder_frames(self._preview), True)

    async def _preview_step(self) -> None:
        mode = self._preview
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
        while not self._stop:
            while True:
                try:
                    self._commands.get_nowait()()
                except queue.Empty:
                    break
            enabled = self.config.get("enabled")
            if enabled:
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
            if self.use_device:
                self._device_step(enabled)
            self._emit_state(enabled)
            deadline = time.monotonic() + TICK
            while time.monotonic() < deadline and not self._stop and self._commands.empty():
                await asyncio.sleep(0.05)
        self._shutdown()

    def _device_step(self, enabled: bool) -> None:
        now = time.monotonic()
        if self._dev is None:
            if now < self._next_open:
                return
            self._next_open = now + REOPEN_INTERVAL
            try:
                self._dev = device.Mystique()
                self._dev.set_mode(device.MODE_MOTION)
                self._display = device.MODE_MOTION
            except device.DeviceBusy:
                self._dev, self._dev_state = None, "busy"
                return
            except (RuntimeError, usb.core.USBError):
                self._drop_device()
                return
            self._dev_state = "connected"
            self._on_screen = None
            self._slideshow_applied = None
            self._uploaded_seq = -1
            self._slot_digest = {}            # unknown what the slots hold after a reconnect
        try:
            slideshow = bool(self.config.get("slideshow"))
            if slideshow != self._slideshow_applied and self._display == device.MODE_MOTION:
                self._dev.set_slideshow(slideshow)
                self._slideshow_applied = slideshow
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
                    self._slot_digest[slot] = self._frame_digest[1]
                    self._last_upload = time.monotonic()
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

    def _show_stored(self, index: int, slot: int) -> None:
        """Show a stored image or animation, first leaving a sensor screen if one is up."""
        if self._display != device.MODE_MOTION:
            self._dev.set_mode(device.MODE_MOTION)
            self._display = device.MODE_MOTION
            slideshow = bool(self.config.get("slideshow"))
            self._dev.set_slideshow(slideshow)
            self._slideshow_applied = slideshow
        self._dev.show_slot(index, slot)

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
        self._dev, self._dev_state = None, "missing"

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
            "live": isinstance(self._frame, Dashboard),
            "pending": enabled and self._frame is not None and not isinstance(self._frame, Dashboard)
                       and self._frame_seq != self._uploaded_seq,
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
