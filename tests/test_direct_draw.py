"""The direct drawing patch (tools/fw/patch.py), run in the emulator against deephack's own driver.

Needs DeepCool's firmware image (from the installed app) and the emulator's packages (the `emu` extra);
skipped without them. Nothing here touches a real cooler.
"""
import io
import struct
import sys
from pathlib import Path

import pytest

from deephack import device

pytest.importorskip("unicorn")
pytest.importorskip("keystone")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "emu"))
sys.path.insert(0, str(ROOT / "tools" / "fw"))
import bench  # noqa: E402
import boot  # noqa: E402
import patch  # noqa: E402

pytestmark = pytest.mark.skipif(not boot.DEFAULT_IMAGE.exists(), reason="DeepCool's firmware image is not installed")
STORED_ID = "0123456789abcdef0123456789abcdef"
LOOP_ID = "fedcba9876543210fedcba9876543210"


def card(colour, box, quality=80) -> bytes:
    from PIL import Image, ImageDraw

    picture = Image.new("RGB", (480, 640), colour)
    ImageDraw.Draw(picture).rectangle(box, fill="white")
    return device.encode_jpeg(picture, quality=quality)


def on_screen(cooler, jpeg, tmp_path) -> bool:
    """The emulated LCD shows this JPEG (RGB565 keeps 5 or 6 bits a channel: differences up to 7)."""
    from PIL import Image, ImageChops

    cooler.screen(tmp_path / "screen.png")
    difference = ImageChops.difference(Image.open(tmp_path / "screen.png").convert("RGB"),
                                       Image.open(io.BytesIO(jpeg)).convert("RGB"))
    return max(high for _, high in difference.getextrema()) <= 7


def on_card(cooler) -> dict:
    """The files on the SD card (the firmware's FatFs writes some names in capitals: compared in lower case)."""
    return {name.lower(): content for name, content in cooler.files().items()}


def state(cooler) -> dict:
    raw = bytes(cooler.machine.uc.mem_read(patch.STATE, 36))
    names = ("first", "second", "size", "received", "sum", "expected", "started", "shown")
    return dict(zip(names, struct.unpack_from("<8I", raw, 4)), phase=raw[0])


@pytest.fixture(scope="module", autouse=True)
def quiet_emulator():
    """Unicorn sets up its memory through access violations it handles itself; pytest's fault handler
    would print a stack for each of them."""
    import faulthandler

    was_on = faulthandler.is_enabled()
    faulthandler.disable()
    yield
    if was_on:
        faulthandler.enable()


@pytest.fixture(scope="module")
def stock_image() -> bytes:
    return boot.DEFAULT_IMAGE.read_bytes()


@pytest.fixture(scope="module")
def cooler(stock_image, tmp_path_factory):
    """A cooler running the patched firmware, in image mode with one stored picture on show."""
    image = tmp_path_factory.mktemp("fw") / "patched.bin"
    image.write_bytes(patch.build(stock_image))
    cooler = bench.EmulatedCooler(firmware=image)
    driver = cooler.driver()
    driver.set_mode(device.MODE_MOTION)
    cooler.stored = card("#203060", (40, 40, 120, 120))
    driver.upload_jpeg(STORED_ID, cooler.stored)
    cooler.run_for(1.0)
    driver.show_slot(0)
    cooler.run_for(0.5)
    return cooler


def test_patch_only_adds_code_and_five_jumps(stock_image):
    patched = patch.build(stock_image)
    assert len(patch.HOOKS) == 5
    assert patched[len(stock_image):] == patch.assemble(patch.APP + len(stock_image))[0]
    changed = [at for at in range(len(stock_image)) if patched[at] != stock_image[at]]
    hooks = {address - patch.APP + i for address, _, _ in patch.HOOKS for i in range(4)}
    assert changed and set(changed) <= hooks
    assert len(patched) - len(stock_image) < 1024


def test_patch_refuses_another_image(stock_image):
    other = bytearray(stock_image)
    other[0x5000] ^= 1
    with pytest.raises(ValueError, match="not the firmware"):
        patch.build(bytes(other))


def test_stock_firmware_is_not_sent_a_frame():
    """The stock firmware answers the unknown command with 'ok': the driver must not take that for a yes."""
    cooler = bench.EmulatedCooler()
    driver = cooler.driver()
    assert driver.can_draw() is False and driver.draw_version() == 0
    sent = cooler.machine.usb.delivered
    with pytest.raises(RuntimeError, match="cannot draw"):
        driver.draw_jpeg(card("#802020", (10, 10, 50, 50)))
    assert cooler.machine.usb.delivered == sent + 1          # the question, and no picture data after it


def test_patched_firmware_still_does_what_the_stock_one_does(cooler, tmp_path):
    assert on_card(cooler) == {f"img/000_{STORED_ID}/angle_0.jpg": cooler.stored}
    assert on_screen(cooler, cooler.stored, tmp_path)
    assert cooler.driver().uuid() == ""                       # a fresh card has no id; the command is answered
    assert state(cooler) == dict.fromkeys(("first", "second", "size", "received", "sum", "expected", "started",
                                           "shown", "phase"), 0)   # and the patch has not done anything yet


