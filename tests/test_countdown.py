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
