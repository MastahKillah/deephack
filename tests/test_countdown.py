from datetime import datetime

from deephack.modes import countdown as cd

NOW = datetime(2026, 11, 19, 10, 0)


def test_parse_target_formats():
    assert cd.parse_target("2026-12-24") == (datetime(2026, 12, 24), False)
    assert cd.parse_target(" 2026-12-24   18:00 ") == (datetime(2026, 12, 24, 18, 0), True)
    assert cd.parse_target("2026-12-24T18:00") == (datetime(2026, 12, 24, 18, 0), True)
    assert cd.parse_target("24.12.2026 18:00") == (datetime(2026, 12, 24, 18, 0), True)
    for bad in ("", "   ", "next friday", "2026-13-01", "12/24/2026"):
        assert cd.parse_target(bad) is None


def test_calendar_days_while_more_than_a_day_away():
    assert cd.remaining(datetime(2026, 12, 24), NOW) == ("ahead", 35, "days to go")
    # 1 day 22 hours away, but two dates ahead: counts dates, like people do
    assert cd.remaining(datetime(2026, 11, 21, 8, 0), NOW) == ("ahead", 2, "days to go")
    assert cd.remaining(datetime(2026, 11, 20, 18, 0), NOW) == ("ahead", 1, "day to go")


def test_hours_then_minutes_on_the_last_day():
    assert cd.remaining(datetime(2026, 11, 20), NOW) == ("ahead", 14, "hours to go")
    assert cd.remaining(datetime(2026, 11, 19, 11, 30), NOW) == ("ahead", 1, "hour to go")
    assert cd.remaining(datetime(2026, 11, 19, 10, 41), NOW) == ("ahead", 45, "minutes to go")
    assert cd.remaining(datetime(2026, 11, 19, 10, 0, 20), NOW) == ("ahead", 5, "minutes to go")
    assert cd.remaining(datetime(2026, 11, 19, 10, 59, 59), NOW) == ("ahead", 60, "minutes to go")


def test_today_and_past():
    assert cd.remaining(datetime(2026, 11, 19), NOW)[0] == "today"
    assert cd.remaining(datetime(2026, 11, 19, 9, 0), NOW)[0] == "today"
    assert cd.remaining(datetime(2026, 11, 18, 23, 0), NOW) == ("past", 1, "day ago")
    assert cd.remaining(datetime(2026, 11, 1), NOW) == ("past", 18, "days ago")


def test_render_sizes():
    for target in (datetime(2026, 12, 24), datetime(3000, 1, 1), datetime(2026, 11, 19)):
        state = cd.remaining(target, NOW)
        assert cd.render_countdown("A rather long title that has to wrap onto a second line", target, False, *state).size == (480, 640)


def test_mode_only_redraws_on_change():
    import asyncio

    mode = cd.Countdown({"title": "Holidays", "date": "2099-12-24"})
    assert asyncio.run(mode.frame()) is not None
    assert asyncio.run(mode.frame()) is None
    assert "days to go" in mode.status
    assert asyncio.run(cd.Countdown({"date": "whenever"}).frame()) is None


# ------------------------------------------------------------------ drawn pictures (the direct drawing firmware)
from datetime import timedelta


def _drawn(date="2026-12-24 18:00", title="Holidays"):
    mode = cd.Countdown({"title": title, "date": date})
    mode.direct = True
    target, has_time = cd.parse_target(date)
    return mode, target, lambda now: mode._drawn(title, target, has_time, now)


def test_split_seconds():
    assert cd.split(0) == (0, 0, 0, 0) and cd.split(59) == (0, 0, 0, 59) and cd.split(3600) == (0, 1, 0, 0)
    assert cd.split(34 * 86400 + 7 * 3600 + 42 * 60 + 15) == (34, 7, 42, 15)


def test_drawn_countdown_ticks_every_second():
    mode, target, at = _drawn()
    now = target - timedelta(days=34, hours=7, minutes=42, seconds=14, milliseconds=400)
    first = at(now)
    assert first.size == (480, 640) and mode.status == "Holidays: 34 d 07:42:15 to go"
    assert abs(mode.tick - 0.41) < 1e-6                                # asks to be called when the second is over
    assert at(now + timedelta(milliseconds=300)) is None and abs(mode.tick - 0.11) < 1e-6
    second = at(now + timedelta(milliseconds=410))
    assert second is not None and mode.status.endswith("34 d 07:42:14 to go") and second.tobytes() != first.tobytes()
    assert abs(mode.tick - 1.0) < 0.02
    last = at(target - timedelta(milliseconds=1))
    assert mode.status.endswith("0 d 00:00:01 to go") and last is not None   # one, until the very moment


def test_last_day_is_a_clock_and_zero_is_a_party():
    mode, target, at = _drawn()
    days = cd.render_ticking("Holidays", target, True, 86400 + 5)
    clock = cd.render_ticking("Holidays", target, True, 86399)
    assert days.tobytes() != clock.tobytes()
    assert at(target - timedelta(hours=3)) is not None
    party = [at(target + timedelta(seconds=1 + s / 10)) for s in range(1, 6)]
    assert mode.tick == cd.PARTY_TICK and len({p.tobytes() for p in party}) == 5     # confetti falling
    assert party[0].tobytes() != cd.render_countdown("Holidays", target, True, "today", 0, "").tobytes()
    assert at(target + timedelta(seconds=1.5)) is None                              # the same moment again
    after = at(target + timedelta(seconds=cd.PARTY_SECONDS + 1))
    assert mode.tick is None and after.tobytes() == cd.render_countdown("Holidays", target, True, "today", 0, "").tobytes()
    assert at(target + timedelta(seconds=cd.PARTY_SECONDS + 60)) is None            # and quiet from then on
    assert cd.render_party("x", target, True, 0.0).tobytes() != cd.render_party("x", target, True, 1.0).tobytes()


def test_running_clock_does_not_shiver():
    """Every digit has the same room: the colons stand where they stood, whatever the digits."""
    target = cd.parse_target("2026-12-24 18:00")[0]
    a = cd.render_ticking("", target, True, 11 * 3600 + 11 * 60 + 11)
    b = cd.render_ticking("", target, True, 10 * 3600 + 8 * 60 + 48)
    box = (0, 250, 480, 370)
    left = lambda img: next(x for x in range(480) if any(sum(img.getpixel((x, y))) > 500 for y in range(260, 360, 4)))
    assert abs(left(a) - left(b)) < 30 and a.crop(box).tobytes() != b.crop(box).tobytes()


def test_countdown_mode_draws_when_it_may_and_stores_when_it_must():
    import asyncio

    mode = cd.Countdown({"title": "Holidays", "date": "2099-12-24"})
    mode.direct = True
    assert asyncio.run(mode.frame()) is not None and 0 < mode.tick <= 1.02 and "to go" in mode.status
    mode.direct = False
    assert asyncio.run(mode.frame()) is not None and mode.tick is None and "days to go" in mode.status
    assert asyncio.run(mode.frame()) is None