def test_frames_are_drawn_from_ram_without_a_file(cooler, tmp_path):
    driver, decoder = cooler.driver(), cooler.machine.jpeg
    assert driver.can_draw() and driver.draw_version() == patch.VERSION == max(patch.CHANGES)
    sources = []
    for colour, box in (("#802020", (200, 100, 400, 300)), ("#208020", (50, 400, 250, 600)), ("#202080", (300, 300, 460, 620))):
        jpeg = card(colour, box)
        driver.draw_jpeg(jpeg)
        cooler.run_for(0.3)
        assert on_screen(cooler, jpeg, tmp_path)
        sources.append(struct.unpack_from("<I", decoder.reg, 0x18)[0])
    now = state(cooler)
    assert now["shown"] == 3 and now["phase"] == 0 and not decoder.errors
    used = [next(b for b in (now["first"], now["second"]) if b + 16 <= s < b + 16 + patch.MAX_FRAME) for s in sources]
    assert used[0] != used[1] and used[1] != used[2]          # never the buffer that is on screen
    assert on_card(cooler) == {f"img/000_{STORED_ID}/angle_0.jpg": cooler.stored}      # nothing was written


def test_frame_with_a_wrong_sum_is_not_shown(cooler, tmp_path):
    driver = cooler.driver()
    good, bad = card("#505050", (100, 100, 300, 300)), card("#a0a000", (0, 0, 479, 639))
    driver.draw_jpeg(good)
    cooler.run_for(0.3)
    assert driver._direct(len(bad), (device.sum16(bad) + 1) & 0xFFFF) == device.DIRECT_GO
    driver.dev.write(1, bad)
    cooler.run_for(0.3)
    assert on_screen(cooler, good, tmp_path) and state(cooler)["phase"] == 0


def test_size_limits(cooler):
    driver = cooler.driver()
    assert driver._direct(0, 0) == device.DIRECT_REFUSED
    assert driver._direct(device.DIRECT_MAX + 1, 0) == device.DIRECT_REFUSED
    with pytest.raises(ValueError):
        driver.draw_jpeg(b"\xff\xd8 not a picture \xff\xd9")


def test_stalled_frame_is_given_up_and_commands_work_again(cooler, tmp_path):
    driver = cooler.driver()
    jpeg = card("#903090", (20, 500, 460, 620))
    assert driver._direct(len(jpeg), device.sum16(jpeg)) == device.DIRECT_GO
    driver.dev.write(1, jpeg[:640])                           # ... and the host goes quiet
    assert state(cooler)["phase"] == 1 and state(cooler)["received"] == 640
    cooler.run_for(patch.TIMEOUT_MS / 1000 + 0.2)
    assert driver.uuid() == ""                                # not swallowed as picture data
    assert state(cooler)["phase"] == 0
    driver.draw_jpeg(jpeg)
    cooler.run_for(0.3)
    assert on_screen(cooler, jpeg, tmp_path)


def test_stored_pictures_still_work_after_direct_frames(cooler, tmp_path):
    driver = cooler.driver()
    replacement = card("#106060", (300, 40, 440, 180))
    driver.upload_jpeg(STORED_ID, replacement)                # the stock upload, into the same slot
    cooler.run_for(1.0)
    driver.show_slot(0)
    cooler.run_for(0.5)
    assert on_card(cooler) == {f"img/000_{STORED_ID}/angle_0.jpg": replacement}
    assert on_screen(cooler, replacement, tmp_path)
    frame = card("#703010", (10, 10, 200, 90))
    driver.draw_jpeg(frame)
    cooler.run_for(0.3)
    assert on_screen(cooler, frame, tmp_path)


def test_frames_are_shown_while_the_task_handler_never_returns(cooler, stock_image, tmp_path):
    """Issue #22. A still inherits the frame time of the animation shown before it as its screen timer.
    With a fast animation that timer is always due, lv_task_handler does not return, and the hook in the
    main loop is not reached: the first patch took one frame then and answered "later" for good."""
    driver, blocks = cooler.driver(), cooler.machine.blocks
    main_hook = patch.assemble(patch.APP + len(stock_image))[1]["main_hook"]
    loop = [card(colour, (100, 100 + 60 * i, 380, 160 + 60 * i), quality=60)
            for i, colour in enumerate(("#600000", "#006000", "#000060", "#606000"))]
    driver.upload_gif(LOOP_ID, loop, 80)                      # 20 ms a frame
    cooler.run_for(1.0)
    driver.show_slot(0, device.TYPE_GIF)
    cooler.run_for(1.5)
    shown, passes = state(cooler)["shown"], blocks[main_hook]
    over = card("#804000", (60, 60, 200, 200))
    driver.draw_jpeg(over)                                    # over the animation: taken, and replaced by its next frame
    cooler.run_for(0.3)
    assert state(cooler)["shown"] == shown + 1 and state(cooler)["phase"] == 0

    driver.show_slot(0)
    cooler.run_for(0.5)
    for colour, box in (("#802020", (200, 100, 400, 300)), ("#208020", (50, 400, 250, 600))):
        jpeg = card(colour, box)
        driver.draw_jpeg(jpeg)
        cooler.run_for(0.3)
        assert on_screen(cooler, jpeg, tmp_path)
    assert state(cooler)["shown"] == shown + 3
    assert blocks[main_hook] == passes                        # all of it without the main loop coming round once
