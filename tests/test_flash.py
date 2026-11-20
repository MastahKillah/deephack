"""The firmware update sender (deephack.firmware, which tools/fw/flash.py and the app use) against the
emulated stock firmware: the image must arrive as FW_update.bin, byte for byte, and the firmware must
then restart; an update that arrives damaged or not at all must leave nothing behind. What the
bootloader does with the file is beyond the emulator. Nothing here touches a real cooler."""
import os
import struct
import sys
import time
from pathlib import Path

import pytest

from deephack import device, firmware

pytest.importorskip("unicorn")
pytest.importorskip("keystone")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "emu"))
sys.path.insert(0, str(ROOT / "tools" / "fw"))
import bench  # noqa: E402
import boot  # noqa: E402
import flash  # noqa: E402
import patch  # noqa: E402

pytestmark = pytest.mark.skipif(not boot.DEFAULT_IMAGE.exists(), reason="DeepCool's firmware image is not installed")
WDT_CR = 0x400A0808
UPLOAD_STATE = 0x200006D4                # the firmware's upload state machine: 0 waits for a header, 1 takes data


@pytest.fixture(scope="module", autouse=True)
def quiet_emulator():
    import faulthandler

    was_on = faulthandler.is_enabled()
    faulthandler.disable()
    yield
    if was_on:
        faulthandler.enable()


@pytest.fixture(scope="module")
def stock() -> bytes:
    return boot.DEFAULT_IMAGE.read_bytes()


def test_version_is_read_the_way_deepcool_counts():
    assert flash.version(0x0246) == "2.70" and flash.version(0x0247) == "2.71"


def test_header_is_an_upload_header_of_type_0(stock):
    header = flash.update_header(stock)
    assert header[:5] == b"DCLd\0" and len(header) == 64
    assert struct.unpack_from("<II", header, 5) == (len(stock), device.sum16(stock))
    assert struct.unpack_from("<H", header, 62)[0] == device.sum16(header[:62])


def test_images_that_cannot_be_firmware_are_refused(stock):
    flash.check_image(stock)
    flash.check_image(patch.build(stock))
    with pytest.raises(ValueError, match="vector table"):
        flash.check_image(b"\xff\xd8" + bytes(200_000))                    # a picture
    with pytest.raises(ValueError, match="size"):
        flash.check_image(stock[:4096])
    eaten = bytearray(stock)
    eaten[64 * 100:64 * 100 + 2] = b"\xaa\x2e"
    with pytest.raises(ValueError, match="AA 2E"):
        flash.check_image(bytes(eaten))


@pytest.mark.parametrize("which", ["short", "stock", "patched"])
def test_the_firmware_stores_the_image_and_restarts(stock, which):
    """The whole images take over a minute each in the emulator: DEEPHACK_FULL_FLASH_TEST=1 runs them
    (both passed on 19 Nov 2026). Otherwise the first 96 KiB of the stock image stand in."""
    if which != "short" and not os.environ.get("DEEPHACK_FULL_FLASH_TEST"):
        pytest.skip("set DEEPHACK_FULL_FLASH_TEST=1 to send a whole image")
    image = {"short": stock[:96 * 1024], "stock": stock}.get(which) or patch.build(stock)
    cooler = bench.EmulatedCooler()
    driver = cooler.driver()
    real_sleep, time.sleep = time.sleep, lambda seconds: cooler.run_for(seconds)
    try:
        flash.send_update(driver, image)
    finally:
        time.sleep = real_sleep
    cooler.run_for(1.0)
    files = {name.lower(): content for name, content in cooler.files().items()}
    assert files == {"fw_update.bin": image}
    assert any(value & 1 for value in cooler.machine.writes[WDT_CR])       # the watchdog that restarts the chip


def test_an_update_that_goes_wrong_is_not_flashed_and_can_be_called_off(stock):
    """Three updates that go wrong, on one cooler, and a picture upload after them. In each case nothing
    may be stored and nothing may restart; "dcldfinish" must end the upload, or the firmware would take
    the next upload for the rest of the file (it did, when this was tried without)."""
    image = stock[:68 * 1024]
    cooler = bench.EmulatedCooler()
    driver, machine = cooler.driver(), cooler.machine
    driver.set_mode(device.MODE_MOTION)

    def send(header: bytes, data: bytes) -> None:
        driver.command(device.CMD_START_DOWNLOAD)
        driver.dev.write(1, header)
        for at in range(0, len(data), 64):
            driver.dev.write(1, data[at:at + 64])
        cooler.run_for(1.0)

    def untouched() -> bool:
        return not cooler.files() and not any(value & 1 for value in machine.writes[WDT_CR])

    def call_off() -> None:
        assert machine.uc.mem_read(UPLOAD_STATE, 1)[0] == 1          # it is still waiting for the file
        driver.dev.write(1, firmware.FINISH)
        cooler.run_for(0.5)
        assert machine.uc.mem_read(UPLOAD_STATE, 1)[0] == 0 and untouched()

    wrong = bytearray(firmware.update_header(image))                 # 1. the header announces another sum
    struct.pack_into("<I", wrong, 9, (device.sum16(image) + 1) & 0xFFFF)
    struct.pack_into("<H", wrong, 62, device.sum16(wrong[:62]))
    send(bytes(wrong), image)
    assert untouched()
    call_off()

    damaged = bytearray(image)                                       # 2. one byte changes on the way
    damaged[40_000] ^= 0x10
    send(firmware.update_header(image), bytes(damaged))
    assert untouched()
    call_off()

    def stop_half_way(sent: int) -> None:                            # 3. the sender stops: it calls the upload off itself
        if sent >= len(image) // 2:
            raise InterruptedError("enough")

    real_sleep, time.sleep = time.sleep, lambda seconds: cooler.run_for(seconds)
    try:
        with pytest.raises(firmware.SendFailed) as failed:
            firmware.send_update(driver, image, stop_half_way)
    finally:
        time.sleep = real_sleep
    cooler.run_for(0.5)
    assert failed.value.ended and 0 <= failed.value.sent - len(image) // 2 < 0x1000
    assert machine.uc.mem_read(UPLOAD_STATE, 1)[0] == 0 and untouched()

    from PIL import Image

    picture = device.encode_jpeg(Image.new("RGB", (480, 640), "#106060"))
    driver.upload_jpeg("f" * 32, picture)                            # and the next upload is a picture again
    cooler.run_for(1.0)
    assert {name.lower(): content for name, content in cooler.files().items()} == {f"img/000_{'f' * 32}/angle_0.jpg": picture}
    assert not any(value & 1 for value in machine.writes[WDT_CR])
