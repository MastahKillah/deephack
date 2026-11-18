"""NBA: the two most recent results and the next two games, for one team or the whole league.

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
from datetime import datetime, timedelta

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


@dataclass(frozen=True)
class Game:
    when: datetime          # local time
    state: str              # pre | in | post
    detail: str             # "Final", "Final/OT", ...
    period: int
    away: Side
    home: Side


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
            sides[c["homeAway"]] = Side(team["abbreviation"], _score(c.get("score")), bool(c.get("winner")), logo)
        when = datetime.fromisoformat(event["date"].replace("Z", "+00:00")).astimezone()
        return Game(when, status["type"]["state"], status["type"].get("shortDetail") or "", int(status.get("period") or 0),
                    sides["away"], sides["home"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


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
        label = "LIVE" + (f" · {'Q' + str(game.period) if game.period <= 4 else 'OT'}" if game.period else "")
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


class Nba(Mode):
    id = "nba"
    title = "NBA"
    description = "Last 2 results, next 2 games"
    glyph = ""
    settings = (
        Setting("team", "Team", "choice", "all", (("all", "All teams"),) + tuple((abbr, name) for abbr, name in TEAMS)),
        Setting("refresh", "Refresh every", "choice", "30", (("15", "15 minutes"), ("30", "30 minutes"), ("60", "1 hour")),
                hint="Scores and logos come from ESPN's public feed. The display is rewritten only when something changed."),
    )

    def __init__(self, values=None):
        super().__init__(values)
        self._next = 0.0
        self._shown = None
        self.placeholder = ("NBA", "Fetching games…")

    def _load(self) -> tuple[list[Game], list[Game], Image.Image]:
        past, upcoming = pick(fetch_games(self.values["team"]))
        return past, upcoming, render_games(self.values["team"], past, upcoming)

    async def frame(self):
        if time.monotonic() < self._next:
            return None
        try:
            past, upcoming, image = await asyncio.to_thread(self._load)
        except (OSError, ValueError) as e:
            self.status = f"Score feed unreachable ({type(e).__name__})"
            self._next = time.monotonic() + RETRY_SECONDS
            return None
        self._next = time.monotonic() + int(self.values["refresh"]) * 60
        nxt = upcoming[0] if upcoming else None
        self.status = (f"Next: {nxt.away.abbr} @ {nxt.home.abbr}, {_day_label(nxt.when).lower()} {nxt.when:%H:%M}" if nxt
                       else "No upcoming games")
        key = (self.values["team"], tuple(past), tuple(upcoming), datetime.now().date())   # the date: "Today" labels roll over
        if key == self._shown:
            return None
        self._shown = key
        return image
