"""Calendar: the next event with a countdown, and the ones after it, from .ics calendar links.

Google Calendar, Outlook.com and iCloud all offer a calendar as an .ics link (Google calls it the
"secret address in iCal format"), and a file exported from anywhere works too. Recurring events, time
zones and cancelled occurrences are handled by the icalendar and recurring-ical-events packages.

The countdown is coarse on purpose, since every change is a flash write: whole days, then whole hours,
then half hours in the last two hours and ten minutes in the last one. About ten writes per event.
"""
from __future__ import annotations

import asyncio
import math
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw

from .. import render
from ..render import H, W
from .base import Mode, Setting

AHEAD_DAYS = 14
RETRY_SECONDS = 300
SOON_MINUTES = 15             # Auto: from this long before an event until it ends
MAX_BYTES = 20_000_000

BG_TOP, BG_BOTTOM = (24, 22, 40), (10, 10, 18)
CARD = (255, 255, 255, 16)
TEXT, MUTED, DIM = (242, 240, 250), (150, 146, 172), (98, 94, 120)
COLOURS = ((132, 160, 255), (255, 152, 112), (118, 212, 152), (232, 132, 222), (240, 200, 90))


@dataclass(frozen=True)
class Event:
    title: str
    start: datetime               # local time; midnight for all-day events
    end: datetime
    all_day: bool
    location: str
    calendar: int                 # index of the link it came from, for its colour


def _local(value, all_day: bool) -> datetime:
    if all_day:
        return datetime.combine(value, datetime.min.time()).astimezone()
    if value.tzinfo is None:                    # "floating" time: as written, in the local zone
        return value.astimezone()
    return value.astimezone(datetime.now().astimezone().tzinfo)


def parse_calendar(data: bytes, index: int, start: datetime, end: datetime) -> list[Event]:
    """Every occurrence between start and end, recurring ones expanded, cancelled ones dropped."""
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(data)
    events = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED" or "DTSTART" not in ev:
            continue
        begin = ev["DTSTART"].dt
        all_day = not isinstance(begin, datetime)
        finish = ev["DTEND"].dt if "DTEND" in ev else None
        if finish is None:
            finish = begin + (ev["DURATION"].dt if "DURATION" in ev else timedelta(days=1) if all_day else timedelta(0))
        events.append(Event(str(ev.get("SUMMARY", "")).strip() or "(no title)", _local(begin, all_day),
                            _local(finish, all_day), all_day, str(ev.get("LOCATION", "")).strip(), index))
    return events


def parse_links(text: str) -> list[str]:
    return [part for part in text.replace(",", " ").split() if part]


def fetch(link: str) -> bytes:
    if link.lower().startswith("webcal://"):
        link = "https://" + link[len("webcal://"):]
    if not link.lower().startswith(("http://", "https://")):
        return Path(link.strip('"')).expanduser().read_bytes()     # an exported file
    req = urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0 (deephack)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(MAX_BYTES)


def upcoming(events: list[Event], now: datetime) -> list[Event]:
    """Events not over yet, by start; timed events before all-day ones of the same day."""
    seen, out = set(), []
    for ev in sorted(events, key=lambda e: (e.start.date(), e.all_day, e.start, e.title)):
        key = (ev.title, ev.start, ev.end)
        if ev.end > now and key not in seen:
            seen.add(key)
            out.append(ev)
    return out


def featured(events: list[Event], now: datetime) -> Event | None:
    """The event to count down to: the one going on or next, preferring timed events over all-day ones today."""
    timed = [e for e in events if not e.all_day and e.end > now]
    today_timed = [e for e in timed if e.start.date() <= now.date()]
    if today_timed:
        return today_timed[0]
    return events[0] if events else None


def until(ev: Event, now: datetime) -> str:
    """Countdown in coarse steps, so the picture only changes every so often."""
    if ev.all_day:
        days = (ev.start.date() - now.date()).days
        return "today" if days <= 0 else "tomorrow" if days == 1 else f"in {days} days"
    if ev.start <= now:
        return "now"
    minutes = (ev.start - now).total_seconds() / 60
    days = (ev.start.date() - now.date()).days
    if days >= 2:
        return f"in {days} days"
    if minutes >= 120:
        return "tomorrow" if days == 1 and minutes >= 12 * 60 else f"in {int(minutes // 60)} h"
    if minutes > 60:
        half_hours = math.ceil(minutes / 30)
        return f"in {half_hours // 2} h" + (" 30 min" if half_hours % 2 else "")
    if minutes > 10:
        return f"in {math.ceil(minutes / 10) * 10} min".replace("in 60 min", "in 1 h")
    return "in a few minutes"


def _day(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    return day.strftime("%a %d %b") if (day - today).days < 7 else day.strftime("%d %b")


def when_line(ev: Event, today: date) -> str:
    if ev.all_day:
        days = (ev.end.date() - ev.start.date()).days
        return f"{_day(ev.start.date(), today)} · all day" + (f" · {days} days" if days > 1 else "")
    span = ev.start.strftime("%H:%M") + (f" – {ev.end.strftime('%H:%M')}" if ev.end > ev.start else "")
    return f"{_day(ev.start.date(), today)} · {span}"


# ------------------------------------------------------------------ drawing
def _card(img: Image.Image, box) -> ImageDraw.ImageDraw:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, radius=18, fill=CARD)
    img.alpha_composite(layer)
    return ImageDraw.Draw(img)


