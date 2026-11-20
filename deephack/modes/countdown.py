"""Days until a date you pick, with hours and then minutes on the last day.

On DeepCool's own firmware the number only changes once a day until the final 24 hours, so this is about
as gentle on the cooler's flash as a mode gets: one write a day, then one an hour, then one every five
minutes in the last hour.

With the direct drawing firmware (`direct`, set by the engine) a picture costs nothing, and the countdown
ticks: days over a clock of hours, minutes and seconds, a new picture every second; on the last day the
clock alone, large; and confetti for a while when it reaches zero.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter

from .. import render
from ..render import H, W
from .base import Mode, Setting

BG_TOP, BG_BOTTOM = (34, 30, 64), (12, 12, 26)
TEXT, MUTED, ACCENT = (244, 244, 252), (160, 158, 190), (150, 132, 255)
TRACK = (255, 255, 255, 20)
MINUTE_STEP = 5           # last hour: minutes are rounded up to this, so the display changes every 5 minutes
PARTY_SECONDS = 600       # drawn pictures: how long the confetti falls once the countdown is at zero
PARTY_TICK = 0.1          # ... and how often it is drawn
CONFETTI = ((150, 132, 255), (255, 214, 102), (255, 122, 144), (112, 214, 190), (244, 244, 252))


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


def split(seconds: int) -> tuple[int, int, int, int]:
    """(days, hours, minutes, seconds) of a number of seconds."""
    return seconds // 86400, seconds // 3600 % 24, seconds // 60 % 60, seconds % 60


@lru_cache(maxsize=4)
def _page(title: str, target: datetime, has_time: bool) -> Image.Image:
    """The page without its number: background, title and the target date on its card. The glow is blurred,
    which is slow, so a countdown that ticks every second draws it once."""
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

    # the target date on a card at the bottom
    card = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle([40, 520, W - 40, 590], radius=18, fill=TRACK)
    img.alpha_composite(card)
    d = ImageDraw.Draw(img)
    when = render.fit(d, _when_text(target, has_time), f(render.REGULAR, 22), W - 110)
    d.text((W / 2, 555), when, font=f(render.REGULAR, 22), fill=TEXT, anchor="mm")
    return img.convert("RGB")


def _clock(d: ImageDraw.ImageDraw, text: str, y: float, font, fill) -> None:
    """A running clock, centred: every digit gets the same room, so it does not shiver as they change."""
    digit = max(d.textlength(c, font=font) for c in "0123456789")
    widths = [digit if c.isdigit() else d.textlength(c, font=font) + digit * 0.1 for c in text]
    x = W / 2 - sum(widths) / 2
    for c, width in zip(text, widths):
        d.text((x + width / 2, y), c, font=font, fill=fill, anchor="mm")
        x += width


def render_ticking(title: str, target: datetime, has_time: bool, seconds: int) -> Image.Image:
    """The countdown with `seconds` to go, to the second: days over a clock, or on the last day the clock alone."""
    img = _page(title, target, has_time).copy()
    d = ImageDraw.Draw(img)
    f = render.font
    days, hours, minutes, secs = split(seconds)
    clock = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    if days:
        big = f"{days:,}".replace(",", " ")
        size = 200
        while size > 60 and d.textlength(big, font=f(render.LIGHT, size)) > W - 70:
            size -= 10
        d.text((W / 2, 290), big, font=f(render.LIGHT, size), fill=TEXT, anchor="mm")
        d.text((W / 2, 398), "day" if days == 1 else "days", font=f(render.SEMIBOLD, 30), fill=ACCENT, anchor="mm")
        _clock(d, clock, 456, f(render.SEMIBOLD, 46), TEXT)
    else:
        _clock(d, clock, 310, f(render.LIGHT, 116), TEXT)
        for x, label in ((W / 2 - 152, "hours"), (W / 2, "minutes"), (W / 2 + 152, "seconds")):
            d.text((x, 392), label, font=f(render.REGULAR, 19), fill=MUTED, anchor="mm")
        d.text((W / 2, 452), "to go", font=f(render.SEMIBOLD, 30), fill=ACCENT, anchor="mm")
    return img


# Confetti: (x, where in its fall it is at time 0, pixels a second, where in its sway, colour, size)
PIECES = tuple((random.Random(i).randrange(0, W), random.Random(i + 100).uniform(0, H + 40), random.Random(i + 200).uniform(90, 220),
                random.Random(i + 300).uniform(0, 6.3), CONFETTI[i % len(CONFETTI)], random.Random(i + 400).choice((8, 10, 12)))
               for i in range(70))


def render_party(title: str, target: datetime, has_time: bool, t: float) -> Image.Image:
    """Zero: "Today is the day", under confetti that has been falling for `t` seconds."""
    img = _page(title, target, has_time).copy()
    d = ImageDraw.Draw(img)
    d.text((W / 2, 318), "Today", font=render.font(render.LIGHT, 150), fill=TEXT, anchor="mm")
    d.text((W / 2, 444), "is the day", font=render.font(render.SEMIBOLD, 30), fill=ACCENT, anchor="mm")
    for i, (x, y0, speed, sway, colour, size) in enumerate(PIECES):
        fallen = speed * t                                   # all of it starts above the picture
        y = (y0 + fallen) % (H + 40) - 40 if fallen > H + 40 - y0 else fallen - y0 - 40
        x += 14 * math.sin(t * 2.2 + sway)
        flat = math.sin(t * 5 + sway) > 0                    # tumbling: seen flat, then on edge
        d.rectangle([x, y, x + (size if flat else size // 2), y + (size // 2 if flat else size)], fill=colour)
    return img


def render_countdown(title: str, target: datetime, has_time: bool, state: str, value: int, unit: str) -> Image.Image:
    img = _page(title, target, has_time).convert("RGBA")
    f = render.font
    d = ImageDraw.Draw(img)

    big = "Today" if state == "today" else f"{value:,}".replace(",", " ")
    size = 230
    while size > 60 and d.textlength(big, font=f(render.LIGHT, size)) > W - 70:
        size -= 10
    d.text((W / 2, 318), big, font=f(render.LIGHT, size), fill=MUTED if state == "past" else TEXT, anchor="mm")
    d.text((W / 2, 444), "is the day" if state == "today" else unit, font=f(render.SEMIBOLD, 30),
           fill=MUTED if state == "past" else ACCENT, anchor="mm")
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
                hint="Counts days, then hours and minutes on the last day. On DeepCool's own firmware it rewrites the "
                     "display once a day until then. With the direct drawing firmware it ticks every second, and "
                     "nothing is stored."),
    )

    def __init__(self, values=None):
        super().__init__(values)
        self._shown = None
        self._look = None                              # drawn pictures: what the last one showed
        self.placeholder = ("Countdown", "Pick a date in the settings on the right.")

    def _drawn(self, title: str, target: datetime, has_time: bool, now: datetime) -> Image.Image | None:
        """The countdown to the second, when that is not what the last picture showed. Asks to be called
        again when the next second begins."""
        left = (target - now).total_seconds()
        if left > 0:
            seconds = math.ceil(left)                  # so it reads 0:00:01 until the very moment
            self.tick = max(0.02, left - (seconds - 1) + 0.01)
            look = (title, target, has_time, seconds)
            self.status = (f"{title}: " if title else "") + "{0} d {1:02d}:{2:02d}:{3:02d} to go".format(*split(seconds))
        elif -left < PARTY_SECONDS:
            self.tick, look = PARTY_TICK, (title, target, has_time, "party", round(-left, 2))
        else:
            self.tick, look = None, (title, target, has_time, remaining(target, now))
        if look == self._look:
            return None
        self._look = look
        if left > 0:
            return render_ticking(title, target, has_time, seconds)
        if -left < PARTY_SECONDS:
            return render_party(title, target, has_time, -left)
        return render_countdown(title, target, has_time, *remaining(target, now))

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
        now = datetime.now()
        state, value, unit = remaining(target, now)
        self.active = state == "today" or (state == "ahead" and not unit.startswith("day"))
        self.status = ((f"{title}: " if title else "") +
                       ("today" if state == "today" else f"{value} {unit}"))
        if self.direct:
            self._shown = None
            return self._drawn(title, target, has_time, now)
        self._look, self.tick = None, None
        key = (title, target, has_time, state, value, unit)
        if key == self._shown:
            return None
        self._shown = key
        return render_countdown(title, target, has_time, state, value, unit)
