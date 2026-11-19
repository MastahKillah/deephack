"""Now Playing: the timeline, the picture with its progress bar, and what the mode returns when pictures
are stored (rarely, after a track has settled) and when they are drawn (at once, every second, with a fade)."""
import asyncio
import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from PIL import Image, ImageChops

from deephack import device
from deephack.modes import now_playing as np_
from deephack.modes.auto import Auto
from deephack.modes.now_playing import NowPlaying, Track, clock, render_track, timeline

NOW = datetime(2026, 11, 19, 20, 0, 0, tzinfo=timezone.utc)


def _cover(colour) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), colour).save(buf, "PNG")
    return buf.getvalue()


RED, BLUE = _cover("#b03030"), _cover("#3040b0")


def _line(position, end, updated_ago, start=0.0):
    return SimpleNamespace(position=timedelta(seconds=position), start_time=timedelta(seconds=start),
                           end_time=timedelta(seconds=end), last_updated_time=NOW - timedelta(seconds=updated_ago))


# ------------------------------------------------------------------ the timeline
def test_the_position_runs_on_from_when_the_player_last_said():
    assert timeline(_line(60, 200, updated_ago=12.5), True, NOW) == (72.5, 200)
    assert timeline(_line(60, 200, updated_ago=12.5), False, NOW) == (60, 200)        # paused: it stays
    assert timeline(_line(70, 210, updated_ago=0, start=10), True, NOW) == (60, 200)
    assert timeline(_line(199.5, 200, updated_ago=1.5), True, NOW) == (200, 200)       # a moment past the end


def test_a_timeline_that_says_nothing_is_not_believed():
    assert timeline(_line(0, 0, updated_ago=0), True, NOW) is None                     # never filled in
    assert timeline(_line(60, 200, updated_ago=6000), True, NOW) is None               # long out of date
    never = SimpleNamespace(position=timedelta(0), start_time=timedelta(0), end_time=timedelta(0),
                            last_updated_time=datetime(1601, 1, 1, tzinfo=timezone.utc))
    assert timeline(never, True, NOW) is None
    assert timeline(SimpleNamespace(), True, NOW) is None


def test_clock():
    assert [clock(s) for s in (0, 9.9, 75, 600, 3599, 3600, 3725)] == ["0:00", "0:09", "1:15", "10:00", "59:59", "1:00:00", "1:02:05"]


# ------------------------------------------------------------------ the picture
def _bar_is_white(picture, x):
    return min(picture.getpixel((x, 595))) > 235


def test_the_progress_bar_shows_how_far_in():
    track = Track("A title", "An artist", "", RED)
    plain, half = render_track(track), render_track(track, progress=(100, 200, True))
    assert plain.size == half.size == (device.WIDTH, device.HEIGHT)
    assert _bar_is_white(half, 120) and not _bar_is_white(half, 360) and not _bar_is_white(plain, 120)
    late = render_track(track, progress=(190, 200, True))
    assert _bar_is_white(late, 360)
    paused = render_track(track, progress=(100, 200, False))
    assert ImageChops.difference(half, paused).getbbox() is not None                   # the pause mark
    assert ImageChops.difference(render_track(track, "art", progress=(100, 200, True)), render_track(track, "art")).getbbox() is None


# ------------------------------------------------------------------ the mode
class Player(NowPlaying):
    """The mode with the Windows media session replaced by `now`."""

    def __init__(self, values=None):
        super().__init__(values)
        self.now: Track | None = None

    async def _track(self):
        self.active = self.now is not None and self.now.playing
        return self.now


def _frame(mode):
    return asyncio.run(mode.frame())


def _same(a, b):
    return ImageChops.difference(a, b).getbbox() is None


def test_stored_pictures_wait_for_a_track_to_settle(monkeypatch):
    clock_ = [1000.0]
    monkeypatch.setattr(np_.time, "monotonic", lambda: clock_[0])
    m = Player()
    assert _frame(m) is None and m.status == "Nothing is playing"
    m.now = Track("One", "Artist", "", RED, 10.0, 200.0)
    first = _frame(m)
    assert _same(first, render_track(m.now)) and m.status == "Artist — One"          # no bar: it would be a write a second
    m.now = Track("One", "Artist", "", RED, 11.0, 200.0)
    assert _frame(m) is None
    m.now = Track("Two", "Artist", "", BLUE, 0.0, 180.0)
    assert _frame(m) is None                                                           # skipping through a playlist
    clock_[0] += NowPlaying.SETTLE + 0.1
    assert _same(_frame(m), render_track(m.now)) and m.tick is None


def test_drawn_pictures_follow_the_player_every_second(monkeypatch):
    clock_ = [1000.0]
    monkeypatch.setattr(np_.time, "monotonic", lambda: clock_[0])
    m = Player()
    m.direct = True
    m.now = Track("One", "Artist", "", RED, 10.2, 200.0)
    assert _same(_frame(m), render_track(m.now, progress=(10, 200, True)))
    m.now = Track("One", "Artist", "", RED, 10.7, 200.0)
    assert _frame(m) is None                                                           # the same second
    m.now = Track("One", "Artist", "", RED, 11.1, 200.0)
    assert _same(_frame(m), render_track(m.now, progress=(11, 200, True)))
    m.now = Track("One", "Artist", "", RED, 11.1, 200.0, playing=False)
    assert _same(_frame(m), render_track(m.now, progress=(11, 200, False)))            # paused, shown as such
    assert _frame(m) is None and m.tick is None                                        # and nothing more to draw


def test_a_new_track_is_on_at_once_and_fades_in(monkeypatch):
    clock_ = [1000.0]
    monkeypatch.setattr(np_.time, "monotonic", lambda: clock_[0])
    m = Player()
    m.direct = True
    m.now = Track("One", "Artist", "", RED, 10.0, 200.0)
    one = _frame(m)
    m.now = Track("Two", "Artist", "", BLUE, 0.0, 180.0)
    two = render_track(m.now, progress=(0, 180, True))
    start = _frame(m)                                                                  # no settling
    assert _same(start, one) and m.tick == np_.FADE_TICK                               # the fade begins at the old picture
    clock_[0] += np_.FADE / 2
    middle = _frame(m)
    assert not _same(middle, one) and not _same(middle, two)
    clock_[0] += np_.FADE
    assert _same(_frame(m), two) and m.tick is None
    assert _frame(m) is None


def test_a_player_without_a_timeline_gets_no_bar_and_cover_only_never_does(monkeypatch):
    m = Player()
    m.direct = True
    m.now = Track("One", "Artist", "", RED)
    assert _same(_frame(m), render_track(m.now)) and _frame(m) is None
    art = Player({"layout": "art"})
    art.direct = True
    art.now = Track("One", "Artist", "", RED, 10.0, 200.0)
    assert _same(_frame(art), render_track(art.now, "art"))
    art.now = Track("Another of the album", "Artist", "", RED, 55.0, 200.0)
    assert _frame(art) is None                                                         # the cover alone: nothing changed


def test_auto_passes_the_flag_on_and_runs_as_fast_as_the_mode_on_screen():
    auto = Auto({"order": "now_playing", "fallback": "countdown"}, lambda mode_id: {})
    player = auto.children["now_playing"] = Player()
    auto.direct = True
    player.now = Track("One", "Artist", "", RED, 10.0, 200.0)
    assert _frame(auto) is not None and player.direct and auto.tick is None
    player.now = Track("Two", "Artist", "", BLUE, 0.0, 180.0)
    assert _frame(auto) is not None and auto.tick == np_.FADE_TICK
