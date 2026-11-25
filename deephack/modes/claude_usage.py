"""Claude Code usage: plan limits plus today's activity, and on a cooler that is drawn on, what burns right now.

Local sources only, no network and no credentials:

* Plan limits (5-hour and weekly) come from a snapshot file written by the claude-hud status line plugin
  (its `display.externalUsageWritePath` option). Claude Code hands those numbers to status line plugins.
  Every open session writes that file with the numbers it heard last, so it is read through Limits, which
  makes one steady number of them.
* Today's tokens and requests are summed from Claude Code's session logs in ~/.claude/projects. Those
  logs get large, so they are read from the end on the first pass and incrementally afterwards.
* Which sessions are working, and which wait for their user, comes from the small file Claude Code keeps
  for each running session in ~/.claude/sessions (read_sessions).

With the direct drawing firmware the page is live (issue #35): the logs are looked at every two seconds
while a session works (30 ms on a PC with ninety logs), the sessions and the limits every second, and
the page is drawn eight times a second while something on it moves: the counts counting up to their new
numbers, a flame for the tokens a minute of the last sixty seconds, a mark for each session at work.
Measured on the cooler: 7.8 pictures a second of 43 KB, 7 % of a processor core for the whole app. When
no session works nothing moves, and a picture is drawn only when what it says has changed.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

from .. import render
from ..config import config_dir
from ..render import H, W
from .base import Mode, Setting

SNAPSHOT_NAME = "claude-usage.json"
BLOCK = 8 * 1024 * 1024
_TS = re.compile(rb'"timestamp":"([0-9T:.\-]+Z)"')

BG_TOP, BG_BOTTOM = (38, 30, 27), (20, 17, 16)
CARD = (255, 255, 255, 16)
ACCENT, AMBER, RED = (217, 119, 87), (240, 180, 70), (236, 96, 96)
TEXT, MUTED, TRACK = (245, 240, 234), (170, 160, 150), (70, 60, 55)


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


# ------------------------------------------------------------------ session logs
def session_of(path: Path) -> str:
    """The session a log belongs to: a session's own log is named after it, and the logs of the agents it
    starts lie in a folder of that name (<session>/subagents/agent-….jsonl)."""
    return path.parent.parent.name if path.parent.name == "subagents" else path.stem


@dataclass
class Day:
    """Token totals for one local calendar day, de-duplicated by API message id."""
    start_utc: str                                   # ISO timestamp of local midnight, in UTC
    messages: dict = field(default_factory=dict)     # message id -> (hour, input, output, cache_read, cache_write, model,
                                                     #                session, when it was logged in epoch seconds)
    offsets: dict = field(default_factory=dict)      # file path -> bytes already read
    names: dict = field(default_factory=dict)        # session -> the folder it works in (the last part of its path)
    written: dict = field(default_factory=dict)      # session -> when one of its logs was last written (epoch seconds)

    def add_line(self, line: bytes) -> None:
        if b'"usage"' not in line or b'"assistant"' not in line:
            return
        try:
            e = json.loads(line)
        except ValueError:
            return
        msg = e.get("message")
        ts = e.get("timestamp")
        if not isinstance(msg, dict) or not isinstance(ts, str) or ts < self.start_utc:
            return
        usage, model = msg.get("usage"), msg.get("model")
        if not isinstance(usage, dict) or not model or model.startswith("<"):
            return
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            hour = when.astimezone().hour
        except ValueError:
            return
        mid = msg.get("id") or e.get("uuid")
        if isinstance(e.get("cwd"), str) and e.get("sessionId"):
            self.names[e["sessionId"]] = re.split(r"[\\/]", e["cwd"].rstrip("\\/"))[-1]
        # one API response is logged once per content block, with the same (or a growing) usage: keep the largest
        prev = self.messages.get(mid)
        out = int(usage.get("output_tokens") or 0)
        if prev is None or out >= prev[2]:
            self.messages[mid] = (hour, int(usage.get("input_tokens") or 0), out, int(usage.get("cache_read_input_tokens") or 0),
                                  int(usage.get("cache_creation_input_tokens") or 0), model, e.get("sessionId"), when.timestamp())

    def _scan_tail(self, path: Path, size: int) -> None:
        """First visit: walk backwards from the end until entries are older than today."""
        start = self.start_utc.encode()
        with open(path, "rb") as fh:
            pos, carry = size, b""
            while pos > 0:
                step = min(BLOCK, pos)
                pos -= step
                fh.seek(pos)
                lines = (fh.read(step) + carry).split(b"\n")
                carry = lines[0] if pos > 0 else b""
                older = False
                for line in (lines[1:] if pos > 0 else lines):
                    m = _TS.search(line[:4096]) or _TS.search(line)
                    if m and m.group(1) < start:
                        older = True
                        continue
                    self.add_line(line)
                if older:
                    break

    def _scan_new(self, path: Path, offset: int, size: int) -> int:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(size - offset)
        end = data.rfind(b"\n")
        if end < 0:
            return offset
        for line in data[:end].split(b"\n"):
            self.add_line(line)
        return offset + end + 1

    def refresh(self, root: Path, since_epoch: float) -> None:
        for path in root.rglob("*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime < since_epoch:
                continue
            key = str(path)
            session = session_of(path)
            self.written[session] = max(self.written.get(session, 0.0), st.st_mtime)
            offset = self.offsets.get(key)
            try:
                if offset is None or offset > st.st_size:
                    self._scan_tail(path, st.st_size)
                    self.offsets[key] = st.st_size
                elif offset < st.st_size:
                    self.offsets[key] = self._scan_new(path, offset, st.st_size)
            except OSError:
                continue

    def totals(self) -> dict:
        t = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "requests": len(self.messages),
             "hours": [0] * 24, "models": {}, "sessions": set()}
        for hour, inp, out, cread, cwrite, model, session, _ in self.messages.values():
            t["input"] += inp
            t["output"] += out
            t["cache_read"] += cread
            t["cache_write"] += cwrite
            t["hours"][hour] += out
            t["models"][model] = t["models"].get(model, 0) + out
            t["sessions"].add(session)
        t["sessions"] = len(t["sessions"])
        return t

    def recent(self, now: float, minutes: int) -> tuple[list[int], int]:
        """What was burned in each of the last `minutes` minutes up to `now` (epoch seconds), oldest first:
        all the tokens of the requests logged in it, read from the cache or not. And the output tokens of
        the last one. The last minute is the burn rate: tokens a minute, right now."""
        trail, out = [0] * minutes, 0
        for _, inp, output, cread, cwrite, _, _, when in self.messages.values():
            back = int((now - when) // 60)
            if 0 <= back < minutes and when <= now:
                trail[minutes - 1 - back] += inp + output + cread + cwrite
                if back == 0:
                    out += output
        return trail, out

    def sessions(self, now: float) -> list[Session]:
        """The sessions whose logs were written a moment ago, as busy ones: what there is to go by when
        Claude Code keeps no record of its sessions (see read_sessions)."""
        return [Session(session, self.names.get(session, "session"), "busy", when)
                for session, when in sorted(self.written.items(), key=lambda kv: -kv[1]) if now - when < WRITING]


# ------------------------------------------------------------------ sessions
WRITING = 30.0               # without Claude Code's own record: a session that wrote to its log this lately is busy
SAME_START = 20_000_000      # a process and a record that began this far apart (100 ns each: 2 s) are not the same
STATES = ("busy", "waiting", "idle")


@dataclass(frozen=True)
class Session:
    id: str
    name: str
    state: str               # "busy", "waiting" (for its user: `why` says for what), or "idle"
    since: float             # ... since when (epoch seconds)
    why: str = ""


def process_started(pid: int) -> int | None:
    """When the running process with that id began, as Windows counts time (100 ns since 1601). None: there
    is no such process, or it has ended."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.windll.kernel32
    except (ImportError, AttributeError):
        return None
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, pid)                  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        made, ended, in_kernel, in_user, code = (wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME(),
                                                 wintypes.FILETIME(), wintypes.DWORD())
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:      # 259: still running
            return None
        if not kernel.GetProcessTimes(handle, ctypes.byref(made), ctypes.byref(ended), ctypes.byref(in_kernel), ctypes.byref(in_user)):
            return None
        return (made.dwHighDateTime << 32) | made.dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def read_sessions(folder: Path, started=None) -> list[Session] | None:
    """The Claude Code sessions that are open, the busy ones first, then the ones that wait for their user.

    Claude Code keeps a small file for each running session in ~/.claude/sessions (<process id>.json), and
    writes into it whether the session is busy, idle or waiting, and since when. (Seen with 2.1.330; it
    is nowhere described, so everything in it is taken with care.) A session that was killed leaves its
    file behind: one whose process is gone, or is another program that got the same id, does not count.
    None when there is no such folder: an older Claude Code, or none."""
    try:
        paths = list(folder.glob("*.json"))
    except OSError:
        return None
    if not folder.is_dir():
        return None
    started, found = started or process_started, []
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf8"))
            pid, state = int(raw["pid"]), raw.get("status")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        began = started(pid)
        if began is None:
            continue
        try:
            if abs(int(raw["procStart"]) - began) > SAME_START:
                continue
        except (KeyError, ValueError, TypeError):
            pass                                                     # no word on when it began: the id will do
        since = raw.get("statusUpdatedAt") or raw.get("updatedAt") or raw.get("startedAt") or 0
        found.append(Session(str(raw.get("sessionId") or pid), str(raw.get("name") or "session"),
                             state if state in STATES else "idle",
                             since / 1000 if isinstance(since, (int, float)) else 0.0,
                             str(raw.get("waitingFor") or "") if state == "waiting" else ""))
    return sorted(found, key=lambda s: (STATES.index(s.state), -s.since if s.state == "waiting" else s.since))


