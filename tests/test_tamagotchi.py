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


# ------------------------------------------------------------------ reactions
import asyncio
from datetime import datetime, timedelta, timezone

from deephack.modes import auto, nba


def test_bedtime():
    assert [pet.asleep(h, "23-7") for h in (22, 23, 0, 6, 7, 12)] == [False, True, True, True, False, False]
    assert [pet.asleep(h, "0-8") for h in (23, 0, 7, 8)] == [False, True, True, False]


def test_debounce_needs_a_settled_signal_and_lingers():
    flag = pet.Debounce(settle=8, linger=90)
    assert not flag.update(True, 0) and not flag.update(False, 5)       # a blip is not enough
    assert flag.update(True, 10)                                         # seen again within the linger: it was playing
    assert flag.update(False, 95) and not flag.update(False, 101)        # off 90 s after it was last seen
    assert not flag.update(True, 200) and flag.update(True, 208)


def _game(hours_ago, home, away, home_score, away_score):
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return nba.parse_event({
        "date": when.strftime("%Y-%m-%dT%H:%MZ"), "status": {"type": {"state": "post", "shortDetail": "Final"}},
        "competitions": [{"competitors": [
            {"homeAway": "home", "winner": home_score > away_score, "score": str(home_score), "team": {"abbreviation": home, "color": "1d428a"}},
            {"homeAway": "away", "winner": away_score > home_score, "score": str(away_score), "team": {"abbreviation": away, "color": "000000"}},
        ]}]})


def test_team_result():
    win = pet.team_result("GS", [_game(30, "GS", "MEM", 90, 99), _game(3, "GS", "MEM", 118, 110)])
    assert win.won and win.line == "GS 118-110 MEM" and sum(win.color) >= 240       # navy brightened
    loss = pet.team_result("MEM", [_game(3, "GS", "MEM", 118, 110)])
    assert not loss.won and loss.line == "MEM 110-118 GS"
    assert pet.team_result("GS", [_game(nba.GAME_HOURS + pet.RESULT_HOURS + 1, "GS", "MEM", 118, 110)]) is None
    assert pet.team_result("all", [_game(3, "GS", "MEM", 118, 110)]) is None
    assert pet.team_result("GS", []) is None


def test_pick_state_follows_the_order():
    flags = {"working": True, "music": True, "night": True, "nba": True}
    win, loss = pet.Result(True, "", (0, 0, 0)), pet.Result(False, "", (0, 0, 0))
    assert pet.pick_state(["working", "music"], flags, None) == "working"
    assert pet.pick_state(["music", "working"], flags, None) == "dancing"
    assert pet.pick_state(["nba", "night"], flags, win) == "cheering"
    assert pet.pick_state(["nba"], flags, loss) == "sulking"
    assert pet.pick_state(["night"], {"night": False}, None) is None


def test_every_state_animates():
    result = pet.Result(True, "UTAH 128-127 WSH", (255, 199, 44))
    for state in ("working", "dancing", "sleeping", "cheering", "sulking"):
        anim = pet.render_animation({"seven_day": (97, None)}, state, result)
        assert len(anim.frames) == pet.FRAMES and all(f.size == (device.WIDTH, device.HEIGHT) for f in anim.frames)
        assert len({f.tobytes() for f in anim.frames}) > 2, state


def test_pet_writes_only_when_its_state_changes(monkeypatch):
    busy, playing, clock = [False], [False], [0.0]

    async def is_busy(self):
        return busy[0]

    async def is_playing(self):
        return playing[0]

    monkeypatch.setattr(pet.Activity, "busy", is_busy)
    monkeypatch.setattr(pet.Playback, "playing", is_playing)
    monkeypatch.setattr(pet, "read_limits", lambda path: {"seven_day": (40, None)})
    monkeypatch.setattr(pet, "asleep", lambda hour, bedtime: False)
    monkeypatch.setattr(pet, "render_animation", lambda limits, state, result: state or "mood")
    monkeypatch.setattr(pet.time, "monotonic", lambda: clock[0])
    m = pet.Tamagotchi({"reacts": "working,music,-night,-nba"})

    def step(seconds=1.0):
        clock[0] += seconds
        return asyncio.run(m.frame())

    assert step() == "mood" and m.status.startswith("Feeling happy")
    assert step() is None
    busy[0] = True
    assert step() == "working" and m.active
    busy[0], playing[0] = False, True
    assert step() == "mood"                                   # music has to settle first
    assert [step(2.0) for _ in range(5)][-1] == "dancing"
    playing[0] = False
    assert step(30) is None                                   # a pause does not stop the dance
    assert step(pet.MUSIC_LINGER) == "mood"


def test_pet_in_auto_knows_the_nba_team():
    a = auto.Auto({"order": "tamagotchi", "fallback": "weather"}, lambda mode_id: {"nba": {"team": "GS"}}.get(mode_id, {}))
    assert a.children["tamagotchi"].team == "GS"
