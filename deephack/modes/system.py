"""System: CPU and GPU temperature and load, memory and fan speeds, with the last hour as graphs.

The readings come from DeepCool's own sensor service ("JZFS Sensor Bridge", installed with the DeepCool
app as a Windows service). It reads the hardware through HWiNFO and answers on a local named pipe, also to
programs without admin rights: framed JSON requests, the same ones the DeepCool app's bundled
tools/fan-sensor-observer.mjs makes. Without the service only CPU load and memory (from Windows) are shown.

On DeepCool's own firmware every redraw is a write to the cooler's flash, so there this is a trend view
redrawn every few minutes, not a live readout. Readings are taken every 10 seconds in between, on a
short-lived thread so a stuck service cannot stall the engine, and kept for three hours, also across restarts.

With the direct drawing firmware (`direct`, set by the engine) a picture costs nothing, and the page is
live: a reading and a redraw every second, the clock with seconds, and graphs that can show the last
minute or the last five, one step per reading. Unlike the Live mode, which fills in the cooler's own
screens, this is our layout, with the GPU on it.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import struct
import threading
import time
import traceback
import zlib
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

from .. import render
from ..config import config_dir
from ..render import H, W
from .base import Mode, Setting

PIPE = r"\\.\pipe\jzfs_sensor_bridge_v2"
MAX_FRAME = 1 << 20
SAMPLE_SECONDS = 10
KEEP_SECONDS = 3 * 3600
FEED_IDLE = 10.0                      # a once-a-second feed's thread ends this long after the last request
LIVE_SECONDS = 5 * 60                 # graphs up to this long are drawn from the once-a-second readings
SAVE_SECONDS = 5 * 60                 # the history file is written this often while the page is live
STORED_MINUTES = 30                   # the shortest graph worth storing a picture of
HISTORY_NAME = "system-history.json"
BUSY_SECONDS, BUSY_LOAD = 120, 75     # Auto: CPU or GPU averaging this load (%) over this long
MIN_POINTS = 6                        # fewer readings in a graph: say it is still collecting, redraw soon
EARLY_REDRAW = 2 * 60

BG_TOP, BG_BOTTOM = (20, 22, 31), (8, 9, 14)
CARD = (255, 255, 255, 14)
TEXT, MUTED, DIM = (240, 242, 248), (140, 146, 166), (86, 92, 112)
CPU_C, GPU_C, MEM_C, FAN_C = (255, 140, 82), (120, 206, 126), (112, 162, 255), (120, 210, 230)
AMBER, RED = (240, 180, 70), (236, 96, 96)


# ------------------------------------------------------------------ readings
@dataclass(frozen=True)
class Reading:
    t: float                                   # epoch seconds
    cpu_temp: float | None = None              # °C
    cpu_load: float | None = None              # %
    cpu_power: float | None = None             # W
    gpu_temp: float | None = None
    gpu_load: float | None = None
    gpu_power: float | None = None
    mem_load: float | None = None              # %
    mem_used: float | None = None              # MB
    mem_total: float | None = None             # MB
    fans: tuple[tuple[str, float], ...] = ()   # (name, rpm), as the board names them

    @property
    def sensors(self) -> bool:
        """Whether this came from the sensor service (temperatures), not just Windows."""
        return self.cpu_temp is not None or self.gpu_temp is not None or bool(self.fans)


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_snapshot(body: dict, t: float) -> Reading:
    """A GET_SENSOR_SNAPSHOT body: {"CPU Temperature": {"Unit": "C", "Value": 75.5}, ..., "Fan list": {name: rpm}}."""
    def value(key):
        entry = body.get(key)
        return _number(entry.get("Value")) if isinstance(entry, dict) else None

    def temp(key):                              # 0 means "no such sensor"
        v = value(key)
        return v if v and v > 0 else None

    used, free = value("Memory Used"), value("Memory Available")
    fans = body.get("Fan list") if isinstance(body.get("Fan list"), dict) else {}
    return Reading(
        t, cpu_temp=temp("CPU Temperature"), cpu_load=value("CPU Usage"), cpu_power=value("CPU Power"),
        gpu_temp=temp("Gpu temperature"), gpu_load=value("Gpu usage"), gpu_power=value("Gpu power"),
        mem_load=value("Memory Load"), mem_used=used, mem_total=used + free if used is not None and free is not None else None,
        fans=tuple((str(name), float(rpm)) for name, rpm in fans.items() if _number(rpm) and rpm > 0),
    )


def _frame(seq: int, op: str) -> bytes:
    payload = json.dumps({"op": op, "body": {}}).encode()
    return b"DCS2" + bytes((2, 1, 0, 0)) + struct.pack("<III", len(payload), seq, zlib.crc32(payload)) + payload


def _read_exact(f, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            raise OSError("sensor pipe closed")
        buf += chunk
    return buf


def read_frame(f) -> tuple[int, int, dict]:
    """(kind, sequence, payload) of the next DCS2 frame. Kind 2 answers a request, 3 is a pushed event."""
    head = _read_exact(f, 20)
    if head[:4] != b"DCS2" or head[4] != 2:
        raise ValueError("not a DCS2 frame")
    size, seq, crc = struct.unpack("<III", head[8:20])
    if size > MAX_FRAME:
        raise ValueError("oversize DCS2 frame")
    body = _read_exact(f, size)
    if zlib.crc32(body) != crc:
        raise ValueError("DCS2 checksum mismatch")
    return head[5], seq, json.loads(body)


class BridgeClient:
    """Connection to DeepCool's sensor service, kept open between snapshots and reopened after an error."""

    def __init__(self, pipe: str = PIPE):
        self.pipe = pipe
        self._f = None
        self._seq = 0

    def _request(self, op: str) -> dict:
        self._seq += 1
        self._f.write(_frame(self._seq, op))
        for _ in range(16):                     # skip events pushed in between
            kind, got, payload = read_frame(self._f)
            if kind == 2 and got == self._seq:
                if not payload.get("ok"):
                    raise ValueError(f"{op} refused")
                return payload
        raise ValueError(f"no answer to {op}")

    def snapshot(self) -> dict | None:
        """The current readings, None when the service is not installed or not answering."""
        try:
            if self._f is None:
                self._f = open(self.pipe, "r+b", buffering=0)
                self._request("HELLO")
            body = self._request("GET_SENSOR_SNAPSHOT").get("body")
            return body if isinstance(body, dict) else None
        except (OSError, ValueError):
            self.close()
            return None

    def close(self) -> None:
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass
            self._f = None


