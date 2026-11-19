"""Live: the cooler's own sensor screens, fed with real readings every second.

The firmware has built-in screens (a big CPU temperature, frequency, fan and pump speeds, a clock, and a
7 minute graph of frequency and temperature) that draw whatever numbers arrive over USB. Nothing of it is
stored, so unlike every other mode this one can change every second without wearing the flash. The
official app feeds them too, but leaves the fan and pump speeds at zero.

The readings come from DeepCool's sensor service (see system.py) on a background thread, once a second,
which stops by itself a few seconds after no mode asks for readings any more. The window shows an
approximation of the screen; the cooler draws its own labels and layout.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from datetime import datetime
from functools import lru_cache

from PIL import Image, ImageDraw

from .. import device, render
from ..render import H, W
from .base import Dashboard, Mode, Setting
from .system import BridgeClient, WindowsCounters, _number

SCREENS = {   # setting value -> (screen, main layout or recorder graphs, style)
    "temperature": ("dashboard", device.DASH_TEMPERATURE, 0),
    "frequency": ("dashboard", device.DASH_FREQUENCY, 0),
    "frequency_ring": ("dashboard", device.DASH_FREQUENCY, 1),
    "fan_pump": ("dashboard", device.DASH_FAN_PUMP, 0),
    "pump": ("dashboard", device.DASH_PUMP, 0),
    "fan": ("dashboard", device.DASH_FAN, 0),
    "time": ("dashboard", device.DASH_TIME, 0),
    "recorder": ("recorder", 0, 0),
    "recorder_frequency": ("recorder", 1, 0),
}
STRIPS = {"system": device.STRIP_SYSTEM, "core": device.STRIP_CORE, "voltage": device.STRIP_VOLTAGE}
FEED_IDLE = 10.0              # the reading thread ends this long after the last request
RECORDER_SECONDS = 7 * 60     # what the cooler's graphs span

BG, GRID = (5, 8, 15), (13, 24, 40)
CYAN, LIGHT, WHITE, ROSE = (70, 190, 255), (150, 212, 255), (240, 246, 255), (238, 160, 170)


# ------------------------------------------------------------------ readings
def pick_fans(fans: dict) -> tuple[float | None, float | None]:
    """(CPU fan, pump) from the board's fan list, e.g. {"AIO Pump": 3341, "CPU": 1700, "CPU_OPT": 829}."""
    fans = {str(name): float(rpm) for name, rpm in fans.items() if _number(rpm) is not None}
    pump = next((rpm for name, rpm in fans.items() if "pump" in name.lower()), None)
    others = [(name.lower().replace("_", " "), rpm) for name, rpm in fans.items() if "pump" not in name.lower()]
    cpu = next((rpm for name, rpm in others if name in ("cpu", "cpu fan", "cpu fan 1", "cpu fan1")), None)
    if cpu is None:
        cpu = next((rpm for name, rpm in others if name.startswith("cpu") and "opt" not in name), None)
    if cpu is None and others:
        cpu = others[0][1]
    return cpu, pump


def dashboard_values(body: dict | None, windows: WindowsCounters) -> tuple[dict[str, float], bool]:
    """The fields of the cooler's sensor frame (device.SENSOR_FIELDS), and whether the sensor service answered."""
    load = windows.cpu_load()                   # every second, so the fallback always has a baseline
    if body is None:
        memory = windows.memory()
        return {"cpu_load": load or 0.0, "ram_usage": memory[0] if memory else 0.0}, False

    def value(key):
        entry = body.get(key)
        return _number(entry.get("Value")) if isinstance(entry, dict) else None

    fan, pump = pick_fans(body.get("Fan list") if isinstance(body.get("Fan list"), dict) else {})
    clock = value("CPU Clock")
    raw = {"cpu_temp": value("CPU Temperature"), "cpu_load": value("CPU Usage"), "ram_usage": value("Memory Load"),
           "v3_3": value("Motherboard v3 volt"), "v5": value("Motherboard v5 volt"), "v12": value("Motherboard v12 volt"),
           "cpu_freq": clock / 1000 if clock else None, "fan_rpm": fan, "pump_rpm": pump}
    return {k: min(max(v or 0.0, 0.0), 65535.0) for k, v in raw.items()}, True