# ------------------------------------------------------------------ activity
BUSY_SECONDS = 10 * 60       # a session counts as busy for this long after it last wrote to its log
CHECK_SECONDS = 30


def last_activity(root: Path | None = None) -> float:
    """When any Claude Code session last wrote to its log (epoch seconds, 0 if never)."""
    newest = 0.0
    for path in (root or claude_dir() / "projects").rglob("*.jsonl"):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


class Activity:
    """Throttled check of whether Claude Code is busy, for the modes' `active` flag."""

    def __init__(self):
        self.every = CHECK_SECONDS       # seconds between two looks at the logs
        self._next = 0.0
        self._last = 0.0

    async def busy(self) -> bool:
        if time.monotonic() >= self._next:
            self._next = time.monotonic() + self.every
            self._last = await asyncio.to_thread(last_activity)
        return time.time() - self._last < BUSY_SECONDS


# ------------------------------------------------------------------ plan limits
def read_limits(path: Path, now: float | None = None) -> dict | None:
    """{'five_hour': (percent, reset_epoch | None), 'seven_day': (...)} from the claude-hud snapshot, or None."""
    now = now or time.time()
    try:
        raw = json.loads(path.read_text(encoding="utf8"))
    except (OSError, ValueError):
        return None
    out = {}
    for key in ("five_hour", "seven_day"):
        w = raw.get(key)
        if not isinstance(w, dict) or not isinstance(w.get("used_percentage"), (int, float)):
            continue
        reset = None
        if isinstance(w.get("resets_at"), str):
            try:
                reset = datetime.fromisoformat(w["resets_at"].replace("Z", "+00:00")).timestamp()
            except ValueError:
                reset = None
        pct = max(0, min(100, round(w["used_percentage"])))
        if reset is not None and reset <= now:
            pct, reset = 0, None      # the window rolled over since the snapshot was written
        out[key] = (pct, reset)
    return out or None