def read_bridge(pipe: str = PIPE) -> dict | None:
    """One sensor snapshot from DeepCool's sensor service, None when it is not installed or not answering."""
    client = BridgeClient(pipe)
    try:
        return client.snapshot()
    finally:
        client.close()


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    @property
    def value(self) -> int:
        return self.high << 32 | self.low


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32), ("total", ctypes.c_uint64),
                ("avail", ctypes.c_uint64), ("page_total", ctypes.c_uint64), ("page_avail", ctypes.c_uint64),
                ("virtual_total", ctypes.c_uint64), ("virtual_avail", ctypes.c_uint64), ("extended", ctypes.c_uint64)]


class WindowsCounters:
    """CPU load and memory straight from Windows, for when the sensor service is missing."""

    def __init__(self):
        self._last: tuple[int, int] | None = None

    def cpu_load(self) -> float | None:
        idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        now = (idle.value, kernel.value + user.value)          # kernel time includes the idle time
        last, self._last = self._last, now
        if last is None or now[1] <= last[1]:
            return None
        return max(0.0, min(100.0, 100.0 * (1 - (now[0] - last[0]) / (now[1] - last[1]))))

    @staticmethod
    def memory() -> tuple[float, float, float] | None:
        """(load %, used MB, total MB)"""
        status = _MemoryStatus(length=ctypes.sizeof(_MemoryStatus))
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        mb = 1024 * 1024
        return float(status.load), (status.total - status.avail) / mb, status.total / mb


def reading_from(body: dict | None, windows: WindowsCounters) -> Reading:
    """A reading from a sensor snapshot, or from Windows alone when the service gave none."""
    now = time.time()
    load = windows.cpu_load()                   # always, so the next fallback reading has a baseline
    if body is not None:
        return parse_snapshot(body, now)
    memory = windows.memory()
    return Reading(now, cpu_load=load, **(dict(zip(("mem_load", "mem_used", "mem_total"), memory)) if memory else {}))


def take_reading(windows: WindowsCounters) -> Reading:
    return reading_from(read_bridge(), windows)