def render_calendar(events: list[Event], now: datetime) -> Image.Image:
    img = render.vertical_gradient(BG_TOP, BG_BOTTOM).convert("RGBA")
    d = ImageDraw.Draw(img)
    small = render.font(render.BOLD, 17)
    today = now.date()
    d.text((28, 34), "CALENDAR", font=small, fill=MUTED, anchor="lm")
    d.text((W - 28, 34), now.strftime("%a %d %b").upper(), font=small, fill=MUTED, anchor="rm")

    main = featured(events, now)
    if main is None:
        d.text((W / 2, 280), "Nothing coming up", font=render.font(render.SEMIBOLD, 34), fill=TEXT, anchor="mm")
        d.text((W / 2, 326), f"in the next {AHEAD_DAYS} days", font=render.font(render.REGULAR, 22), fill=MUTED, anchor="mm")
        return img.convert("RGB")

    colour = COLOURS[main.calendar % len(COLOURS)]
    dd = _card(img, [20, 62, W - 20, 318])
    dd.rounded_rectangle([20, 62, 28, 318], radius=4, fill=colour)
    count = until(main, now)
    dd.text((46, 112), count, font=render.font(render.BOLD, 54 if len(count) < 12 else 42), fill=colour, anchor="lm")
    title_font = render.font(render.SEMIBOLD, 31)
    lines = render.wrap(dd, main.title, title_font, W - 92, 2)
    y = 170
    for line in lines:
        dd.text((46, y), line, font=title_font, fill=TEXT, anchor="lm")
        y += 40
    sub = render.font(render.REGULAR, 21)
    dd.text((46, y + 6), when_line(main, today), font=sub, fill=MUTED, anchor="lm")
    if main.location:
        dd.text((46, y + 38), render.fit(dd, main.location, render.font(render.REGULAR, 19), W - 92),
                font=render.font(render.REGULAR, 19), fill=DIM, anchor="lm")

    later = [e for e in events if e is not main][:4]
    d.text((28, 346), "LATER" if later else "NOTHING ELSE COMING UP", font=small, fill=MUTED, anchor="lm")
    row_title, row_when = render.font(render.SEMIBOLD, 22), render.font(render.REGULAR, 18)
    for i, ev in enumerate(later):
        y = 372 + i * 66
        d.rounded_rectangle([28, y + 4, 33, y + 52], radius=2, fill=COLOURS[ev.calendar % len(COLOURS)])
        d.text((48, y + 16), when_line(ev, today), font=row_when, fill=MUTED, anchor="lm")
        d.text((48, y + 42), render.fit(d, ev.title, row_title, W - 76), font=row_title, fill=TEXT, anchor="lm")
    return img.convert("RGB")


class Calendar(Mode):
    id = "calendar"
    title = "Calendar"
    description = "Next events, with a countdown"
    glyph = ""
    claims = "event soon"
    settings = (
        Setting("links", "Calendar links", "secret", "", placeholder="https://… .ics",
                hint="One or more .ics links, separated by spaces, or the path of an exported .ics file. Google "
                     "Calendar: settings of a calendar, “Secret address in iCal format”. Outlook.com: Settings, "
                     "Shared calendars, Publish a calendar, ICS link. Anyone with such a link can read the calendar, "
                     "so it is hidden here (it is kept in deephack's settings file)."),
        Setting("refresh", "Check for changes every", "choice", "30", (("15", "15 minutes"), ("30", "30 minutes"), ("60", "1 hour")),
                hint="The countdown moves in coarse steps (days, hours, then half hours and ten minutes) because "
                     "every change is a write to the cooler's flash."),
    )

    def __init__(self, values=None):
        super().__init__(values)
        self._next_fetch = 0.0
        self._events: list[Event] | None = None
        self._errors: list[str] = []
        self._shown = None
        self.placeholder = ("Calendar", "Paste a calendar link in the settings" if not parse_links(self.values["links"]) else "Fetching…")

    def _load(self) -> tuple[list[Event], list[str]]:
        now = datetime.now().astimezone()
        start, end = now - timedelta(days=1), now + timedelta(days=AHEAD_DAYS)
        events, errors = [], []
        for i, link in enumerate(parse_links(self.values["links"])):
            try:
                events += parse_calendar(fetch(link), i, start, end)
            except Exception as e:              # a broken or unreachable calendar must not hide the others
                errors.append(f"calendar {i + 1}: {type(e).__name__}")
        return events, errors

    def claiming(self, now: datetime) -> bool:
        return any(not e.all_day and e.start - timedelta(minutes=SOON_MINUTES) <= now < e.end for e in self._events or ())

    async def frame(self):
        if not parse_links(self.values["links"]):
            self.status = "No calendar link set"
            return None
        if time.monotonic() >= self._next_fetch:
            events, errors = await asyncio.to_thread(self._load)
            if events or not errors:
                self._events = events
            self._errors = errors
            self._next_fetch = time.monotonic() + (RETRY_SECONDS if errors and not events else int(self.values["refresh"]) * 60)
        if self._events is None:
            self.status = "Could not read the calendar (" + ", ".join(self._errors) + ")"
            return None
        now = datetime.now().astimezone()
        events = upcoming(self._events, now)
        self.active = self.claiming(now)
        main = featured(events, now)
        self.status = (f"{main.title} {until(main, now)}" if main else "Nothing coming up") + \
                      (" · " + ", ".join(self._errors) if self._errors else "")
        # what the picture shows: the countdown text and the listed events, nothing finer
        key = (now.date(), until(main, now) if main else None, tuple(events[:5]))
        if key == self._shown:
            return None
        self._shown = key
        return await asyncio.to_thread(render_calendar, events, now)
