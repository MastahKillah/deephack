"""The firmware update sender (tools/fw/flash.py) against the emulated stock firmware: the image must
arrive as FW_update.bin, byte for byte, and the firmware must then restart. What the bootloader does
with the file is beyond the emulator. Nothing here touches a real cooler."""
import os
import struct
import sys
import time
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
import flash  # noqa: E402
import patch  # noqa: E402

pytestmark = pytest.mark.skipif(not boot.DEFAULT_IMAGE.exists(), reason="DeepCool's firmware image is not installed")
WDT_CR = 0x400A0808


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
