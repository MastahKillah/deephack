import asyncio
import math

import pytest

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
    assert len(frames) == visualizer.LEVELS == 24
    assert all(f.size == (device.WIDTH, device.HEIGHT) for f in frames) and len({f.tobytes() for f in frames}) == 24
    assert visualizer.frame_index(0, 0) == 0 and visualizer.frame_index(1, 1) == 23
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
    assert isinstance(first, Flipbook) and first.index == 0 and first.key == "visualizer-fire-3" and m.status == "Nothing is playing"
    assert asyncio.run(m.frame()) is None
    listener.now = (1.0, 0.5)
    second = asyncio.run(m.frame())
    assert second.index == 20 and second.key == first.key and m.status == "Following the music"
    assert m.tick == visualizer.TICK


# ------------------------------------------------------------------ engine: a flipbook on the cooler
def _eight():
    """A book of eight frames, from the quietest to the loudest."""
    return list(visualizer.frames("aurora"))[::3]


def _book_engine(tmp_path, monkeypatch, official=1):
    monkeypatch.setattr(engine, "_stored_count", lambda kind: official)   # the official app's stills
    monkeypatch.setattr(engine, "FLIP_INTERVAL", 0.0)                     # the tests flip faster than a cooler may
    monkeypatch.setattr(engine, "DRAW_SETTLE", 0.0)
    config = Config(tmp_path / "config.json")
    eng = engine.Engine(config)
    eng._make_mode()
    states = []
    eng.stateChanged.connect(states.append)

    def step(index, key="book-a", frames=None):
        eng._set_frame(Flipbook(key, frames or _eight(), index))
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
    frames = _eight()
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


def test_stored_frames_are_flipped_no_faster_than_the_firmware_shows_them(tmp_path, monkeypatch, cooler):
    eng, config, states, step = _book_engine(tmp_path, monkeypatch)
    for _ in range(8):
        step(0)
    assert cooler.screen == ("jpg", 2)
    monkeypatch.setattr(engine, "FLIP_INTERVAL", 60.0)
    step(7)
    assert cooler.screen == ("jpg", 2)                                  # too soon after the last one
    monkeypatch.setattr(engine, "FLIP_INTERVAL", 0.0)
    step(7)
    assert cooler.screen == ("jpg", 8)


# ------------------------------------------------------------------ engine: frames drawn straight from USB
def _drawn(frame):
    return ("drawn", device.encode_jpeg(frame, quality=engine.DRAW_QUALITY, tuned=True))


def test_the_direct_drawing_firmware_is_drawn_on_and_nothing_is_stored(tmp_path, monkeypatch, cooler):
    cooler.direct = True
    eng, config, states, step = _book_engine(tmp_path, monkeypatch)
    book = list(visualizer.frames("aurora"))
    step(5, frames=book)
    assert cooler.screen == ("jpg", 1) and cooler.flash_writes == 1     # our own still, stored once, is the ground
    step(5, frames=book)
    assert cooler.screen == _drawn(book[5])
    for index in (0, 23, 11):                                           # every one of the 24 steps, not seven of them
        step(index, frames=book)
        assert cooler.screen == _drawn(book[index])
    step(11, frames=book)
    assert cooler.draws == 4 and cooler.flash_writes == 1 and config.get("book") is None
    assert states[-1]["live"] and states[-1]["mode_live"] == "drawn" and not states[-1]["storing"]

    monkeypatch.setattr(engine, "REDRAW", 0.0)                          # an unchanged frame is sent again now and then
    step(11, frames=book)
    assert cooler.draws == 5 and cooler.screen == _drawn(book[11])


def test_a_frame_is_not_drawn_before_the_still_under_it_is_on_screen(tmp_path, monkeypatch, cooler):
    cooler.direct = True
    eng, config, states, step = _book_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "DRAW_SETTLE", 60.0)
    for _ in range(3):
        step(4)
    assert cooler.screen == ("jpg", 1) and cooler.draws == 0


def test_drawn_frames_go_over_a_still_also_when_an_animation_was_on(tmp_path, monkeypatch, cooler):
    cooler.direct = True
    cooler.gifs.append(["app-gif-0", [], 1000])
    with device.Mystique() as m:                                        # what was on before deephack started
        m.set_mode(device.MODE_MOTION)
        m.show_slot(0, device.TYPE_GIF)
    eng, config, states, step = _book_engine(tmp_path, monkeypatch)
    for index in (3, 3, 5, 1):          # after an animation the firmware takes one frame and no more, unless
        step(index)                     # the image screen was left and entered again in between
    assert cooler.screen == _drawn(_eight()[1]) and cooler.shown == ("jpg", 1) and cooler.draws == 3


