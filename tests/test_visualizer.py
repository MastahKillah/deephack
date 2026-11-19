import asyncio
import math

from deephack import audio, device, engine
from deephack.config import Config, book_id
from deephack.modes import visualizer
from deephack.modes.base import Flipbook


# ------------------------------------------------------------------ sound
def _tone(hz, seconds=0.1, rate=44100, volume=0.5):
    return [volume * math.sin(2 * math.pi * hz * i / rate) for i in range(int(rate * seconds))]


def test_bands_tell_bass_from_the_rest():
    bands = audio.Bands()
    bass, rest = bands.feed(_tone(60), 44100, 0.1)
    assert bass == 1.0 and rest == 1.0                    # the first sound sets what "loud" means
    b = audio.Bands()
    b.feed([x + y for x, y in zip(_tone(60), _tone(3000))], 44100, 0.1)          # both, as the reference
    b.feed(_tone(60), 44100, 0.1)
    low = b.feed(_tone(60), 44100, 0.1)
    assert low[0] > 0.9 and low[1] < 0.4                  # a bass note: bass high, the rest falling away
    b = audio.Bands()
    b.feed([x + y for x, y in zip(_tone(60), _tone(3000))], 44100, 0.1)
    b.feed(_tone(3000), 44100, 0.1)
    high = b.feed(_tone(3000), 44100, 0.1)
    assert high[1] > 0.9 and high[0] < 0.4


def test_envelopes_fall_quickly_and_silence_is_zero():
    b = audio.Bands()
    b.feed(_tone(60), 44100, 0.1)
    after = b.feed([0.0] * 4410, 44100, 0.12)
    assert 0.4 < after[0] < 0.6                           # halved after FALL_SECONDS
    for _ in range(40):
        after = b.feed([], 44100, 0.1)
    assert after == (0.0, 0.0)
    assert audio.Bands().feed([], 44100, 0.1) == (0.0, 0.0)


def test_quiet_music_still_fills_the_range():
    b = audio.Bands()
    levels = [b.feed(_tone(60, volume=0.01 * (1 + i % 4)), 44100, 0.1)[0] for i in range(40)]
    assert max(levels[8:]) == 1.0 and min(levels[8:]) < 0.6


class FakeCapture:
    rate = 44100

    def read(self):
        return _tone(60, 0.02)

    def close(self):
        pass


def test_listener_runs_while_asked(monkeypatch):
    import time
    monkeypatch.setattr(audio, "IDLE_SECONDS", 0.3)
    listener = audio.Listener(capture=FakeCapture)
    listener.levels()
    deadline = time.monotonic() + 3
    while listener.levels() == (0.0, 0.0) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert listener.levels()[0] > 0.5 and listener.available
    thread = listener._thread
    thread.join(3)
    assert not thread.is_alive()


# ------------------------------------------------------------------ the mode
def test_frames_and_index():
    frames = visualizer.frames("aurora")
    assert len(frames) == visualizer.LEVELS == 8
    assert all(f.size == (device.WIDTH, device.HEIGHT) for f in frames) and len({f.tobytes() for f in frames}) == 8
    assert visualizer.frame_index(0, 0) == 0 and visualizer.frame_index(1, 1) == 7
    assert visualizer.frame_index(1, 0) > visualizer.frame_index(0, 1) > 0        # the bass counts most
    for palette in visualizer.PALETTES:
        assert visualizer.render_frame(palette, 3).size == (device.WIDTH, device.HEIGHT)


class FakeListener:
    available = True
    now = (0.0, 0.0)

    def levels(self):
        return self.now


def test_mode_returns_a_book_only_when_the_frame_changes():
    listener = FakeListener()
    m = visualizer.Visualizer({"palette": "fire"}, listener=listener)
    first = asyncio.run(m.frame())
    assert isinstance(first, Flipbook) and first.index == 0 and first.key == "visualizer-fire-2" and m.status == "Nothing is playing"
    assert asyncio.run(m.frame()) is None
    listener.now = (1.0, 0.5)
    second = asyncio.run(m.frame())
    assert second.index == 6 and second.key == first.key and m.status == "Following the music"
    assert m.tick == 0.1