class LiveFeed:
    """Readings once a second on a daemon thread, shared by every Live mode (the live one and a preview).

    The thread starts on the first request and ends FEED_IDLE seconds after the last one, so a mode that
    is switched away (the engine does not close modes) leaves nothing running."""

    def __init__(self, read=None):
        self._read = read
        self._lock = threading.Lock()
        self._latest: tuple[float, dict[str, float], bool] | None = None
        self._thread: threading.Thread | None = None
        self._asked = 0.0

    def latest(self) -> tuple[float, dict[str, float], bool] | None:
        """(time, values, from the sensor service) of the newest reading, None before the first."""
        with self._lock:
            self._asked = time.monotonic()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True, name="deephack-live")
                self._thread.start()
            return self._latest

    def _run(self) -> None:
        client, windows = BridgeClient(), WindowsCounters()
        read = self._read or (lambda: dashboard_values(client.snapshot(), windows))
        try:
            while time.monotonic() - self._asked < FEED_IDLE:
                started = time.monotonic()
                values, sensors = read()
                with self._lock:
                    self._latest = (time.time(), values, sensors)
                time.sleep(max(0.05, 1.0 - (time.monotonic() - started)))
        finally:
            client.close()


FEED = LiveFeed()


# ------------------------------------------------------------------ preview
@lru_cache(maxsize=1)
def _background() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    for x in range(0, W, 16):
        d.line([(x, 0), (x, H)], fill=GRID)
    for y in range(0, H, 16):
        d.line([(0, y), (W, y)], fill=GRID)
    return img


def _gauge(d: ImageDraw.ImageDraw, cy: float, label: str, value: str, unit: str, size: int = 128) -> None:
    d.text((W / 2, cy - size * 0.78), label, font=render.font(render.SEMIBOLD, 25), fill=CYAN, anchor="mm")
    d.text((W / 2, cy), value, font=render.font(render.BOLD, size), fill=WHITE, anchor="mm")
    d.text((W / 2, cy + size * 0.68), unit, font=render.font(render.SEMIBOLD, 26), fill=CYAN, anchor="mm")


def _strip(d: ImageDraw.ImageDraw, strip: str, v: dict[str, float]) -> None:
    cells = {
        "system": ((f"{v.get('cpu_freq', 0):.2f}", "GHz"), (f"{v.get('cpu_load', 0):.0f}", "% CPU"), (f"{v.get('ram_usage', 0):.0f}", "% RAM")),
        "core": ((f"{v.get('cpu_freq', 0):.2f}", "GHz"), (f"{v.get('ram_usage', 0):.0f}", "% RAM")),
        "voltage": ((f"{v.get('v3_3', 0):.2f}", "3.3V"), (f"{v.get('v5', 0):.2f}", "5V"), (f"{v.get('v12', 0):.2f}", "12V")),
    }[strip]
    col = W / len(cells)
    for i, (value, label) in enumerate(cells):
        cx = col * (i + 0.5)
        d.text((cx, 540), value, font=render.font(render.SEMIBOLD, 34), fill=LIGHT, anchor="mm")
        d.text((cx, 576), label, font=render.font(render.SEMIBOLD, 17), fill=CYAN, anchor="mm")


def _graph(img: Image.Image, box, points: list[tuple[float, float]], t1: float, lo: float, hi: float, colour) -> None:
    x0, y0, x1, y1 = box
    d = ImageDraw.Draw(img)
    d.line([(x0, y1), (x1, y1)], fill=colour, width=3)
    pts = [(x0 + (x1 - x0) * (1 - (t1 - t) / RECORDER_SECONDS), y1 - (y1 - y0) * (min(max(v, lo), hi) - lo) / (hi - lo))
           for t, v in points if t1 - t <= RECORDER_SECONDS]
    if len(pts) > 1:
        d.line(pts, fill=colour, width=4, joint="curve")