SAME_WINDOW = 120            # reset times this many seconds apart are the same window
WATCH_SECONDS, WATCH_STEP = 6.0, 0.25
WRITTEN_LATELY = 15.0


def _written_lately(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime < WRITTEN_LATELY
    except OSError:
        return False


class Limits:
    """The plan limits from the snapshot, steadied.

    Each Claude Code session's status line writes the snapshot, every few seconds, with the numbers that
    session heard with its last answer. With several sessions open the file flips between their values
    (seen with seven of them: 51, 52, 56, 59, 60, 63 and 65 % within five seconds), and one look at it
    gets any of them. Usage only grows until its window resets, so the highest number for the newest
    window is the one that is true; a session that has been idle for hours has an old, lower one."""

    def __init__(self):
        self._held: dict[str, tuple[int, float | None]] = {}

    def update(self, reading: dict | None, now: float | None = None) -> dict | None:
        """Take in what read_limits() returned; the steadied limits, in its form."""
        now = now or time.time()
        reading = reading or {}
        for key, (pct, reset) in reading.items():
            held = self._held.get(key)
            if held is None or held[1] is None or (reset is not None and reset > held[1] + SAME_WINDOW):
                self._held[key] = (pct, reset)             # nothing to compare it with, or a newer window
            elif reset is not None and reset >= held[1] - SAME_WINDOW and pct > held[0]:
                self._held[key] = (pct, held[1])
        for key, (_, reset) in list(self._held.items()):
            if (reset is None and key not in reading) or (reset is not None and reset <= now):
                del self._held[key]                        # gone from the snapshot, or its window ran out
        return dict(self._held) or None

    def read(self, path: Path, watch: float | None = None, read=None) -> dict | None:
        """Read the snapshot. While sessions are writing it, keep reading for `watch` seconds, so that each
        of them has had its say (they take turns within about five). Blocks that long: call it in a thread.
        Without a watch the number may start low and rises as the reads add up."""
        read = read or read_limits
        out = self.update(read(path))
        end = time.perf_counter() + (WATCH_SECONDS if watch is None else watch)
        while time.perf_counter() < end and _written_lately(path):
            time.sleep(WATCH_STEP)
            out = self.update(read(path))
        return out


# ------------------------------------------------------------------ rendering
def compact(n: int) -> str:
    for limit, div, suffix in ((1e9, 1e9, "B"), (1e6, 1e6, "M"), (1e3, 1e3, "K")):
        if n >= limit:
            v = n / div
            return f"{v:.0f}{suffix}" if v >= 100 else f"{v:.1f}{suffix}".replace(".0", "")
    return str(n)


def model_name(model: str) -> str:
    parts = model.removeprefix("claude-").split("-")
    parts = [p for p in parts if not (p.isdigit() and len(p) == 8)]      # drop date suffixes
    name = [p for p in parts if not p.isdigit()]
    version = [p for p in parts if p.isdigit()]
    return " ".join(p.capitalize() for p in name) + (" " + ".".join(version) if version else "")


def _reset_text(reset: float | None) -> str:
    if reset is None:
        return ""
    dt = datetime.fromtimestamp(reset)
    return dt.strftime("resets %H:%M") if dt.date() == datetime.now().date() else dt.strftime("resets %a %H:%M")


def _card(img: Image.Image, box) -> None:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, radius=18, fill=CARD)
    img.alpha_composite(layer)


