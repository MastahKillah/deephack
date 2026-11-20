"""Claude Code usage: plan limits plus today's activity.

Two local sources, no network and no credentials:

* Plan limits (5-hour and weekly) come from a snapshot file written by the claude-hud status line plugin
  (its `display.externalUsageWritePath` option). Claude Code hands those numbers to status line plugins.
* Today's tokens and requests are summed from Claude Code's session logs in ~/.claude/projects. Those
  logs get large, so they are read from the end on the first pass and incrementally afterwards.
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
@dataclass
class Day:
    """Token totals for one local calendar day, de-duplicated by API message id."""
    start_utc: str                                   # ISO timestamp of local midnight, in UTC
    messages: dict = field(default_factory=dict)     # message id -> (hour, input, output, cache_read, cache_write, model, session)
    offsets: dict = field(default_factory=dict)      # file path -> bytes already read

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
            hour = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().hour
        except ValueError:
            return
        mid = msg.get("id") or e.get("uuid")
        # one API response is logged once per content block, with the same (or a growing) usage: keep the largest
        prev = self.messages.get(mid)
        out = int(usage.get("output_tokens") or 0)
        if prev is None or out >= prev[2]:
            self.messages[mid] = (hour, int(usage.get("input_tokens") or 0), out, int(usage.get("cache_read_input_tokens") or 0),
                                  int(usage.get("cache_creation_input_tokens") or 0), model, e.get("sessionId"))

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
        for hour, inp, out, cread, cwrite, model, session in self.messages.values():
            t["input"] += inp
            t["output"] += out
            t["cache_read"] += cread
            t["cache_write"] += cwrite
            t["hours"][hour] += out
            t["models"][model] = t["models"].get(model, 0) + out
            t["sessions"].add(session)
        t["sessions"] = len(t["sessions"])
        return t


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


class ClaudeUsage(Mode):
    id = "claude_usage"
    title = "Claude Code"
    description = "Plan limits and today's usage"
    glyph = ""
    claims = "Claude busy"
    settings = (
        Setting("refresh", "Refresh every", "choice", "15", (("5", "5 minutes"), ("15", "15 minutes"), ("30", "30 minutes"), ("60", "1 hour")),
                hint="Each refresh rewrites the display. Plan limits need the claude-hud plugin writing a usage snapshot; "
                     "today's numbers come from Claude Code's local session logs."),
    )

    def __init__(self, values=None):
        super().__init__(values)
        self._day: Day | None = None
        self._next = 0.0
        self._shown = None
        self.snapshot = config_dir() / SNAPSHOT_NAME
        self.placeholder = ("Claude Code", "Reading today's session logs…")
        self._activity = Activity()

    def _collect(self) -> tuple[dict | None, dict]:
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if self._day is None or self._day.start_utc != start_utc:
            self._day = Day(start_utc)
        self._day.refresh(claude_dir() / "projects", midnight.timestamp())
        return read_limits(self.snapshot), self._day.totals()

    async def frame(self):
        self.active = await self._activity.busy()
        if time.monotonic() < self._next:
            return None
        self._next = time.monotonic() + int(self.values["refresh"]) * 60
        limits, totals = await asyncio.to_thread(self._collect)
        five = (limits or {}).get("five_hour")
        self.status = (f"5-hour limit {five[0]}% · " if five else "") + f"{compact(totals['output'])} output tokens today"
        key = (json.dumps(limits, sort_keys=True), json.dumps(totals, sort_keys=True), datetime.now().date().isoformat())
        if key == self._shown:
            return None
        self._shown = key
        return render_usage(limits, totals)