class Feed:
    """Something read from the sensor service once a second on a daemon thread, shared by every mode that
    wants it (the one on the cooler and a preview).

    `convert(snapshot or None, WindowsCounters)` makes a tuple of it; `latest()` gives the newest one with
    its time in front. The thread starts on the first request and ends `idle()` seconds after the last
    one, so a mode that is switched away (the engine does not close modes) leaves nothing running. With
    `keep`, that many of the newest are kept for `recent()`."""

    def __init__(self, convert, read=None, keep: int = 0):
        self._convert = convert
        self._read = read
        self._lock = threading.Lock()
        self._latest: tuple | None = None
        self._recent: deque[tuple] = deque(maxlen=keep)
        self._thread: threading.Thread | None = None
        self._asked = 0.0

    def idle(self) -> float:
        return FEED_IDLE

    def latest(self) -> tuple | None:
        """(time, *what convert made) of the newest reading, None before the first."""
        with self._lock:
            self._asked = time.monotonic()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True, name="deephack-live")
                self._thread.start()
            return self._latest

    def recent(self) -> list[tuple]:
        with self._lock:
            return list(self._recent)

    def _run(self) -> None:
        client, windows = BridgeClient(), WindowsCounters()
        read = self._read or (lambda: self._convert(client.snapshot(), windows))
        try:
            while time.monotonic() - self._asked < self.idle():
                started = time.monotonic()
                got = (time.time(), *read())
                with self._lock:
                    self._latest = got
                    self._recent.append(got)
                time.sleep(max(0.05, 1.0 - (time.monotonic() - started)))
        finally:
            client.close()


READINGS = Feed(lambda body, windows: (reading_from(body, windows),), keep=LIVE_SECONDS + 30)


# ------------------------------------------------------------------ history
class _Job:
    """Runs a function on a daemon thread (a hung pipe must neither stall the engine nor block quitting)."""

    def __init__(self, fn, *args):
        self.result = None
        self.done = threading.Event()
        threading.Thread(target=self._run, args=(fn, args), daemon=True, name="deephack-sensors").start()

    def _run(self, fn, args) -> None:
        try:
            self.result = fn(*args)
        except Exception:
            traceback.print_exc()
        finally:
            self.done.set()


class History:
    """Readings of the last three hours, shared by every System mode instance (the live one and a preview)."""

    def __init__(self, path: Path | None):
        self.path = path
        self.readings: deque[Reading] = deque()
        self.windows = WindowsCounters()
        self._job: _Job | None = None
        self._next = 0.0
        self.load()

    def load(self) -> None:
        if self.path is None:
            return
        try:
            rows = json.loads(self.path.read_text(encoding="utf8"))
            cutoff = time.time() - KEEP_SECONDS
            for row in rows:
                if row.get("t", 0) >= cutoff:
                    row["fans"] = tuple(tuple(f) for f in row.get("fans") or ())
                    self.readings.append(Reading(**row))
        except (OSError, ValueError, TypeError, AttributeError):
            self.readings.clear()

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.write_text(json.dumps([asdict(r) for r in self.readings], separators=(",", ":")), encoding="utf8")
        except OSError:
            pass

    def add(self, reading: Reading) -> None:
        self.readings.append(reading)
        while self.readings and self.readings[0].t < reading.t - KEEP_SECONDS:
            self.readings.popleft()

    def tick(self, now: float | None = None) -> None:
        """Collect a finished reading and start the next one when it is due. Never blocks."""
        now = now or time.time()
        if self._job is not None and self._job.done.is_set():
            if self._job.result is not None:
                self.add(self._job.result)
            self._job = None
        if self._job is None and now >= self._next:
            self._next = now + SAMPLE_SECONDS
            self._job = _Job(take_reading, self.windows)

    def latest(self) -> Reading | None:
        return self.readings[-1] if self.readings else None

    def since(self, t: float) -> list[Reading]:
        return [r for r in self.readings if r.t >= t]


_shared: History | None = None


def shared_history() -> History:
    global _shared
    if _shared is None:
        _shared = History(config_dir() / HISTORY_NAME)
    return _shared


