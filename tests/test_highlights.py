"""A finished NBA game's highlights: which YouTube video they are (highlights.py), and the NBA mode playing
them. YouTube and the score feed are stood in for."""
import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import av
import pytest

from deephack import highlights
from deephack.highlights import Seen, judge, pick
from deephack.modes import nba, video
from deephack.modes.base import Part
from deephack.musicvideo import Candidate

AWAY, HOME, DAY = "Boston Celtics", "Golden State Warriors", date(2026, 11, 22)
WHOLE = Candidate("w", "Golden State Warriors vs Boston Celtics Full Game Highlights - November 22, 2026 | NBA Season",
                  "GAMETIME HIGHLIGHTS", 594, 300_000, True)
ENDING = Candidate("e", "Final 3:11 DRAMATIC ENDING Celtics at Warriors | November 22, 2026", "NBA", 597, 900_000, True)
TEAMS_OWN = Candidate("t", "Boston Celtics at Golden State Warriors (November 22, 2026) | FULL GAME HIGHLIGHTS", "Boston Celtics", 120, 50_000, True)
OFFICIAL = Candidate("o", "CELTICS at WARRIORS | FULL GAME HIGHLIGHTS | November 22, 2026", "NBA", 600, 2_000_000, True)


# ---------------------------------------------------------------- which video
def test_a_video_that_names_both_teams_and_the_day_is_the_games():
    for candidate in (WHOLE, ENDING, TEAMS_OWN, OFFICIAL):
        assert judge(candidate, AWAY, HOME, DAY) is not None, candidate.title
    short = Candidate("s", "Game Highlights: Portland Trail Blazers 124, Milwaukee Bucks 114 | Nov. 19, 2026", "Portland Trail Blazers", 120, 1, True)
    assert judge(short, "Portland Trail Blazers", "Milwaukee Bucks", date(2026, 11, 19)) is not None        # "Nov. 19, 2026" is a day too
    blazers = Candidate("b", "Final 3:15 INSANE ENDING Blazers at 76ers | November 17, 2026", "NBA", 400, 1, True)
    assert judge(blazers, "Portland Trail Blazers", "Philadelphia 76ers", date(2026, 11, 17)) is not None     # what they are also called
    sixers = Candidate("x", "Sixers vs Trail Blazers Full Game Highlights | November 17, 2026", "GAMETIME HIGHLIGHTS", 590, 1, True)
    assert judge(sixers, "Portland Trail Blazers", "Philadelphia 76ers", date(2026, 11, 17)) is not None
    assert highlights.nickname("LA Clippers") == "Clippers" and highlights.nickname("Portland Trail Blazers") == "Trail Blazers"


@pytest.mark.parametrize("candidate, why", [
    (Candidate("a", "Golden State Warriors vs Boston Celtics Full Game Highlights - November 20, 2026", "GAMETIME HIGHLIGHTS", 594, 1, True), "another day's game"),
    (Candidate("b", "Golden State Warriors vs Los Angeles Lakers Full Game Highlights - November 22, 2026", "GAMETIME HIGHLIGHTS", 594, 1, True), "another game"),
    (Candidate("c", "FlightReacts To CELTICS at WARRIORS | FULL GAME HIGHLIGHTS | November 22, 2026!", "NotYourAverageFlight", 990, 1, True), "somebody watching them"),
    (Candidate("d", "Golden State Warriors vs Boston Celtics Full Game Highlights - November 22, 2026", "some fan", 594, 1, False), "a channel nobody vouches for"),
    (Candidate("e", "CELTICS vs WARRIORS | LAS VEGAS SUMMER LEAGUE | FULL GAME HIGHLIGHTS | November 22, 2026", "NBA", 640, 1, True), "the summer league"),
    (Candidate("f", "Celtics at Warriors Full Game | November 22, 2026", "NBA", 2 * 3600, 1, True), "the whole game, two hours of it"),
    (Candidate("g", "Celtics at Warriors Full Game Highlights | November 22, 2026", "NBA", 20, 1, True), "a few seconds"),
    (Candidate("h", "Celtics vs Warriors Preview & Prediction | November 22, 2026", "ESPN", 400, 1, True), "before the game"),
    (Candidate("i", "Jayson Tatum 40 PTS vs Warriors | November 22, 2026", "NBA", 300, 1, True), "one player, and one team named"),
    (Candidate("j", "Celtics at Warriors postgame press conference highlights | November 22, 2026", "NBC Sports Boston", 600, 1, True), "talking"),
    (Candidate("k", "NBA 2K27 Celtics at Warriors Full Game Highlights | November 22, 2026", "Gamer", 600, 1, True), "a video game"),
])
def test_what_is_not_the_games_highlights(candidate, why):
    assert judge(candidate, AWAY, HOME, DAY) is None, why


