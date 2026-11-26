import asyncio

from PIL import Image

from deephack.modes import game_cover as gc
from deephack.modes import system
from deephack.modes.base import Part
from deephack.modes.system import Reading


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def setup(monkeypatch, app, art=Image.new("RGB", (600, 900), "red")):
    clock = Clock()
    state = {"app": app, "art": art, "loads": 0}
    monkeypatch.setattr(gc.time, "monotonic", clock)
    monkeypatch.setattr(gc, "running_app", lambda: state["app"])

    def load(self, app_id):
        state["loads"] += 1
        return f"Game {app_id}", state["art"]

    monkeypatch.setattr(gc.GameCover, "_load", load)
    return clock, state


def step(mode, clock, seconds=gc.POLL_SECONDS):
    clock.t += seconds
    return asyncio.run(mode.frame())


def test_draws_a_game_once_it_has_settled(monkeypatch):
    clock, state = setup(monkeypatch, 730)
    m = gc.GameCover()
    assert step(m, clock) is None                       # just started: could be a launcher flashing through
    assert step(m, clock, gc.SETTLE_SECONDS) is not None and m.status == "Game 730"
    assert step(m, clock) is None and state["loads"] == 1
    state["app"] = 0
    assert step(m, clock) is None                       # game closed: the cover stays up, no write


def test_without_a_cover_draws_the_name_once_and_retries_later(monkeypatch):
    clock, state = setup(monkeypatch, 42, art=None)
    m = gc.GameCover()
    step(m, clock)
    assert step(m, clock, gc.SETTLE_SECONDS) is not None and "no cover" in m.status
    assert step(m, clock) is None and state["loads"] == 1
    assert step(m, clock, gc.RETRY_SECONDS) is None and state["loads"] == 2      # still nothing: no new write
    state["art"] = Image.new("RGB", (600, 900), "blue")
    assert step(m, clock, gc.RETRY_SECONDS) is not None and state["loads"] == 3


def test_no_steam(monkeypatch):
    clock, _ = setup(monkeypatch, 0)
    m = gc.GameCover()
    assert step(m, clock) is None and m.status == "No game running"


def test_render_layouts():
    art = Image.new("RGB", (600, 900), "green")
    for layout in ("full", "art"):
        assert gc.render_cover("Game", art, layout).size == (480, 640)
        assert gc.render_cover("A game without a cover and with a long name", None, layout).size == (480, 640)
    assert gc.render_cover("Game", art, "art").getpixel((240, 320)) == (0, 128, 0)


# ---- readings over the cover while the game runs (issue #37)
class Feed:
    """The System mode's once-a-second readings, as a test hands them out."""

    def __init__(self):
        self.got, self.asked = None, 0

    def put(self, t, **values):
        self.got = (t, Reading(t, **values))

    def latest(self):
        self.asked += 1
        return self.got


def drawn(monkeypatch, app=730, **values):
    """A mode on a cooler that draws, with its game settled and its cover on screen."""
    clock, state = setup(monkeypatch, app)
    feed = Feed()
    m = gc.GameCover(values, feed=feed)
    m.direct = True
    step(m, clock)
    return m, clock, state, feed


def test_the_strip_is_drawn_once_per_reading_while_the_game_runs(monkeypatch):
    m, clock, state, feed = drawn(monkeypatch)
    page = step(m, clock, gc.SETTLE_SECONDS)                 # no reading yet: the cover, with room for them
    assert isinstance(page, Image.Image) and page.tobytes() == gc.render_cover("Game 730", state["art"], "full", True).tobytes()
    feed.put(1.0, cpu_temp=64, cpu_load=36, gpu_temp=71, gpu_load=98)
    part = step(m, clock, 0.5)
    assert isinstance(part, Part) and part.page is page and part.at == (0, gc.STRIP_TOP) and part.fits
    assert part.picture.size == (480, gc.STRIP_H) and part.picture.tobytes() != page.crop((0, gc.STRIP_TOP, 480, 640)).tobytes()
    assert part.whole.crop((0, 0, 480, gc.STRIP_TOP)).tobytes() == page.crop((0, 0, 480, gc.STRIP_TOP)).tobytes()
    assert m.status == "Game 730 · CPU 64° · GPU 71°"
    assert step(m, clock, 0.5) is None                       # the same reading: nothing new
    feed.put(2.0, cpu_temp=65, cpu_load=40, gpu_temp=71, gpu_load=98)
    again = step(m, clock, 0.5)
    assert again.page is page and again.picture.tobytes() != part.picture.tobytes() and state["loads"] == 1


