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


def on_screen(cooler, picture, tmp_path) -> bool:
    """The emulated LCD shows this JPEG, or this picture (RGB565 keeps 5 or 6 bits a channel: differences
    up to 7)."""
    from PIL import Image, ImageChops

    cooler.screen(tmp_path / "screen.png")
    wanted = Image.open(io.BytesIO(picture)) if isinstance(picture, bytes) else picture
    difference = ImageChops.difference(Image.open(tmp_path / "screen.png").convert("RGB"), wanted.convert("RGB"))
    return max(high for _, high in difference.getextrema()) <= 7


def on_card(cooler) -> dict:
    """The files on the SD card (the firmware's FatFs writes some names in capitals: compared in lower case)."""
    return {name.lower(): content for name, content in cooler.files().items()}


def state(cooler) -> dict:
    """The patch's own RAM (the layout is above SOURCE in patch.py)."""
    raw = bytes(cooler.machine.uc.mem_read(patch.STATE, patch.STATE_SIZE))
    names = ("first", "second", "third", "size", "received", "sum", "expected", "started", "shown")
    return dict(zip(names, struct.unpack_from("<9I", raw, 4)), phase=raw[0], page=raw[2], part=raw[3])


FRAME_BUFFERS = (0x800061A0, 0x8009C1C0)                    # the LCD's two, 480 x 640 x 2 bytes each