def render_usage(limits: dict | None, t: dict) -> Image.Image:
    img = render.vertical_gradient(BG_TOP, BG_BOTTOM).convert("RGBA")
    f = render.font
    d = ImageDraw.Draw(img)

    # header with a small spark
    for i in range(6):
        a = math.pi * i / 6
        d.line([40 - math.cos(a) * 13, 46 - math.sin(a) * 13, 40 + math.cos(a) * 13, 46 + math.sin(a) * 13], fill=ACCENT, width=4)
    d.text((64, 46), "Claude Code", font=f(render.SEMIBOLD, 30), fill=TEXT, anchor="lm")
    d.text((W - 24, 48), datetime.now().strftime("%a %d %b"), font=f(render.REGULAR, 19), fill=MUTED, anchor="rm")

    # plan limits: only the windows the plan actually has (some plans have no 5-hour window)
    rows = [(label, limits[key]) for key, label in (("five_hour", "5-hour limit"), ("seven_day", "Weekly limit")) if limits and key in limits]
    # the "today" card moves up into whatever space the limits card does not need
    up = (2 - len(rows)) * 74 + (18 if not rows else 0)
    if rows:
        _card(img, [20, 84, W - 20, 250 - up])
        d = ImageDraw.Draw(img)
    for i, (label, (pct, reset)) in enumerate(rows):
        y = 100 + i * 74
        d.text((40, y + 12), label, font=f(render.SEMIBOLD, 21), fill=TEXT, anchor="lm")
        d.rounded_rectangle([40, y + 34, W - 40, y + 46], radius=6, fill=TRACK)
        colour = RED if pct >= 85 else AMBER if pct >= 60 else ACCENT
        d.text((W - 40, y + 12), f"{pct}%", font=f(render.BOLD, 24), fill=colour, anchor="rm")
        sub = _reset_text(reset)
        if sub:
            d.text((W - 40 - d.textlength(f"{pct}%", font=f(render.BOLD, 24)) - 12, y + 13), sub, font=f(render.REGULAR, 17), fill=MUTED, anchor="rm")
        if pct > 0:
            d.rounded_rectangle([40, y + 34, 40 + max(12, (W - 80) * pct / 100), y + 46], radius=6, fill=colour)

    # today
    _card(img, [20, 266 - up, W - 20, H - 20])
    d = ImageDraw.Draw(img)
    d.text((40, 290 - up), "TODAY", font=f(render.BOLD, 16), fill=MUTED, anchor="lm")
    sessions = f"{t['sessions']} session{'s' if t['sessions'] != 1 else ''}"
    d.text((W - 40, 290 - up), sessions, font=f(render.REGULAR, 17), fill=MUTED, anchor="rm")
    tiles = (("Output", t["output"]), ("Input", t["input"] + t["cache_write"]), ("Cache reads", t["cache_read"]), ("Requests", t["requests"]))
    col_w = (W - 80) / 4
    for i, (label, value) in enumerate(tiles):
        x = 40 + col_w * i
        d.text((x, 332 - up), compact(value), font=f(render.SEMIBOLD, 30), fill=TEXT, anchor="lm")
        d.text((x, 362 - up), label, font=f(render.REGULAR, 16), fill=MUTED, anchor="lm")

    # output tokens per hour (the chart takes the space freed above)
    top, bottom, left, right = 412 - up, 504, 40, W - 40
    d.text((left, 392 - up), "Output tokens per hour", font=f(render.REGULAR, 15), fill=MUTED, anchor="lm")
    peak = max(t["hours"]) or 1
    slot = (right - left) / 24
    now_hour = datetime.now().hour
    for h, v in enumerate(t["hours"]):
        x0 = left + slot * h + 2
        height = (bottom - top) * v / peak
        if v:
            d.rounded_rectangle([x0, bottom - max(4, height), x0 + slot - 4, bottom], radius=3, fill=ACCENT if h == now_hour else (176, 104, 82))
        else:
            d.rounded_rectangle([x0, bottom - 3, x0 + slot - 4, bottom], radius=1, fill=TRACK)
    for h in (0, 6, 12, 18):
        d.text((left + slot * h + 2, bottom + 8), f"{h:02d}", font=f(render.REGULAR, 15), fill=MUTED, anchor="la")
    d.text((right, bottom + 8), "24", font=f(render.REGULAR, 15), fill=MUTED, anchor="ra")

    # share of output by model
    models = sorted(t["models"].items(), key=lambda kv: -kv[1])
    total = sum(v for _, v in models) or 1
    palette = (ACCENT, (232, 196, 150), (140, 150, 190), (120, 120, 120))
    y, x = 552, float(left)
    d.rounded_rectangle([left, y, right, y + 10], radius=5, fill=TRACK)
    for i, (_, v) in enumerate(models[:4]):
        w = (right - left) * v / total
        if w >= 2:
            d.rounded_rectangle([x, y, x + w, y + 10], radius=5, fill=palette[i])
        x += w
    lx = float(left)
    for i, (name, v) in enumerate(models[:3]):
        label = f"{model_name(name)} {round(100 * v / total)}%"
        d.ellipse([lx, y + 26, lx + 10, y + 36], fill=palette[i])
        d.text((lx + 16, y + 31), label, font=f(render.REGULAR, 16), fill=MUTED, anchor="lm")
        lx += 16 + d.textlength(label, font=f(render.REGULAR, 16)) + 18
    if not models:
        d.text((left, y + 31), "No Claude Code activity yet today", font=f(render.REGULAR, 16), fill=MUTED, anchor="lm")
    return img.convert("RGB")


