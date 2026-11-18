"""Background engine: runs the active mode and keeps the cooler's display in sync with it.

Lives in its own thread with an asyncio loop (the Windows media APIs are async, USB calls are short and
blocking). The UI talks to it through thread-safe methods and listens to two signals.
"""
from __future__ import annotations

import asyncio
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
from .config import SLOT_ID, Config
from .modes import BY_ID, MODES, Mode

MIN_UPLOAD_INTERVAL = 8.0     # seconds between flash writes, whatever the mode asks for
REOPEN_INTERVAL = 3.0
TICK = 0.5
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


def _stored_jpg_count() -> int:
    """The device has no known 'list files' command; the official app tracks its uploads in this folder."""
    base = Path(os.environ.get("APPDATA", "")) / "DeepCool" / "Pictures"
    return sum(1 for p in base.glob("*/jpg/*") if p.is_dir())


def to_qimage(img: Image.Image) -> QImage:
    rgba = img.convert("RGBA")
    return QImage(rgba.tobytes(), rgba.width, rgba.height, QImage.Format.Format_RGBA8888).copy()


class Engine(QThread):
    frameChanged = Signal(QImage, bool)   # image, is_placeholder
    stateChanged = Signal(dict)

    def __init__(self, config: Config, use_device: bool = True, parent=None):
        super().__init__(parent)
        self.config = config
        self.use_device = use_device
        self._commands: queue.Queue = queue.Queue()
        self._stop = False
        self._mode: Mode | None = None
        self._frame: Image.Image | None = None
        self._frame_seq = 0
        self._uploaded_seq = -1
        self._last_upload = 0.0
        self._last_upload_wall: float | None = None
        self._dev: device.Mystique | None = None
        self._dev_state = "missing" if use_device else "preview"   # connected | busy | missing | preview
        self._next_open = 0.0
        self._on_screen = None            # "ours" | "restore" | None (unknown)
        self._slideshow_applied: bool | None = None
        self._last_state: dict | None = None
        self._placeholder_shown = None

    # ---- thread-safe API for the UI ------------------------------------
    def reload_mode(self) -> None:
        """Call after the selected mode or its settings changed in the config."""
        self._commands.put(self._make_mode)

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
        self._mode = cls(self.config.mode_settings(cls.id))
        self._frame = None
        self._emit_placeholder()

    def _emit_placeholder(self) -> None:
        self._placeholder_shown = self._mode.placeholder
        title, subtitle = self._mode.placeholder or (self._mode.title, "")
        self.frameChanged.emit(to_qimage(render.message(title, subtitle)), True)

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
                    self._frame = img
                    self._frame_seq += 1
                    self.frameChanged.emit(to_qimage(img), False)
                elif self._frame is None and self._mode.placeholder != self._placeholder_shown:
                    self._emit_placeholder()   # a mode may refine its placeholder (e.g. "city not found")
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
        try:
            slideshow = bool(self.config.get("slideshow"))
            if slideshow != self._slideshow_applied:
                self._dev.set_slideshow(slideshow)
                self._slideshow_applied = slideshow
            if not enabled:
                if self._on_screen != "restore":
                    self._dev.show_slot(self.config.get("restore_index"))
                    self._on_screen = "restore"
                return
            if self._frame is None:
                return
            if self._frame_seq != self._uploaded_seq:
                if now - self._last_upload < MIN_UPLOAD_INTERVAL:
                    return
                index = self.config.get("slot_index")
                if index is None:
                    index = _stored_jpg_count()
                    self.config.set("slot_index", index)
                self._dev.upload_jpeg(SLOT_ID, device.encode_jpeg(self._frame))
                self._dev.show_slot(index)
                self._uploaded_seq = self._frame_seq
                self._last_upload, self._last_upload_wall = time.monotonic(), time.time()
                self._on_screen = "ours"
            elif self._on_screen != "ours":
                self._dev.show_slot(self.config.get("slot_index"))
                self._on_screen = "ours"
        except (usb.core.USBError, RuntimeError):
            traceback.print_exc()
            self._drop_device()

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
            "last_upload": self._last_upload_wall,
            "pending": enabled and self._frame is not None and self._frame_seq != self._uploaded_seq,
        }
        if state != self._last_state:
            self._last_state = state
            self.stateChanged.emit(state)

    def _shutdown(self) -> None:
        if self._dev is None:
            return
        try:
            if self._on_screen == "ours":
                self._dev.show_slot(self.config.get("restore_index"))
        except (usb.core.USBError, RuntimeError):
            pass
        self._drop_device()