def working_hard(readings: list[Reading], now: float) -> bool:
    recent = [r for r in readings if r.t >= now - BUSY_SECONDS]
    if len(recent) < BUSY_SECONDS / SAMPLE_SECONDS / 2:
        return False
    for key in ("cpu_load", "gpu_load"):
        values = [getattr(r, key) for r in recent if getattr(r, key) is not None]
        if values and sum(values) / len(values) >= BUSY_LOAD:
            return True
    return False


# ------------------------------------------------------------------ drawing
def _segments(readings: list[Reading], key: str) -> list[list[tuple[float, float]]]:
    """Runs of (t, value) without gaps: a missing value or a pause in the readings breaks the line."""
    runs, run, last_t = [], [], None
    for r in readings:
        v = getattr(r, key)
        if v is None or (last_t is not None and r.t - last_t > 3 * SAMPLE_SECONDS):
            if run:
                runs.append(run)
            run = []
        if v is not None:
            run.append((r.t, v))
        last_t = r.t
    if run:
        runs.append(run)
    return runs


def chart(img: Image.Image, box, readings: list[Reading], t0: float, t1: float, colour,
          line: str | None, area: str | None, line_range: tuple[float, float]) -> None:
    """Load (0-100 %) as a soft area and a temperature as a line, on a shared time axis from t0 to t1."""
    x0, y0, x1, y1 = box
    if sum(1 for r in readings if getattr(r, area or line) is not None) < MIN_POINTS:
        ImageDraw.Draw(img).text(((x0 + x1) / 2, (y0 + y1) / 2), "Collecting readings…", font=render.font(render.REGULAR, 16),
                                 fill=DIM, anchor="mm")
    s = 2                                                   # drawn at twice the size for smooth edges
    w, h = (x1 - x0) * s, (y1 - y0) * s
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.line([(0, h - s), (w, h - s)], fill=DIM + (90,), width=s)

    def x(t):
        return (t - t0) / (t1 - t0) * (w - 1)

    def y(v, lo, hi, pad):
        return pad + (h - 2 * pad) * (1 - (min(max(v, lo), hi) - lo) / (hi - lo))

    if area:
        for run in _segments(readings, area):
            pts = [(x(t), y(v, 0, 100, 0)) for t, v in run]
            if len(pts) == 1:
                pts.append((pts[0][0] + s, pts[0][1]))
            d.polygon(pts + [(pts[-1][0], h), (pts[0][0], h)], fill=colour + (52 if line else 90,))
    if line:
        lo, hi = line_range
        for run in _segments(readings, line):
            pts = [(x(t), y(v, lo, hi, 3 * s)) for t, v in run]
            if len(pts) == 1:
                d.ellipse([pts[0][0] - 3 * s, pts[0][1] - 3 * s, pts[0][0] + 3 * s, pts[0][1] + 3 * s], fill=colour + (255,))
            else:
                d.line(pts, fill=colour + (255,), width=3 * s, joint="curve")
    img.alpha_composite(layer.resize((x1 - x0, y1 - y0), Image.LANCZOS), (x0, y0))


def _temp_range(readings: list[Reading], key: str) -> tuple[float, float]:
    """Fit the line, but never zoom in closer than 20 °C, so small wobbles stay small."""
    values = [getattr(r, key) for r in readings if getattr(r, key) is not None]
    if not values:
        return 20.0, 100.0
    lo, hi = min(values) - 4, max(values) + 4
    if hi - lo < 20:
        mid = (lo + hi) / 2
        lo, hi = mid - 10, mid + 10
    return lo, hi


def _temp_colour(value: float, warm: float, hot: float):
    return RED if value >= hot else AMBER if value >= warm else TEXT


CARD_TOPS, CARD_H, FANS_TOP = (64, 226, 388), 150, 550


@lru_cache(maxsize=1)
def _page() -> Image.Image:
    """The page without its content: the background and the four cards. It is the same every time and a
    third of the drawing, which matters when the page is drawn every second."""
    img = render.vertical_gradient(BG_TOP, BG_BOTTOM).convert("RGBA")
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for top in CARD_TOPS:
        d.rounded_rectangle([20, top, W - 20, top + CARD_H], radius=16, fill=CARD)
    d.rounded_rectangle([20, FANS_TOP, W - 20, H - 18], radius=16, fill=CARD)
    img.alpha_composite(layer)
    return img


def _fmt(value: float | None, unit: str, digits: int = 0) -> str:
    return "–" if value is None else f"{value:.{digits}f}{unit}"


