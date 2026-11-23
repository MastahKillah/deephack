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


# ------------------------------------------------------------------ the Christmas look
def at_time(monkeypatch, moment):
    """The mode's clock, standing at `moment`."""
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(cd, "datetime", Clock)


def test_the_next_christmas_day():
    assert cd.christmas_day(datetime(2026, 11, 23, 10, 0)) == datetime(2026, 12, 25)
    assert cd.christmas_day(datetime(2026, 12, 25, 23, 59)) == datetime(2026, 12, 25)      # the day itself, all of it
    assert cd.christmas_day(datetime(2026, 12, 26, 0, 0)) == datetime(2027, 12, 25)


def test_christmas_counts_to_the_25th_without_being_told_a_date(monkeypatch):
    import asyncio

    at_time(monkeypatch, datetime(2026, 11, 23, 10, 0))
    mode = cd.Countdown({"look": "christmas"})
    picture = asyncio.run(mode.frame())
    assert picture.size == (480, 640) and mode.status == "Christmas: 32 days to go"
    assert asyncio.run(mode.frame()) is None                                               # stored pictures: once a day
    plain = cd.Countdown({"title": "Christmas", "date": "2026-12-25"})
    assert asyncio.run(plain.frame()).tobytes() != picture.tobytes()                       # another picture altogether
    assert asyncio.run(cd.Countdown({}).frame()) is None                                   # the plain look still wants a date

    own = cd.Countdown({"look": "christmas", "title": "Holidays", "date": "2026-12-24 18:00"})
    assert asyncio.run(own.frame()) is not None and own.status == "Holidays: 31 days to go"
    untitled = cd.Countdown({"look": "christmas", "date": "2026-12-24"})
    assert asyncio.run(untitled.frame()) is not None and untitled.status == "31 days to go"


def test_christmas_snows_between_the_seconds(monkeypatch):
    import asyncio

    moment = datetime(2026, 11, 23, 10, 0, 0, 200000)
    at_time(monkeypatch, moment)
    mode = cd.Countdown({"look": "christmas"})
    mode.direct = True
    first = asyncio.run(mode.frame())
    assert mode.tick == cd.SNOW_TICK and mode.status == "Christmas: 31 d 14:00:00 to go"
    assert asyncio.run(mode.frame()) is None                                               # the same moment again
    at_time(monkeypatch, moment + timedelta(milliseconds=100))
    second = asyncio.run(mode.frame())
    assert second is not None and second.tobytes() != first.tobytes()                      # the same second, other snow
    top = (150, 250, 330, 330)                                                             # the middle of the big number
    assert sum(a != b for a, b in zip(first.crop(top).tobytes(), second.crop(top).tobytes())) < 3000     # ... the number stands
    at_time(monkeypatch, moment + timedelta(seconds=1))
    assert asyncio.run(mode.frame()) is not None and mode.status.endswith("31 d 13:59:59 to go")


def test_it_is_merry_christmas_for_all_of_the_day(monkeypatch):
    import asyncio

    at_time(monkeypatch, datetime(2026, 12, 25, 15, 30))
    mode = cd.Countdown({"look": "christmas"})
    mode.direct = True
    merry = asyncio.run(mode.frame())
    assert merry is not None and mode.tick == cd.SNOW_TICK and mode.status == "Christmas: today" and mode.active
    at_time(monkeypatch, datetime(2026, 12, 25, 15, 30, 0, 100000))
    assert asyncio.run(mode.frame()).tobytes() != merry.tobytes()                          # hours after midnight: still snowing
    mode.direct = False                                                                    # a cooler that stores: one picture
    still = asyncio.run(mode.frame())
    assert still is not None and mode.tick is None and asyncio.run(mode.frame()) is None
    assert still.tobytes() == cd.render_party("Christmas", datetime(2026, 12, 25), False, 0.0, "christmas").tobytes()
    assert still.tobytes() == cd.render_party("", datetime(2026, 12, 25), False, 0.0, "christmas").tobytes()      # not said twice

    at_time(monkeypatch, datetime(2026, 12, 26, 8, 0))                                     # the day after: on to the next one
    again = cd.Countdown({"look": "christmas"})
    assert asyncio.run(again.frame()) is not None and again.status == "Christmas: 364 days to go"
    own = cd.Countdown({"look": "christmas", "date": "2026-12-25", "title": "Christmas"})  # a date of one's own stays past
    assert asyncio.run(own.frame()) is not None and own.status == "Christmas: 1 day ago"


def test_the_lights_glow_and_the_looks_have_their_own_colours():
    target = datetime(2026, 12, 25)
    a = cd.render_ticking("Christmas", target, False, 86400 * 3, "christmas", 0.0)
    b = cd.render_ticking("Christmas", target, False, 86400 * 3, "christmas", 1.5)
    string = (0, 0, 480, 60)                                                               # the string of lights along the top
    assert a.crop(string).tobytes() != b.crop(string).tobytes()
    assert cd.colours("christmas") != cd.colours("plain") == (cd.TEXT, cd.MUTED, cd.ACCENT)
    assert len(cd.LIGHTS) > 12 and all(0 <= x <= 480 and 0 <= y <= 640 for x, y, _ in cd.LIGHTS)
    assert cd.render_ticking("x", target, False, 5).tobytes() == cd.render_ticking("x", target, False, 5, "plain", 9.0).tobytes()
    setting = {s.key: s for s in cd.Countdown.settings}["look"]
    assert setting.default == "plain" and dict(setting.choices) == {"plain": "Plain", "christmas": "Christmas"}
