"""Calendar: the next event with a countdown, and the ones after it, from .ics calendar links.

Google Calendar, Outlook.com and iCloud all offer a calendar as an .ics link (Google calls it the
"secret address in iCal format"), and a file exported from anywhere works too. Recurring events, time
zones and cancelled occurrences are handled by the icalendar and recurring-ical-events packages.

On DeepCool's own firmware the countdown is coarse on purpose, since every change is a flash write:
whole days, then whole hours, then half hours in the last two hours and ten minutes in the last one.
About ten writes per event.

With the direct drawing firmware (`direct`, set by the engine) a picture costs nothing: the countdown
goes by the minute ("in 12 min") and by the second in the last one, an event that is going on has a bar
for how far it has run and says how long it has left, and when it ends the next one slides up.
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
SLIDE, SLIDE_TICK = 0.6, 0.05 # drawn pictures: seconds the next event takes to slide up, and between its pictures
HEADER = 54                   # ... the rows at the top that stay where they are meanwhile
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


def until_exact(ev: Event, now: datetime) -> str:
    """Countdown for pictures that cost nothing: by the minute, and by the second in the last one."""
    if ev.all_day or ev.start <= now:
        return until(ev, now)
    seconds = (ev.start - now).total_seconds()
    days = (ev.start.date() - now.date()).days
    if days >= 2:
        return f"in {days} days"
    if days == 1 and seconds >= 12 * 3600:
        return "tomorrow"
    seconds = math.ceil(seconds)                       # (so that "in 1 min" is followed by "in 59 s")
    if seconds < 60:
        return f"in {seconds} s"
    hours, minutes = divmod(seconds // 60, 60)
    return f"in {minutes} min" if not hours else f"in {hours} h" + (f" {minutes} min" if minutes else "")


def progress(ev: Event, now: datetime) -> tuple[float, int] | None:
    """(how far a timed event that is going on has run, 0.0 to 1.0; whole minutes it has left, rounded up)."""
    if ev.all_day or not ev.start <= now < ev.end:
        return None
    return (now - ev.start) / (ev.end - ev.start), math.ceil((ev.end - now).total_seconds() / 60)


def left_text(minutes: int) -> str:
    hours, minutes = divmod(minutes, 60)
    return f"{minutes} min left" if not hours else f"{hours} h" + (f" {minutes} min" if minutes else "") + " left"


def slide(old: Image.Image, new: Image.Image, done: float) -> Image.Image:
    """`new` coming up from below and pushing `old` out at the top, `done` (0.0 to 1.0) of the way, quick at
    first and settling at the end. The header stays put."""
    shift = round((H - HEADER) * (1 - (1 - min(1.0, max(0.0, done))) ** 3))
    img = new.copy()
    img.paste(old.crop((0, HEADER + shift, W, H)), (0, HEADER))
    img.paste(new.crop((0, HEADER, W, HEADER + shift)), (0, H - shift))
    return img


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


def render_calendar(events: list[Event], now: datetime, exact: bool = False) -> Image.Image:
    """`exact`: the countdown to the minute (until_exact), and a bar under an event that is going on."""
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
    count = until_exact(main, now) if exact else until(main, now)
    dd.text((46, 112), count, font=render.font(render.BOLD, 54 if len(count) < 12 else 42), fill=colour, anchor="lm")
    run = progress(main, now) if exact else None
    title_font = render.font(render.SEMIBOLD, 31)
    lines = render.wrap(dd, main.title, title_font, W - 92, 2)
    y = 170
    for line in lines:
        dd.text((46, y), line, font=title_font, fill=TEXT, anchor="lm")
        y += 40
    sub = render.font(render.REGULAR, 21)
    dd.text((46, y + 6), when_line(main, today) + (f" · {left_text(run[1])}" if run else ""), font=sub, fill=MUTED, anchor="lm")
    if run:
        dd.rounded_rectangle([46, 298, W - 46, 304], radius=3, fill=(255, 255, 255, 40))
        dd.rounded_rectangle([46, 298, 46 + max(6, (W - 92) * run[0]), 304], radius=3, fill=colour)
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
                hint="On DeepCool's own firmware the countdown moves in coarse steps (days, hours, then half hours "
                     "and ten minutes) because every change is a write to the cooler's flash. With the direct drawing "
                     "firmware it goes by the minute, and by the second in the last one."),
    )

    def __init__(self, values=None):
        super().__init__(values)
        self._next_fetch = 0.0
        self._events: list[Event] | None = None
        self._errors: list[str] = []
        self._shown = None
        self._look = None                              # drawn pictures: what the last one showed
        self._picture: Image.Image | None = None       # ... the picture itself
        self._main = None                              # ... the event it counted down to
        self._slide: tuple[Image.Image, float] | None = None    # ... what the next slides up over, since when
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

    async def _drawn(self, events: list[Event], main: Event | None, now: datetime) -> Image.Image | None:
        """The calendar to the minute, when the last picture showed something else; while the event after
        one that ended slides up, a picture of that."""
        run = progress(main, now) if main else None
        look = (now.date(), until_exact(main, now) if main else None, tuple(events[:5]), run and run[1])
        clock = time.monotonic()
        if look != self._look:
            picture = await asyncio.to_thread(render_calendar, events, now, True)
            if self._picture is not None and self._main is not None and main is not None and \
                    (main.title, main.start) != (self._main.title, self._main.start):
                self._slide = (self._picture, clock)           # another event's turn
            self._look, self._picture, self._main = look, picture, main
        elif self._slide is None:
            self.tick = self._pace(main, now)
            return None
        if self._slide is not None:
            done = (clock - self._slide[1]) / SLIDE
            if done < 1:
                self.tick = SLIDE_TICK
                return slide(self._slide[0], self._picture, done)
            self._slide = None
        self.tick = self._pace(main, now)
        return self._picture

    @staticmethod
    def _pace(main: Event | None, now: datetime) -> float | None:
        """When frame() is wanted again: at the next second in the last minute before an event, and the
        moment one that is going on ends; else as usual."""
        if main is None or main.all_day:
            return None
        seconds = (main.start - now).total_seconds()
        if 0 < seconds < 61:
            return max(0.02, seconds % 1 + 0.01)
        left = (main.end - now).total_seconds()
        return left + 0.01 if seconds <= 0 and 0 < left < 1 else None

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
        count = until_exact if self.direct else until
        self.status = (f"{main.title} {count(main, now)}" if main else "Nothing coming up") + \
                      (" · " + ", ".join(self._errors) if self._errors else "")
        if self.direct:
            self._shown = None
            return await self._drawn(events, main, now)
        self._look, self._picture, self._slide, self.tick = None, None, None, None
        # what the picture shows: the countdown text and the listed events, nothing finer
        key = (now.date(), until(main, now) if main else None, tuple(events[:5]))
        if key == self._shown:
            return None
        self._shown = key
        return await asyncio.to_thread(render_calendar, events, now)