def test_the_strip_and_the_lights_end_with_the_game(monkeypatch):
    m, clock, state, feed = drawn(monkeypatch)
    feed.put(1.0, cpu_temp=64, gpu_temp=71)
    assert isinstance(step(m, clock, gc.SETTLE_SECONDS), Part) and m.lights is not None
    state["app"] = 0
    alone = step(m, clock)
    assert alone.tobytes() == gc.render_cover("Game 730", state["art"], "full").tobytes()      # as it always was
    assert m.lights is None and m.status == "Game 730"
    asked = feed.asked
    feed.put(2.0, cpu_temp=70, gpu_temp=75)
    assert step(m, clock) is None and feed.asked == asked    # no game: nobody reads the sensors for this
    state["app"] = 730                                       # started again: its cover is up already, so at once
    assert isinstance(step(m, clock), Part) and state["loads"] == 1 and m.lights is not None


def test_the_cover_alone_when_asked_and_the_lights_are_another_matter(monkeypatch):
    m, clock, state, feed = drawn(monkeypatch, readings="off")
    feed.put(1.0, cpu_temp=64, gpu_temp=71)
    alone = step(m, clock, gc.SETTLE_SECONDS)
    assert alone.tobytes() == gc.render_cover("Game 730", state["art"], "full").tobytes()
    assert m.lights == gc.temperature_lights(feed.got[1], True)
    feed.put(2.0, cpu_temp=74, gpu_temp=71)                  # the lights move on, a part of the way each reading
    assert step(m, clock) is None and m.lights == gc.temperature_lights(Reading(0, cpu_temp=67, gpu_temp=71))
    feed.put(3.0, cpu_temp=74, gpu_temp=None)
    assert step(m, clock) is None and m.lights == gc.temperature_lights(Reading(0, cpu_temp=69.1))

    m, clock, state, feed = drawn(monkeypatch, lights="cover")
    feed.put(1.0, cpu_temp=64, gpu_temp=71)
    assert isinstance(step(m, clock, gc.SETTLE_SECONDS), Part) and m.lights is None

    m, clock, state, feed = drawn(monkeypatch, readings="off", lights="cover")
    assert step(m, clock, gc.SETTLE_SECONDS) is not None and step(m, clock) is None and feed.asked == 0


def test_no_strip_on_a_cooler_that_stores_its_pictures(monkeypatch):
    clock, state = setup(monkeypatch, 730)
    feed = Feed()
    feed.put(1.0, cpu_temp=64, gpu_temp=71)
    m = gc.GameCover(feed=feed)
    step(m, clock)
    assert step(m, clock, gc.SETTLE_SECONDS).tobytes() == gc.render_cover("Game 730", state["art"], "full").tobytes()
    for _ in range(5):
        feed.put(feed.got[0] + 1, cpu_temp=70, gpu_temp=75)
        assert step(m, clock) is None                        # a picture is a write to the flash there
    assert feed.asked == 0 and m.lights is None and m.status == "Game 730"
    m.direct = True                                          # the cooler got the firmware meanwhile
    assert isinstance(step(m, clock), Part) and state["loads"] == 1


def test_another_game_gets_its_own_page(monkeypatch):
    m, clock, state, feed = drawn(monkeypatch)
    feed.put(1.0, cpu_temp=64, gpu_temp=71)
    first = step(m, clock, gc.SETTLE_SECONDS)
    state["app"] = 440
    assert step(m, clock).tobytes() == gc.render_cover("Game 730", state["art"], "full").tobytes()   # 730 is over
    second = step(m, clock, gc.SETTLE_SECONDS)
    assert isinstance(second, Part) and second.page is not first.page and m.status.startswith("Game 440 · CPU")


