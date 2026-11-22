"""Mirror: a window or a part of the screen on the cooler. The screen and its windows are stood in for;
one test at the end takes a real picture of the real screen, where there is one."""
import asyncio
import sys

import pytest
from PIL import Image

from deephack import capture, engine
from deephack.config import Config
from deephack.modes import BY_ID, mirror
from deephack.modes.mirror import Mirror, describe, parse, part_value, shape, window_value

RED, BLUE, GREEN = (220, 30, 30), (30, 30, 220), (30, 200, 60)
CHROME = capture.Window(7, "chrome.exe", "Chrome_WidgetWin_1", "A film - Google Chrome")


def halves(width=800, height=600, left=RED, right=BLUE) -> Image.Image:
    picture = Image.new("RGB", (width, height), left)
    picture.paste(right, (width // 2, 0, width, height))
    return picture


class Screen:
    """Stands in for the grabber: hands out the picture it was given last."""

    def __init__(self):
        self.now = halves()
        self.asked: list[tuple] = []
        self.fails: str | None = None
        self.closed = False

    def part(self, left, top, width, height):
        self.asked.append(("part", left, top, width, height))
        if self.fails:
            raise OSError(self.fails)
        return self.now.copy()

    def window(self, hwnd):
        self.asked.append(("window", hwnd))
        return self.now.copy()

    def close(self):
        self.closed = True


class Desk:
    """Stands in for the windows of the PC."""

    def __init__(self, *windows):
        self.open = list(windows)
        self.down: set[int] = set()              # the minimised ones
        self.sought = 0

    def alive(self, hwnd):
        return any(w.hwnd == hwnd for w in self.open)

    def minimised(self, hwnd):
        return hwnd in self.down

    def find(self, program, kind, title):
        self.sought += 1
        return capture.find(program, kind, title, self.open)


def make(values, desk=None, direct=True, clock=None):
    screens = []

    def grabber():
        screens.append(Screen())
        return screens[-1]

    mode = Mirror(values, grabber=grabber, windows=desk or Desk(), clock=clock or (lambda: 0.0))
    mode.direct = direct
    return mode, screens[0]


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(mirror, "LAG", 0.0)


# ---------------------------------------------------------------- the setting
def test_what_to_show_is_written_as_a_line_and_read_back():
    assert parse(part_value(10, -20, 960, 1280)) == ("part", 10, -20, 960, 1280)         # a screen left of the main one
    assert parse(window_value(CHROME)) == ("window", "chrome.exe", "Chrome_WidgetWin_1", "A film - Google Chrome")
    for junk in ("", "part:1,2,3", "part:a,b,c,d", "part:0,0,8,600", "window:only\ttwo", "camera:0", None):
        assert parse(junk) is None
    assert describe("") == "Nothing chosen"
    assert describe(part_value(0, 0, 960, 1280)) == "Part of the screen · 960 × 1280"
    assert describe(window_value(CHROME)) == "A film - Google Chrome"
    assert describe(window_value(capture.Window(1, "game.exe", "Game", ""))) == "game.exe"


def test_a_window_is_found_again_by_its_program_and_kind():
    other_tab = capture.Window(9, "chrome.exe", "Chrome_WidgetWin_1", "Mail - Google Chrome")
    devtools = capture.Window(8, "chrome.exe", "Chrome_Tool", "A film - Google Chrome")
    notepad = capture.Window(3, "notepad.exe", "Notepad", "A film - Google Chrome")
    wanted = ("chrome.exe", "Chrome_WidgetWin_1", "A film - Google Chrome")
    assert capture.find(*wanted, [notepad, devtools, other_tab, CHROME]) is CHROME        # the very title, if it is there
    assert capture.find(*wanted, [notepad, devtools, other_tab]) is other_tab             # else the program's window
    assert capture.find("CHROME.EXE", *wanted[1:], [CHROME]) is CHROME
    assert capture.find(*wanted, [notepad, devtools]) is None
    assert CHROME.label == "A film - Google Chrome"                                       # the program is in the title
    assert capture.Window(1, "vlc.exe", "Qt", "holiday.mp4").label == "holiday.mp4 · vlc"


# ---------------------------------------------------------------- the picture
def test_the_picture_fills_the_screen_or_is_shown_whole():
    wide = halves(800, 600)
    full = shape(wide, "fill")
    assert full.size == (480, 640)
    assert full.getpixel((10, 320))[0] > 150 and full.getpixel((470, 320))[2] > 150       # the middle: both halves, no bars
    assert full.getpixel((240, 2)) != (0, 0, 0)
    whole = shape(wide, "fit")
    assert whole.size == (480, 640)
    assert whole.getpixel((240, 10)) == (0, 0, 0) == whole.getpixel((240, 630))           # bars above and below
    assert whole.getpixel((20, 320))[0] > 150 and whole.getpixel((460, 320))[2] > 150
    small = shape(Image.new("RGB", (48, 64), GREEN), "fit")                               # a small one is made larger
    assert small.getpixel((5, 5)) == GREEN == small.getpixel((470, 630))


def test_the_picture_can_lie_on_its_side():
    wide = halves(1280, 960)                                 # 4 to 3: on its side it fills the screen whole
    left = shape(wide, "fit", "left")
    assert left.size == (480, 640)
    assert left.getpixel((240, 600))[0] > 150 and left.getpixel((240, 40))[2] > 150       # its left edge is at the bottom
    right = shape(wide, "fill", "right")
    assert right.getpixel((240, 40))[0] > 150 and right.getpixel((240, 600))[2] > 150     # ... or at the top
    assert left.getpixel((5, 320)) != (0, 0, 0)


# ---------------------------------------------------------------- the mode
def test_nothing_is_chosen_at_first():
    mode, screen = make({})
    assert asyncio.run(mode.frame()) is None
    assert mode.status == "Nothing chosen" and mode.placeholder[0] == "Mirror"
    assert not screen.asked and mode.tick == mirror.TICK
    assert Mirror.needs_direct and BY_ID["mirror"] is Mirror


def test_only_a_picture_that_differs_is_sent():
    async def main():
        mode, screen = make({"source": part_value(100, 50, 800, 600)})
        first = await mode.frame()
        assert first.size == (480, 640) and screen.asked[0] == ("part", 100, 50, 800, 600)
        assert mode.status == "Part of the screen · 800 × 600"
        assert [await mode.frame() for _ in range(5)] == [None] * 5
        screen.now = halves(left=GREEN)
        again = None
        for _ in range(3):                                   # the one taken ahead may still be of the old screen
            again = again or await mode.frame()
        assert again is not None and again.getpixel((10, 320))[1] > 150
        await mode.close()
        assert screen.closed

    asyncio.run(main())


def test_a_screen_that_stands_still_is_looked_at_less_often():
    async def main():
        mode, screen = make({"source": part_value(0, 0, 800, 600)})
        await mode.frame()
        for _ in range(mirror.STILL_AFTER + 4):
            await mode.frame()
        await mode._drop()
        before = len(screen.asked)
        for _ in range(4 * mirror.STILL_EVERY):
            assert await mode.frame() is None
        assert len(screen.asked) - before == 4               # every fourth time only
        screen.now = halves(left=GREEN)
        got = [await mode.frame() for _ in range(mirror.STILL_EVERY)]
        assert sum(picture is not None for picture in got) == 1          # ... and at once when something moves
        before = len(screen.asked)
        for _ in range(8):
            await mode.frame()
        assert len(screen.asked) - before >= 7               # and every time again from then on
        await mode.close()

    asyncio.run(main())


def test_the_next_picture_is_taken_while_the_last_one_is_drawn():
    async def main():
        mode, screen = make({"source": part_value(0, 0, 800, 600)})
        assert await mode.frame() is not None
        assert mode._ahead is not None                       # under way already, before the engine asks again
        await asyncio.wait_for(asyncio.shield(mode._ahead), 2)
        assert len(screen.asked) == 2
        assert await mode.frame() is None                    # that one was the same picture ...
        await asyncio.wait_for(asyncio.shield(mode._ahead), 2)
        assert len(screen.asked) == 3                        # ... and the next was begun at once
        await mode.close()
        assert mode._ahead is None

    asyncio.run(main())


def test_another_way_of_showing_it_draws_it_anew():
    async def main():
        mode, screen = make({"source": part_value(0, 0, 800, 600)})
        first = await mode.frame()
        assert first.getpixel((240, 5)) != (0, 0, 0)
        mode.values["fit"] = "fit"                           # as the engine does for a setting that is `live`
        whole = await mode.frame()
        assert whole is not None and whole.getpixel((240, 5)) == (0, 0, 0)
        mode.values["turn"] = "left"
        assert await mode.frame() is not None
        mode.values["source"] = part_value(5, 5, 400, 300)
        assert await mode.frame() is not None and screen.asked[-1][:5] == ("part", 5, 5, 400, 300)
        await mode.close()

    asyncio.run(main())


def test_a_window_is_followed_minimised_closed_and_back():
    async def main():
        now = [0.0]
        desk = Desk(CHROME)
        mode, screen = make({"source": window_value(CHROME)}, desk, clock=lambda: now[0])
        assert (await mode.frame()).size == (480, 640)
        assert screen.asked == [("window", 7)] and mode.status == "A film - Google Chrome · 800 × 600"
        await mode._drop()

        desk.down.add(7)
        said = await mode.frame()
        assert said is not None and mode.status == "A film - Google Chrome · minimised"
        assert [await mode.frame() for _ in range(3)] == [None] * 3              # said once
        desk.down.clear()
        assert await mode.frame() is not None and mode.status.endswith("800 × 600")      # the same picture, but shown again
        await mode._drop()

        desk.open.clear()                                    # the program is closed
        assert await mode.frame() is not None and mode.status.endswith("closed")
        sought = desk.sought
        for _ in range(5):
            assert await mode.frame() is None
        assert desk.sought == sought                         # not looked for at every tick
        desk.open.append(capture.Window(31, "chrome.exe", "Chrome_WidgetWin_1", "Another tab - Google Chrome"))
        now[0] += mirror.SEEK
        back = await mode.frame()
        assert back is not None and screen.asked[-1] == ("window", 31)           # started anew: another window, found
        await mode.close()

    asyncio.run(main())


def test_a_cooler_without_direct_drawing_gets_one_picture():
    async def main():
        mode, screen = make({"source": part_value(0, 0, 800, 600)}, direct=False)
        assert await mode.frame() is not None
        assert mode.status == "One picture: mirroring needs direct drawing" and mode.tick is None
        screen.now = halves(left=GREEN)
        assert [await mode.frame() for _ in range(3)] == [None] * 3 and len(screen.asked) == 1
        mode.values["source"] = part_value(0, 0, 640, 480)
        assert await mode.frame() is not None                # another choice: another picture
        mode.direct = True                                   # the firmware was installed meanwhile
        assert await mode.frame() is None                    # the picture it has is the one on the screen
        assert mode.tick == mirror.TICK and mode.status == "Part of the screen · 640 × 480"
        screen.now = halves(left=RED, right=GREEN)
        assert any([await mode.frame() for _ in range(3)])   # and from here on it follows
        await mode.close()

    asyncio.run(main())


def test_a_screen_that_cannot_be_copied_is_said_so():
    async def main():
        mode, screen = make({"source": part_value(0, 0, 800, 600)})
        screen.fails = "the screen could not be copied"
        assert await mode.frame() is not None
        assert mode.status == "Part of the screen · 800 × 600 · the screen could not be copied"
        assert await mode.frame() is None
        screen.fails = None
        assert await mode.frame() is not None
        await mode.close()

    asyncio.run(main())


def test_the_engine_runs_it_and_closes_it(tmp_path):
    config = Config(tmp_path / "config.json")
    config.set("mode", "mirror")
    eng = engine.Engine(config, use_device=False)
    eng._make_mode()
    assert isinstance(eng._mode, Mirror)
    setting = {s.key: s for s in Mirror.settings}
    assert setting["source"].kind == "capture" and all(s.live for s in Mirror.settings)
    eng.mode_value("mirror", "source", part_value(0, 0, 64, 48))
    eng._commands.get_nowait()()
    assert eng._mode.values["source"] == part_value(0, 0, 64, 48)


# ---------------------------------------------------------------- the real screen
@pytest.mark.skipif(sys.platform != "win32", reason="Windows' screen")
def test_a_real_picture_of_the_real_screen():
    grabber = capture.Grabber()
    try:
        picture = grabber.part(0, 0, 64, 48)
    except OSError:
        pytest.skip("no screen to copy from here")
    assert picture.size == (64, 48) and picture.mode == "RGB" and len(grabber.raw) == 64 * 48 * 4
    assert grabber.part(0, 0, 32, 16).size == (32, 16)       # another size: other memory
    with pytest.raises(OSError):
        grabber.part(0, 0, 0, 10)
    assert grabber.window(0) is None                         # no such window
    grabber.close()
    grabber.close()
    left, top, width, height = capture.screen()
    assert width > 0 and height > 0
    assert all(isinstance(w, capture.Window) and w.title for w in capture.windows())
