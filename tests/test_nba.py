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
