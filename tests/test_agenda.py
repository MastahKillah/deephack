import asyncio
from datetime import datetime, timedelta, timezone

from deephack import device
from deephack.modes import agenda

UTC = timezone.utc

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VTIMEZONE
TZID:Europe/Berlin
BEGIN:STANDARD
DTSTART:19701025T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700329T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:standup
SUMMARY:Standup
DTSTART;TZID=Europe/Berlin:20261102T100000
DTEND;TZID=Europe/Berlin:20261102T101500
RRULE:FREQ=WEEKLY;BYDAY=MO
EXDATE;TZID=Europe/Berlin:20261123T100000
LOCATION:Room 4
END:VEVENT
BEGIN:VEVENT
UID:trip
SUMMARY:Trip
DTSTART;VALUE=DATE:20261121
DTEND;VALUE=DATE:20261124
END:VEVENT
BEGIN:VEVENT
UID:gone
SUMMARY:Cancelled thing
STATUS:CANCELLED
DTSTART:20261120T120000Z
DTEND:20261120T130000Z
END:VEVENT
BEGIN:VEVENT
UID:call
SUMMARY:Call
DTSTART:20261120T150000Z
DURATION:PT45M
END:VEVENT
END:VCALENDAR
"""


def test_parse_expands_recurrences_and_skips_cancelled():
    start, end = datetime(2026, 11, 19, tzinfo=UTC), datetime(2026, 12, 2, tzinfo=UTC)
    events = agenda.parse_calendar(ICS, 1, start, end)
    timed = sorted((e.title, e.start.astimezone(UTC).strftime("%m-%d %H:%M")) for e in events if not e.all_day)
    assert timed == [("Call", "11-20 15:00"), ("Standup", "11-30 09:00")]
    # the 23rd is excluded, the 30th is in winter time: 10:00 Berlin = 09:00 UTC
    call = next(e for e in events if e.title == "Call")
    assert call.end - call.start == timedelta(minutes=45) and call.calendar == 1 and not call.all_day
    trip = next(e for e in events if e.title == "Trip")
    assert trip.all_day and trip.start.date().isoformat() == "2026-11-21" and (trip.end.date() - trip.start.date()).days == 3
    assert next(e for e in events if e.title == "Standup").location == "Room 4"


def _ev(title, start, minutes=30, all_day=False, cal=0):
    return agenda.Event(title, start, start + timedelta(minutes=minutes), all_day, "", cal)


def test_countdown_moves_in_coarse_steps():
    now = datetime(2026, 11, 19, 9, 0).astimezone()
    steps = {m: agenda.until(_ev("x", now + timedelta(minutes=m)), now) for m in (5, 10, 11, 35, 60, 61, 90, 91, 119, 120, 300)}
    assert steps == {5: "in a few minutes", 10: "in a few minutes", 11: "in 20 min", 35: "in 40 min", 60: "in 1 h",
                     61: "in 1 h 30 min", 90: "in 1 h 30 min", 91: "in 2 h", 119: "in 2 h", 120: "in 2 h", 300: "in 5 h"}
    assert agenda.until(_ev("x", now - timedelta(minutes=5)), now) == "now"
    assert agenda.until(_ev("x", now + timedelta(days=1, hours=1)), now) == "tomorrow"
    assert agenda.until(_ev("x", now + timedelta(days=3)), now) == "in 3 days"
    day = datetime(2026, 11, 21).astimezone()
    assert agenda.until(_ev("trip", day, 3 * 24 * 60, all_day=True), now) == "in 2 days"
    assert agenda.until(_ev("trip", now.replace(hour=0), 24 * 60, all_day=True), now) == "today"


def test_upcoming_and_featured():
    now = datetime(2026, 11, 19, 9, 0).astimezone()
    holiday = _ev("Holiday", now.replace(hour=0), 24 * 60, all_day=True)
    later = _ev("Later", now + timedelta(hours=3))
    past = _ev("Past", now - timedelta(hours=2))
    tomorrow = _ev("Tomorrow", now + timedelta(days=1))
    events = agenda.upcoming([tomorrow, later, holiday, past, later], now)
    assert [e.title for e in events] == ["Later", "Holiday", "Tomorrow"]        # duplicates and past ones gone
    assert agenda.featured(events, now).title == "Later"                      # a meeting today beats the all-day event
    assert agenda.featured(agenda.upcoming([holiday, tomorrow], now), now).title == "Holiday"
    assert agenda.featured([], now) is None


def test_render():
    now = datetime(2026, 11, 19, 9, 0).astimezone()
    events = [_ev("Standup with a rather long title that has to wrap onto a second line", now + timedelta(minutes=40)),
              _ev("Trip", now.replace(hour=0) + timedelta(days=2), 3 * 24 * 60, all_day=True, cal=1)] + \
             [_ev(f"Thing {i}", now + timedelta(days=i, hours=2), cal=i) for i in range(1, 6)]
    for evs in (events, events[:1], []):
        img = agenda.render_calendar(evs, now)
        assert img.size == (device.WIDTH, device.HEIGHT) and img.mode == "RGB"


def test_links_and_files(tmp_path):
    assert agenda.parse_links(" https://a/x.ics, webcal://b/y.ics  ") == ["https://a/x.ics", "webcal://b/y.ics"]
    path = tmp_path / "cal.ics"
    path.write_bytes(ICS)
    assert agenda.fetch(f'"{path}"') == ICS


def test_mode_from_a_file_redraws_only_when_the_countdown_moves(tmp_path, monkeypatch):
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    start = (now + timedelta(minutes=12)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    end = (now + timedelta(minutes=42)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = tmp_path / "cal.ics"
    path.write_bytes(f"BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:1\nSUMMARY:Review\nDTSTART:{start}\nDTEND:{end}\n"
                     f"END:VEVENT\nEND:VCALENDAR\n".encode())
    m = agenda.Calendar({"links": str(path)})
    assert asyncio.run(m.frame()) is not None and m.status == "Review in 20 min" and m.active   # within 15 min soon
    assert asyncio.run(m.frame()) is None
    monkeypatch.setattr(agenda.Calendar, "_load", lambda self: ([], ["calendar 1: URLError"]))
    m._next_fetch = 0
    assert asyncio.run(m.frame()) is None and "URLError" in m.status          # a failed fetch keeps the last events


def test_mode_without_links():
    m = agenda.Calendar({})
    assert asyncio.run(m.frame()) is None and m.status == "No calendar link set" and "Paste" in m.placeholder[1]


# ------------------------------------------------------------------ drawn pictures (the direct drawing firmware)
def test_exact_countdown_by_the_minute_and_the_second():
    now = datetime(2026, 11, 19, 9, 0).astimezone()
    at = lambda **left: agenda.until_exact(_ev("x", now + timedelta(**left)), now)
    assert [at(seconds=s) for s in (1, 0.2, 59, 59.5, 60, 119, 120)] == \
        ["in 1 s", "in 1 s", "in 59 s", "in 1 min", "in 1 min", "in 1 min", "in 2 min"]
    assert [at(minutes=m) for m in (12, 59, 60, 61, 135, 300)] == ["in 12 min", "in 59 min", "in 1 h", "in 1 h 1 min", "in 2 h 15 min", "in 5 h"]
    assert at(days=1, hours=1) == "tomorrow" and at(days=3) == "in 3 days" and at(hours=10) == "in 10 h"
    assert agenda.until_exact(_ev("x", now - timedelta(minutes=5)), now) == "now"
    day = datetime(2026, 11, 21).astimezone()
    assert agenda.until_exact(_ev("trip", day, 3 * 24 * 60, all_day=True), now) == "in 2 days"


def test_progress_of_an_event_that_is_going_on():
    now = datetime(2026, 11, 19, 9, 0).astimezone()
    assert agenda.progress(_ev("x", now - timedelta(minutes=15), 60), now) == (0.25, 45)
    assert agenda.progress(_ev("x", now - timedelta(minutes=59, seconds=30), 60), now)[1] == 1
    assert agenda.progress(_ev("x", now + timedelta(minutes=5), 60), now) is None
    assert agenda.progress(_ev("x", now - timedelta(minutes=90), 60), now) is None
    assert agenda.progress(_ev("x", now.replace(hour=0), 24 * 60, all_day=True), now) is None
    assert [agenda.left_text(m) for m in (1, 45, 60, 75)] == ["1 min left", "45 min left", "1 h left", "1 h 15 min left"]
    going = [_ev("Review", now - timedelta(minutes=15), 60), _ev("Later", now + timedelta(hours=3))]
    exact, coarse = agenda.render_calendar(going, now, True), agenda.render_calendar(going, now)
    assert exact.size == coarse.size and exact.tobytes() != coarse.tobytes()         # the bar, and "45 min left"
    assert exact.getpixel((60, 301)) != coarse.getpixel((60, 301)) and exact.getpixel((420, 301)) != exact.getpixel((60, 301))


def test_slide_pushes_the_old_picture_out_at_the_top():
    old, new = agenda.Image.new("RGB", (480, 640), "red"), agenda.Image.new("RGB", (480, 640), "blue")
    start, middle, end = (agenda.slide(old, new, t) for t in (0.0, 0.3, 1.0))
    assert start.getpixel((5, 600)) == (255, 0, 0) and start.getpixel((5, 10)) == (0, 0, 255)       # the header is the new one's
    assert middle.getpixel((5, agenda.HEADER + 2)) == (255, 0, 0) and middle.getpixel((5, 630)) == (0, 0, 255)
    assert end.tobytes() == new.tobytes()
    edge = lambda img: next(y for y in range(agenda.HEADER, 640) if img.getpixel((5, y)) == (0, 0, 255))
    assert edge(agenda.slide(old, new, 0.2)) > edge(middle) > edge(agenda.slide(old, new, 0.6))     # coming up


def test_drawn_calendar_counts_runs_and_slides(monkeypatch):
    clock = [50.0]
    monkeypatch.setattr(agenda.time, "monotonic", lambda: clock[0])
    now = datetime(2026, 11, 19, 9, 0).astimezone()
    review, call = _ev("Review", now + timedelta(seconds=150), 30), _ev("Call", now + timedelta(hours=2), 30, cal=1)
    m = agenda.Calendar({"links": "x"})
    m.direct = True

    def at(when, seconds=0.5):
        clock[0] += seconds
        events = agenda.upcoming([review, call], when)
        return asyncio.run(m._drawn(events, agenda.featured(events, when), when))

    assert at(now) is not None and m.tick is None                                    # "in 2 min": the usual pace
    assert at(now + timedelta(seconds=20)) is None                                   # still "in 2 min"
    assert at(now + timedelta(seconds=91)) is not None and m.tick is not None        # "in 59 s": by the second now
    assert at(now + timedelta(seconds=91.2)) is None and 0.02 <= m.tick <= 1.01
    assert at(now + timedelta(seconds=92.5)) is not None
    assert at(now + timedelta(seconds=150)) is not None and m.tick is None           # "now", with its bar
    assert at(now + timedelta(seconds=170)) is None
    assert at(now + timedelta(seconds=211)) is not None                              # a minute less left
    at(now + timedelta(minutes=32, seconds=29, milliseconds=600))
    assert abs(m.tick - 0.41) < 1e-6                                                 # wanted again the moment it ends
    shown = m._picture
    first = at(now + timedelta(minutes=32, seconds=31))                              # the review is over: the call slides up
    assert first is not None and m.tick == agenda.SLIDE_TICK and first.tobytes() != m._picture.tobytes()
    second = at(now + timedelta(minutes=32, seconds=31), agenda.SLIDE_TICK)
    assert second.tobytes() != first.tobytes() and m.tick == agenda.SLIDE_TICK
    last = at(now + timedelta(minutes=32, seconds=32), agenda.SLIDE)
    assert last.tobytes() == m._picture.tobytes() != shown.tobytes() and m.tick is None
    assert at(now + timedelta(minutes=32, seconds=33)) is None


def test_calendar_mode_draws_when_it_may_and_stores_when_it_must(tmp_path):
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    start = (now + timedelta(minutes=12, seconds=30)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    end = (now + timedelta(minutes=42)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = tmp_path / "cal.ics"
    path.write_bytes(f"BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:1\nSUMMARY:Review\nDTSTART:{start}\nDTEND:{end}\n"
                     f"END:VEVENT\nEND:VCALENDAR\n".encode())
    m = agenda.Calendar({"links": str(path)})
    m.direct = True
    assert asyncio.run(m.frame()) is not None and m.status in ("Review in 12 min", "Review in 11 min")
    assert asyncio.run(m.frame()) is None
    m.direct = False                                                                 # e.g. the slide show was switched on
    assert asyncio.run(m.frame()) is not None and m.status == "Review in 20 min" and asyncio.run(m.frame()) is None
