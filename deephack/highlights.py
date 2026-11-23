"""The highlights of a finished NBA game: which YouTube video they are, if there is one yet.

For the NBA mode, which can play them once after the picked team's game. The NBA's own channel used to
post "Full Game Highlights" of every game; in November 2026 it does so for one in a great many (it posts
the last minutes of close games, a nightly recap, single players). Whole games' highlights come from
other channels YouTube has verified, and short ones from the teams' own channels. So the video is picked
by rule, not by channel:

- **It is this game:** both teams are named in the title, and so is the day it was played (as it is
  written in the United States, where the day is the one the game began on there).
- **It is highlights, and not something about them:** no reactions, previews, predictions, simulations,
  watch parties; between one and thirty minutes long; on a verified channel.
- The best of what passes: a whole game's highlights before one team's or the last minutes, the NBA's
  own before anybody else's, the longer before the shorter.

Tried on the 43 games of a week (17 to 22 Nov 2026): 39 found, every one the right game and every one
ten minutes of the whole game (36 from one channel, 3 from another); two and a half seconds a game. The
search does not answer the same way twice: the four that were missed were found in another run, so the
NBA mode asks again every ten minutes. An earlier rule let through somebody watching the highlights
("FlightReacts To ..."), which the words now keep out.

When they come: 2.5 to 3.7 hours after tip-off for the nine games measured, most often a little over
three: half an hour to an hour after the final.

Highlights of another game are worse than none: when nothing passes, there are none (yet).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date
from pathlib import Path

from . import ytdlp
from .musicvideo import Candidate, plain

SEARCH = 10
SHORTEST, LONGEST = 60, 30 * 60
NOT_IT = ("react", "preview", "prediction", "predict", "2k", "simulation", "watch party", "live stream", "livestream", "podcast",
          "summer league", "g league", "wnba", "press conference", "postgame", "post game", "interview", "breakdown", "film",
          "odds", "picks", "fantasy", "mix", "career", "top 10", "top 5")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
OTHER_NAMES = {"Trail Blazers": ("blazers",), "76ers": ("sixers",)}      # what a team is also called in a title


def nickname(team: str) -> str:
    """ "Portland Trail Blazers" -> "Trail Blazers"; "LA Clippers" -> "Clippers"."""
    return "Trail Blazers" if team.endswith("Trail Blazers") else team.split()[-1]


def names(team: str) -> tuple[str, ...]:
    """What a team may be called in a title, plain: its nickname, and what else people say."""
    nick = nickname(team)
    return (plain(nick), *OTHER_NAMES.get(nick, ()))


def days(played: date) -> tuple[str, ...]:
    """The day as a title writes it, plain: "november 22 2026", "nov 22 2026"."""
    month = MONTHS[played.month - 1]
    return (plain(f"{month} {played.day}, {played.year}"), plain(f"{month[:3]} {played.day}, {played.year}"))


def judge(candidate: Candidate, away: str, home: str, played: date) -> float | None:
    """How well a video fits a game (the teams by their full names): None when it does not."""
    its, channel = f" {plain(candidate.title)} ", plain(candidate.channel)
    if not candidate.verified or not SHORTEST <= candidate.seconds <= LONGEST:
        return None
    if not all(any(f" {name} " in its for name in names(team)) for team in (away, home)):
        return None
    if not any(f" {day} " in its for day in days(played)):
        return None
    if any(word in its for word in NOT_IT) or "highlight" not in its and "ending" not in its:
        return None
    whole = "full game highlights" in its or "game highlights" in its
    own = channel in (plain(away), plain(home))                          # a team's own channel
    return 5.0 * whole + 3.0 * (channel == "nba") + 1.0 * own + 2.0 * min(candidate.seconds, 600) / 600


def search(away: str, home: str, played: date) -> list[Candidate]:
    """What YouTube's search gives for a game. Asks the net; two seconds or so.

    Asked in two ways, as the two kinds of title are written: the same question does not give the same
    videos every time, and one way alone missed one game in six that the other found."""
    yt_dlp = ytdlp.module()
    day = f"{MONTHS[played.month - 1]} {played.day}, {played.year}"
    found: dict[str, Candidate] = {}
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}) as ydl:
        for words in (f"{home} vs {away} full game highlights {day}", f"{nickname(away)} at {nickname(home)} highlights {day}"):
            for e in (ydl.extract_info(f"ytsearch{SEARCH}:{words}", download=False) or {}).get("entries") or []:
                if e and e.get("id"):
                    found.setdefault(str(e["id"]), Candidate(
                        str(e["id"]), e.get("title") or "", e.get("channel") or e.get("uploader") or "",
                        float(e.get("duration") or 0), int(e.get("view_count") or 0), bool(e.get("channel_is_verified"))))
    return list(found.values())


def pick(candidates: list[Candidate], away: str, home: str, played: date) -> Candidate | None:
    judged = [(score, candidate.views, candidate) for candidate in candidates
              if (score := judge(candidate, away, home, played)) is not None]
    return max(judged, key=lambda j: j[:2])[2] if judged else None


def find(away: str, home: str, played: date, search=search) -> Candidate | None:
    """The game's highlights, or None when there are none that are surely this game's. Raises what the
    search raises."""
    return pick(search(away, home, played), away, home, played)


class Seen:
    """Which game's highlights are which video, and whether they have been played: kept in a file, so a
    game's highlights play once and are searched for no more than needed."""

    KEEP = 60

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        try:
            self._games: dict[str, dict] = json.loads(path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            self._games = {}

    def get(self, key: str) -> dict:
        with self._lock:
            return dict(self._games.get(key) or {})

    def put(self, key: str, **what) -> None:
        with self._lock:
            self._games[key] = {**(self._games.get(key) or {}), **what, "at": time.time()}
            while len(self._games) > self.KEEP:
                self._games.pop(next(iter(self._games)))
            try:
                self.path.write_text(json.dumps(self._games), encoding="utf8")
            except OSError:
                pass
