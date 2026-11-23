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


# ------------------------------------------------------------------ drawn pictures (the direct drawing firmware)
import random
from types import SimpleNamespace


class _Sound:
    bass = rest = 0.0

    def levels(self):
        return self.bass, self.rest


def _drawn_pet(monkeypatch, reacts="working,music,night,-nba", team="all"):
    """A pet whose pictures are drawn, in a world the test controls. Its pictures are (state, frame, limits, how)."""
    world = SimpleNamespace(busy=False, playing=False, night=False, clock=100.0, limits={"seven_day": (40, None)}, sound=_Sound())

    async def is_busy(self):
        return world.busy

    async def is_playing(self):
        return world.playing

    monkeypatch.setattr(pet.Activity, "busy", is_busy)
    monkeypatch.setattr(pet.Playback, "playing", is_playing)
    monkeypatch.setattr(pet, "read_limits", lambda path: world.limits)
    monkeypatch.setattr(pet, "asleep", lambda hour, bedtime: world.night)
    monkeypatch.setattr(pet, "render_frame", lambda f, limits, state, result, **how: (state, f, limits, how))
    monkeypatch.setattr(pet.time, "monotonic", lambda: world.clock)
    m = pet.Tamagotchi({"reacts": reacts}, team=team, listener=world.sound)
    m.direct = True
    m._rng = random.Random(1)

    def step(seconds=pet.FAST_TICK):
        world.clock += seconds
        return asyncio.run(m.frame())

    def run(seconds, tick=pet.FAST_TICK):
        return [p for p in (step(tick) for _ in range(round(seconds / tick))) if p is not None]

    return m, world, step, run


def test_beats_are_jumps_of_a_level():
    beat = pet.Beat()
    heard = [beat.feed(bass, 0.0, i * 0.1) for i, bass in enumerate((0.0, 1.0, 0.75, 0.56, 0.42, 0.9, 0.95, 0.7, 0.1, 0.25, 0.2))]
    assert heard == [0, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2]        # 0.1 to 0.25 is a jump, but not to a beat's level
    assert beat.at == 5 * 0.1
    beat = pet.Beat()
    assert [beat.feed(bass, 0.0, i * 0.1) for i, bass in enumerate((1.0, 0.4, 1.0, 0.4, 0.4, 1.0))] == [1, 1, 1, 1, 1, 2]   # too soon
    beat = pet.Beat()                                         # a held bass note, and the drums above it
    assert [beat.feed(0.8, rest, i * 0.2) for i, rest in enumerate((0.2, 0.2, 0.7, 0.4, 0.3, 0.3, 0.6))] == [1, 1, 2, 2, 2, 2, 3]
    assert beat.hop == 0.6
    beat.feed(0.8, 0.5, 1.2 + pet.HOP_FALL)
    assert abs(beat.hop - 0.3) < 1e-9                         # and down again, whatever the levels do


def test_drawn_pet_reacts_at_once(monkeypatch):
    m, world, step, run = _drawn_pet(monkeypatch)
    assert step()[0] == "happy" and m.tick == pet.SLOW_TICK
    world.playing = True
    assert run(pet.MUSIC_CHECK_DRAWN + 0.1)[-1][0] == "dancing"        # no waiting for the music to settle
    assert m.tick == pet.FAST_TICK and m.status.startswith("Dancing")
    world.playing = False
    assert run(0.5)[-1][0] == "dancing"                                # a short pause is not the end of it
    assert run(pet.MUSIC_LINGER_DRAWN + pet.MUSIC_CHECK_DRAWN)[-1][0] == "happy"
    world.busy = True
    assert step()[0] == "working" and m.active
    assert m._activity.every == pet.BUSY_CHECK_DRAWN


def test_drawn_pet_dances_to_the_beat(monkeypatch):
    m, world, step, run = _drawn_pet(monkeypatch)
    world.playing = True
    looped = run(1.0)
    assert looped[-1][0] == "dancing" and looped[-1][3]["pose"] is None   # nothing heard yet: the loop
    world.sound.bass = 1.0
    state, _, _, how = step()
    first = how["pose"]
    assert first.dy == -pet.HOP and how["glow"] == pet.GLOWS - 1 and how["risen"] is not None
    world.sound.bass = 0.95                                           # the note is held: the pet comes down all the same
    falling = step(pet.HOP_FALL)[3]
    assert falling["pose"].dy == -22 and falling["pose"].dx == first.dx and falling["glow"] == how["glow"]
    world.sound.bass = 0.0
    assert step()[3]["glow"] == 0
    run(pet.BEAT_GAP)
    world.sound.rest = 0.9                                            # a beat in the rest of the sound counts too
    assert step()[3]["pose"].dx != first.dx                           # the next beat is the next step
    world.sound.bass = world.sound.rest = 0.0
    assert run(pet.BEATLESS + 0.5)[-1][3]["pose"] is None             # music without a beat: the loop again


