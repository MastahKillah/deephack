"""Persistent settings, stored as JSON in %APPDATA%\\deephack\\config.json."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from . import APP_NAME

# Fixed id of our image slot on the device; uploading to the same id overwrites in place.
SLOT_ID = "0de3b4c4" + "0" * 23 + "1"
GIF_SLOT_ID = "0de3b4c4" + "0" * 23 + "2"     # same idea for animations, which the device stores separately



def book_id(i: int) -> str:
    """File id of frame i of the flipbook (a set of stills a mode flips through, see modes.base.Flipbook)."""
    return "0de3b4c4" + "0" * 20 + f"b{i:03x}"


DEFAULTS = {
    "enabled": True,
    "mode": "now_playing",
    "slideshow": False,        # False: keep our image on screen. True: cooler cycles through all stored images.
    "close_to_tray": True,
    "light": "screen",         # the pump head's light strip: screen (edge colours) | motherboard | warning
    "brightness": 70,          # screen backlight, percent (70 is what the official app sends)
    "slot_index": None,        # index of our image among the JPGs stored on the device
    "ground": False,           # True: that image is the plain one frames are drawn over (direct drawing firmware)
    "gif_slot_index": None,    # index of our animation among the GIFs stored on the device
    "restore_index": 0,        # JPG shown when deephack is paused or quits
    "book": None,              # the flipbook stored on the device: {"key", "first" (index of frame 0), "count"}
    "modes": {},               # per-mode settings, keyed by mode id
}


def config_dir() -> Path:
    d = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "config.json"
        self._lock = threading.Lock()
        self.data = json.loads(json.dumps(DEFAULTS))
        try:
            self.data.update(json.loads(self.path.read_text(encoding="utf8")))
        except (OSError, ValueError):
            pass

    def get(self, key: str):
        with self._lock:
            return self.data.get(key, DEFAULTS.get(key))

    def set(self, key: str, value) -> None:
        with self._lock:
            self.data[key] = value
            self._save()

    def mode_settings(self, mode_id: str) -> dict:
        with self._lock:
            return dict(self.data["modes"].get(mode_id, {}))

    def set_mode_setting(self, mode_id: str, key: str, value) -> None:
        with self._lock:
            self.data["modes"].setdefault(mode_id, {})[key] = value
            self._save()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf8")
        tmp.replace(self.path)