# ------------------------------------------------------------------ engine: a flipbook on the cooler
def _book_engine(tmp_path, monkeypatch, official=1):
    monkeypatch.setattr(engine, "_stored_count", lambda kind: official)   # the official app's stills
    config = Config(tmp_path / "config.json")
    eng = engine.Engine(config)
    eng._make_mode()
    states = []
    eng.stateChanged.connect(states.append)

    def step(index, key="book-a", frames=None):
        eng._set_frame(Flipbook(key, frames or list(visualizer.frames("aurora")), index))
        eng._device_step(True)
        eng._emit_state(True)

    return eng, config, states, step


def test_engine_stores_a_flipbook_once_and_then_only_flips(tmp_path, monkeypatch, cooler):
    eng, config, states, step = _book_engine(tmp_path, monkeypatch)
    for _ in range(7):                                                  # one frame stored per step
        step(2)
    assert cooler.flash_writes == 8 and len(cooler.stills) == 9         # our own still, then the 7 of 8 frames that fit
    assert [s[0] for s in cooler.stills[2:]] == [book_id(i) for i in range(7)]
    assert config.get("book") == {"key": "book-a", "first": 2, "count": 7}
    assert [s["storing"] for s in states if s["storing"]] == [(i, 7) for i in range(1, 7)]
    step(2)
    assert cooler.screen == ("jpg", 4) and cooler.flash_writes == 8
    for index, slot in ((0, 2), (7, 8), (1, 3), (1, 3), (5, 6)):        # eight steps spread over seven pictures
        step(index)
        assert cooler.screen == ("jpg", slot)
    assert cooler.flash_writes == 8 and states[-1]["live"] and states[-1]["mode_live"] == "flipbook"

    again = engine.Engine(config)                                       # after a restart: nothing is stored again
    frames = list(visualizer.frames("aurora"))
    again._set_frame(Flipbook("book-a", frames, 3))
    again._device_step(True)
    again._device_step(True)
    assert cooler.flash_writes == 8 and cooler.screen == ("jpg", 5)

    for _ in range(8):                                                  # other frames: replaced in place
        again._set_frame(Flipbook("book-b", frames[::-1], 0))
        again._device_step(True)
    assert cooler.flash_writes == 15 and len(cooler.stills) == 9 and cooler.screen == ("jpg", 2)
    assert config.get("book")["key"] == "book-b"


def test_a_fuller_cooler_gets_fewer_steps(tmp_path, monkeypatch, cooler):
    cooler.stills.extend([f"app-still-{i}", b""] for i in range(1, 4))  # four pictures of the user's own
    eng, config, states, step = _book_engine(tmp_path, monkeypatch, official=4)
    for _ in range(5):                                                  # our own still, then the 4 that fit
        step(0)
    assert config.get("book") == {"key": "book-a", "first": 5, "count": 4} and len(cooler.stills) == 9
    brightness = [sum(cooler.still(i).resize((1, 1)).getpixel((0, 0))) for i in range(5, 9)]
    assert brightness == sorted(brightness) and len(set(brightness)) == 4       # from the first to the last, evenly
    for index, slot in ((0, 5), (7, 8), (2, 6), (5, 7)):
        step(index)
        assert cooler.screen == ("jpg", slot)


def test_no_room_for_a_flipbook(tmp_path, monkeypatch, cooler):
    cooler.stills.extend([f"app-still-{i}", b""] for i in range(1, 8))  # eight of the user's own
    cooler.stills.append(["ours", b""])
    eng, config, states, step = _book_engine(tmp_path, monkeypatch, official=8)
    config.set("slot_index", 8)
    step(3)
    assert cooler.flash_writes == 0 and states[-1]["no_room"] and config.get("book") is None


def test_the_model_knows_the_still_limit(cooler):
    from PIL import Image
    with device.Mystique() as m:
        for i in range(device.MAX_STILLS):
            m.upload_jpeg(book_id(i), device.encode_jpeg(Image.new("RGB", (480, 640), "red")))
    assert len(cooler.stills) == device.MAX_STILLS and any("one still too many" in v for v in cooler.violations)
    cooler.violations.clear()


def test_engine_loop_runs_faster_for_a_mode_that_asks(tmp_path):
    eng = engine.Engine(Config(tmp_path / "config.json"), use_device=False)
    eng._mode = visualizer.Visualizer({}, listener=FakeListener())
    assert min(m.tick or engine.TICK for m in (eng._mode, eng._preview) if m is not None) == 0.1