def test_drawn_percentages_count_to_the_new_value(monkeypatch):
    m, world, step, run = _drawn_pet(monkeypatch)
    assert step()[2] == {"seven_day": (40, None)}
    world.limits = {"seven_day": (43, None)}
    world.clock += pet.LIMITS_CHECK_DRAWN
    shown = [p[2]["seven_day"][0] for p in (step(), step(), step())]
    assert shown == [41, 42, 43] and m.tick == pet.SLOW_TICK          # fast only while it counted
    world.limits = {"five_hour": (5, None), "seven_day": (43, None)}
    world.clock += pet.LIMITS_CHECK_DRAWN
    assert step()[2] == {"five_hour": (5, None), "seven_day": (43, None)}    # a new window is there at once
    world.limits = None
    world.clock += pet.LIMITS_CHECK_DRAWN
    assert step()[2] is None


def test_drawn_pet_yawns_before_it_sleeps(monkeypatch):
    m, world, step, run = _drawn_pet(monkeypatch)
    step()
    world.night = True
    pictures = run(len(pet.YAWN) * pet.STEP + 1.0, pet.SLOW_TICK)
    states = [p[0] for p in pictures]
    assert states[0] == "yawning" and states[-1] == "sleeping" and "yawning" not in states[states.index("sleeping"):]
    assert max(p[3]["pose"].mouth for p in pictures if p[0] == "yawning") == 2
    assert len({p[3]["pose"] for p in pictures if p[0] == "yawning"}) == len(set(pet.YAWN))
    m, world, step, run = _drawn_pet(monkeypatch)
    world.night = True
    assert step()[0] == "sleeping"                                    # it was asleep already when the mode started


def test_drawn_pet_blinks_and_waves_now_and_then(monkeypatch):
    m, world, step, run = _drawn_pet(monkeypatch)
    pictures = run(60.0, pet.SLOW_TICK)
    assert all(p[0] == "happy" for p in pictures)
    closed = sum(p[3]["pose"].eyes == "closed" for p in pictures)
    waving = sum(p[3]["pose"].arms != (0, 0) for p in pictures)
    assert 5 <= closed < len(pictures) / 8 and 8 <= waving < len(pictures) / 4
    world.limits = {"seven_day": (70, None)}                          # "fine" blinks too, but does not wave
    pictures = [p for p in run(60.0, pet.SLOW_TICK) if p[0] == "fine"]
    assert any(p[3]["pose"].eyes == "closed" for p in pictures) and all(p[3]["pose"].arms == (0, 0) for p in pictures)


def test_drawn_pet_returns_nothing_while_nothing_moves(monkeypatch):
    m, world, step, run = _drawn_pet(monkeypatch)
    world.busy = True                                                 # working: a loop of 8 frames in 2 s
    pictures = run(4.0, pet.SLOW_TICK)
    assert 15 <= len(pictures) <= 17 and [p[1] for p in pictures[:9]] == [0, 1, 2, 3, 4, 5, 6, 7, 0]


def test_drawn_frames_render():
    limits = {"five_hour": (12, None), "seven_day": (97, None)}
    plain = pet.render_frame(0, limits, "dancing")
    lit = pet.render_frame(0, limits, "dancing", pose=pet.dance_pose(3, 1.0, pet.BODY), risen=120, glow=pet.GLOWS - 1)
    assert lit.size == plain.size == (device.WIDTH, device.HEIGHT) and lit.tobytes() != plain.tobytes()
    assert lit.getpixel((2, 2)) != plain.getpixel((2, 2))             # the edges, which the cooler's light follows
    assert pet.render_frame(0, limits, "dancing", glow=0).tobytes() == plain.tobytes()
    yawns = {pet.render_frame(0, limits, "yawning", pose=pet.yawn_pose(i, pet.BODY)).tobytes() for i in range(len(pet.YAWN))}
    assert len(yawns) == len(set(pet.YAWN))
    assert pet.render_frame(0, limits, "yawning", pose=pet.yawn_pose(0, pet.BODY)).getpixel((2, 2)) == \
        pet.render_frame(0, limits, "sleeping").getpixel((2, 2))


