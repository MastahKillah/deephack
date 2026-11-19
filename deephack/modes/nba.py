"""NBA: the two most recent results and the next two games, for one team or the whole league.

With a team picked, its games in progress take over the screen as a big scoreboard that is redrawn every
few minutes (never play by play: every redraw is a write to the cooler's flash).

Data and logos come from ESPN's public (undocumented) scoreboard feed. Logos are downloaded on first use
and cached in %APPDATA%\\deephack\\cache\\nba; nothing is bundled with the app.
"""
from __future__ import annotations

import asyncio
import io
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw

from .. import render
from ..config import config_dir
from ..render import H, W
from .base import Mode, Setting

API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/"
LOGO_HOST_SUFFIX = ".espncdn.com"
RETRY_SECONDS = 120
SEARCH_DAYS = 5          # league view: how far to look back / ahead for two games each

TEAMS = (
    ("ATL", "Atlanta Hawks"), ("BOS", "Boston Celtics"), ("BKN", "Brooklyn Nets"), ("CHA", "Charlotte Hornets"),
    ("CHI", "Chicago Bulls"), ("CLE", "Cleveland Cavaliers"), ("DAL", "Dallas Mavericks"), ("DEN", "Denver Nuggets"),
    ("DET", "Detroit Pistons"), ("GS", "Golden State Warriors"), ("HOU", "Houston Rockets"), ("IND", "Indiana Pacers"),
    ("LAC", "LA Clippers"), ("LAL", "Los Angeles Lakers"), ("MEM", "Memphis Grizzlies"), ("MIA", "Miami Heat"),
    ("MIL", "Milwaukee Bucks"), ("MIN", "Minnesota Timberwolves"), ("NO", "New Orleans Pelicans"), ("NY", "New York Knicks"),
    ("OKC", "Oklahoma City Thunder"), ("ORL", "Orlando Magic"), ("PHI", "Philadelphia 76ers"), ("PHX", "Phoenix Suns"),
    ("POR", "Portland Trail Blazers"), ("SAC", "Sacramento Kings"), ("SA", "San Antonio Spurs"), ("TOR", "Toronto Raptors"),
    ("UTAH", "Utah Jazz"), ("WSH", "Washington Wizards"),
)
TEAM_NAMES = dict(TEAMS)

BG_TOP, BG_BOTTOM = (22, 28, 48), (10, 12, 22)
CARD = (255, 255, 255, 18)
TEXT, MUTED, DIM = (244, 246, 252), (150, 158, 180), (112, 120, 142)
ORANGE, LIVE = (238, 122, 48), (240, 84, 84)


@dataclass(frozen=True)
class Side:
    abbr: str
    score: str | None
    winner: bool
    logo: str | None
    record: str | None = None          # "11-4"
    lines: tuple[int, ...] = ()        # points per period
    color: str | None = None           # hex, without '#'


@dataclass(frozen=True)
class Game:
    when: datetime          # local time
    state: str              # pre | in | post
    detail: str             # "Final", "Final/OT", "7:32 - 3rd", "Halftime", ...
    period: int
    away: Side
    home: Side
    clock: str = ""         # game clock in the current period, "7:32"


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (deephack)"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.load(r)


