"""The model itself: the real driver passes it, and it catches what is known to go wrong."""
import io
import struct

from PIL import Image

from deephack import device
from deephack.config import GIF_SLOT_ID, SLOT_ID

JPEG = device.encode_jpeg(Image.new("RGB", (480, 640), "red"))


def test_the_driver_speaks_the_protocol(cooler):
    with device.Mystique() as m:
        assert m.uuid() == cooler.uuid
        m.set_mode(device.MODE_MOTION)
        m.upload_jpeg(SLOT_ID, JPEG)
        m.show_slot(1, device.TYPE_JPG)
        m.upload_gif(GIF_SLOT_ID, [JPEG, device.encode_jpeg(Image.new("RGB", (480, 640), "blue"))], 1000)
        m.show_slot(0, device.TYPE_GIF)
        m.upload_jpeg(SLOT_ID, device.encode_jpeg(Image.new("RGB", (480, 640), (0, 255, 0))))  # same id: in place
        m.set_options(70, device.LIGHT_SCREEN)
        m.send_sensors(cpu_temp=55)
        m.set_time()
    assert len(cooler.stills) == 2 and cooler.still(1).getpixel((10, 10))[1] > 200        # replaced, not added
    assert len(cooler.gifs) == 1 and len(cooler.gifs[0][1]) == 2 and cooler.gifs[0][2] == 1000
    assert cooler.screen == ("gif", 0) and cooler.flash_writes == 4 and cooler.light == device.LIGHT_SCREEN
    assert cooler.sensors[:3] == bytes([55, 0, 0]) and cooler.clock[0] >= 2026


def _violations(cooler, action):
    with device.Mystique() as m:
        try:
            action(m)
        except RuntimeError:
            pass                                    # the driver also notices some of these
    found, cooler.violations[:] = list(cooler.violations), []
    return found


def test_it_catches_an_unfinished_upload(cooler):
    def unfinished(m):
        m.command(device.CMD_START_DOWNLOAD)
        m.dev.write(1, device.upload_header(SLOT_ID, JPEG), 1000)
        m.dev.write(1, JPEG[:64], 1000)
        m.set_mode(device.MODE_MOTION)              # a command before dcldfinish
    assert any("before dcldfinish" in v for v in _violations(cooler, unfinished))


def test_it_catches_bad_headers_and_data(cooler):
    def bad_sum(m):
        m.command(device.CMD_START_DOWNLOAD)
        header = bytearray(device.upload_header(SLOT_ID, JPEG))
        struct.pack_into("<I", header, 9, 1234)     # wrong data sum, header checksum fixed up
        struct.pack_into("<H", header, 62, device.sum16(header[:62]))
        m.dev.write(1, bytes(header), 1000)
        for off in range(0, len(JPEG), 64):
            m.dev.write(1, JPEG[off:off + 64], 1000)
        m.dev.write(1, b"dcldfinish".ljust(55, b"\0"), 1000)
    assert any("sum16" in v for v in _violations(cooler, bad_sum))

    def bad_header(m):
        m.command(device.CMD_START_DOWNLOAD)
        header = bytearray(device.upload_header(SLOT_ID, JPEG))
        header[62] ^= 1
        m.dev.write(1, bytes(header), 1000)
    assert any("header checksum" in v for v in _violations(cooler, bad_header))
    cooler._upload = None


def test_it_catches_wrong_pictures_and_slots(cooler):
    buf = io.BytesIO()
    Image.new("RGB", (240, 320), "red").save(buf, "JPEG")           # encode_jpeg would scale it to fit
    small = buf.getvalue()
    assert any("480x640" in v for v in _violations(cooler, lambda m: m.upload_jpeg(SLOT_ID, small)))
    assert any("only 2 are stored" in v for v in _violations(cooler, lambda m: (m.set_mode(device.MODE_MOTION), m.show_slot(5))))
    assert any("image mode" in v for v in _violations(cooler, lambda m: (m.set_mode(device.MODE_SITUATIONAL), m.show_slot(0))))


def test_it_catches_the_destructive_commands(cooler):
    assert any("erases" in v for v in _violations(cooler, lambda m: m.command(device.CMD_DELETE_ALL)))
    assert any("erases" in v for v in _violations(cooler, lambda m: m.command(device.CMD_FACTORY_RESET)))


def test_unknown_commands_are_refused_like_the_firmware_does(cooler):
    with device.Mystique() as m:
        try:
            m.command(0x20)
        except RuntimeError as e:
            assert "unexpected reply" in str(e)
        else:
            raise AssertionError("expected the driver to reject the reply")
