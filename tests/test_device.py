"""Protocol encoding checked against packets captured from the official DeepCool app."""
import pytest

from deephack import device

# "get uuid" and "show jpg slot 1" exactly as the official app sent them
GET_UUID = bytes.fromhex("aa2e12" + "00" * 39 + "48494443" + "0202")
SHOW_SLOT_1 = bytes.fromhex("aa2e080001" + "00" * 37 + "48494443" + "f901")
SET_TIME = bytes.fromhex("aa2e0aea070b12112618" + "00" * 32 + "48494443" + "5703")
# upload header for a 52395 byte JPEG whose bytes sum to 0x6f7e7b
HEADER = bytes.fromhex(
    "44434c6401abcc00007b7e0000000000000000003966353830316337346164663433616438343461306631373263613065653431"
    "000000000000000000008e0c"
)


def test_command_frames_match_capture():
    assert device.frame(device.CMD_GET_UUID) == GET_UUID
    assert device.frame(device.CMD_SHOW_SLOT, bytes([device.TYPE_JPG, 1])) == SHOW_SLOT_1
    assert device.frame(device.CMD_SET_TIME, bytes.fromhex("ea070b12112618")) == SET_TIME


def test_frame_layout():
    f = device.frame(0x03, b"\x02")
    assert len(f) == 48 and f[0] == 0xAA and f[1] == 0x2E and f[42:46] == b"HIDC"
    assert int.from_bytes(f[46:], "little") == sum(f[:46]) & 0xFFFF


def test_upload_header_matches_capture():
    # stand-in for the captured JPEG with the same length and byte sum: 28654 x 0xFF, one 0x69, rest zero
    data = b"\xff" * 28654 + b"\x69" + b"\x00" * (52395 - 28655)
    assert sum(data) == 0x6F7E7B
    assert device.upload_header("9f5801c74adf43ad844a0f172ca0ee41", data) == HEADER


def test_upload_header_refuses_the_firmware_update_type():
    with pytest.raises(ValueError, match="firmware update"):
        device.upload_header("0" * 32, b"\xff\xd8\xff\xd9", device.FILE_FIRMWARE)


def test_upload_header_rejects_bad_id():
    try:
        device.upload_header("short", b"")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_encode_jpeg_is_what_the_firmware_expects():
    from PIL import Image

    jpeg = device.encode_jpeg(Image.new("RGB", (100, 100), (200, 30, 30)))
    assert jpeg[:2] == b"\xff\xd8" and jpeg[-2:] == b"\xff\xd9"
    import io

    im = Image.open(io.BytesIO(jpeg))
    assert im.size == (device.WIDTH, device.HEIGHT)
    assert not im.info.get("progressive")
    assert [layer[1:3] for layer in im.layer] == [(2, 2), (1, 1), (1, 1)]  # 4:2:0