def _event(hours_ago, state, home_score, away_score):
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return nba.parse_event({
        "date": when.strftime("%Y-%m-%dT%H:%MZ"), "status": {"type": {"state": state, "shortDetail": ""}},
        "competitions": [{"competitors": [
            {"homeAway": "home", "winner": state == "post" and home_score > away_score, "score": str(home_score), "team": {"abbreviation": "GS", "color": "1d428a"}},
            {"homeAway": "away", "winner": state == "post" and away_score > home_score, "score": str(away_score), "team": {"abbreviation": "MEM", "color": "000000"}},
        ]}]})


def test_drawn_pet_watches_the_game_for_its_final(monkeypatch):
    asked = []
    live = _event(1, "in", 60, 58)
    final = [None]
    monkeypatch.setattr(nba, "fetch_games", lambda team: asked.append("schedule") or [live])
    monkeypatch.setattr(nba, "fetch_live", lambda team, game: asked.append("live") or final[0] or live)
    m, world, step, run = _drawn_pet(monkeypatch, reacts="nba,-working,-music,-night", team="GS")
    assert step()[0] == "happy" and asked == ["schedule"]
    world.clock += pet.NBA_LIVE_CHECK
    step()
    assert m.status.startswith("Feeling happy") and asked == ["schedule", "live"]     # still playing
    final[0] = _event(1, "post", 118, 110)
    world.clock += pet.NBA_LIVE_CHECK
    assert step()[0] == "cheering" and m.status == "Celebrating GS 118-110 MEM · weekly 40%"
    world.clock += pet.NBA_LIVE_CHECK
    step()
    assert asked == ["schedule", "live", "live"]                      # and back to the schedule's own pace


def test_stored_pet_is_as_before_after_drawing(monkeypatch):
    m, world, step, run = _drawn_pet(monkeypatch)
    monkeypatch.setattr(pet, "render_animation", lambda limits, state, result: ("stored", state))
    world.playing = True
    assert run(1.0)[-1][0] == "dancing"
    m.direct = False                                                  # e.g. the slide show was switched on
    assert step(pet.MUSIC_CHECK) == ("stored", "dancing") and m.tick is None
    assert step(1.0) is None and m._activity.every == pet.CHECK_SECONDS


# ------------------------------------------------------------------ the song it dances to
def test_the_song_is_written_above_the_dancing_pet():
    limits = {"seven_day": (40, None)}
    plain = pet.render_frame(0, limits, "dancing")
    named = pet.render_frame(0, limits, "dancing", song=("Blinding Lights", "The Weeknd"))
    top, rest = (0, 0, 480, 110), (0, 120, 480, 640)
    assert named.crop(top).tobytes() != plain.crop(top).tobytes()                     # above the pet ...
    assert named.crop(rest).tobytes() == plain.crop(rest).tobytes()                   # ... and nowhere else
    assert pet.render_frame(0, limits, "dancing", song=("", "")).tobytes() == plain.tobytes()
    other = pet.render_frame(0, limits, "dancing", song=("Numb", "Linkin Park"))
    assert other.crop(top).tobytes() != named.crop(top).tobytes()
    for state in ("working", "sleeping", None):                                        # only while it dances
        assert pet.render_frame(0, limits, state, song=("Numb", "Linkin Park")).tobytes() == pet.render_frame(0, limits, state).tobytes()


def test_a_long_title_gets_smaller_letters_and_then_dots():
    short, long, longer = (pet._song(title, "Queen") for title in ("Numb", "Bohemian Rhapsody, the 2011 one", "Bohemian Rhapsody " * 6))
    height = lambda img: img.crop((0, 0, 480, 40)).getbbox()[3]                        # of the title's letters
    assert height(short) > height(long)                                                # three times, then twice the size
    for img in (short, long, longer):
        left, _, right, bottom = img.getbbox()
        assert left >= 8 and right <= 472 and bottom <= 100                            # in the picture, above the pet
    assert pet._song("Numb", "").getbbox()[3] < pet._song("Numb", "Linkin Park").getbbox()[3]     # no artist: one line
    for other_letters in ("Дорогие мои москвичи", "Zdob și Zdub", "夜に駆ける", "봄날"):       # letters of other scripts
        assert pet._song(other_letters, "").getbbox() is not None