def decoded(cooler, since: int) -> set:
    """What the firmware has had its JPEG decoder do since: (width, height, x, y) on the screen, or
    "elsewhere" for a picture decoded into other memory than a frame buffer."""
    seen = set()
    for address, width, height in cooler.machine.jpeg.decoded[since:]:
        base = next((b for b in FRAME_BUFFERS if b <= address < b + 480 * 640 * 2), None)
        seen.add("elsewhere" if base is None else (width, height, (address - base) // 2 % 480, (address - base) // 2 // 480))
    return seen


def part_of(colour, size=(480, 272), box=(40, 40, 200, 200)) -> bytes:
    from PIL import Image, ImageDraw

    picture = Image.new("RGB", size, colour)
    ImageDraw.Draw(picture).ellipse(box, fill="white")
    return device.encode_jpeg(picture, quality=80, part=True)


def with_part(page: bytes, part: bytes, at):
    """What the screen shows with a part on a page (for on_screen)."""
    from PIL import Image

    whole = Image.open(io.BytesIO(page)).convert("RGB")
    whole.paste(Image.open(io.BytesIO(part)).convert("RGB"), at)
    return whole


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
    code = patch.assemble(patch.APP + len(stock_image))[0]
    assert patched[len(stock_image):] == code + patch.logo()              # the code, then the start screen's picture
    changed = [at for at in range(len(stock_image)) if patched[at] != stock_image[at]]
    hooks = {address - patch.APP + i for address, _, _ in patch.HOOKS for i in range(4)}
    assert changed and set(changed) <= hooks
    assert len(code) < 2048
    # The bootloader (read from the cooler) writes the file 4 KiB at a time from 0xE000 to the end of the
    # flash at 0x80000 and stops there: a longer image would lose its end without a word.
    assert patch.MOST == 0x80000 - patch.APP and len(patched) <= patch.MOST
    assert device.jpeg_size(patch.logo()) == (480, 640) and patch.logo() == (ROOT / "tools" / "fw" / "logo.jpg").read_bytes()


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
    assert set(state(cooler).values()) == {0}                 # and the patch has not done anything yet


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
    buffers = (now["first"], now["second"], now["third"])
    assert all(buffers) and patch.STATE_SIZE == 68
    used = [next(b for b in buffers if b + 16 <= s < b + 16 + patch.MAX_FRAME) for s in sources]
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


def test_a_part_is_decoded_into_its_place_on_the_page(cooler, tmp_path):
    """Version 3. A part of the picture is a JPEG of its own, shown by a second, small picture on top of
    the page: the stock firmware decodes it straight into the frame buffer where that picture lies."""
    driver, decoder = cooler.driver(), cooler.machine.jpeg
    page = card("#602020", (40, 500, 440, 620))
    driver.draw_jpeg(page)
    cooler.run_for(0.3)
    assert on_screen(cooler, page, tmp_path) and state(cooler)["part"] == 0
    shown, since = state(cooler)["shown"], len(decoder.decoded)

    buffers = []
    for colour in ("#2060c0", "#c06020", "#20c060", "#c020c0"):
        part = part_of(colour)
        driver.draw_jpeg(part, at=(0, 176))
        cooler.run_for(0.3)
        assert on_screen(cooler, with_part(page, part, (0, 176)), tmp_path)
        now = state(cooler)
        buffers.append((now["page"], now["part"]))
    assert state(cooler)["shown"] == shown + 4 and not decoder.errors
    assert len({page_buffer for page_buffer, _ in buffers}) == 1          # the page stays where it is, and a part
    assert all(part and part != page_buffer for page_buffer, part in buffers)      # never goes into its buffer
    assert all(a[1] != b[1] for a, b in zip(buffers, buffers[1:]))        # nor into the last part's, which is on show
    # Every decode went into a frame buffer: the page whole, the part at its place. (A bit of a picture
    # would be decoded into RAM first and kept there by its address: the patch redraws the page whole
    # instead, when the part appears or moves.)
    assert decoded(cooler, since) == {(480, 640, 0, 0), (480, 272, 0, 176)}

    small, since = part_of("#e0e000", (160, 96), (20, 20, 80, 80)), len(decoder.decoded)
    driver.draw_jpeg(small, at=(304, 16))                                 # somewhere else, another size:
    cooler.run_for(0.3)
    assert on_screen(cooler, with_part(page, small, (304, 16)), tmp_path)    # the first one's place shows the page
    now = decoded(cooler, since)                                          # (the last part may have been drawn once
    assert now >= {(480, 640, 0, 0), (160, 96, 304, 16)} and "elsewhere" not in now      # more while this one came in)

    other, since = card("#205050", (100, 100, 300, 300)), len(decoder.decoded)
    driver.draw_jpeg(other)                                               # a new page: the part is gone
    cooler.run_for(0.3)
    assert on_screen(cooler, other, tmp_path) and state(cooler)["part"] == 0
    cooler.run_for(0.3)
    assert "elsewhere" not in decoded(cooler, since) and not decoder.errors


def test_a_part_needs_a_page_of_ours_under_it(cooler, tmp_path):
    driver = cooler.driver()
    page, part = card("#404080", (60, 60, 420, 160)), part_of("#ffffff")
    driver.draw_jpeg(page)
    cooler.run_for(0.3)
    driver.draw_jpeg(part, at=(0, 176))
    cooler.run_for(0.3)
    assert state(cooler)["part"] != 0
    stored = next(iter(on_card(cooler).values()))
    driver.upload_jpeg(LOOP_ID, card("#101010", (0, 0, 20, 20)))          # the stock firmware shows a picture of its
    cooler.run_for(1.0)                                                   # own: the part must not stay on top of it
    driver.show_slot(0)
    cooler.run_for(0.6)
    assert state(cooler)["part"] == 0 and on_screen(cooler, stored, tmp_path)
    sent = cooler.machine.usb.delivered
    with pytest.raises(device.NoPage):
        driver.draw_jpeg(part, at=(0, 176))
    assert cooler.machine.usb.delivered == sent + 1                       # the question, and no picture data after it
    driver.draw_jpeg(page)                                                # the page again, and the part is taken
    cooler.run_for(0.3)
    driver.draw_jpeg(part, at=(0, 176))
    cooler.run_for(0.3)
    assert on_screen(cooler, with_part(page, part, (0, 176)), tmp_path)

    with pytest.raises(ValueError):                                       # the driver: not past the screen's edge,
        driver.draw_jpeg(part, at=(0, 400))                               # which would be past the frame buffer's end
    with pytest.raises(ValueError):
        driver.draw_jpeg(part_of("#ffffff", (480, 270)), at=(0, 0))       # and whole blocks only


def test_memory_can_be_read(cooler, stock_image):
    """Version 4: command 21 gives up to 39 bytes of the cooler's memory a reply."""
    driver = cooler.driver()
    assert driver.draw_version() >= device.PEEK_VERSION
    assert driver.peek(patch.APP, 8) == stock_image[:8]                   # the stack pointer and the reset handler
    assert driver.peek(patch.APP + 0x1000, 500) == stock_image[0x1000:0x1000 + 500]     # more than one reply holds
    at = patch.APP + len(stock_image)
    assert driver.peek(at, 64) == patch.assemble(at)[0][:64]              # the patch itself
    raw = driver.peek(patch.STATE, patch.STATE_SIZE)                      # RAM: the patch's own state
    assert raw == bytes(cooler.machine.uc.mem_read(patch.STATE, patch.STATE_SIZE))
    assert driver.peek(patch.APP, 0) == b""
    for address, count in ((0x4000_0000, 4), (0x0007_FFFF, 2), (0x7000_0000, 16), (0x2000_FFF0, 32)):
        with pytest.raises(ValueError):                                   # registers, the end of the flash, the
            driver.peek(address, count)                                   # serial flash's window: not asked for
    assert driver.uuid() == ""                                            # and the other commands go on as before


def test_stock_firmware_is_not_asked_for_its_memory_twice():
    cooler = bench.EmulatedCooler()
    with pytest.raises(RuntimeError, match="cannot be asked"):
        cooler.driver().peek(patch.APP, 8)


def test_our_picture_is_on_the_start_screen(stock_image, tmp_path):
    """Versions 5 and 6. The cooler shows DeepCool's logo for four seconds when it starts, a picture from a
    file nothing reaches, and then again on the screen where it waits for a host: for as long as the PC
    takes to start. The patch puts its own over both, the first before anything is drawn."""
    from PIL import Image, ImageChops

    machine = boot.Machine(patch.build(stock_image), None, flash=bench.fat.build({"ui.bin": bench.uibin.stock()}))
    machine.sd.load(bench.fat.build({}, megabytes=64, cluster_sectors=8, label="COOLER"))
    machine.block_budget = 0
    main_hook = patch.assemble(patch.APP + len(stock_image))[1]["main_hook"]
    logo = Image.open(io.BytesIO(patch.logo())).convert("RGB")

    def differs() -> int:
        boot.screenshot(machine, tmp_path / "start.png")
        return max(high for _, high in ImageChops.difference(Image.open(tmp_path / "start.png").convert("RGB"), logo).getextrema())

    while not machine.blocks[main_hook]:                                  # until the main loop comes round first
        machine.run(blocks=20_000)
        assert sum(machine.blocks.values()) < bench.BOOT_LIMIT and machine.stuck is None
    assert not machine.jpeg.decoded                                       # nothing has been drawn before that
    machine.run(blocks=200_000)
    assert not machine.uc.mem_read(bench.READY, 1)[0]                     # the start screen is on,
    assert differs() <= 7                                                 # and it is our picture, all of it
    assert [(width, height) for _, width, height in machine.jpeg.decoded] == [(480, 640)] and not machine.jpeg.errors
    assert machine.jpeg.decoded[0][0] in FRAME_BUFFERS                    # decoded straight into the frame buffer
    assert bytes(machine.uc.mem_read(patch.STATE + 60, 1)) == b"\x01"     # so far the start screen only
    while not machine.uc.mem_read(bench.READY, 1)[0]:
        machine.run(blocks=250_000)
        assert sum(machine.blocks.values()) < bench.BOOT_LIMIT and machine.stuck is None
    machine.run(blocks=400_000)                                           # four seconds later the firmware goes on,
    assert machine.stuck is None and differs() <= 7                       # to its idle screen: our picture again
    assert bytes(machine.uc.mem_read(patch.STATE + 60, 1)) == b"\x03"
    assert {(width, height) for _, width, height in machine.jpeg.decoded} == {(480, 640)} and not machine.jpeg.errors
    assert all(address in FRAME_BUFFERS for address, _, _ in machine.jpeg.decoded)       # the pictures under it: never
    idle = struct.unpack("<I", bytes(machine.uc.mem_read(patch.IDLE_SCREEN, 4)))[0]
    children, child = 0, struct.unpack("<I", bytes(machine.uc.mem_read(idle + 8, 4)))[0]
    while child:
        children, child = children + 1, struct.unpack("<I", bytes(machine.uc.mem_read(child + 0x64, 4)))[0]
    assert children == 5                                                  # its four pictures, and ours: put there once


def test_the_screen_can_be_read_back_from_the_cooler(cooler, tmp_path):
    """tools/screen.py: the frame buffer that is on the glass, through command 21. What a JPEG became,
    pixel for pixel, which is how tuned Huffman tables and parts were checked on the real cooler."""
    from PIL import Image, ImageChops

    sys.path.insert(0, str(ROOT / "tools"))
    import screen

    driver = cooler.driver()
    shown, drawing = screen.buffers(driver)
    assert {shown, drawing} == set(FRAME_BUFFERS)                         # LVGL's own record of them
    jpeg = card("#307050", (40, 300, 440, 600))
    driver.draw_jpeg(jpeg)
    cooler.run_for(0.3)
    lines = (100, 450, 590)                                               # (a line is 25 questions: slow in here)
    raw = screen.read(driver, lines)
    assert len(raw) == len(lines) * 960
    wanted = Image.open(io.BytesIO(jpeg)).convert("RGB")
    got = screen.picture(raw)
    for i, y in enumerate(lines):
        difference = ImageChops.difference(got.crop((0, i, 480, i + 1)), wanted.crop((0, y, 480, y + 1)))
        assert max(high for _, high in difference.getextrema()) <= 7      # (RGB565)
    tuned = device.encode_jpeg(Image.open(io.BytesIO(jpeg)), quality=80, tuned=True)
    plain = device.encode_jpeg(Image.open(io.BytesIO(jpeg)), quality=80)
    seen = []
    for data in (plain, tuned):
        driver.draw_jpeg(data)
        cooler.run_for(0.3)
        seen.append(screen.read(driver, lines))
    assert seen[0] == seen[1] and len(tuned) < len(plain)                 # the same picture from fewer bytes
