"""Days until a date you pick, with hours and then minutes on the last day.

The number only changes once a day until the final 24 hours, so this is about as gentle on the cooler's
flash as a mode gets: one write a day, then one an hour, then one every five minutes in the last hour.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from PIL import Image, ImageDraw, ImageFilter

from .. import render
from ..render import H, W
from .base import Mode, Setting

BG_TOP, BG_BOTTOM = (34, 30, 64), (12, 12, 26)
TEXT, MUTED, ACCENT = (244, 244, 252), (160, 158, 190), (150, 132, 255)
TRACK = (255, 255, 255, 20)
MINUTE_STEP = 5           # last hour: minutes are rounded up to this, so the display changes every 5 minutes


def parse_target(text: str) -> tuple[datetime, bool] | None:
    """(target, has_time) from '2026-12-24', '2026-12-24 18:00' or '24.12.2026 18:00'; None if unreadable."""
    text = " ".join(text.replace("T", " ").split())
    if not text:
        return None
    for fmt, has_time in (("%Y-%m-%d %H:%M", True), ("%Y-%m-%d", False), ("%d.%m.%Y %H:%M", True), ("%d.%m.%Y", False)):
        try:
            return datetime.strptime(text, fmt), has_time
        except ValueError:
            continue
    return None


def remaining(target: datetime, now: datetime) -> tuple[str, int, str]:
    """What the big number shows: (state, value, unit), state being 'ahead', 'today' or 'past'.

    More than 24 hours away counts calendar days, the way people say "Christmas is in five days".
    Inside the last 24 hours it switches to hours, and to minutes (rounded up) in the last hour.
    """
    left = target - now
    if left.total_seconds() <= 0:
        if target.date() == now.date():
            return "today", 0, ""
        days = (now.date() - target.date()).days
        return "past", days, "day ago" if days == 1 else "days ago"
    if left >= timedelta(days=1):
        days = (target.date() - now.date()).days
        return "ahead", days, "day to go" if days == 1 else "days to go"
    if left >= timedelta(hours=1):
        hours = int(left.total_seconds() // 3600)
        return "ahead", hours, "hour to go" if hours == 1 else "hours to go"
    minutes = min(60, MINUTE_STEP * math.ceil(left.total_seconds() / 60 / MINUTE_STEP))
    return "ahead", minutes, "minutes to go"


def _when_text(target: datetime, has_time: bool) -> str:
    text = f"{target:%A} {target.day} {target:%B %Y}"
    return text + (f", {target:%H:%M}" if has_time else "")


def render_countdown(title: str, target: datetime, has_time: bool, state: str, value: int, unit: str) -> Image.Image:
    img = render.vertical_gradient(BG_TOP, BG_BOTTOM).convert("RGBA")
    f = render.font

    # soft glow behind the number
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W / 2 - 200, 150, W / 2 + 200, 470], fill=ACCENT + (34,))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(60)))
    d = ImageDraw.Draw(img)

    d.text((W / 2, 56), "COUNTDOWN", font=f(render.BOLD, 17), fill=MUTED, anchor="mm")
    y = 96
    for line in render.wrap(d, title or "Countdown", f(render.SEMIBOLD, 36), W - 60, 2):
        d.text((W / 2, y), line, font=f(render.SEMIBOLD, 36), fill=TEXT, anchor="ma")
        y += 46

    big = "Today" if state == "today" else f"{value:,}".replace(",", " ")
    size = 230
    while size > 60 and d.textlength(big, font=f(render.LIGHT, size)) > W - 70:
        size -= 10
    d.text((W / 2, 318), big, font=f(render.LIGHT, size), fill=MUTED if state == "past" else TEXT, anchor="mm")
    d.text((W / 2, 444), "is the day" if state == "today" else unit, font=f(render.SEMIBOLD, 30),
           fill=MUTED if state == "past" else ACCENT, anchor="mm")

    # the target date on a card at the bottom
    card = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle([40, 520, W - 40, 590], radius=18, fill=TRACK)
    img.alpha_composite(card)
    d = ImageDraw.Draw(img)
    when = render.fit(d, _when_text(target, has_time), f(render.REGULAR, 22), W - 110)
    d.text((W / 2, 555), when, font=f(render.REGULAR, 22), fill=TEXT, anchor="mm")
    return img.convert("RGB")


class Countdown(Mode):
    id = "countdown"
    title = "Countdown"
    description = "Days until a date you pick"
    claims = "last day"
    glyph = ""
    settings = (
        Setting("title", "Title", "text", "", placeholder="e.g. Holidays"),
        Setting("date", "Date", "text", "", placeholder="2026-12-24 or 2026-12-24 18:00",
                hint="Counts days, then hours and minutes on the last day. Rewrites the display once a day until then."),
    )

    def __init__(self, values=None):
        super().__init__(values)
        self._shown = None
        self.placeholder = ("Countdown", "Pick a date in the settings on the right.")

    async def frame(self):
        parsed = parse_target(self.values["date"])
        self.active = False
        if parsed is None:
            self.status = "No date set" if not self.values["date"].strip() else "Date not understood"
            if self.values["date"].strip():
                self.placeholder = ("Date not understood", "Write it as 2026-12-24, optionally with a time: 2026-12-24 18:00.")
            return None
        target, has_time = parsed
        title = self.values["title"].strip()
        state, value, unit = remaining(target, datetime.now())
        self.active = state == "today" or (state == "ahead" and not unit.startswith("day"))
        self.status = ((f"{title}: " if title else "") +
                       ("today" if state == "today" else f"{value} {unit}"))
        key = (title, target, has_time, state, value, unit)
        if key == self._shown:
            return None
        self._shown = key
        return render_countdown(title, target, has_time, state, value, unit)
