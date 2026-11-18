from deephack import device
from deephack.modes import tamagotchi as pet


def test_gif_frame_header_matches_capture():
    # second frame of a 4 frame, 1000 ms animation as sent by the official app: 13687 bytes, byte sum 0x..dfc7
    captured = bytes.fromhex(
        "44434c6402773500 00c7df0000 0400 e803 0100 00".replace(" ", "")
        + b"f2a3ed8f59684ad688cc02ef8e2d5a80".hex() + "00" * 10 + "ad0d"
    )
    data = b"\xff" * 224 + b"\xa7" + b"\x00" * (13687 - 225)
    assert sum(data) & 0xFFFF == 0xDFC7
    header = device.upload_header("f2a3ed8f59684ad688cc02ef8e2d5a80", data, device.FILE_GIF, frame_count=4, duration_ms=1000, frame_index=1)
    assert header == captured


def test_still_header_unchanged_by_frame_fields():
    assert device.upload_header("0" * 32, b"\x01")[13:19] == b"\x00" * 6


def test_moods():
    assert [pet.mood_for(p) for p in (None, 0, 49, 50, 79, 80, 94, 95, 100)] == \
        ["happy", "happy", "happy", "fine", "fine", "stressed", "stressed", "exhausted", "exhausted"]


def test_animation_shape_and_motion():
    anim = pet.render_animation({"seven_day": (27, None)})
    assert len(anim.frames) == pet.FRAMES and anim.duration_ms <= device.MAX_GIF_DURATION_MS
    assert all(f.size == (device.WIDTH, device.HEIGHT) for f in anim.frames)
    assert anim.frames[0].tobytes() != anim.frames[2].tobytes()          # it actually moves
    for limits in (None, {"five_hour": (100, None), "seven_day": (97, None)}, {"seven_day": (88, None)}):
        assert len(pet.render_animation(limits).frames) == pet.FRAMES
