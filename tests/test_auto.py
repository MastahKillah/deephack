import asyncio
from datetime import datetime, timedelta, timezone

from PIL import Image

from deephack import device, engine
from deephack.config import Config
from deephack.modes import auto, nba
from deephack.modes.base import Animation, Mode, parse_order


class Fake(Mode):
    """A mode whose activity and frames the test controls."""

    def __init__(self, mode_id, colour):
        super().__init__()
        self.id, self.title, self.colour = mode_id, mode_id.title(), colour
        self.pending = Image.new("RGB", (480, 640), colour)

    async def frame(self):
        img, self.pending = self.pending, None
        return img


def make(order="a,b,-c", fallback="weather"):
    a = auto.Auto({"order": "", "fallback": "weather"})
    a.order = [i for i, on in parse_order(order, ["a", "b", "c"]) if on]
    a.fallback = fallback
    a.children = {"a": Fake("a", "red"), "b": Fake("b", "green"), "c": Fake("c", "blue"), fallback: Fake(fallback, "gray")}
    return a


def step(a, clock, monkeypatch, seconds=1.0):
    clock[0] += seconds
    monkeypatch.setattr(auto.time, "monotonic", lambda: clock[0])
    return asyncio.run(a.frame())


def colour(img):
    return img.getpixel((0, 0)) if img is not None else None


def test_parse_order():
    ids = ["x", "y", "z"]
    assert parse_order("y,-x", ids) == [("y", True), ("x", False), ("z", False)]
    assert parse_order("bogus, z ,z,-y", ids) == [("z", True), ("y", False), ("x", False)]
    assert parse_order("", ids) == [("x", False), ("y", False), ("z", False)]


def test_fallback_until_a_mode_has_something(monkeypatch):
    a, clock = make(), [0.0]
    assert colour(step(a, clock, monkeypatch)) == (128, 128, 128) and a.chosen == "weather"
    assert step(a, clock, monkeypatch) is None                       # nothing new: no write


def test_priority_order_and_linger(monkeypatch):
    a, clock = make(), [0.0]
    step(a, clock, monkeypatch)
    a.children["b"].active = True
    assert colour(step(a, clock, monkeypatch)) == (0, 128, 0)       # b has something
    a.children["a"].active = True
    assert colour(step(a, clock, monkeypatch)) == (255, 0, 0)       # a ranks higher
    a.children["a"].active = False
    assert step(a, clock, monkeypatch, auto.LINGER - 5) is None      # a lingers on
    assert colour(step(a, clock, monkeypatch, 10)) == (0, 128, 0)   # then b, still active, takes over
    a.children["b"].active = False
    step(a, clock, monkeypatch, auto.LINGER + 1)
    assert a.chosen == "weather"


def test_switched_off_modes_and_frameless_modes_never_win(monkeypatch):
    a, clock = make(), [0.0]
    a.children["c"].active = True                                    # "-c": not in the rotation
    a.children["a"].active = True
    a.children["a"].pending = None                                   # active, but nothing drawn yet
    step(a, clock, monkeypatch)
    assert a.chosen == "weather"


def test_new_frames_of_the_chosen_mode_pass_through(monkeypatch):
    a, clock = make(), [0.0]
    a.children["a"].active = True
    step(a, clock, monkeypatch)
    a.children["a"].pending = Image.new("RGB", (480, 640), "yellow")
    a.children["b"].pending = Image.new("RGB", (480, 640), "white")  # b is not showing: ignored
    assert colour(step(a, clock, monkeypatch)) == (255, 255, 0)
    assert step(a, clock, monkeypatch) is None


def test_auto_builds_children_with_their_own_settings():
    seen = []
    a = auto.Auto({"order": "nba,-game_cover", "fallback": "ticker"}, lambda mode_id: seen.append(mode_id) or {})
    assert list(a.children) == ["nba", "ticker"] and seen == ["nba", "ticker"]


def test_nba_claims_around_the_picked_teams_games():
    now = datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc)

    def game(hours, state):
        g = nba.parse_event({"date": (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%MZ"), "status": {"type": {"state": state}},
                             "competitions": [{"competitors": [{"homeAway": "home", "team": {"abbreviation": "GS"}},
                                                               {"homeAway": "away", "team": {"abbreviation": "MEM"}}]}]})
        return g

    m = nba.Nba({"team": "GS"})
    assert not m.claiming(now)                                       # nothing fetched yet
    m._games = ([game(-30, "post")], [game(5, "pre")], None)
    assert not m.claiming(now)
    m._games = ([game(-30, "post")], [game(0.5, "pre")], None)
    assert m.claiming(now)                                           # tip-off within the hour
    m._games = ([game(-3, "post")], [game(40, "pre")], None)
    assert m.claiming(now)                                           # final not long ago
    m._games = ([game(-4, "post")], [game(40, "pre")], None)
    assert not m.claiming(now)
    assert not nba.Nba({"team": "all"}).claiming(now)


# ------------------------------------------------------------------ engine: no upload for an image already stored
class FakeDevice:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args: self.calls.append(name)


def test_engine_skips_uploads_of_stored_images(tmp_path, monkeypatch):
    dev = FakeDevice()
    monkeypatch.setattr(device, "Mystique", lambda: dev)
    config = Config(tmp_path / "config.json")
    config.set("slot_index", 3)
    config.set("gif_slot_index", 1)
    eng = engine.Engine(config)
    clock = [1000.0]
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])

    def show(frame, wait=engine.MIN_UPLOAD_INTERVAL + 1):
        clock[0] += wait
        eng._frame, eng._frame_seq = frame, eng._frame_seq + 1
        dev.calls.clear()
        eng._device_step(True)
        return [c for c in dev.calls if c in ("upload_jpeg", "upload_gif", "show_slot")]

    still = Image.new("RGB", (480, 640), "red")
    pet = Animation([Image.new("RGB", (480, 640), c) for c in ("blue", "green")], 1000)
    assert show(still) == ["upload_jpeg", "show_slot"]
    assert show(pet) == ["upload_gif", "show_slot"]
    assert show(Image.new("RGB", (480, 640), "red"), wait=0) == ["show_slot"]     # back to the same still: no write, no wait
    assert show(Animation([Image.new("RGB", (480, 640), c) for c in ("blue", "green")], 1000), wait=0) == ["show_slot"]
    assert show(Image.new("RGB", (480, 640), "white"), wait=0) == []              # new content still waits for the rate limit
    assert engine.digest(still) != engine.digest(pet)