def test_the_whole_game_before_its_last_minutes_and_the_nbas_own_before_anybodys():
    assert pick([ENDING, TEAMS_OWN, WHOLE], AWAY, HOME, DAY) is WHOLE         # ten minutes of all of it
    assert pick([ENDING, TEAMS_OWN, WHOLE, OFFICIAL], AWAY, HOME, DAY) is OFFICIAL
    assert pick([ENDING, TEAMS_OWN], AWAY, HOME, DAY) is TEAMS_OWN
    assert pick([ENDING], AWAY, HOME, DAY) is ENDING
    assert pick([], AWAY, HOME, DAY) is None
    asked = []
    assert highlights.find(AWAY, HOME, DAY, search=lambda *game: (asked.append(game), [WHOLE])[1]) is WHOLE
    assert asked == [(AWAY, HOME, DAY)]
    assert highlights.days(date(2026, 3, 5)) == ("march 5 2026", "mar 5 2026")


def test_which_video_and_whether_it_was_played_is_kept(tmp_path):
    seen = Seen(tmp_path / "seen.json")
    assert seen.get("2026-11-22:BOS@GS") == {}
    seen.put("2026-11-22:BOS@GS", link="https://y/w")
    seen.put("2026-11-22:BOS@GS", played=True)
    kept = Seen(tmp_path / "seen.json").get("2026-11-22:BOS@GS")              # after a restart
    assert kept["link"] == "https://y/w" and kept["played"] is True
    for i in range(Seen.KEEP + 4):
        seen.put(f"game {i}", link="x")
    assert len(Seen(tmp_path / "seen.json")._games) == Seen.KEEP
    assert Seen(tmp_path / "none" / "seen.json").get("x") == {}


# ---------------------------------------------------------------- the mode
class Reel:
    """Stands in for the video's player."""

    def __init__(self, source, on_end=None):
        self.source, self.on_end = source, on_end
        self.state, self.position, self.duration, self.seq, self.stopped, self.sound, self.volume = "playing", 0.0, 592.0, 0, False, None, None
        self.rate = 30.0
        self.frame = av.VideoFrame(640, 360, "yuv420p")
        for plane, value in zip(self.frame.planes, (210, 100, 180)):
            plane.update(bytes([value]) * plane.buffer_size)

    def ask(self):
        self.seq, self.position = self.seq + 1, self.position + 0.04

    def latest(self):
        return self.seq, self.frame

    def set_sound(self, on):
        self.sound = on

    def stop(self):
        self.stopped = True


def side(abbr, score, lines=()):
    return nba.Side(abbr, str(score), False, None, "10-5", tuple(lines), "007a33")


def make(tmp_path, monkeypatch, values=None, found=WHOLE, ended=3.0, direct=True):
    """The mode with BOS picked, whose game ended `ended` hours ago, and a search that finds `found`."""
    game = nba.Game(datetime.now().astimezone() - timedelta(hours=ended), "post", "Final", 4, side("BOS", 87), side("GS", 88))
    world = SimpleNamespace(game=game, searched=[], reels=[], found=found)
    monkeypatch.setattr(nba, "fetch_games", lambda team: [world.game])
    monkeypatch.setattr(nba, "logo", lambda side, size: None)

    def find(away, home, day):
        world.searched.append((away, home, day))
        if isinstance(world.found, Exception):
            raise world.found
        return world.found

    def player(*args, **kwargs):
        world.reels.append(Reel(*args, **kwargs))
        return world.reels[-1]

    mode = nba.Nba({"team": "BOS", "highlights": "play", **(values or {})})
    mode.direct, mode.audible = direct, True
    mode._find, mode._make_player = find, player
    mode._details = lambda team, game: nba.Game(game.when, "post", "Final", 4, side("BOS", 87, (20, 22, 33, 12)), side("GS", 88, (27, 17, 25, 19)))
    mode._seen = Seen(tmp_path / "seen.json")
    return mode, world