def test_a_cooler_that_stops_taking_frames_is_started_over(tmp_path, monkeypatch, cooler):
    cooler.direct = True
    eng, config, states, step = _book_engine(tmp_path, monkeypatch)
    step(3)
    step(3)
    cooler.stuck = True
    monkeypatch.setattr(engine, "DRAW_STUCK", -1.0)
    step(5)                             # refused
    step(5)                             # refused for too long
    assert cooler.screen == _drawn(_eight()[3])
    step(5)                             # the image screen is entered afresh
    step(5)
    assert cooler.screen == _drawn(_eight()[5]) and not cooler.stuck


def test_the_stored_picture_comes_back_after_drawn_frames(tmp_path, monkeypatch, cooler):
    """The firmware ignores "show" for the still it has selected: the engine has to leave the image screen
    and come back (the model keeps the drawn frame up otherwise, as the cooler did)."""
    cooler.direct = True
    eng, config, states, step = _book_engine(tmp_path, monkeypatch)
    step(2)
    step(2)
    assert cooler.screen == _drawn(_eight()[2])
    eng._device_step(False)                                             # paused: the user's own picture
    assert cooler.screen == ("jpg", 0)
    modes = [payload[0] for kind, cmd, payload in cooler.log if kind == "cmd" and cmd == device.CMD_MODE]
    assert modes[-2:] == [device.MODE_SITUATIONAL, device.MODE_MOTION]


def test_a_picture_after_drawn_frames_is_drawn_too_without_a_detour(tmp_path, monkeypatch, cooler):
    cooler.direct = True
    eng, config, states, step = _book_engine(tmp_path, monkeypatch)
    step(2)
    step(2)
    before = len(cooler.log)
    eng._set_frame(_eight()[6])
    eng._device_step(True)
    assert cooler.screen == ("drawn", engine.frame_jpeg(_eight()[6])) and cooler.flash_writes == 1
    assert not [e for e in cooler.log[before:] if e[0] == "cmd" and e[1] == device.CMD_MODE]


def test_the_model_knows_what_a_drawn_frame_needs(cooler):
    cooler.direct = True
    jpeg = device.encode_jpeg(_eight()[0])
    with device.Mystique() as m:
        assert m.can_draw()
        m.set_mode(device.MODE_MOTION)
        m.show_slot(0)
        m.draw_jpeg(jpeg)
        assert cooler.screen == ("drawn", jpeg)
        m.show_slot(0)
        assert cooler.screen == ("drawn", jpeg)                         # the slot it has selected: nothing happens
        m.set_mode(device.MODE_SITUATIONAL)
        m.draw_jpeg(jpeg)
        m.set_mode(device.MODE_MOTION)
        m.show_slot(0, device.TYPE_GIF)
        m.show_slot(0)
        m.draw_jpeg(jpeg)                                               # after an animation: this one, and no more
        with pytest.raises(TimeoutError):
            m.draw_jpeg(jpeg)
    assert any("no stored still on show" in v for v in cooler.violations)
    cooler.violations.clear()


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
    assert min(m.tick or engine.TICK for m in (eng._mode, eng._preview) if m is not None) == visualizer.TICK


# ------------------------------------------------------------------ the spectrum
def _window(*tones, rate=48000, volume=0.4):
    return [sum(volume * math.sin(2 * math.pi * hz * i / rate) for hz in tones) for i in range(audio.WINDOW)]


def test_spectrum_puts_a_tone_in_its_band():
    sp = audio.Spectrum()
    assert len(sp.frequencies) == audio.BANDS and sp.frequencies[0] == audio.BAND_LOW
    assert abs(sp.frequencies[-1] - audio.BAND_HIGH) < 1e-6
    for hz in (63, 440, 3000, 9000):
        strengths = audio.Spectrum().strengths(_window(hz), 48000)
        loudest = max(range(audio.BANDS), key=strengths.__getitem__)
        nearest = min(range(audio.BANDS), key=lambda i: abs(math.log(sp.frequencies[i] / hz)))
        assert abs(loudest - nearest) <= 1, hz
        far = [s for i, s in enumerate(strengths) if abs(i - loudest) > 4]
        assert max(far) < 0.1 * strengths[loudest], hz              # and not everywhere else
    assert audio.Spectrum().strengths(_window(440, rate=44100), 44100)[9] > 0.2      # the tables follow the rate


