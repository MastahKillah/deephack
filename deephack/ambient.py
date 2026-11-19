"""Ambient light: the PC's RGB lights take the colours of whatever deephack shows.

Runs on its own thread next to the engine: the engine hands over every new frame (show() never
blocks), this picks the vivid colour along the picture's edges, ambilight style, and fades every LED
OpenRGB knows to it over a second. Nothing here touches the cooler's flash or waits on the device.
"""
from __future__ import annotations

import colorsys
import threading
import time

from PIL import Image

from . import openrgb
from .modes.base import Animation, Dashboard

FADE_SECONDS = 0.8
STEP_SECONDS = 0.04
RETRY_SECONDS = 5.0
REFRESH_SECONDS = 30.0          # look for new devices and resized zones
DIM = (24, 18, 12)              # a dark picture: a faint warm glow rather than off


def screen_colour(img: Image.Image) -> tuple[int, int, int]:
    """The vivid colour along the picture's edges: saturated pixels count most, grey ones barely."""
    small = img.convert("RGB").resize((24, 32), Image.BILINEAR)
    w, h = small.size
    px = small.load()
    ring = [px[x, y] for y in range(h) for x in range(w) if x < 3 or x >= w - 3 or y < 3 or y >= h - 3]
    total, weights, brightness = [0.0, 0.0, 0.0], 0.0, 0.0
    for r, g, b in ring:
        _, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        weight = s * v + 0.01
        for i, c in enumerate((r, g, b)):
            total[i] += c * weight
        weights += weight
        brightness += v
    hue, sat, val = colorsys.rgb_to_hsv(*(c / weights / 255 for c in total))
    brightness /= len(ring)
    if val < 0.08:
        return DIM
    if sat < 0.18:                                   # a grey picture: soft white, as bright as the picture
        level = 0.25 + 0.6 * brightness
        return tuple(round(255 * level * c) for c in (1.0, 0.86, 0.72))
    sat = min(1.0, sat * 1.4 + 0.15)                 # LEDs wash colours out: push them
    level = min(1.0, 0.45 + brightness)
    return tuple(round(255 * c) for c in colorsys.hsv_to_rgb(hue, sat, level))


def _picture(frame) -> Image.Image | None:
    if isinstance(frame, Animation):
        return frame.frames[0] if frame.frames else None
    if isinstance(frame, Dashboard):
        return frame.preview
    return frame if isinstance(frame, Image.Image) else None


class Ambient:
    def __init__(self, config, connect=openrgb.Client):
        self.config = config
        self._connect = connect
        self._lock = threading.Lock()
        self._frame = None
        self._dirty = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = "off"               # off | unreachable | "<n> device(s): names" | no LEDs

    # ---- called from the engine
    def show(self, frame) -> None:
        img = _picture(frame)
        if img is not None:
            with self._lock:
                self._frame, self._dirty = img, True

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="deephack-ambient")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ---- the thread
    def _run(self) -> None:
        client, devices = None, []
        current, start, target, since = (0, 0, 0), (0, 0, 0), None, 0.0
        next_refresh = 0.0
        while not self._stop.is_set():
            if not self.config.get("ambient"):
                if client is not None:
                    client.close()
                    client = None
                self.status = "off"
                self._stop.wait(0.5)
                continue
            now = time.monotonic()
            try:
                if client is None:
                    client = self._connect()
                    next_refresh, current = 0.0, None
                if now >= next_refresh or client.list_changed:
                    devices = [d for d in client.devices() if d.leds]
                    for d in devices:
                        client.set_custom_mode(d.index)
                    next_refresh, current = now + REFRESH_SECONDS, None
                    self.status = (f"{len(devices)} device{'s' if len(devices) != 1 else ''}: " + ", ".join(d.name for d in devices)
                                   if devices else "no LEDs")
                with self._lock:
                    frame, dirty, self._dirty = self._frame, self._dirty, False
                if dirty and frame is not None:
                    start, target, since = current or (0, 0, 0), screen_colour(frame), now
                if target is not None:
                    t = min(1.0, (now - since) / FADE_SECONDS)
                    colour = tuple(round(a + (b - a) * t) for a, b in zip(start, target))
                    if colour != current:
                        for d in devices:
                            client.set_leds(d.index, [colour] * d.leds)
                        current = colour
            except OSError:
                if client is not None:
                    client.close()
                client, current = None, None
                self.status = "unreachable"
                self._stop.wait(RETRY_SECONDS)
                continue
            self._stop.wait(STEP_SECONDS)
        if client is not None:
            client.close()