def test_the_pet_names_the_song_only_when_pictures_cost_nothing(monkeypatch):
    world = SimpleNamespace(clock=100.0)

    async def is_playing(self):
        return True

    async def not_busy(self):
        return False

    monkeypatch.setattr(pet.Playback, "playing", is_playing)
    monkeypatch.setattr(pet.Activity, "busy", not_busy)
    monkeypatch.setattr(pet.time, "monotonic", lambda: world.clock)
    monkeypatch.setattr(pet, "read_limits", lambda path: {"seven_day": (40, None)})
    mode = pet.Tamagotchi({"reacts": "music"}, listener=_Sound())
    mode.direct = True
    mode._playback.title, mode._playback.artist = "Blinding Lights", "The Weeknd"
    first = asyncio.run(mode.frame())
    assert first is not None and mode.status.startswith("Dancing to Blinding Lights · ")
    mode._playback.title, mode._playback.artist = "Numb", "Linkin Park"                # the next track
    world.clock += 0.01
    second = asyncio.run(mode.frame())
    assert second is not None and second.crop((0, 0, 480, 110)).tobytes() != first.crop((0, 0, 480, 110)).tobytes()

    stored = pet.Tamagotchi({"reacts": "music"}, listener=_Sound())                    # DeepCool's firmware: an animation
    stored._playback.title = "Blinding Lights"
    animation = None
    for _ in range(20):                                                                # the music has to have settled
        world.clock += pet.MUSIC_CHECK
        animation = asyncio.run(stored.frame()) or animation
        if stored.status.startswith("Dancing"):
            break
    assert stored.status.startswith("Dancing to Blinding Lights")
    assert animation.frames[0].tobytes() == pet.render_frame(0, {"seven_day": (40, None)}, "dancing").tobytes()
    stored._playback.title = "Numb"                                                    # another song is no reason to store it anew
    world.clock += 10
    assert asyncio.run(stored.frame()) is None


def test_playback_says_what_plays(monkeypatch):
    import sys

    Status = SimpleNamespace(PLAYING="playing", PAUSED="paused")                       # Windows' own is not asked: no test
    monkeypatch.setitem(sys.modules, "winrt.windows.media.control", SimpleNamespace(  # should need a media player
        GlobalSystemMediaTransportControlsSessionManager=None, GlobalSystemMediaTransportControlsSessionPlaybackStatus=Status))

    class Session:
        status, props = Status.PLAYING, SimpleNamespace(title=" Numb ", artist="Linkin Park")

        def get_playback_info(self):
            return SimpleNamespace(playback_status=self.status)

        async def try_get_media_properties_async(self):
            return self.props

    session = Session()
    playback = pet.Playback()
    playback._manager = SimpleNamespace(get_current_session=lambda: session)
    assert asyncio.run(playback.playing()) and (playback.title, playback.artist) == ("Numb", "Linkin Park")
    session.status = Status.PAUSED
    session.props = SimpleNamespace(title="Other", artist="")
    assert not asyncio.run(playback.playing()) and playback.title == "Numb"            # kept while it pauses
    session.status, session.props = Status.PLAYING, None
    assert asyncio.run(playback.playing()) and (playback.title, playback.artist) == ("", "")


