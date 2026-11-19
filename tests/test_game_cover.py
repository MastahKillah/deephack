import asyncio

from PIL import Image

from deephack.modes import game_cover as gc


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
