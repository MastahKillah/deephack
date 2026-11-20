"""The engine on a cooler with the direct drawing firmware: a mode's pictures are drawn from USB, not stored."""
import random

import pytest
from PIL import Image

from deephack import device, engine
from deephack.config import SLOT_ID, Config
from deephack.modes.base import Animation

RED, BLUE = Image.new("RGB", (480, 640), "#a02020"), Image.new("RGB", (480, 640), "#2040b0")


def _drawn(picture):
    return ("drawn", engine.frame_jpeg(picture))


def _engine(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_stored_count", lambda kind: 1)          # the official app's one still, one animation
    monkeypatch.setattr(engine, "DRAW_SETTLE", 0.0)
    eng = engine.Engine(Config(tmp_path / "config.json"))
    eng._make_mode()
    return eng


def _show(eng, frame, steps=2, enabled=True):
    eng._set_frame(frame)
    for _ in range(steps):
        eng._device_step(enabled)


def _is_black(picture):
    return sum(picture.resize((1, 1)).getpixel((0, 0))) < 12


def test_pictures_are_drawn_and_only_the_ground_is_stored(tmp_path, monkeypatch, cooler):
    cooler.direct = True
    eng = _engine(tmp_path, monkeypatch)
    states = []
    eng.stateChanged.connect(states.append)
    _show(eng, RED)
    assert cooler.screen == _drawn(RED) and cooler.shown == ("jpg", 1)
    assert cooler.flash_writes == 1 and _is_black(cooler.still(1))        # what the frames are drawn over
    assert eng.config.get("ground") and eng.config.get("slot_index") == 1
    _show(eng, BLUE, steps=1)                                             # at once: no wait between two pictures
    assert cooler.screen == _drawn(BLUE) and cooler.flash_writes == 1 and cooler.draws == 2
    eng._emit_state(True)
    assert states[-1]["drawn"] and not states[-1]["pending"] and states[-1]["last_upload"]

    again = _engine(tmp_path, monkeypatch)                                # after a restart nothing is stored at all
    _show(again, RED)
    assert cooler.screen == _drawn(RED) and cooler.flash_writes == 1


def test_a_picture_left_in_our_slot_is_replaced_by_the_ground(tmp_path, monkeypatch, cooler):
    cooler.direct = True
    cooler.stills.append([SLOT_ID, device.encode_jpeg(RED)])              # stored by an earlier deephack
    eng = _engine(tmp_path, monkeypatch)
    eng.config.set("slot_index", 1)
    _show(eng, BLUE)
    assert len(cooler.stills) == 2 and _is_black(cooler.still(1)) and cooler.flash_writes == 1
    assert cooler.screen == _drawn(BLUE)


def test_an_unchanged_picture_is_sent_again_now_and_then(tmp_path, monkeypatch, cooler):
    cooler.direct = True
    eng = _engine(tmp_path, monkeypatch)
    _show(eng, RED, steps=4)
    assert cooler.draws == 1
    monkeypatch.setattr(engine, "REDRAW_STILL", 0.0)
    eng._device_step(True)
    assert cooler.draws == 2 and cooler.screen == _drawn(RED)


def test_a_picture_too_large_for_the_firmware_is_drawn_at_a_lower_quality(tmp_path, monkeypatch, cooler):
    noise = Image.frombytes("RGB", (480, 640), random.Random(1).randbytes(480 * 640 * 3))
    assert len(device.encode_jpeg(noise, quality=92)) > device.DIRECT_MAX
    assert len(engine.frame_jpeg(noise)) <= device.DIRECT_MAX
    assert engine.frame_jpeg(RED) == device.encode_jpeg(RED, quality=92)   # as good as a stored one when it fits
    cooler.direct = True
    eng = _engine(tmp_path, monkeypatch)
    _show(eng, noise)
    assert cooler.screen == _drawn(noise)


def _played(picture):
    return ("drawn", engine.frame_jpeg(picture, engine.PLAY_QUALITY))


def test_an_animation_is_played_from_here_frame_by_frame(tmp_path, monkeypatch, cooler):
    cooler.direct = True
    clock = [5000.0]
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])
    eng = _engine(tmp_path, monkeypatch)
    _show(eng, Animation([RED, BLUE, BLUE, RED], 400))                    # a tenth of a second per frame
    assert cooler.screen == _played(RED) and cooler.flash_writes == 1 and not cooler.gifs
    assert eng._again == pytest.approx(0.1)                               # when the next frame is due

    def later(seconds):
        clock[0] += seconds
        eng._device_step(True)
        return cooler.screen, cooler.draws

    assert later(0.1) == (_played(BLUE), 2)
    assert later(0.1) == (_played(BLUE), 2)                               # the same picture again: not sent
    assert later(0.04) == (_played(BLUE), 2) and eng._again == pytest.approx(0.06)
    assert later(0.06) == (_played(RED), 3)
    assert later(0.1) == (_played(RED), 3)                                # round to the first frame, the same picture
    assert later(0.1) == (_played(BLUE), 4)
    eng._emit_state(True)
    assert eng._last_state["drawn"] and not eng._last_state["pending"]
    assert cooler.flash_writes == 1 and cooler.shown == ("jpg", 1)        # over the ground still, never an animation

    _show(eng, RED, steps=1)                                              # a picture after it: no detour needed
    assert cooler.screen == _drawn(RED)