# ------------------------------------------------------------------ the album's cover behind the dance
def _a_cover(colour=(40, 160, 220), size=(300, 300)) -> bytes:
    import io

    from PIL import Image

    picture = Image.new("RGB", size, colour)
    picture.paste((250, 240, 60), (0, 0, size[0] // 2, size[1] // 2))
    data = io.BytesIO()
    picture.save(data, "PNG")
    return data.getvalue()


def test_the_pet_dances_in_front_of_the_cover_and_the_bass_still_flashes():
    limits, art = {"seven_day": (40, None)}, _a_cover()
    plain = pet.render_frame(0, limits, "dancing", glow=3)
    quiet, loud = (pet.render_frame(0, limits, "dancing", glow=glow, art=art) for glow in (0, pet.GLOWS - 1))
    assert quiet.size == (480, 640) and quiet.tobytes() != plain.tobytes()
    left, right = quiet.getpixel((60, 250)), quiet.getpixel((420, 400))                # the cover's two colours show
    assert left[0] > left[2] and right[2] > right[0]
    assert quiet.tobytes() != loud.tobytes()                                           # ... and the bass is over them
    purple = lambda c: c[0] + c[2] - 2 * c[1]
    assert purple(loud.getpixel((420, 400))) > purple(quiet.getpixel((420, 400)))
    assert loud.getpixel((2, 300)) != quiet.getpixel((2, 300))                         # at the edges too: the cooler's light
    assert sum(quiet.getpixel((240, 4))) < sum(quiet.getpixel((240, 300)))            # darker where the words are
    wide = pet.render_frame(0, limits, "dancing", art=_a_cover(size=(640, 200)))       # any shape of picture fills it
    assert wide.getpixel((5, 300)) != (0, 0, 0) and wide.getpixel((475, 300)) != (0, 0, 0)
    assert pet.render_frame(0, limits, "dancing", glow=3, art=b"not a picture").tobytes() == plain.tobytes()
    assert pet.render_frame(0, limits, "working", art=art).tobytes() == pet.render_frame(0, limits, "working").tobytes()


def test_the_cover_is_a_setting_and_only_for_pictures_that_cost_nothing(monkeypatch):
    world = SimpleNamespace(clock=100.0)

    async def is_playing(self):
        return True

    async def not_busy(self):
        return False

    monkeypatch.setattr(pet.Playback, "playing", is_playing)
    monkeypatch.setattr(pet.Activity, "busy", not_busy)
    monkeypatch.setattr(pet.time, "monotonic", lambda: world.clock)
    monkeypatch.setattr(pet, "read_limits", lambda path: {"seven_day": (40, None)})
    setting = {s.key: s for s in pet.Tamagotchi.settings}["cover"]
    assert setting.default == "art" and [value for value, _ in setting.choices] == ["art", "plain"]

    def dancing(values, direct=True):
        mode = pet.Tamagotchi({"reacts": "music", **values}, listener=_Sound())
        mode.direct = direct
        mode._playback.art = _a_cover()
        picture = asyncio.run(mode.frame())
        return mode, picture

    with_cover, a = dancing({})
    without, b = dancing({"cover": "plain"})
    assert with_cover._playback.want_art and not without._playback.want_art            # the cover is not even read
    assert a.tobytes() != b.tobytes()
    stored, _ = dancing({}, direct=False)
    assert not stored._playback.want_art                                               # DeepCool's firmware: its own colours

    with_cover._playback.art = _a_cover((200, 40, 40))                                 # the next album
    world.clock += 0.01
    assert asyncio.run(with_cover.frame()).tobytes() != a.tobytes()


def test_a_tracks_cover_is_read_once_and_asked_for_again_while_there_is_none(monkeypatch):
    import sys

    from deephack.modes import now_playing

    Status = SimpleNamespace(PLAYING="playing")
    monkeypatch.setitem(sys.modules, "winrt.windows.media.control", SimpleNamespace(
        GlobalSystemMediaTransportControlsSessionManager=None, GlobalSystemMediaTransportControlsSessionPlaybackStatus=Status))
    reads, covers = [], {"thumb-a": b"cover a", "thumb-b": b"cover b", None: None}

    async def read(ref):
        reads.append(ref)
        return covers[ref]

    monkeypatch.setattr(now_playing, "_read_thumbnail", read)
    props = SimpleNamespace(title="One", artist="A", thumbnail=None)
    session = SimpleNamespace(get_playback_info=lambda: SimpleNamespace(playback_status="playing"))

    async def properties():
        return props

    session.try_get_media_properties_async = properties
    playback = pet.Playback()
    playback._manager = SimpleNamespace(get_current_session=lambda: session)
    look = lambda: asyncio.run(playback.playing())

    assert look() and playback.art is None and reads == []                             # nobody wants the cover: not read
    playback.want_art = True
    assert look() and playback.art is None and reads == [None]                         # the player has none yet
    props.thumbnail = "thumb-a"
    assert look() and playback.art == b"cover a"                                       # ... and now it has
    count = len(reads)
    assert look() and look() and len(reads) == count                                   # read once for a track
    props.title, props.thumbnail = "Two", None
    assert look() and playback.art == b"cover a"                                       # the next track: the old one stays for now
    props.thumbnail = "thumb-b"
    assert look() and playback.art == b"cover b"
    props.title, props.thumbnail = "Three", None                                       # a track without a cover
    for _ in range(pet.Playback.ART_TRIES + 3):
        look()
    assert playback.art is None and reads.count(None) == 2 + pet.Playback.ART_TRIES    # asked a few times, then no more