def _fan_name(name: str) -> str:
    lower = name.lower()
    if "pump" in lower:
        return "Pump"
    if lower == "cpu":
        return "CPU fan"
    return name.replace("_", " ").title() if name.isupper() else name.replace("_", " ")


def render_system(readings: list[Reading], now: float, span: float, live: bool = False) -> Image.Image:
    """`live`: the page is redrawn every second, so the clock shows seconds."""
    img = _page().copy()
    d = dd = ImageDraw.Draw(img)
    small, label_font, sub_font = render.font(render.BOLD, 17), render.font(render.SEMIBOLD, 23), render.font(render.REGULAR, 19)
    big, peak_font = render.font(render.BOLD, 46), render.font(render.REGULAR, 17)
    window = ("last hour" if span == 3600 else f"last {span / 3600:g} hours" if span > 3600 else "last minute" if span == 60
              else f"last {span / 60:g} minutes")
    d.text((28, 34), f"SYSTEM  ·  {window.upper()}", font=small, fill=MUTED, anchor="lm")
    d.text((W - 28, 34), datetime.fromtimestamp(now).strftime("%H:%M:%S" if live else "%H:%M"), font=small, fill=MUTED, anchor="rm")

    latest = readings[-1] if readings else Reading(now)
    t0 = now - span
    shown = [r for r in readings if r.t >= t0]

    def card(y, title, colour, value, value_colour, sub, peak, line, area, warm_hot=None):
        x0, x1, h = 20, W - 20, CARD_H
        dd.text((x0 + 18, y + 14), title, font=label_font, fill=colour)
        dd.text((x0 + 18, y + 48), sub, font=sub_font, fill=MUTED)
        dd.text((x1 - 18, y + 6), value, font=big, fill=value_colour, anchor="ra")
        if peak:
            dd.text((x1 - 18, y + 62), peak, font=peak_font, fill=DIM, anchor="ra")
        chart(img, (x0 + 16, y + 84, x1 - 16, y + h - 12), shown, t0, now, colour, line,
              area, _temp_range(shown, line) if line else (0, 100))

    for i, (name, colour, temp_key, load_key, power_key, warm, hot) in enumerate((
            ("CPU", CPU_C, "cpu_temp", "cpu_load", "cpu_power", 85, 95),
            ("GPU", GPU_C, "gpu_temp", "gpu_load", "gpu_power", 80, 88))):
        temp, load, power = getattr(latest, temp_key), getattr(latest, load_key), getattr(latest, power_key)
        temps = [getattr(r, temp_key) for r in shown if getattr(r, temp_key) is not None]
        if temp is not None:
            value, value_colour = f"{temp:.0f}°", _temp_colour(temp, warm, hot)
            sub = f"{_fmt(load, '%')} load" + (f"  ·  {power:.0f} W" if power is not None else "")
        else:
            value, value_colour = _fmt(load, "%"), TEXT
            sub = "load" if load is not None else "no readings"
        peak = f"peak {max(temps):.0f}°" if len(temps) > 1 else ""
        card(CARD_TOPS[i], name, colour, value, value_colour, sub, peak, temp_key if temps else None, load_key)

    used, total = latest.mem_used, latest.mem_total
    sub = f"{used / 1024:.1f} of {total / 1024:.0f} GB used" if used is not None and total else "no readings"
    card(CARD_TOPS[2], "Memory", MEM_C, _fmt(latest.mem_load, "%"), TEXT, sub, "", None, "mem_load")

    # fans and pump: just the numbers, they barely move
    y = FANS_TOP
    fans = sorted(latest.fans, key=lambda f: (0 if "pump" in f[0].lower() else 1 if f[0].lower() == "cpu" else 2))[:3]
    if fans:
        col = (W - 40) / len(fans)
        for i, (name, rpm) in enumerate(fans):
            cx = 20 + col * (i + 0.5)
            dd.text((cx, y + 22), _fan_name(name).upper(), font=small, fill=FAN_C, anchor="mm")
            dd.text((cx, y + 50), f"{rpm:,.0f} rpm".replace(",", " "), font=sub_font, fill=TEXT, anchor="mm")
    elif latest.sensors:
        dd.text((W / 2, y + 36), "No fan readings", font=sub_font, fill=MUTED, anchor="mm")
    else:
        note_font = render.font(render.REGULAR, 17)
        lines = render.wrap(dd, "Temperatures and fans need DeepCool's sensor service, installed with the DeepCool app",
                            note_font, W - 80, 2)
        for i, text in enumerate(lines):
            dd.text((W / 2, y + 36 + (i - (len(lines) - 1) / 2) * 22), text, font=note_font, fill=MUTED, anchor="mm")
    return img.convert("RGB")