def test_deepcools_own_firmware_gets_its_animations_stored(tmp_path, monkeypatch, cooler):
    cooler.gifs.append(["app-gif-0", [], 1000])
    eng = _engine(tmp_path, monkeypatch)
    _show(eng, Animation([RED, BLUE], 400))
    assert cooler.screen == ("gif", 1) and cooler.flash_writes == 2 and cooler.draws == 0 and eng._again is None


def test_a_picture_that_cannot_be_drawn_yet_is_tried_again_soon(tmp_path, monkeypatch, cooler):
    cooler.direct = True
    eng = _engine(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "DRAW_SETTLE", 60.0)
    _show(eng, RED)
    assert cooler.draws == 0 and eng._again == engine.DRAW_RETRY          # not left until the mode's next turn
    monkeypatch.setattr(engine, "DRAW_SETTLE", 0.0)
    eng._draw_from = 0.0
    eng._device_step(True)
    assert cooler.screen == _drawn(RED) and eng._again is None


def test_during_a_slide_show_pictures_are_stored_as_before(tmp_path, monkeypatch, cooler):
    cooler.direct = True
    eng = _engine(tmp_path, monkeypatch)
    eng.config.set("slideshow", True)
    _show(eng, RED)
    assert cooler.screen == ("jpg", 1) and cooler.draws == 0 and cooler.flash_writes == 1
    assert not eng.config.get("ground") and not _is_black(cooler.still(1))
    eng.config.set("slideshow", False)    # switched off: drawn from here on, over the ground
    for _ in range(3):
        eng._device_step(True)
    assert cooler.screen == _drawn(RED) and cooler.flash_writes == 2 and _is_black(cooler.still(1))


def test_paused_shows_the_users_picture_and_resumed_draws_again(tmp_path, monkeypatch, cooler):
    cooler.direct = True
    eng = _engine(tmp_path, monkeypatch)
    _show(eng, RED)
    eng._device_step(False)
    assert cooler.screen == ("jpg", 0)    # not the drawn frame: the engine left the image screen and came back
    for _ in range(2):
        eng._device_step(True)
    assert cooler.screen == _drawn(RED) and cooler.flash_writes == 1


def test_deepcools_own_firmware_gets_its_pictures_stored(tmp_path, monkeypatch, cooler):
    eng = _engine(tmp_path, monkeypatch)
    _show(eng, RED)
    assert cooler.screen == ("jpg", 1) and cooler.flash_writes == 1 and cooler.draws == 0
    assert not eng.config.get("ground") and not _is_black(cooler.still(1))
    eng._emit_state(True)
    assert not eng._last_state["drawn"]


@pytest.mark.parametrize("quality", engine.STILL_QUALITIES)
def test_every_quality_gives_a_frame_the_driver_takes(quality):
    assert device.jpeg_size(device.encode_jpeg(RED, quality=quality)) == (device.WIDTH, device.HEIGHT)


def test_pictures_that_come_many_a_second_are_sent_smaller(tmp_path, monkeypatch, cooler):
    cooler.direct = True
    eng = _engine(tmp_path, monkeypatch)
    noisy = Image.radial_gradient("L").resize((480, 640)).convert("RGB")
    eng._mode.tick = 0.05                                                 # a mode that draws twenty times a second
    _show(eng, noisy)
    fast = cooler.screen[1]
    assert fast == engine.frame_jpeg(noisy, engine.DRAW_QUALITY)
    eng._mode.tick = None
    _show(eng, noisy, steps=1)
    assert cooler.screen[1] == engine.frame_jpeg(noisy) and len(fast) < len(cooler.screen[1])