def test_what_the_strip_shows():
    full = gc.strip_cells(Reading(0, cpu_temp=64.4, cpu_load=36.2, gpu_temp=88, gpu_load=98, mem_load=66))
    assert [(c[0], c[2], c[4], c[5]) for c in full] == [("CPU", "64°", "36%", 36.2), ("GPU", "88°", "98%", 98)]
    assert full[0][3] == system.TEXT and full[1][3] == system.RED        # as the System page has them: hot from 88
    bare = gc.strip_cells(Reading(0, cpu_load=41, mem_load=66))          # no sensor service: what Windows knows
    assert [(c[0], c[2], c[4]) for c in bare] == [("CPU", "41%", "load"), ("RAM", "66%", "used")]
    assert gc.strip_cells(Reading(0)) == [] and gc.readings_line(Reading(0, cpu_load=41, mem_load=66)) == "CPU 41% · RAM 66%"
    page = gc.render_cover("Game", None, "art", True)
    assert gc.draw_strip(page, Reading(0)).picture.tobytes() == page.crop((0, gc.STRIP_TOP, 480, 640)).tobytes()
    for layout in ("full", "art"):
        with_room, without = gc.render_cover("Game", None, layout, True), gc.render_cover("Game", None, layout)
        assert with_room.size == (480, 640) and with_room.tobytes() != without.tobytes()


def lit(side):
    return sum(1 for colour in side if any(colour))


def test_the_lights_are_two_temperatures():
    cool = gc.temperature_lights(Reading(0, cpu_temp=30, gpu_temp=20))
    assert len(cool) == 14 and lit(cool[:7]) == 1 and lit(cool[7:]) == 1
    assert any(cool[0]) and any(cool[13])                    # the lowest light of each side: 0 and 13
    assert cool[0][2] > cool[0][0] and cool[0][2] > cool[0][1]           # blue
    warm = gc.temperature_lights(Reading(0, cpu_temp=62.5, gpu_temp=59))     # half way to hot, each
    assert lit(warm[:7]) == 4 and lit(warm[7:]) == 4 and warm[0] == warm[1] == warm[2] == warm[13]
    assert warm[0][1] > warm[0][0] and warm[0][1] > warm[0][2]           # green
    assert max(max(c) for c in warm) == round(255 * gc.LIGHT_POWER)
    near = gc.temperature_lights(Reading(0, cpu_temp=94, gpu_temp=30))
    assert lit(near[:7]) == 7 and near[0][0] > 4 * near[0][1] and lit(near[7:]) == 1       # red, all but full
    hot = gc.temperature_lights(Reading(0, cpu_temp=95, gpu_temp=60), True)
    assert hot[:7] == [(255, 0, 0)] * 7 and lit(hot[7:]) == 4            # the alarm is one side's own
    assert gc.temperature_lights(Reading(0, cpu_temp=95, gpu_temp=60), False)[:7] == [(76, 0, 0)] * 7
    one = gc.temperature_lights(Reading(0, cpu_temp=62.5))               # no GPU reading: both sides the CPU's
    assert one[:7] == one[7:][::-1] and lit(one[:7]) == 4
    assert gc.temperature_lights(Reading(0, cpu_load=40)) is None        # no temperatures: the cover's own edge


def test_the_lights_take_a_part_of_the_way_to_a_new_reading():
    first = Reading(1, cpu_temp=60, gpu_temp=40, cpu_load=10)
    assert gc.settled(None, first) is first
    then = gc.settled(first, Reading(2, cpu_temp=70, gpu_temp=40, cpu_load=90))
    assert (then.cpu_temp, then.gpu_temp, then.cpu_load, then.t) == (63, 40, 90, 2)
    gone = gc.settled(then, Reading(3, cpu_temp=None, gpu_temp=50))
    assert gone.cpu_temp is None and gone.gpu_temp == 43
    assert gc.settled(gone, Reading(4, cpu_temp=80, gpu_temp=50)).cpu_temp == 80         # back: from where it is


def test_a_hot_side_pulses_with_the_readings(monkeypatch):
    m, clock, state, feed = drawn(monkeypatch)
    feed.put(1.0, cpu_temp=97, gpu_temp=60)
    step(m, clock, gc.SETTLE_SECONDS)
    seen = [m.lights[0]]
    for t in (2.0, 3.0, 4.0):
        feed.put(t, cpu_temp=97, gpu_temp=60)
        step(m, clock, 0.5)
        seen.append(m.lights[0])
        step(m, clock, 0.5)                                  # the same reading again: the beat waits for the next
        assert m.lights[0] == seen[-1]
    assert seen in ([(255, 0, 0), (76, 0, 0)] * 2, [(76, 0, 0), (255, 0, 0)] * 2) and m.lights_fade == gc.LIGHT_FADE
