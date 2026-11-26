"""The 14 lights on the pump head: the colours deephack picks for them (deephack.light), the command that
carries them, and the engine's part, against the cooler model. What the patched firmware does with the
command is in test_direct_draw.py (the emulator)."""
import struct

import pytest
from PIL import Image, ImageDraw

from deephack import device, engine, light
from deephack.config import Config
from deephack.modes.base import Animation

LEFT, RIGHT = slice(0, 7), slice(7, 14)                  # up the left side, then down the right


def _engine(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_stored_count", lambda kind: 1)
    monkeypatch.setattr(engine, "DRAW_SETTLE", 0.0)
    eng = engine.Engine(Config(tmp_path / "config.json"))
    eng._make_mode()
    return eng


def _clock(monkeypatch) -> list:
    """The engine's clock, to be set by the test (its first entry). Only the engine's: the module `time`
    itself is everyone's, and a thread of another test that found it running backwards has died of it."""
    import time

    now = [100.0]

    class Clock:
        def __getattr__(self, name):
            return getattr(time, name)

        @staticmethod
        def monotonic() -> float:
            return now[0]

    monkeypatch.setattr(engine, "time", Clock())
    return now


def _show(eng, frame, steps=2, enabled=True):
    eng._set_frame(frame)
    for _ in range(steps):
        eng._device_step(enabled)
        eng._light_step(enabled)


def halves(left, right):
    picture = Image.new("RGB", (480, 640), left)
    ImageDraw.Draw(picture).rectangle((240, 0, 479, 639), fill=right)
    return picture


# ---- the colours
def test_each_side_takes_its_own_colour_and_black_is_dark():
    picked = light.colours(halves((255, 0, 0), (0, 0, 255)))
    assert len(picked) == device.LIGHTS
    assert all(c == (255, 0, 0) for c in picked[LEFT]) and all(c == (0, 0, 255) for c in picked[RIGHT])
    assert light.colours(Image.new("RGB", (480, 640))) == [(0, 0, 0)] * 14
    assert light.colours(Image.new("RGB", (480, 640), (6, 6, 10))) == [(0, 0, 0)] * 14       # an empty page does not glow
    assert all(c == (255, 255, 255) for c in light.colours(Image.new("RGB", (480, 640), "white")))


def test_the_lights_run_up_the_left_side_and_down_the_right():
    picture = Image.new("RGB", (480, 640))
    draw = ImageDraw.Draw(picture)
    draw.rectangle((0, 0, 479, 159), fill=(0, 255, 0))                    # green along the top
    draw.rectangle((0, 480, 479, 639), fill=(255, 0, 0))                  # red along the bottom
    picked = light.colours(picture)
    assert picked[0] == picked[13] == (255, 0, 0) and picked[6] == picked[7] == (0, 255, 0)
    assert picked[3] == picked[10] == (0, 0, 0)                           # the middle of either side sees neither
    red_up = [c[0] for c in picked[LEFT]]
    assert red_up == sorted(red_up, reverse=True) and 0 < picked[2][0] < 255          # it runs out over its neighbour


def test_a_coloured_shape_counts_for_more_than_the_text_around_it():
    """What DeepCool's firmware does with such a page: one pixel, 5 in from the edge, which is the page's
    dark ground here. And a line of white text there would make the light white."""
    page = Image.new("RGB", (480, 640), (14, 14, 20))
    draw = ImageDraw.Draw(page)
    draw.rectangle((30, 250, 100, 390), fill=(255, 120, 0))               # an orange mark, 30 pixels in
    draw.rectangle((30, 20, 110, 30), fill=(255, 255, 255))               # a white line near the top
    picked = light.colours(page)
    red, green, blue = picked[3]                                          # the middle of the left side
    assert red > 100 and red > 2 * green and blue < green / 2             # orange, and deeper than the picture's
    top = picked[6]
    assert max(top) - min(top) < 10 and max(top) > 12                     # the white line gives white, dimly
    assert max(picked[10]) < 24                                           # only the page's dark ground on the right: faint


def test_a_dark_picture_still_glows_and_the_level_dims_it():
    dim = light.colours(Image.new("RGB", (480, 640), (60, 20, 20)))       # 4 % of full red, as light
    assert 30 < dim[0][0] < 80 and dim[0][1] < dim[0][0] / 3              # lifted: true to the picture would be 10
    half = light.colours(halves((255, 0, 0), (0, 0, 255)), level=0.5)
    assert half[0] == (128, 0, 0) and half[13] == (0, 0, 128)
    assert light.colours(halves((255, 0, 0), (0, 0, 255)), level=0) == [(0, 0, 0)] * 14


def test_any_size_of_picture_will_do():
    small = halves((255, 0, 0), (0, 0, 255)).resize((120, 160), Image.NEAREST)
    assert light.colours(small) == light.colours(halves((255, 0, 0), (0, 0, 255)))
    assert len(light.colours(Image.new("L", (48, 64), 200))) == 14


# ---- the command
def test_the_packet_is_a_command_with_what_did_not_fit_after_it():
    colours = [(i, 100 + i, 200 + i) for i in range(14)]
    packet = device.light_packet(colours, pace=40)
    assert len(packet) == 64 and packet[:3] == bytes([0xAA, 0x2E, device.CMD_LIGHT])
    assert packet[:48] == device.frame(device.CMD_LIGHT, bytes(c for colour in colours[:13] for c in colour))
    assert packet[48:53] == bytes([13, 113, 213, 1, 40]) and packet[53:] == bytes(11)
    assert struct.unpack_from("<H", packet, 46)[0] == device.sum16(packet[:46])
    assert device.light_packet(colours, on=False)[51:53] == bytes(2)
    assert device.light_packet([(300, -5, 12.6)] * 14)[3:6] == bytes([255, 0, 13])
    for wrong in ([(0, 0, 0)] * 13, [(0, 0)] * 14):
        with pytest.raises(ValueError):
            device.light_packet(wrong)
    with pytest.raises(ValueError):
        device.light_packet(colours, pace=256)


def test_the_pace_for_a_time():
    assert device.light_pace(0) == 0 and device.light_pace(0.01) == 0     # at once
    assert device.light_pace(0.4) == 10 and device.light_pace(0.12) == 34
    assert device.light_pace(60) == 1                                     # never 0, which would be "at once"
    assert device.light_pace(0.017) == 241


def test_the_driver_says_whether_the_cooler_took_them(cooler):
    with device.Mystique() as m:
        m.set_mode(device.MODE_MOTION)
        m.set_options(70, device.LIGHT_SCREEN)
        assert m.set_light([(1, 2, 3)] * 14) is False and cooler.lights is None        # DeepCool's firmware
        cooler.direct = 15
        assert m.set_light([(1, 2, 3)] * 14) is False and cooler.lights is None
        cooler.direct = 16
        assert m.set_light([(1, 2, 3)] * 13 + [(7, 8, 9)], pace=20) is True
        assert cooler.lights == ([(1, 2, 3)] * 13 + [(7, 8, 9)], 20)
        assert m.release_light() is True and cooler.lights is None


# ---- the engine
def test_the_lights_follow_what_is_drawn(tmp_path, monkeypatch, cooler):
    cooler.direct = 16
    eng = _engine(tmp_path, monkeypatch)
    picture = halves((255, 0, 0), (0, 0, 255))
    _show(eng, picture)
    assert cooler.light == device.LIGHT_SCREEN
    assert cooler.lights == (light.colours(picture), device.light_pace(engine.LIGHT_FADE))
    told = cooler.light_commands
    _show(eng, picture, steps=3)
    assert cooler.light_commands == told                                  # nothing new: nothing said
    monkeypatch.setattr(engine, "LIGHT_EVERY", 0.0)
    other = halves((0, 255, 0), (255, 255, 0))
    _show(eng, other, steps=1)
    assert cooler.lights[0] == light.colours(other) and cooler.light_commands == told + 1


def test_they_are_said_again_before_the_cooler_takes_them_back(tmp_path, monkeypatch, cooler):
    cooler.direct = 16
    eng = _engine(tmp_path, monkeypatch)
    now = _clock(monkeypatch)
    _show(eng, halves((255, 0, 0), (0, 0, 255)))
    told = cooler.light_commands
    now[0] += engine.LIGHT_AGAIN - 0.1
    eng._light_step(True)
    assert cooler.light_commands == told
    now[0] += 0.2
    eng._light_step(True)
    assert cooler.light_commands == told + 1 and engine.LIGHT_AGAIN < device.LIGHT_KEEP / 2


def test_a_new_colour_waits_its_turn_and_is_not_lost(tmp_path, monkeypatch, cooler):
    """Pictures that come thirty a second: the lights are told at most every LIGHT_EVERY, and the last
    colour gets there."""
    cooler.direct = 16
    eng = _engine(tmp_path, monkeypatch)
    now = _clock(monkeypatch)
    first, second = halves((255, 0, 0), (0, 0, 255)), halves((0, 255, 0), (0, 255, 0))
    _show(eng, first)
    told = cooler.light_commands
    now[0] += engine.LIGHT_EVERY / 2
    _show(eng, second, steps=1)
    assert cooler.light_commands == told and cooler.lights[0] == light.colours(first)
    assert 0 < eng._again <= engine.LIGHT_EVERY                           # the engine comes back for it
    now[0] += engine.LIGHT_EVERY
    eng._light_step(True)
    assert cooler.lights[0] == light.colours(second)


def test_quick_pictures_fade_quickly(tmp_path, monkeypatch, cooler):
    cooler.direct = 16
    eng = _engine(tmp_path, monkeypatch)
    red, blue = Image.new("RGB", (480, 640), "red"), Image.new("RGB", (480, 640), "blue")
    _show(eng, Animation([red, blue], 400), steps=3)
    assert cooler.lights is not None and cooler.lights[1] == device.light_pace(engine.LIGHT_FADE_FAST)
    assert cooler.lights[0] in (light.colours(red), light.colours(blue))


def test_the_level_is_a_setting(tmp_path, monkeypatch, cooler):
    cooler.direct = 16
    eng = _engine(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "LIGHT_EVERY", 0.0)
    picture = halves((255, 0, 0), (0, 0, 255))
    eng.config.set("light_level", 50)
    _show(eng, picture)
    assert cooler.lights[0] == light.colours(picture, 0.5) and cooler.lights[0][0] == (128, 0, 0)
    eng.config.set("light_level", 100)
    eng._light_step(True)
    assert cooler.lights[0][0] == (255, 0, 0)


def test_the_cooler_picks_again_when_the_lights_are_not_ours(tmp_path, monkeypatch, cooler):
    cooler.direct = 16
    eng = _engine(tmp_path, monkeypatch)
    picture = halves((255, 0, 0), (0, 0, 255))
    _show(eng, picture)
    assert cooler.lights is not None
    eng.config.set("light", "warning")                                    # another source for the light
    _show(eng, picture, steps=1)
    assert cooler.lights is None and cooler.light == device.LIGHT_WARNING
    eng.config.set("light", "screen")
    _show(eng, picture, steps=1)
    assert cooler.lights is not None
    _show(eng, picture, steps=1, enabled=False)                           # paused: the stored picture is on show
    assert cooler.lights is None and cooler.screen == ("jpg", 0)
    _show(eng, picture, steps=3)
    assert cooler.lights is not None
    eng._shutdown()                                                       # deephack closes
    assert cooler.lights is None


def test_a_slide_show_and_older_firmware_are_left_to_the_cooler(tmp_path, monkeypatch, cooler):
    cooler.direct = 16
    eng = _engine(tmp_path, monkeypatch)
    eng.config.set("slideshow", True)                                     # stored pictures: the cooler's own picking
    _show(eng, halves((255, 0, 0), (0, 0, 255)))
    assert cooler.lights is None and cooler.light_commands == 0
    cooler.direct = 15
    older = _engine(tmp_path / "older", monkeypatch) if (tmp_path / "older").mkdir() is None else None
    _show(older, halves((255, 0, 0), (0, 0, 255)))
    assert cooler.screen[0] == "drawn" and cooler.light_commands == 0 and not older._lights


def test_what_the_coolers_own_firmware_picks():
    """One pixel a light, 5 in from the edge, through the frame buffer's 5, 6 and 5 bits."""
    page = Image.new("RGB", (480, 640), (14, 14, 20))
    draw = ImageDraw.Draw(page)
    draw.rectangle((30, 250, 100, 390), fill=(255, 120, 0))               # the orange mark of the test above: not seen
    draw.point((5, 305), fill=(255, 255, 255))                            # ... and one white pixel is a whole light
    draw.point((475, 635), fill=(1, 2, 3))
    picked = light.cooler_colours(page)
    assert picked[3] == (248, 252, 248) and picked[13] == (0, 0, 0)
    assert all(c == (8, 12, 16) for i, c in enumerate(picked) if i not in (3, 13))
    assert len(light.COOLER_PIXELS) == device.LIGHTS
    assert light.cooler_colours(halves((255, 0, 0), (0, 0, 255)).resize((240, 320))) == [(248, 0, 0)] * 7 + [(0, 0, 248)] * 7


# ---- a mode's own lights (issue #57)
def test_a_colour_for_a_light_and_a_side_as_a_meter():
    assert light.led((255, 0, 0)) == (255, 0, 0) and light.led((128, 0, 0)) == (255, 0, 0)       # the colour, not how bright
    assert light.led((255, 0, 0), 0.5) == (128, 0, 0) and light.led((255, 0, 0), 0) == light.led((0, 0, 0)) == (0, 0, 0)
    orange = light.led((255, 128, 0))
    assert orange[0] == 255 and 20 < orange[1] < 60 and orange[2] == 0    # deeper than the picture's, as the picked ones
    assert light.led((300, -4, 0), 2.0) == (255, 0, 0)
    reds = [(255, 0, 0)] * 7
    assert light.meter(0, reds) == [(0, 0, 0)] * 7 and light.meter(1, reds) == [(255, 0, 0)] * 7
    half = light.meter(0.5, reds)                                         # three and a half of seven, from the bottom
    assert half[:3] == [(255, 0, 0)] * 3 and half[3] == (128, 0, 0) and half[4:] == [(0, 0, 0)] * 3
    assert light.meter(1, reds, power=0.5) == [(128, 0, 0)] * 7 and light.meter(7, reds) == light.meter(1, reds)
    left, right = [(i, 0, 0) for i in range(7)], [(0, i, 0) for i in range(7)]
    all14 = light.arrange(left, right)                                    # up the left side, down the right
    assert all14[:7] == left and all14[7] == (0, 6, 0) and all14[13] == (0, 0, 0) and len(all14) == device.LIGHTS
    with pytest.raises(ValueError):
        light.arrange(left, right[:6])


def test_a_mode_may_say_what_the_lights_show(tmp_path, monkeypatch, cooler):
    cooler.direct = 16
    eng = _engine(tmp_path, monkeypatch)
    now = _clock(monkeypatch)
    said = []
    eng.lightsChanged.connect(lambda mode, colours: said.append((mode, colours)))
    picture = halves((255, 0, 0), (0, 0, 255))
    _show(eng, picture)
    assert cooler.lights[0] == light.colours(picture)                     # it says nothing: the picture's edge
    own = light.arrange([(0, 255, 0)] * 7, [(255, 255, 0)] * 7)
    eng._mode.lights = own
    eng._light_step(True)
    assert cooler.lights[0] == light.colours(picture)                     # too soon after the last ones ...
    now[0] += engine.LIGHT_EVERY_OWN
    eng._light_step(True)
    assert cooler.lights == (own, device.light_pace(engine.LIGHT_FADE_FAST))      # ... and then they are the mode's
    eng._mode.lights_fade = 0.6
    eng.config.set("light_level", 50)
    now[0] += engine.LIGHT_EVERY_OWN
    eng._light_step(True)
    assert cooler.lights == ([(0, 128, 0)] * 7 + [(128, 128, 0)] * 7, device.light_pace(0.6))
    eng._mode.lights = [(1, 2, 3)] * 5                                    # not 14 of them: as if it had said nothing
    now[0] += 1
    eng._light_step(True)
    assert cooler.lights[0] == light.colours(picture, 0.5)
    eng._mode.lights = None
    eng._say_lights(eng._mode)
    eng._mode.lights = own
    eng._say_lights(eng._mode)
    eng._say_lights(eng._mode)                                            # the window hears of a change, once
    assert said == [(eng._mode.id, [tuple(c) for c in own])]