# ------------------------------------------------------------------ the live page (direct drawing)
FRAME = 0.125                # seconds between two pictures while something on the page moves
LOOK = 1.0                   # ... between two looks at the sessions and the limits
LOGS_BUSY, LOGS_IDLE = 2.0, 10.0     # ... and at the logs: while a session works, and while none does
TRAIL = 30                   # minutes of burning the small graph shows
SPARK_STEPS, FLAME_STEPS, PULSE_STEPS = 12, 10, 8      # pictures in one turn of the spark, one flicker, one pulse
FLAME_LEVELS = (100_000, 300_000, 1_000_000, 3_000_000)     # tokens a minute from which the flame is a size larger
FLAME = ((232, 98, 60), (245, 170, 66), (255, 232, 168))    # its colours, from the outside in
SMOOTH = 4                   # the small moving pictures are drawn this many times too large and made smaller


class Rolling:
    """A number on its way to the one that is true: `value` covers a fixed share of what is left with every
    second, so that a counter counts up to its new number and does not jump there. And never less than
    would bring it there in four times `pace`: left to itself the last digit would creep for a quarter of
    a minute (seen on the cooler), and the page would be drawn all that time for it."""

    def __init__(self, pace: float = 0.8, near: float = 0.5, falls: bool = True):
        self.pace, self.near, self.falls = pace, near, falls         # falls: False for a count, which only begins anew
        self.value: float | None = None
        self.target = 0.0
        self._least = 0.0                                            # the least it moves in a second

    def to(self, target: float) -> None:
        if self.value is None or (target < self.value and not self.falls):
            self.value = float(target)
        if target != self.target:
            self._least = abs(target - self.value) / (4 * self.pace)
        self.target = float(target)

    def step(self, seconds: float) -> bool:
        """Move on by so much time. Whether it is still on its way."""
        if self.value is None or self.value == self.target:
            return False
        left = self.target - self.value
        move = max(abs(left) * (1 - math.exp(-seconds / self.pace)), self._least * seconds)
        self.value = self.target if abs(left) - move < self.near else self.value + math.copysign(move, left)
        return self.value != self.target