def _score(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("displayValue")
    return str(value) if value not in (None, "") else None


def parse_event(event: dict) -> Game | None:
    """Normalise an event from either the scoreboard or a team schedule (their shapes differ slightly)."""
    try:
        comp = event["competitions"][0]
        status = comp.get("status") or event["status"]
        sides = {}
        for c in comp["competitors"]:
            team = c["team"]
            logo = team.get("logo") or next((l.get("href") for l in team.get("logos") or [] if l.get("href")), None)
            records = c.get("records") or c.get("record") or []
            record = next((r["summary"] for r in records if isinstance(r, dict) and r.get("summary")), None)
            lines = tuple(int(l.get("value") or 0) for l in c.get("linescores") or [] if isinstance(l, dict))
            sides[c["homeAway"]] = Side(team["abbreviation"], _score(c.get("score")), bool(c.get("winner")), logo,
                                        record, lines, team.get("color"))
        when = datetime.fromisoformat(event["date"].replace("Z", "+00:00")).astimezone()
        return Game(when, status["type"]["state"], status["type"].get("shortDetail") or "", int(status.get("period") or 0),
                    sides["away"], sides["home"], str(status.get("displayClock") or ""))
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def period_name(period: int) -> str:
    return f"Q{period}" if period <= 4 else "OT" if period == 5 else f"{period - 4}OT"


def live_label(game: Game) -> str:
    """'Q3 · 7:32', or the feed's own words for breaks ('Halftime', 'End of 3rd')."""
    if not game.period or any(w in game.detail for w in ("Half", "End", "Delayed", "Postponed")):
        return game.detail or "Live"
    clock = game.clock if game.clock not in ("", "0.0", "0:00") else ""
    return period_name(game.period) + (f" · {clock}" if clock else "")


def pick(games: list[Game]) -> tuple[list[Game], list[Game]]:
    """(two most recent finished games, oldest first; next two games, live ones first)."""
    games = sorted(games, key=lambda g: g.when)
    past = [g for g in games if g.state == "post"][-2:]
    upcoming = [g for g in games if g.state != "post"][:2]
    return past, upcoming


def fetch_games(team: str) -> list[Game]:
    if team != "all":
        events = _get_json(API + f"teams/{urllib.parse.quote(team.lower())}/schedule").get("events", [])
        return [g for g in map(parse_event, events) if g]
    # league wide: walk outwards from today, one day per request, until both lists are full
    games: dict[tuple, Game] = {}
    today = datetime.now().date()
    for offset in [0, -1, 1] + [s * d for d in range(2, SEARCH_DAYS + 1) for s in (-1, 1)]:
        past, upcoming = pick(list(games.values()))
        if (offset < 0 and len(past) >= 2) or (offset > 0 and len(upcoming) >= 2):
            continue
        day = today + timedelta(days=offset)
        for event in _get_json(API + f"scoreboard?dates={day:%Y%m%d}").get("events", []):
            g = parse_event(event)
            if g:
                games[(g.when, g.away.abbr, g.home.abbr)] = g
    return list(games.values())


def live_candidate(upcoming: list[Game], now: datetime | None = None) -> Game | None:
    """The game in progress, or one that should be by its start time (the schedule can lag behind tip-off)."""
    now = now or datetime.now().astimezone()
    return next((g for g in upcoming if g.state == "in" or (g.state == "pre" and g.when <= now)), None)


def fetch_live(team: str, game: Game) -> Game | None:
    """Fresh state of one team's game from the scoreboard, which carries the clock and points per period."""
    us_date = (game.when.astimezone(timezone.utc) - timedelta(hours=5)).date()     # the feed files games by US date
    for url in (API + "scoreboard", API + f"scoreboard?dates={us_date:%Y%m%d}"):
        for event in _get_json(url).get("events", []):
            g = parse_event(event)
            if g and team in (g.away.abbr, g.home.abbr) and abs((g.when - game.when).total_seconds()) < 12 * 3600:
                return g
    return None


# ------------------------------------------------------------------ logos
def logo(side: Side, size: int) -> Image.Image | None:
    cache = config_dir() / "cache" / "nba"
    path = cache / f"{''.join(ch for ch in side.abbr.lower() if ch.isalnum())}.png"
    if not path.exists():
        url = urllib.parse.urlparse(side.logo or "")
        if url.scheme != "https" or not (url.hostname or "").endswith(LOGO_HOST_SUFFIX):
            return None
        try:
            req = urllib.request.Request(side.logo, headers={"User-Agent": "Mozilla/5.0 (deephack)"})
            with urllib.request.urlopen(req, timeout=12) as r:
                data = r.read(2_000_000)
            Image.open(io.BytesIO(data)).verify()
            cache.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except (OSError, ValueError, SyntaxError):
            return None
    try:
        img = Image.open(path).convert("RGBA")
    except OSError:
        return None
    img.thumbnail((size, size), Image.LANCZOS)
    return img


# ------------------------------------------------------------------ rendering
def _day_label(when: datetime) -> str:
    delta = (when.date() - datetime.now().date()).days
    return {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get(delta, when.strftime("%a %d %b"))


def _team(img: Image.Image, d: ImageDraw.ImageDraw, side: Side, cx: int, y: int) -> ImageDraw.ImageDraw:
    mark = logo(side, 66)
    if mark is not None:
        img.alpha_composite(mark, (cx - mark.width // 2, y + 8 + (66 - mark.height) // 2))
        d = ImageDraw.Draw(img)
    else:
        d.ellipse([cx - 30, y + 11, cx + 30, y + 71], fill=(60, 68, 96))
    d.text((cx, y + 80), side.abbr, font=render.font(render.SEMIBOLD, 18), fill=TEXT, anchor="ma")
    return d


def _card(img: Image.Image, game: Game, y: int) -> None:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle([20, y, W - 20, y + 112], radius=16, fill=CARD)
    img.alpha_composite(layer)
    d = ImageDraw.Draw(img)
    d = _team(img, d, game.away, 76, y)
    d = _team(img, d, game.home, W - 76, y)
    mid = W // 2
    big, sub = render.font(render.BOLD, 40), render.font(render.REGULAR, 17)
    if game.state == "pre":
        d.text((mid, y + 44), game.when.strftime("%H:%M"), font=big, fill=TEXT, anchor="mm")
        d.text((mid, y + 82), _day_label(game.when), font=sub, fill=MUTED, anchor="mm")
        return
    live = game.state == "in"
    for side, x, anchor in ((game.away, mid - 16, "rm"), (game.home, mid + 16, "lm")):
        colour = LIVE if live else TEXT if side.winner else DIM
        d.text((x, y + 44), side.score or "-", font=big, fill=colour, anchor=anchor)
    d.text((mid, y + 42), "–", font=render.font(render.REGULAR, 28), fill=DIM, anchor="mm")
    if live:
        label = "LIVE" + (f" · {period_name(game.period)}" if game.period else "")
        d.text((mid, y + 82), label, font=render.font(render.SEMIBOLD, 17), fill=LIVE, anchor="mm")
    else:
        d.text((mid, y + 82), f"{game.detail or 'Final'} · {_day_label(game.when)}", font=sub, fill=MUTED, anchor="mm")


def render_games(team: str, past: list[Game], upcoming: list[Game]) -> Image.Image:
    img = render.vertical_gradient(BG_TOP, BG_BOTTOM).convert("RGBA")
    d = ImageDraw.Draw(img)
    # a small basketball
    d.ellipse([24, 20, 60, 56], fill=ORANGE)
    d.line([42, 20, 42, 56], fill=(120, 56, 20), width=2)
    d.line([24, 38, 60, 38], fill=(120, 56, 20), width=2)
    d.arc([8, 20, 36, 56], -60, 60, fill=(120, 56, 20), width=2)
    d.arc([48, 20, 76, 56], 120, 240, fill=(120, 56, 20), width=2)
    d.text((72, 38), "NBA", font=render.font(render.BOLD, 30), fill=TEXT, anchor="lm")
    title = TEAM_NAMES.get(team, "Around the league")
    d.text((W - 24, 40), render.fit(d, title, render.font(render.REGULAR, 19), 250), font=render.font(render.REGULAR, 19), fill=MUTED, anchor="rm")

    y = 70
    for label, games, empty in (("RECENT", past, "No recent games"), ("UPCOMING", upcoming, "No games scheduled")):
        d = ImageDraw.Draw(img)
        d.text((24, y + 12), label, font=render.font(render.BOLD, 15), fill=MUTED, anchor="lm")
        y += 26
        if not games:
            d.text((W / 2, y + 112), empty, font=render.font(render.REGULAR, 20), fill=DIM, anchor="mm")
        for i, game in enumerate(games[:2]):
            _card(img, game, y + i * 120)
        y += 240 + 4
    return img.convert("RGB")


def _rgb(color: str | None, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    try:
        return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))
    except (TypeError, ValueError, IndexError):
        return fallback


def _points(score: str | None) -> int:
    try:
        return int(score or 0)
    except ValueError:
        return 0


def render_live(team: str, game: Game) -> Image.Image:
    """Big scoreboard for a game in progress, tinted with the picked team's colour."""
    ours = game.away if game.away.abbr == team else game.home
    tint = _rgb(ours.color, BG_TOP)
    img = render.vertical_gradient(tuple(round(c * 0.45 + b * 0.55) for c, b in zip(tint, BG_TOP)), BG_BOTTOM).convert("RGBA")
    d = ImageDraw.Draw(img)
    f = render.font

    # header: LIVE pill and the game clock
    d.rounded_rectangle([24, 24, 104, 58], radius=17, fill=LIVE)
    d.text((64, 41), "LIVE", font=f(render.BOLD, 19), fill=(255, 255, 255), anchor="mm")
    d.text((W - 24, 41), live_label(game), font=f(render.SEMIBOLD, 26), fill=TEXT, anchor="rm")

    # teams: logo, abbreviation, record, score (the trailing team dimmed)
    a, h = _points(game.away.score), _points(game.home.score)
    size = 104
    while size > 60 and max(d.textlength(s.score or "0", font=f(render.BOLD, size)) for s in (game.away, game.home)) > 172:
        size -= 4                                   # three-digit scores must leave room for the dash
    for side, cx, behind in ((game.away, 128, a < h), (game.home, W - 128, h < a)):
        mark = logo(side, 132)
        if mark is not None:
            img.alpha_composite(mark, (cx - mark.width // 2, 96 + (132 - mark.height) // 2))
            d = ImageDraw.Draw(img)
        else:
            d.ellipse([cx - 60, 102, cx + 60, 222], fill=(60, 68, 96))
        d.text((cx, 248), side.abbr, font=f(render.BOLD, 28), fill=TEXT, anchor="ma")
        d.text((cx, 286), side.record or "", font=f(render.REGULAR, 18), fill=MUTED, anchor="ma")
        d.text((cx, 392), side.score or "0", font=f(render.BOLD, size), fill=DIM if behind else TEXT, anchor="mm")
    d.text((W / 2, 162), "@", font=f(render.REGULAR, 26), fill=DIM, anchor="mm")
    d.text((W / 2, 388), "–", font=f(render.LIGHT, 60), fill=DIM, anchor="mm")

    # points per period
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle([20, 478, W - 20, H - 20], radius=16, fill=CARD)
    img.alpha_composite(layer)
    d = ImageDraw.Draw(img)
    periods = max(len(game.away.lines), len(game.home.lines), 4)
    first, last = 120, W - 112
    step = (last - first) / (periods - 1)
    head, num = f(render.SEMIBOLD, 15), f(render.REGULAR, 21)
    for i in range(periods):
        x = first + step * i
        d.text((x, 502), period_name(i + 1), font=head, fill=MUTED, anchor="mm")
        for side, y in ((game.away, 544), (game.home, 590)):
            played = i < len(side.lines)
            d.text((x, y), str(side.lines[i]) if played else "–", font=num, fill=TEXT if played else DIM, anchor="mm")
    d.text((W - 50, 502), "T", font=head, fill=MUTED, anchor="mm")
    for side, y in ((game.away, 544), (game.home, 590)):
        d.text((44, y), side.abbr, font=f(render.SEMIBOLD, 19), fill=TEXT, anchor="lm")
        d.text((W - 50, y), side.score or "0", font=f(render.BOLD, 21), fill=TEXT, anchor="mm")
    return img.convert("RGB")


class Nba(Mode):
    id = "nba"
    title = "NBA"
    description = "Last 2 results, next 2 games"
    glyph = ""
    settings = (
        Setting("team", "Team", "choice", "all", (("all", "All teams"),) + tuple((abbr, name) for abbr, name in TEAMS)),
        Setting("refresh", "Refresh every", "choice", "30", (("15", "15 minutes"), ("30", "30 minutes"), ("60", "1 hour")),
                hint="Scores and logos come from ESPN's public feed. The display is rewritten only when something changed."),
        Setting("live", "During your team's games", "choice", "5",
                (("3", "Scoreboard, every 3 minutes"), ("5", "Scoreboard, every 5 minutes"),
                 ("10", "Scoreboard, every 10 minutes"), ("off", "Keep this view")),
                hint="With a team picked, its games in progress show as a big scoreboard. "
                     "Each update is a write to the cooler's flash."),
    )

    def __init__(self, values=None):
        super().__init__(values)
        self._next = 0.0
        self._shown = None
        self.placeholder = ("NBA", "Fetching games…")

    @property
    def live_minutes(self) -> int | None:
        """Scoreboard refresh while the picked team plays; None when there is no live view."""
        if self.values["team"] == "all" or self.values["live"] == "off":
            return None
        return int(self.values["live"])

    def _load(self) -> tuple[list[Game], list[Game], Game | None, Image.Image]:
        team = self.values["team"]
        past, upcoming = pick(fetch_games(team))
        candidate = live_candidate(upcoming) if self.live_minutes else None
        live = fetch_live(team, candidate) if candidate else None
        if live is not None and live.state == "in":
            return past, upcoming, live, render_live(team, live)
        return past, upcoming, None, render_games(team, past, upcoming)

    def _schedule(self, upcoming: list[Game], live: Game | None) -> None:
        now, refresh = time.monotonic(), int(self.values["refresh"]) * 60
        if self.live_minutes is None:
            self._next = now + refresh
        elif live is not None or live_candidate(upcoming):
            self._next = now + self.live_minutes * 60       # in progress, or should have started by now
        else:
            # wake up at the next tip-off instead of up to a whole refresh interval later
            starts = [(g.when - datetime.now().astimezone()).total_seconds() for g in upcoming if g.state == "pre"]
            soon = [s for s in starts if 0 < s < refresh]
            self._next = now + (min(soon) + 30 if soon else refresh)

    async def frame(self):
        if time.monotonic() < self._next:
            return None
        try:
            past, upcoming, live, image = await asyncio.to_thread(self._load)
        except (OSError, ValueError) as e:
            self.status = f"Score feed unreachable ({type(e).__name__})"
            self._next = time.monotonic() + RETRY_SECONDS
            return None
        self._schedule(upcoming, live)
        if live is not None:
            self.status = f"LIVE {live.away.abbr} {live.away.score or 0} – {live.home.score or 0} {live.home.abbr} · {live_label(live)}"
            if ("live", live) == self._shown:
                return None
            self._shown = ("live", live)
            return image
        nxt = upcoming[0] if upcoming else None
        self.status = (f"Next: {nxt.away.abbr} @ {nxt.home.abbr}, {_day_label(nxt.when).lower()} {nxt.when:%H:%M}" if nxt
                       else "No upcoming games")
        key = (self.values["team"], tuple(past), tuple(upcoming), datetime.now().date())   # the date: "Today" labels roll over
        if key == self._shown:
            return None
        self._shown = key
        return image