def test_spectrum_levels_rise_at_once_fall_quickly_and_leave_a_mark():
    sp = audio.Spectrum()
    levels, peaks = sp.feed(_window(440), 48000, 0.05)
    band = max(range(audio.BANDS), key=levels.__getitem__)
    assert levels[band] == 1.0 and peaks[band] == 1.0               # the first sound sets what "loud" means
    levels, peaks = sp.feed([0.0] * audio.WINDOW, 48000, audio.FALL_SECONDS)
    assert 0.4 < levels[band] < 0.6 and peaks[band] > 0.8           # the bar has halved, its mark hardly moved
    for _ in range(100):
        levels, peaks = sp.feed([0.0] * audio.WINDOW, 48000, 0.1)
    assert not any(levels) and not any(peaks)
    assert audio.Spectrum().feed([], 48000, 0.05) == ([0.0] * audio.BANDS, [0.0] * audio.BANDS)   # nothing heard yet


def test_spectrum_shows_quiet_bands_next_to_a_loud_one():
    sp = audio.Spectrum()
    loud_and_quiet = [a + 0.1 * b for a, b in zip(_window(100), _window(3000))]
    levels, _ = sp.feed(loud_and_quiet, 48000, 0.05)
    treble = max(range(12, audio.BANDS), key=levels.__getitem__)
    assert max(levels[:6]) == 1.0 and 0.25 < levels[treble] < 0.9   # a tenth as loud, but well in sight


def test_the_dot_product_without_python_312():
    from operator import mul
    a, b = [0.5, -1.0, 2.0], [4.0, 0.25, 1.5]
    assert audio._dot(a, b) == sum(map(mul, a, b)) == 4.75


class ToneCapture:
    rate = 48000
    playing = True

    def read(self):
        return _tone(100, 0.02, rate=48000) if ToneCapture.playing else []

    def close(self):
        pass


def test_listener_gives_the_spectrum_and_silence_after_the_player_stops(monkeypatch):
    import time
    monkeypatch.setattr(audio, "IDLE_SECONDS", 0.5)
    ToneCapture.playing = True
    listener = audio.Listener(capture=ToneCapture)
    deadline = time.monotonic() + 3
    while max(listener.spectrum()[0]) < 0.9 and time.monotonic() < deadline:
        time.sleep(0.02)
    levels, peaks = listener.spectrum()
    assert max(levels[:6]) > 0.9 and max(levels[12:]) < 0.2 and listener.available
    ToneCapture.playing = False                                      # a paused player sends no samples at all
    deadline = time.monotonic() + 3
    while any(listener.spectrum()[0]) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not any(listener.spectrum()[0])


# ------------------------------------------------------------------ the mode: bars when it can draw
class SpectrumListener(FakeListener):
    def __init__(self):
        self.bars = ([0.0] * audio.BANDS, [0.0] * audio.BANDS)

    def spectrum(self):
        return self.bars


def _lit(img, band, above):
    """Is the bar of this band lit that many pixels above the floor?"""
    x = visualizer.BARS_LEFT + band * (visualizer.BAR_W + visualizer.BAR_GAP) + visualizer.BAR_W // 2
    return max(img.getpixel((x, visualizer.FLOOR - above))) > 120