def render_preview(screen: str, strip: str, values: dict[str, float], recent: list[tuple[float, dict[str, float]]],
                   now: float) -> Image.Image:
    """deephack's approximation of the cooler's sensor screen (the cooler draws its own)."""
    img = _background().copy()
    d = ImageDraw.Draw(img)
    v = values
    kind, main, style = SCREENS[screen]
    if kind == "recorder":
        d.text((40, 70), "CPU", font=render.font(render.BOLD, 46), fill=CYAN, anchor="lm")
        d.text((W - 40, 56), f"{v.get('cpu_freq', 0):.2f} GHz", font=render.font(render.BOLD, 34), fill=LIGHT, anchor="rm")
        both = main == 0
        if both:
            d.text((W - 40, 100), f"{v.get('cpu_temp', 0):.0f} °C", font=render.font(render.BOLD, 30), fill=ROSE, anchor="rm")
        label = render.font(render.SEMIBOLD, 18)
        freq = [(t, r.get("cpu_freq", 0)) for t, r in recent]
        top, bottom = (150, 330) if both else (150, 560)
        d.text((40, top - 14), "CPU FREQUENCY", font=label, fill=CYAN, anchor="lm")
        _graph(img, (40, top + 10, W - 40, bottom), freq, now, 0, max([f for _, f in freq] + [1]) * 1.15, CYAN)
        if both:
            temp = [(t, r.get("cpu_temp", 0)) for t, r in recent]
            d.text((40, 380), "CPU TEMPERATURE", font=label, fill=ROSE, anchor="lm")
            _graph(img, (40, 400, W - 40, 560), temp, now, 20, 100, ROSE)
        d.text((40, 590), "-7 min", font=label, fill=CYAN, anchor="lm")
        d.text((W - 40, 590), "now", font=label, fill=CYAN, anchor="rm")
        return img

    if main == device.DASH_TEMPERATURE:
        _gauge(d, 250, "CPU TEMPERATURE", f"{v.get('cpu_temp', 0):.0f}", "°C", 150)
    elif main == device.DASH_FREQUENCY:
        freq = v.get("cpu_freq", 0)
        if style == 1:                       # ring around the number
            box = [W / 2 - 165, 250 - 165, W / 2 + 165, 250 + 165]
            d.arc(box, 135, 405, fill=GRID, width=14)
            d.arc(box, 135, 135 + 270 * min(freq / 6, 1), fill=CYAN, width=14)
        _gauge(d, 250, "CPU FREQUENCY", f"{freq:.2f}", "GHz", 116 if style == 1 else 140)
    elif main == device.DASH_PUMP:
        _gauge(d, 250, "PUMP SPEED", f"{v.get('pump_rpm', 0):.0f}", "RPM", 130)
    elif main == device.DASH_FAN:
        _gauge(d, 250, "CPU FAN SPEED", f"{v.get('fan_rpm', 0):.0f}", "RPM", 130)
    elif main == device.DASH_FAN_PUMP:
        _gauge(d, 170, "CPU FAN SPEED", f"{v.get('fan_rpm', 0):.0f}", "RPM", 92)
        _gauge(d, 370, "PUMP SPEED", f"{v.get('pump_rpm', 0):.0f}", "RPM", 92)
    elif main == device.DASH_TIME:
        t = datetime.fromtimestamp(now)
        d.text((W / 2, 230), t.strftime("%H:%M"), font=render.font(render.BOLD, 140), fill=WHITE, anchor="mm")
        d.text((W / 2, 340), t.strftime("%a %d %b").upper(), font=render.font(render.SEMIBOLD, 28), fill=CYAN, anchor="mm")
    _strip(d, strip, v)
    return img


def status_line(values: dict[str, float], sensors: bool) -> str:
    if not sensors:
        return f"CPU {values.get('cpu_load', 0):.0f}% · RAM {values.get('ram_usage', 0):.0f}% · no sensor service"
    parts = [f"CPU {values.get('cpu_temp', 0):.0f}°", f"{values.get('cpu_freq', 0):.2f} GHz"]
    if values.get("pump_rpm"):
        parts.append(f"pump {values['pump_rpm']:.0f} rpm")
    if values.get("fan_rpm"):
        parts.append(f"fan {values['fan_rpm']:.0f} rpm")
    return " · ".join(parts)


class Live(Mode):
    id = "live"
    title = "Live"
    description = "The cooler's own gauges, live"
    glyph = ""
    settings = (
        Setting("screen", "Screen", "choice", "temperature", (
            ("temperature", "CPU temperature"), ("frequency", "CPU frequency"), ("frequency_ring", "CPU frequency, ring"),
            ("fan_pump", "CPU fan and pump"), ("pump", "Pump speed"), ("fan", "CPU fan speed"), ("time", "Clock"),
            ("recorder", "Graphs: frequency and temperature"), ("recorder_frequency", "Graph: frequency")),
                hint="The cooler draws these screens itself from numbers deephack sends every second, so nothing is "
                     "written to its flash. Labels and layout are the cooler's; the preview here is an approximation."),
        Setting("strip", "Bottom row", "choice", "system",
                (("system", "Frequency, CPU load, RAM"), ("core", "Frequency, RAM"), ("voltage", "3.3 V, 5 V and 12 V")),
                hint="Not shown with the graphs. Readings come from DeepCool's sensor service, like the System mode."),
    )

    def __init__(self, values=None, feed: LiveFeed | None = None):
        super().__init__(values)
        self.feed = feed or FEED
        self._last: float | None = None
        self._recent: deque[tuple[float, dict[str, float]]] = deque(maxlen=RECORDER_SECONDS + 30)
        self.placeholder = ("Live", "Waiting for the first readings…")

    async def frame(self):
        latest = self.feed.latest()
        if latest is None or latest[0] == self._last:
            return None                                   # one frame per reading, once a second
        t, values, sensors = latest
        self._last = t
        self._recent.append((t, values))
        self.status = status_line(values, sensors)
        screen = self.values["screen"] if self.values["screen"] in SCREENS else "temperature"
        strip = self.values["strip"] if self.values["strip"] in STRIPS else "system"
        kind, main, style = SCREENS[screen]
        layout = (main,) if kind == "recorder" else (main, style, STRIPS[strip])
        preview = await asyncio.to_thread(render_preview, screen, strip, values, list(self._recent), t)
        return Dashboard(kind, layout, values, preview)