def status_line(r: Reading) -> str:
    parts = []
    for name, temp, load in (("CPU", r.cpu_temp, r.cpu_load), ("GPU", r.gpu_temp, r.gpu_load)):
        if temp is not None:
            parts.append(f"{name} {temp:.0f}° {_fmt(load, '%')}")
        elif load is not None:
            parts.append(f"{name} {load:.0f}%")
    if r.mem_load is not None:
        parts.append(f"RAM {r.mem_load:.0f}%")
    return " · ".join(parts) or "No readings"


class SystemMonitor(Mode):
    id = "system"
    title = "System"
    description = "Temperatures, load and fans"
    glyph = "\uec4a"
    claims = "PC working hard"
    settings = (
        Setting("refresh", "Redraw every", "choice", "10", (("5", "5 minutes"), ("10", "10 minutes"), ("15", "15 minutes"), ("30", "30 minutes")),
                hint="On DeepCool's own firmware: readings are taken every 10 seconds, and the graphs are redrawn "
                     "less often because every redraw is a write to the cooler's flash. With the direct drawing "
                     "firmware the page is redrawn every second and this is not used."),
        Setting("window", "Graphs show", "choice", "60",
                (("1", "Last minute"), ("5", "Last 5 minutes"), ("30", "Last 30 minutes"), ("60", "Last hour"), ("180", "Last 3 hours")),
                hint="The two shortest are for the direct drawing firmware, where the graphs move every second; "
                     "without it they show 30 minutes. Temperatures and fans come from DeepCool's sensor service, "
                     "which the DeepCool app installs. Without it only CPU load and memory are shown."),
    )

    def __init__(self, values=None, history: History | None = None, feed: Feed | None = None):
        super().__init__(values)
        self.history = history or shared_history()
        self.feed = feed or READINGS
        self._next_draw = 0.0
        self._started = time.time()
        self._live_at: float | None = None             # time of the reading the live page was last drawn from
        self._next_save = 0.0
        self.placeholder = ("System", "Taking the first readings…")

    async def _live(self, minutes: int):
        """The page as it is right now, once per reading of the once-a-second feed."""
        got = self.feed.latest()
        if got is None or got[0] == self._live_at:
            return None
        self._live_at, reading = got
        self.status = status_line(reading) + ("" if reading.sensors else " · no sensor service")
        span = minutes * 60
        if span <= LIVE_SECONDS:
            readings = [r for _, r in self.feed.recent()]
        else:                                          # the trend, and the line drawn up to this second
            readings = self.history.since(reading.t - span) + [reading]
        if time.monotonic() >= self._next_save:
            self._next_save = time.monotonic() + SAVE_SECONDS
            await asyncio.to_thread(self.history.save)
        return await asyncio.to_thread(render_system, readings, reading.t, span, True)

    async def frame(self):
        self.history.tick()
        now = time.time()
        latest = self.history.latest()
        minutes = int(self.values["window"])
        if self.direct:
            self.active = working_hard(self.history.since(now - BUSY_SECONDS), now)
            return await self._live(minutes)
        span = max(minutes, STORED_MINUTES) * 60
        readings = self.history.since(now - max(span, BUSY_SECONDS))
        self.active = working_hard(readings, now)
        if latest is None or latest.t < self._started - SAMPLE_SECONDS:
            return None                         # saved history only: wait for a current reading
        self.status = status_line(latest) + ("" if latest.sensors else " · no sensor service")
        if time.monotonic() < self._next_draw:
            return None
        shown = [r for r in readings if r.t >= now - span]
        # a nearly empty graph (first start) is redrawn once it has something to show; later ones wait
        wait = EARLY_REDRAW if len(shown) < MIN_POINTS else int(self.values["refresh"]) * 60
        self._next_draw = time.monotonic() + wait
        await asyncio.to_thread(self.history.save)
        return await asyncio.to_thread(render_system, readings, now, span)