def test_the_spectrum_is_drawn_anew_for_every_change_when_the_cooler_can_draw():
    from PIL import Image
    listener = SpectrumListener()
    m = visualizer.Visualizer({}, listener=listener)
    m.direct = True
    quiet = asyncio.run(m.frame())
    assert isinstance(quiet, Image.Image) and quiet.size == (device.WIDTH, device.HEIGHT) and m.status == "Nothing is playing"
    assert asyncio.run(m.frame()) is None                               # the same sound: nothing to draw
    levels = [0.0] * audio.BANDS
    levels[2], levels[20] = 1.0, 0.5
    marks = list(levels)
    marks[20] = 0.9
    listener.bars = (levels, marks)
    loud = asyncio.run(m.frame())
    assert m.status == "Following the music"
    reach = visualizer.REACH
    assert _lit(loud, 2, reach - 10) and _lit(loud, 20, reach // 2 - 10) and not _lit(loud, 20, reach // 2 + 30)
    assert _lit(loud, 20, round(0.9 * reach) + 1)                       # its peak mark, above the bar
    assert not _lit(loud, 10, 20) and not _lit(quiet, 2, 20)
    assert loud.resize((1, 1)).getpixel((0, 0)) > quiet.resize((1, 1)).getpixel((0, 0))    # the glow follows the bass


def test_the_orb_when_the_cooler_cannot_draw_or_the_orb_is_asked_for():
    listener = SpectrumListener()
    stored = visualizer.Visualizer({}, listener=listener)
    assert isinstance(asyncio.run(stored.frame()), Flipbook)
    orb = visualizer.Visualizer({"style": "orb"}, listener=listener)
    orb.direct = True
    assert isinstance(asyncio.run(orb.frame()), Flipbook)
    both = visualizer.Visualizer({}, listener=listener)                 # a cooler that is plugged in later
    assert isinstance(asyncio.run(both.frame()), Flipbook)
    both.direct = True
    assert not isinstance(asyncio.run(both.frame()), Flipbook)
    both.direct = False
    assert isinstance(asyncio.run(both.frame()), Flipbook)


def test_every_palette_draws_bars():
    for palette in visualizer.PALETTES:
        grounds = visualizer.bar_grounds(palette)
        assert len(grounds) == visualizer.GLOWS
        img = visualizer.render_bars(grounds[3], palette, [100] * audio.BANDS, [150] * audio.BANDS)
        assert img.size == (device.WIDTH, device.HEIGHT) and _lit(img, 0, 50) and _lit(img, audio.BANDS - 1, 50)


def test_a_cooler_that_shows_a_picture_every_turn_gets_fifty_five_a_second(monkeypatch):
    """Patch 12: the panel's every turn, 18 ms. The spectrum is asked for that often then, at a JPEG quality
    that keeps a frame inside one turn."""
    import asyncio

    from deephack import device
    from deephack.modes import video

    class Sound:
        available = True

        def spectrum(self):
            return (0.5,) * visualizer.BANDS, (0.6,) * visualizer.BANDS

        def levels(self):
            return 0.5, 0.5

    m = visualizer.Visualizer(listener=Sound())
    m.direct, m.fast = True, True
    asyncio.run(m.frame())
    assert m.tick == visualizer.TICK and m.quality is None                # as before, on an older patch
    m.turn = device.TURN
    asyncio.run(m.frame())
    assert m.tick == visualizer.QUICK_TICK < 0.02 and m.quality == visualizer.QUICK_QUALITY
    m.sent_bytes = 30_000                                                 # too much for one turn: a little less sharp
    asyncio.run(m.frame())
    assert m.quality == visualizer.QUICK_QUALITY - 3
    m.sent_bytes = 9_000
    asyncio.run(m.frame())
    assert m.quality == visualizer.QUICK_QUALITY - 2
    m.direct = False                                                      # DeepCool's firmware: the flipbook, as ever
    asyncio.run(m.frame())
    assert m.tick == visualizer.TICK and m.quality is None


def test_the_lights_are_two_level_meters_in_the_looks_colours():
    """Issue #57: the left side's lights rise with the low notes, the right side's with the high ones."""
    silent = visualizer.lights("fire", 0.0, 0.0)
    assert silent == [(0, 0, 0)] * device.LIGHTS
    loud = visualizer.lights("fire", 1.0, 0.0)
    assert all(max(c) == 255 for c in loud[:7]) and loud[7:] == [(0, 0, 0)] * 7           # the left side, all of it
    assert loud[0] != loud[6] and loud[0][0] == 255                       # the look's colours from quiet to loud
    half = visualizer.lights("fire", 0.0, 0.5)
    lit = [max(c) > 0 for c in half[7:]]                                  # the right side, top to bottom
    assert half[:7] == [(0, 0, 0)] * 7 and lit[-4:] == [True] * 4 and lit[:2] == [False] * 2     # from the bottom up
    assert visualizer.lights("mono", 1.0, 1.0) == [(255, 255, 255)] * 14

    listener = SpectrumListener()
    m = visualizer.Visualizer({}, listener=listener)
    m.direct = True
    asyncio.run(m.frame())
    assert m.lights == [(0, 0, 0)] * 14 and m.lights_fade == visualizer.LIGHT_FADE
    levels = [0.0] * audio.BANDS
    levels[2] = 1.0                                                       # a low note
    listener.bars = (levels, levels)
    asyncio.run(m.frame())
    assert max(m.lights[0]) > 0 and m.lights[7:] == [(0, 0, 0)] * 7
    asyncio.run(m.frame())                                                # the same sound: no picture, the lights stay
    assert max(m.lights[0]) > 0

    orb = visualizer.Visualizer({"style": "orb"}, listener=FakeListener())
    orb.listener.now = (0.0, 1.0)
    asyncio.run(orb.frame())
    assert orb.lights[:7] == [(0, 0, 0)] * 7 and all(max(c) == 255 for c in orb.lights[7:])