async def steps(mode, count=6):
    pictures = []
    for _ in range(count):
        picture = await mode.frame()
        if picture is not None:
            pictures.append(picture)
        await asyncio.sleep(0.01)                                            # the search runs in a thread
    return pictures


def test_highlights_are_off_unless_switched_on_and_only_for_a_picked_team(tmp_path, monkeypatch):
    setting = {s.key: s for s in nba.Nba.settings}["highlights"]
    assert setting.default == "off" and nba.Nba.actions == (("highlights", "Highlights"),)
    for values, direct in (({"highlights": "off"}, True), ({"team": "all"}, True), ({}, False)):
        mode, world = make(tmp_path, monkeypatch, values, direct=direct)
        pictures = asyncio.run(steps(mode))
        assert len(pictures) == 1 and world.searched == [] and world.reels == [] and mode.tick is None      # the scores, once


def test_the_last_games_highlights_play_once_and_then_the_scores_are_back(tmp_path, monkeypatch):
    async def main():
        mode, world = make(tmp_path, monkeypatch)
        scores = (await steps(mode, 1))[0]
        pictures = await steps(mode, 8)
        assert world.searched == [("Boston Celtics", "Golden State Warriors", nba.played_on(world.game))]
        reel = world.reels[0]
        assert reel.source == WHOLE.link and mode.tick == video.TICK and mode.quality == video.QUALITY
        mode.sent_bytes = 60_000                                             # more than the cooler takes in a 30th of a second:
        await steps(mode, 2)
        assert video.LEAST_QUALITY <= mode.quality < video.QUALITY           # less sharp, and not fewer pictures
        assert mode.status.startswith("Highlights: BOS at GS · 0:00 of 9:52") and mode.active       # Auto keeps it on screen
        last = pictures[-1]
        assert isinstance(last, Part) and last.fits and last.at == (0, nba.REEL_TOP)       # only the video's rows change:
        assert last.picture.size == (480, nba.REEL_PART) and all(p.page is last.page for p in pictures)     # one page
        assert nba.REEL_TOP + nba.REEL_PART <= 468                           # (the points per period begin below them)
        page = last.whole
        middle = (240, nba.REEL_TOP + nba.REEL_SIZE[1] // 2)
        assert last.page.getpixel(middle) == (0, 0, 0)                       # the page by itself has a hole there
        assert page.size == (480, 640) and page.getpixel(middle) != scores.getpixel(middle)          # the video, in the middle
        assert page.getpixel((240, 20)) != (0, 0, 0) and page.getpixel((240, 620)) != (0, 0, 0)      # with the score round it
        assert page.getpixel((104, 544)) != page.getpixel((104, 520))        # the points per period, which the feed's
        assert reel.sound is False                                           # list of games does not have. No sound unless set

        reel.on_end()                                                        # the video is over
        back = await steps(mode, 3)
        assert reel.stopped and mode.tick is None and mode.quality is None and len(back) == 1
        assert back[0].tobytes() == scores.tobytes() and not mode.status.startswith("Highlights")
        assert mode._seen.get(nba.reel_key(world.game))["played"] is True
        await steps(mode, 4)
        assert len(world.reels) == 1 and len(world.searched) == 1            # once

        again = nba.Nba({"team": "BOS", "highlights": "play"})               # and not again after a restart
        again.direct, again._find, again._make_player, again._seen = True, mode._find, mode._make_player, Seen(tmp_path / "seen.json")
        await steps(again, 4)
        assert len(world.reels) == 1 and len(world.searched) == 1

    asyncio.run(main())


def test_they_are_looked_for_again_until_youtube_has_them(tmp_path, monkeypatch):
    async def main():
        mode, world = make(tmp_path, monkeypatch, found=None)
        await steps(mode, 5)
        assert len(world.searched) == 1 and world.reels == [] and mode.tick is None
        await steps(mode, 5)
        assert len(world.searched) == 1                                      # not at every look
        world.found = OSError("no net")
        mode._next_look = 0.0                                                # ten minutes later
        await steps(mode, 4)
        assert len(world.searched) == 2 and world.reels == []
        world.found = WHOLE
        mode._next_look = 0.0
        await steps(mode, 5)
        assert len(world.searched) == 3 and len(world.reels) == 1

    asyncio.run(main())


def test_an_old_game_is_left_alone_but_the_button_plays_it_and_ends_it(tmp_path, monkeypatch):
    async def main():
        mode, world = make(tmp_path, monkeypatch, ended=40.0)                # the day before yesterday
        await steps(mode, 4)
        assert world.searched == [] and world.reels == []
        mode.act("highlights")
        await steps(mode, 5)
        assert len(world.reels) == 1 and mode.status.startswith("Highlights")
        mode.act("highlights")                                               # enough of them
        await steps(mode, 2)
        assert world.reels[0].stopped and mode.tick is None
        mode.act("highlights")                                               # and once more, although they were played
        await steps(mode, 5)
        assert len(world.reels) == 2 and len(world.searched) == 1            # which video it is, is known

        (tmp_path / "other").mkdir()
        off, world = make(tmp_path / "other", monkeypatch, {"highlights": "off"}, found=None)
        await steps(off, 2)
        off.act("highlights")                                                # the button, with the setting off and no video
        await steps(off, 5)
        assert len(world.searched) == 1 and "no highlights on YouTube yet" in off.status
        await steps(off, 5)
        assert len(world.searched) == 1                                      # asked once: the button does not keep looking
        off._note_until = 0.0                                                # and some minutes later it says no more
        await steps(off, 1)
        assert off.status == "No upcoming games"

    asyncio.run(main())


def test_a_video_that_will_not_play_and_a_mode_that_is_closed(tmp_path, monkeypatch):
    async def main():
        mode, world = make(tmp_path, monkeypatch, {"sound": "60"})
        await steps(mode, 6)
        reel = world.reels[0]
        assert reel.sound is True and reel.volume == 0.6                     # as loud as the setting says
        reel.state = "failed"
        back = await steps(mode, 3)
        assert reel.stopped and len(back) == 1 and mode.tick is None
        assert not mode._seen.get(nba.reel_key(world.game)).get("played") and "would not play" in mode.status
        await steps(mode, 6)
        assert len(world.reels) == 1 and len(world.searched) == 1            # not over and over: ten minutes later
        mode.act("highlights")                                               # or when the button asks
        await steps(mode, 5)
        assert len(world.reels) == 2 and len(world.searched) == 2 and not world.reels[1].stopped
        await mode.close()
        assert world.reels[1].stopped

    asyncio.run(main())


def test_in_auto_the_mode_on_screen_is_the_one_heard_and_packs_its_pictures_its_way():
    from deephack.modes import auto

    async def main():
        mode = auto.Auto({"order": "nba,now_playing", "fallback": "weather"})
        mode.direct, mode.audible = True, True
        seen = {}

        def child(name, active, quality):
            async def frame():
                seen[name] = (it.direct, it.audible)
                it.active = active
                return None
            it = mode.children[name]
            it.frame, it.quality = frame, quality

        child("nba", True, 75)
        child("now_playing", True, None)
        child("weather", False, None)
        mode._latest = {name: (1, None) for name in mode.children}
        mode._faded = lambda img, now: img
        await mode.frame()
        await mode.frame()
        assert mode.chosen == "nba" and mode.quality == 75
        assert seen == {"nba": (True, True), "now_playing": (True, False), "weather": (True, False)}

    asyncio.run(main())
