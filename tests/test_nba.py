from datetime import datetime, timedelta, timezone

from deephack.modes import nba


def scoreboard_event(date, state, away=("NY", "111", True), home=("TOR", "102", False), detail="Final", period=4):
    def side(which, t):
        return {"homeAway": which, "score": t[1], "winner": t[2],
                "team": {"abbreviation": t[0], "logo": f"https://a.espncdn.com/i/teamlogos/nba/500/scoreboard/{t[0].lower()}.png"}}
    return {"date": date, "status": {"period": period, "type": {"state": state, "shortDetail": detail}},
            "competitions": [{"competitors": [side("home", home), side("away", away)]}]}


def test_parse_scoreboard_event():
    g = nba.parse_event(scoreboard_event("2026-11-18T00:30Z", "post"))
    assert (g.away.abbr, g.away.score, g.away.winner) == ("NY", "111", True)
    assert (g.home.abbr, g.home.score, g.home.winner) == ("TOR", "102", False)
    assert g.state == "post" and g.detail == "Final"
    assert g.when == datetime(2026, 11, 18, 0, 30, tzinfo=timezone.utc)      # same instant, local zone
    assert g.home.logo.endswith("tor.png")


def test_parse_team_schedule_event():
    # the schedule endpoint nests the status in the competition, scores are objects and logos a list
    event = {"date": "2026-11-19T03:00Z", "competitions": [{
        "status": {"period": 0, "type": {"state": "pre", "shortDetail": "11/18 - 10:00 PM EST"}},
        "competitors": [
            {"homeAway": "home", "score": None, "team": {"abbreviation": "LAL", "logos": [{"href": "https://a.espncdn.com/x/lal.png"}]}},
            {"homeAway": "away", "score": {"value": 0.0, "displayValue": "0"}, "team": {"abbreviation": "UTAH", "logos": []}},
        ]}]}
    g = nba.parse_event(event)
    assert g.state == "pre" and g.home.abbr == "LAL" and g.home.score is None and g.away.score == "0"
    assert g.home.logo == "https://a.espncdn.com/x/lal.png" and g.away.logo is None


def test_parse_rejects_malformed():
    assert nba.parse_event({}) is None
    assert nba.parse_event({"date": "nonsense", "status": {"type": {"state": "pre"}}, "competitions": [{"competitors": []}]}) is None


def test_pick_two_recent_and_two_next():
    base = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    def game(days, state):
        return nba.parse_event(scoreboard_event((base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%MZ"), state))
    games = [game(3, "pre"), game(-1, "post"), game(-3, "post"), game(0, "in"), game(-2, "post"), game(1, "pre")]
    past, upcoming = nba.pick(games)
    assert [(g.when - base).days for g in past] == [-2, -1]
    assert [g.state for g in upcoming] == ["in", "pre"] and (upcoming[1].when - base).days == 1


def test_logo_refuses_foreign_hosts(tmp_path, monkeypatch):
    monkeypatch.setattr(nba, "config_dir", lambda: tmp_path)
    for url in ("http://a.espncdn.com/x.png", "https://example.com/x.png", "https://espncdn.com.evil.test/x.png", None):
        assert nba.logo(nba.Side("ZZZ", None, False, url), 66) is None


def test_render_with_no_games(tmp_path, monkeypatch):
    monkeypatch.setattr(nba, "config_dir", lambda: tmp_path)
    assert nba.render_games("all", [], []).size == (480, 640)


def live_event(state="in", detail="7:32 - 3rd", period=3, clock="7:32", date="2026-11-20T01:00Z"):
    def side(which, abbr, lines, color):
        return {"homeAway": which, "score": str(sum(lines)), "records": [{"name": "overall", "summary": "8-7"}],
                "linescores": [{"value": float(v)} for v in lines], "team": {"abbreviation": abbr, "color": color}}
    return {"date": date, "status": {"period": period, "displayClock": clock, "type": {"state": state, "shortDetail": detail}},
            "competitions": [{"competitors": [side("home", "GS", [25, 33, 20], "fdb927"), side("away", "MEM", [30, 41, 12], None)]}]}


def test_parse_live_details():
    g = nba.parse_event(live_event())
    assert g.clock == "7:32" and g.period == 3
    assert g.home.lines == (25, 33, 20) and g.home.record == "8-7" and g.home.color == "fdb927"
    assert g.away.score == "83" and g.away.color is None


def test_live_label():
    assert nba.live_label(nba.parse_event(live_event())) == "Q3 · 7:32"
    assert nba.live_label(nba.parse_event(live_event(detail="Halftime", period=2, clock="0.0"))) == "Halftime"
    assert nba.live_label(nba.parse_event(live_event(detail="End of 3rd", clock="0.0"))) == "End of 3rd"
    assert nba.live_label(nba.parse_event(live_event(detail="1:02 - OT", period=5, clock="1:02"))) == "OT · 1:02"
    assert nba.period_name(6) == "2OT"


def test_live_candidate_includes_games_that_should_have_started():
    now = datetime(2026, 11, 20, 2, 0, tzinfo=timezone.utc)
    started_late = nba.parse_event(scoreboard_event("2026-11-20T01:00Z", "pre"))
    later = nba.parse_event(scoreboard_event("2026-11-21T01:00Z", "pre"))
    assert nba.live_candidate([started_late, later], now) is started_late
    assert nba.live_candidate([later], now) is None
    live = nba.parse_event(scoreboard_event("2026-11-21T01:00Z", "in"))
    assert nba.live_candidate([live], now) is live


def test_render_live(tmp_path, monkeypatch):
    monkeypatch.setattr(nba, "config_dir", lambda: tmp_path)
    for event in (live_event(), live_event(detail="1:02 - 2OT", period=6, clock="1:02")):
        assert nba.render_live("GS", nba.parse_event(event)).size == (480, 640)


def test_mode_switches_to_the_scoreboard_while_the_team_plays(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(nba, "config_dir", lambda: tmp_path)
    tip = datetime.now(timezone.utc) - timedelta(hours=1)
    schedule = [nba.parse_event(scoreboard_event(tip.strftime("%Y-%m-%dT%H:%MZ"), "pre", home=("GS", None, False), away=("MEM", None, False)))]
    feed = {"live": nba.parse_event(live_event(date=tip.strftime("%Y-%m-%dT%H:%MZ")))}
    monkeypatch.setattr(nba, "fetch_games", lambda team: schedule)
    monkeypatch.setattr(nba, "fetch_live", lambda team, game: feed["live"])

    m = nba.Nba({"team": "GS", "live": "5"})
    assert asyncio.run(m.frame()) is not None and m.status.startswith("LIVE MEM 83")
    assert m._next - __import__("time").monotonic() <= 5 * 60 + 1
    m._next = 0
    assert asyncio.run(m.frame()) is None                     # same score: no new write
    feed["live"] = nba.parse_event(live_event(state="post", detail="Final"))
    m._next = 0
    assert asyncio.run(m.frame()) is not None and not m.status.startswith("LIVE")

    off = nba.Nba({"team": "GS", "live": "off"})
    assert asyncio.run(off.frame()) is not None and not off.status.startswith("LIVE")