def clock_span(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def ago(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600} h" if s >= 2 * 3600 else f"{s // 60} min" if s >= 60 else "a moment"


def flame_level(burn: float) -> int:
    """How large the flame is for so many tokens a minute: 0 (nothing burns) to 5."""
    return 0 if burn < 1 else 1 + sum(burn >= level for level in FLAME_LEVELS)


@lru_cache(maxsize=None)
def _flame(level: int, step: int) -> Image.Image:
    """A flame, 56 by 72, at one step of its flicker. Level 0 is what is left when nothing burns."""
    w, h = 56 * SMOOTH, 72 * SMOOTH
    tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    turn = 2 * math.pi * step / FLAME_STEPS
    size = 0.46 + 0.54 * level / 5 if level else 0.4
    tongues = zip((1.0, 0.62, 0.32), FLAME, (0.0, 1.1, 2.3)) if level else ((1.0, TRACK, 0.0),)
    for share, colour, lag in tongues:
        flicker = (1 + 0.06 * math.sin(turn + lag) + 0.04 * math.sin(2 * turn + 2 * lag)) if level else 1.0
        height, width = h * 0.88 * size * share * flicker, w * 0.86 * size * share
        sway = w * 0.09 * size * math.sin(turn + lag + 0.6) if level else 0.0
        points = []
        for i in range(64):
            a = 2 * math.pi * i / 64
            up = (1 + math.cos(a)) / 2                               # 1 at the tip, 0 at the foot
            points.append((w / 2 + sway * up * up + width / 2 * math.sin(a) * abs(math.sin(a / 2)) ** 1.7 * 1.25,
                           h - 2 * SMOOTH - height * up))
        d.polygon(points, fill=colour)
    return tile.resize((56, 72), Image.LANCZOS)


@lru_cache(maxsize=None)
def _spark(step: int) -> Image.Image:
    """The spark of the header, 34 by 34, turned by `step` of the SPARK_STEPS that bring it back to itself."""
    side, r = 34 * SMOOTH, 13 * SMOOTH
    tile = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    for i in range(6):
        a = math.pi * (i + step / SPARK_STEPS) / 6
        d.line([side / 2 - math.cos(a) * r, side / 2 - math.sin(a) * r, side / 2 + math.cos(a) * r, side / 2 + math.sin(a) * r],
               fill=ACCENT, width=4 * SMOOTH)
    return tile.resize((34, 34), Image.LANCZOS)


@lru_cache(maxsize=None)
def _dot(colour: tuple, step: int | None) -> Image.Image:
    """A session's mark, 24 by 24: a dot, and around a busy one a ring that grows and fades."""
    side = 24 * SMOOTH
    tile = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    if step is not None:
        grown = step / PULSE_STEPS
        r = (5 + 6 * grown) * SMOOTH
        d.ellipse([side / 2 - r, side / 2 - r, side / 2 + r, side / 2 + r], outline=colour + (round(200 * (1 - grown)),), width=2 * SMOOTH)
    r = 5 * SMOOTH
    d.ellipse([side / 2 - r, side / 2 - r, side / 2 + r, side / 2 + r], fill=colour + (255,))
    return tile.resize((24, 24), Image.LANCZOS)


@lru_cache(maxsize=4)
def _ground(limits: int) -> Image.Image:
    """The live page with nothing on it, for so many limits: the ground and its three cards."""
    img = render.vertical_gradient(BG_TOP, BG_BOTTOM).convert("RGBA")
    top = 84
    if limits:
        _card(img, [20, top, W - 20, top + 8 + 66 * limits])
        top += 8 + 66 * limits + 12
    tall = 112 + (5 - limits) * 26
    _card(img, [20, top, W - 20, top + tall])
    _card(img, [20, top + tall + 12, W - 20, H - 20])
    return img


def render_live(limits: dict | None, t: dict, sessions: list[Session], trail: list[int], shown: dict,
                now: float, clock: float, last: float = 0.0) -> Image.Image:
    """The page as it is this moment, for a cooler that is drawn on.

    `shown`: the numbers as they are counted up to right now (Rolling): "output", "input", "cache_read",
    "requests", "burn" (tokens a minute), "burn_out" (the output tokens among them) and the limits'
    percentages under their names. `now`: epoch seconds, for how long a session has been at it. `clock`:
    seconds that run on, for what turns and flickers. `last`: when the last request was logged."""
    rows = [(key, label, limits[key]) for key, label in (("five_hour", "5-hour limit"), ("seven_day", "Weekly limit")) if limits and key in limits]
    img = _ground(len(rows)).copy()
    f = render.font
    d = ImageDraw.Draw(img)
    working = [s for s in sessions if s.state == "busy"]
    waiting = [s for s in sessions if s.state == "waiting"]
    beat = int(clock / FRAME)

    img.alpha_composite(_spark(beat % SPARK_STEPS if working else 0), (23, 29))
    d.text((64, 46), "Claude Code", font=f(render.SEMIBOLD, 30), fill=TEXT, anchor="lm")
    d.text((W - 24, 48), datetime.now().strftime("%a %d %b"), font=f(render.REGULAR, 19), fill=MUTED, anchor="rm")

    # the limits, as on the stored page but closer together, the bars where the count has got to
    top = 84 + (8 + 66 * len(rows) + 12 if rows else 0)
    for i, (key, label, (pct, reset)) in enumerate(rows):
        y = 96 + i * 66
        at = shown.get(key, pct)
        pct = round(at)
        d.text((40, y + 12), label, font=f(render.SEMIBOLD, 21), fill=TEXT, anchor="lm")
        d.rounded_rectangle([40, y + 34, W - 40, y + 46], radius=6, fill=TRACK)
        colour = RED if pct >= 85 else AMBER if pct >= 60 else ACCENT
        d.text((W - 40, y + 12), f"{pct}%", font=f(render.BOLD, 24), fill=colour, anchor="rm")
        sub = _reset_text(reset)
        if sub:
            d.text((W - 40 - d.textlength(f"{pct}%", font=f(render.BOLD, 24)) - 12, y + 13), sub, font=f(render.REGULAR, 17), fill=MUTED, anchor="rm")
        if at > 0:
            d.rounded_rectangle([40, y + 34, 40 + max(12, (W - 80) * at / 100), y + 46], radius=6, fill=colour)

    # now: what burns, and who is at it. A line more for the sessions for each limit that is not there.
    lines = 5 - len(rows)
    d.text((40, top + 22), "NOW", font=f(render.BOLD, 16), fill=MUTED, anchor="lm")
    count = " · ".join(text for text in (f"{len(working)} working" if working else "", f"{len(waiting)} waiting" if waiting else "") if text)
    d.text((W - 40, top + 22), count or "nobody working", font=f(render.REGULAR, 17), fill=MUTED, anchor="rm")
    burn = max(0, round(shown.get("burn", 0)))
    img.alpha_composite(_flame(flame_level(burn), beat % FLAME_STEPS), (32, top + 26))
    d.text((96, top + 60), f"{burn:,}", font=f(render.SEMIBOLD, 38), fill=TEXT if burn else MUTED, anchor="lm")
    under = f"tokens a minute · {compact(max(0, round(shown.get('burn_out', 0))))} output" if burn else (
        f"tokens a minute · last request {ago(now - last)} ago" if last else "tokens a minute")
    d.text((96, top + 90), under, font=f(render.REGULAR, 15), fill=MUTED, anchor="lm")
    left, right, foot, tall = 322, W - 40, top + 80, 44               # the last half hour, a bar a minute
    peak, slot = max(trail, default=0) or 1, (right - left) / max(1, len(trail))
    for i, v in enumerate(trail):
        x0 = left + slot * i
        newest = i == len(trail) - 1
        d.rectangle([x0, foot - (max(3, tall * v / peak) if v else 2), x0 + slot - 1.5, foot],
                    fill=(ACCENT if newest else (176, 104, 82)) if v else TRACK)
    y = top + 118
    marks = working + waiting
    for i, s in enumerate(marks[:lines]):
        if i == lines - 1 and len(marks) > lines:
            d.text((40, y), f"and {len(marks) - i} more", font=f(render.REGULAR, 17), fill=MUTED, anchor="lm")
            break
        busy = s.state == "busy"
        img.alpha_composite(_dot(ACCENT if busy else AMBER, beat % PULSE_STEPS if busy else None), (34, y - 12))
        side = " · ".join(text for text in (s.why, clock_span(now - s.since)) if text)
        d.text((W - 40, y), side, font=f(render.REGULAR, 17), fill=MUTED if busy else AMBER, anchor="rm")
        room = W - 40 - d.textlength(side, font=f(render.REGULAR, 17)) - 16 - 66
        d.text((66, y), render.fit(d, s.name, f(render.SEMIBOLD, 18), room), font=f(render.SEMIBOLD, 18), fill=TEXT, anchor="lm")
        y += 26
    if not marks:
        idle = len(sessions)
        d.text((40, y), f"{idle} session{'s' if idle != 1 else ''} open, none at work" if idle else "No session is open",
               font=f(render.REGULAR, 17), fill=MUTED, anchor="lm")

    # today: the four counts in full, and the day's hours in the room that is left
    top = top + 112 + lines * 26 + 12
    d.text((40, top + 22), "TODAY", font=f(render.BOLD, 16), fill=MUTED, anchor="lm")
    d.text((W - 40, top + 22), f"{t['sessions']} session{'s' if t['sessions'] != 1 else ''}", font=f(render.REGULAR, 17), fill=MUTED, anchor="rm")
    tiles = (("Output", "output", t["output"]), ("Input", "input", t["input"] + t["cache_write"]),
             ("Cache reads", "cache_read", t["cache_read"]), ("Requests", "requests", t["requests"]))
    for i, (label, key, value) in enumerate(tiles):
        x, y = 40 + (W - 80) / 2 * (i % 2), top + 52 + 45 * (i // 2)
        d.text((x, y), f"{max(0, round(shown.get(key, value))):,}", font=f(render.SEMIBOLD, 24), fill=TEXT, anchor="lm")
        d.text((x, y + 21), label, font=f(render.REGULAR, 13), fill=MUTED, anchor="lm")
    head, bottom, left, right = top + 138, H - 44, 40, W - 40
    named = bottom - head >= 40                                      # room for the hours under the bars
    bottom = bottom if named else H - 32
    if bottom - head >= 16:
        peak = max(t["hours"]) or 1
        slot = (right - left) / 24
        hour = datetime.now().hour
        for h, v in enumerate(t["hours"]):
            x0 = left + slot * h + 2
            if v:
                d.rounded_rectangle([x0, bottom - max(4, (bottom - head) * v / peak), x0 + slot - 4, bottom], radius=3,
                                    fill=ACCENT if h == hour else (176, 104, 82))
            else:
                d.rounded_rectangle([x0, bottom - 3, x0 + slot - 4, bottom], radius=1, fill=TRACK)
        for h in (0, 6, 12, 18) if named else ():
            d.text((left + slot * h + 2, bottom + 4), f"{h:02d}", font=f(render.REGULAR, 13), fill=MUTED, anchor="la")
        if named:
            d.text((right, bottom + 4), "24", font=f(render.REGULAR, 13), fill=MUTED, anchor="ra")
    return img.convert("RGB")


class ClaudeUsage(Mode):
    id = "claude_usage"
    title = "Claude Code"
    description = "Plan limits and today's usage"
    glyph = ""
    claims = "Claude busy"
    settings = (
        Setting("refresh", "Refresh every", "choice", "15", (("5", "5 minutes"), ("15", "15 minutes"), ("30", "30 minutes"), ("60", "1 hour")),
                hint="On DeepCool's own firmware, where each refresh rewrites the display. With the direct drawing "
                     "firmware the page is live and this is not used: the numbers count up as requests are logged, "
                     "a flame shows how many tokens a minute are burning, and each session that is working or "
                     "waiting for you has its line. Plan limits need the claude-hud plugin writing a usage "
                     "snapshot; everything else comes from Claude Code's own files on this PC."),
    )

    def __init__(self, values=None):
        super().__init__(values)
        self._day: Day | None = None
        self._next = 0.0
        self._shown = None
        self.snapshot = config_dir() / SNAPSHOT_NAME
        self.logs, self.sessions = claude_dir() / "projects", claude_dir() / "sessions"
        self.placeholder = ("Claude Code", "Reading today's session logs…")
        self._activity = Activity()
        self._limits = Limits()
        # the live page:
        self._look_at = self._logs_at = 0.0                 # when the sessions, and the logs, are next looked at
        self._live: tuple | None = None                     # (limits, totals, sessions, trail, last request) of the last look
        self._rolls = {key: Rolling(falls=False) for key in ("output", "input", "cache_read", "requests")}
        self._rolls.update({"burn": Rolling(1.2), "burn_out": Rolling(1.2),
                            "five_hour": Rolling(near=0.05), "seven_day": Rolling(near=0.05)})
        self._stepped: float | None = None                  # when the counts last moved on
        self._read: str | None = None                       # the day whose logs have been read at least once
        self._still = None                                  # what the last picture showed, while nothing on it moves

    def _today(self) -> Day:
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if self._day is None or self._day.start_utc != start_utc:
            self._day = Day(start_utc)
        return self._day

    def _collect(self) -> tuple[dict | None, dict]:
        day = self._today()
        day.refresh(self.logs, datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        return self._limits.read(self.snapshot), day.totals()

    def _look(self, logs: bool) -> tuple:
        """One look at what Claude Code is doing: the sessions and the limits, and with `logs` the logs too."""
        now = time.time()
        day = self._today()
        if logs or self._read != day.start_utc:
            day.refresh(self.logs, datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            self._read = day.start_utc
        sessions = read_sessions(self.sessions)
        if sessions is None:
            sessions = day.sessions(now)
        trail, out = day.recent(now, TRAIL)
        last = max((m[7] for m in day.messages.values()), default=0.0)
        return self._limits.update(read_limits(self.snapshot)), day.totals(), sessions, trail, out, last

    async def _live_frame(self):
        """The page for a cooler that is drawn on: a picture whenever something on it has moved."""
        now = time.monotonic()
        if now >= self._look_at:
            self._look_at = now + LOOK
            logs = now >= self._logs_at
            if logs:
                working = self._live is not None and any(s.state == "busy" for s in self._live[2])
                self._logs_at = now + (LOGS_BUSY if working else LOGS_IDLE)
            limits, totals, sessions, trail, out, last = await asyncio.to_thread(self._look, logs)
            self._live = (limits, totals, sessions, trail, last)
            for key, value in (("output", totals["output"]), ("input", totals["input"] + totals["cache_write"]),
                               ("cache_read", totals["cache_read"]), ("requests", totals["requests"]),
                               ("burn", trail[-1]), ("burn_out", out)):
                self._rolls[key].to(value)
            for key, (pct, _) in (limits or {}).items():
                self._rolls[key].to(pct)
            at_work = sum(s.state == "busy" for s in sessions)
            five = (limits or {}).get("five_hour")
            self.status = " · ".join(text for text in (
                f"{at_work} working" if at_work else "", f"{compact(trail[-1])} tokens a minute" if trail[-1] else "",
                f"5-hour limit {five[0]}%" if five else "", f"{compact(totals['output'])} output tokens today") if text)
        limits, totals, sessions, trail, last = self._live
        moving = [roll.step(now - (self._stepped or now)) for roll in self._rolls.values()]
        self._stepped = now
        lively = any(moving) or any(s.state == "busy" for s in sessions)
        self.tick = FRAME if lively else None
        if not lively:                                      # nothing turns or counts: a picture when what it says changes
            waits = any(s.state == "waiting" for s in sessions)
            still = (json.dumps(limits, sort_keys=True), json.dumps(totals, sort_keys=True), tuple(sessions), tuple(trail),
                     datetime.now().date().isoformat(), int(time.time()) // (1 if waits else 60))
            if still == self._still:
                return None
            self._still = still
        else:
            self._still = None
        shown = {key: roll.value for key, roll in self._rolls.items() if roll.value is not None}
        return await asyncio.to_thread(render_live, limits, totals, sessions, trail, shown, time.time(), now, last)

    async def frame(self):
        self.active = await self._activity.busy()
        if self.direct:
            return await self._live_frame()
        self.tick = None
        if time.monotonic() < self._next:
            return None
        self._next = time.monotonic() + int(self.values["refresh"]) * 60
        self._look_at = self._logs_at = 0.0                 # (should the cooler be drawn on later: look at once)
        limits, totals = await asyncio.to_thread(self._collect)
        five = (limits or {}).get("five_hour")
        self.status = (f"5-hour limit {five[0]}% · " if five else "") + f"{compact(totals['output'])} output tokens today"
        key = (json.dumps(limits, sort_keys=True), json.dumps(totals, sort_keys=True), datetime.now().date().isoformat())
        if key == self._shown:
            return None
        self._shown = key
        return render_usage(limits, totals)
