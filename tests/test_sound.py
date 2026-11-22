"""Sound for the modes that play a video. No sound is made: a stand-in takes the speaker's place, plays in
real time (or slower) and keeps what it was given. The videos, with a tone in them, are made here."""
import array
import asyncio
import math
import sys
import time
import types
from fractions import Fraction

import pytest
from PIL import Image

from deephack import engine
from deephack.config import Config
from deephack.modes import tiktok, video
from deephack.modes.tiktok import Clip, Feed, TikTok
from deephack.modes.video import Player, Video
from deephack.sound import FRAME, RATE, Speaker

av = pytest.importorskip("av")
FPS, SECONDS, TONE = 20, 3, 440


class Device:
    """What WaveOut is to the Speaker: it plays what it is given as time goes by, and counts."""

    def __init__(self, speed=1.0):
        self.speed, self.heard, self.written = speed, bytearray(), 0
        self._played, self._seen = 0.0, None
        self.paused, self.resets, self.volumes, self.closed = False, 0, [], False

    def _go(self):
        now = time.monotonic()
        if self._seen is not None and not self.paused:
            self._played = min(self.written, self._played + (now - self._seen) * RATE * self.speed)
        self._seen = now

    def write(self, data):
        self._go()
        self.heard += data
        self.written += len(data) // FRAME

    def played(self):
        self._go()
        return int(self._played)

    def reset(self):
        self.resets, self.written, self._played = self.resets + 1, 0, 0.0

    def pause(self):
        self._go()
        self.paused = True

    def restart(self):
        self._go()
        self.paused = False

    def set_volume(self, volume):
        self.volumes.append(volume)

    def close(self):
        self.closed = True


def make(path, picture=True, sound=True, seconds=SECONDS):
    """A video whose every frame is brighter than the one before, with a tone of 440 Hz on it."""
    with av.open(str(path), "w") as out:
        if picture:
            film = out.add_stream("mpeg4", rate=FPS)
            film.width, film.height, film.pix_fmt = 180, 320, "yuv420p"
        if sound:
            track = out.add_stream("aac", rate=44100, layout="mono")
            form = av.AudioResampler(format=track.format.name, layout="mono", rate=44100, frame_size=1024)
            tone = array.array("h", (round(9000 * math.sin(2 * math.pi * TONE * n / 44100)) for n in range(44100 * seconds)))
        for i in range(FPS * seconds):
            if picture:
                for packet in film.encode(av.VideoFrame.from_image(Image.new("RGB", (180, 320), (40 + 3 * i, 0, 0)))):
                    out.mux(packet)
            if sound:                                          # the sound next to its picture, as a camera writes it
                step = 44100 // FPS
                piece = av.AudioFrame(format="s16", layout="mono", samples=step)
                piece.planes[0].update(tone[i * step:(i + 1) * step].tobytes())
                piece.sample_rate, piece.pts, piece.time_base = 44100, i * step, Fraction(1, 44100)
                for ready in form.resample(piece):
                    for packet in track.encode(ready):
                        out.mux(packet)
        for stream in ([film] if picture else []) + ([track] if sound else []):
            for packet in stream.encode():
                out.mux(packet)
    return path


@pytest.fixture(scope="module")
def films(tmp_path_factory):
    folder = tmp_path_factory.mktemp("sound")
    return types.SimpleNamespace(both=make(folder / "both.mp4"), picture=make(folder / "picture.mp4", sound=False),
                                 sound=make(folder / "sound.m4a", picture=False), short=make(folder / "short.mp4", seconds=1))


def player_for(source, device=None, **more):
    device = device or Device()
    player = Player(str(source), speaker=lambda: Speaker(device), **more)
    player.set_sound(True)
    return player, device


def play(player, seconds, each=None):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        player.ask()
        if each:
            each()
        time.sleep(0.01)


def tone_in(heard: bytes) -> tuple[float, int]:
    """Of sound as the speaker gets it: the pitch of its left channel in Hz, and how loud it gets."""
    left = array.array("h", bytes(heard))[0::2]
    left = left[RATE // 10:]                                   # past the encoder's first moments
    crossings = sum(1 for a, b in zip(left, left[1:]) if (a < 0) != (b < 0))
    return crossings / 2 / (len(left) / RATE), max(left)


# ---- the speaker
def test_the_speaker_knows_what_is_being_heard():
    device = Device()
    speaker = Speaker(device)
    assert speaker.position() is None and speaker.ahead() == 0 and device.volumes == [1.0]
    quiet = bytes(FRAME * RATE // 10)                          # a tenth of a second
    for i in range(3):
        assert speaker.write(quiet, 5.0 + i / 10, speaker.epoch)
    speaker.write(quiet * 3, 60.0, speaker.epoch)              # the video jumped: this belongs somewhere else
    time.sleep(0.15)
    assert 5.1 < speaker.position() < 5.25 and 0.4 < speaker.ahead() < 0.5
    time.sleep(0.25)
    assert 60.05 < speaker.position() < 60.2                   # 0.4 s played: 0.3 of the first, 0.1 of the second
    speaker.pause()
    here = speaker.position()
    time.sleep(0.1)
    assert speaker.position() == here and device.paused
    speaker.resume()
    time.sleep(0.3)
    assert speaker.position() is None and speaker.ahead() == 0     # played out


def test_a_reset_stops_at_once_and_refuses_what_was_meant_for_before():
    device = Device()
    speaker = Speaker(device)
    old = speaker.epoch
    speaker.write(bytes(FRAME * RATE), 0.0, old)
    speaker.pause()
    speaker.reset()
    assert speaker.position() is None and device.resets == 1 and not device.paused and not speaker.paused
    assert not speaker.write(bytes(FRAME * 100), 1.0, old) and device.written == 0
    assert speaker.write(bytes(FRAME * 100), 0.0, speaker.epoch) and device.written == 100
    speaker.volume = 0.25
    assert device.volumes[-1] == 0.25


def test_a_device_that_fails_makes_it_silent_not_broken():
    class Gone(Device):
        def write(self, data):
            raise OSError("no sound: waveOutWrite failed (6)")

    speaker = Speaker(Gone())
    assert not speaker.write(bytes(FRAME * 100), 0.0, speaker.epoch)
    assert "waveOutWrite" in speaker.broken and speaker.position() is None and speaker.ahead() == 0
    speaker.reset()
    speaker.volume = 0.5                                       # nothing of this raises


# ---- the player
def test_a_files_sound_is_played_as_it_is(films):
    player, device = player_for(films.both)
    play(player, 1.5)
    assert player.state == "playing" and player.silent == ""
    assert 1.0 < len(device.heard) / FRAME / RATE < 2.2         # as much as was played, and a little ahead
    pitch, loud = tone_in(device.heard)
    assert abs(pitch - TONE) < 8 and 6000 < loud < 12000       # the tone, at its pitch and loudness: not noise
    right = array.array("h", bytes(device.heard))[1::2]
    assert right == array.array("h", bytes(device.heard))[0::2]     # one channel in the file: the same from both sides
    player.stop()


def test_the_picture_goes_by_what_is_heard_not_by_the_clock(films):
    player, device = player_for(films.both, Device(speed=0.5))     # a speaker that plays at half the pace
    apart = []

    def look():
        heard = player._speaker.position() if player._speaker else None
        if heard is not None and player.state == "playing" and heard > 0.2:
            apart.append(player.position - heard)

    started = time.monotonic()
    play(player, 2.0, look)
    assert 0.7 < player.position < 1.3                         # two seconds at half the pace: about one second in
    assert len(apart) > 50
    apart.sort()
    assert video.LEAD - 0.08 < apart[len(apart) // 2] < video.LEAD + 0.03     # the frame of that moment, a little ahead
    assert apart[len(apart) // 20] > video.LEAD - 0.2 and apart[-len(apart) // 20] < video.LEAD + 0.06    # all but a few
    assert time.monotonic() - started < 2.6
    player.stop()


def test_sound_and_picture_from_one_stream(films, monkeypatch):
    monkeypatch.setattr(video, "read_twice", lambda address: False)     # as an address on the net is read
    player, device = player_for(films.both)
    apart, seen = [], set()

    def look():
        heard = player._speaker.position() if player._speaker else None
        seen.add(player.latest()[0])
        if heard is not None and heard > 0.2:
            apart.append(player.position - heard)

    play(player, 1.5, look)
    pitch, loud = tone_in(device.heard)
    assert abs(pitch - TONE) < 8 and 6000 < loud < 12000
    assert len(seen) >= 20 and len(apart) > 50                 # 20 frames a second, and none left out
    apart.sort()
    assert video.LEAD - 0.08 < apart[len(apart) // 2] < video.LEAD + 0.03
    assert 0.9 < player.position < 1.7
    player.stop()


def test_sound_from_an_address_of_its_own(films):
    player, device = player_for(films.picture, resolve=lambda source: (source, {}, "A film", str(films.sound)))
    play(player, 1.2)
    pitch, loud = tone_in(device.heard)
    assert abs(pitch - TONE) < 8 and loud > 6000 and player.silent == "" and 0.7 < player.position < 1.4
    player.stop()


def test_a_video_without_sound_plays_at_once_and_says_so(films):
    player, device = player_for(films.picture)
    started = time.monotonic()
    while player.latest()[1] is None and time.monotonic() - started < 2:
        player.ask()
        time.sleep(0.01)
    play(player, 0.5)
    assert player.position > 0.3 and player.silent == "this video has no sound" and not device.heard
    player.stop()


def test_a_pc_without_sound_plays_the_picture(films):
    def nothing():
        raise OSError("no sound: waveOutOpen failed (6)")

    player = Player(str(films.both), speaker=nothing)
    player.set_sound(True)
    play(player, 0.6)
    assert player.state == "playing" and player.position > 0.3 and "waveOutOpen" in player.silent
    player.stop()


def test_without_sound_wanted_nothing_is_opened(films):
    made = []
    player = Player(str(films.both), speaker=lambda: made.append(1))
    play(player, 0.4)
    assert player.state == "playing" and not made
    player.stop()


def test_another_video_cuts_the_sound_at_once(films):
    player, device = player_for(films.both, resolve=lambda source: (source, {}, "A film"))
    play(player, 0.8)
    assert device.written > 0 and player.opened == 1
    player.skip()
    assert device.written == 0 and player._speaker.position() is None     # there and then, not when the thread notices
    resets = device.resets
    play(player, 0.8)
    assert player.opened == 2 and 0.3 < player.position < 1.0 and device.resets > resets
    assert 0.2 < player._speaker.position() < 1.0                  # the sound of the new one, from its beginning
    player.stop()
    assert device.written == 0


def test_switching_the_sound_on_goes_on_where_the_video_is(films):
    device = Device()
    player = Player(str(films.both), speaker=lambda: Speaker(device))
    play(player, 1.6)
    assert not device.heard and 1.3 < player.position < 1.9
    player.set_sound(True)
    play(player, 0.7)
    assert player.opened == 2 and 1.6 < player.position < 2.8 and device.heard
    assert 1.6 < player._speaker.position() < 2.8              # its sound from there too, not from the beginning
    player.set_sound(False)
    play(player, 0.4)
    assert player.opened == 3 and device.written == 0
    player.stop()


def test_nobody_looking_and_the_sound_waits(films):
    player, device = player_for(films.both)
    play(player, 0.6)
    time.sleep(video.HUSH + 0.3)
    assert device.paused
    here = player.position
    time.sleep(0.3)
    assert player.position == here                             # and the picture with it
    play(player, 0.4)
    assert not device.paused and here + 0.2 < player.position < here + 0.6
    player.stop()


def test_at_its_end_the_speaker_is_closed(films, monkeypatch):
    monkeypatch.setattr(video, "IDLE", 0.3)
    player, device = player_for(films.both)
    play(player, 0.5)
    time.sleep(0.8)
    assert player.state == "idle" and device.closed and player._speaker is None


# ---- a link
def test_with_sound_yt_dlp_is_asked_for_both_streams(monkeypatch):
    asked = {}

    class Stand:
        def __init__(self, options):
            asked.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, link, download):
            if "+bestaudio" not in asked["format"]:
                return {"url": "https://stream.invalid/video", "title": "A title", "http_headers": {"User-Agent": "x"}}
            return {"title": "A title", "requested_formats": [
                {"url": "https://stream.invalid/sound", "vcodec": "none", "acodec": "opus"},
                {"url": "https://stream.invalid/video", "vcodec": "avc1", "acodec": "none", "http_headers": {"User-Agent": "x"}}]}

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=Stand))
    address, options, title, sound = video.resolve("https://www.youtube.com/watch?v=abc", 720, sound=True)
    assert (address, title, sound) == ("https://stream.invalid/video", "A title", "https://stream.invalid/sound")
    assert options["headers"] == "User-Agent: x\r\n" and "height<=720" in asked["format"]
    assert video.resolve("https://www.youtube.com/watch?v=abc", 720)[3] is None and "bestaudio" not in asked["format"]


# ---- the modes
async def frames(mode, seconds, between=None):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        await mode.frame()
        if between:
            between()
        await asyncio.sleep(0.02)


def video_mode(films, device, **values):
    mode = Video({"source": str(films.both), **values}, player=lambda *a, **k: Player(*a, speaker=lambda: Speaker(device), **k))
    mode.direct = mode.audible = True
    return mode


def test_the_mode_plays_sound_as_loud_as_it_is_set_and_mute_silences_it(films):
    device = Device()
    mode = video_mode(films, device, sound="40")
    asyncio.run(frames(mode, 0.8))
    assert mode._player.sound and device.heard and device.volumes[-1] == 0.4 and "muted" not in mode.status
    mode.act("mute")
    assert device.volumes[-1] == 0.0                           # at once, not at the next frame
    asyncio.run(frames(mode, 0.2))
    assert mode.status.endswith(" · muted") and device.volumes[-1] == 0.0 and mode._player.opened == 1
    mode.act("mute")
    mode.values["sound"] = "80"                                # the setting is changed while it plays
    asyncio.run(frames(mode, 0.2))
    assert device.volumes[-1] == 0.8 and mode._player.opened == 1 and "muted" not in mode.status
    asyncio.run(mode.close())
    assert device.written == 0
    assert Video.actions == (("mute", "Mute"),) and {s.key: s.live for s in Video.settings}["sound"]


def test_sound_is_off_unless_switched_on_and_only_on_the_cooler(films):
    device = Device()
    mode = video_mode(films, device)
    asyncio.run(frames(mode, 0.5))
    assert mode.values["sound"] == "0" and not mode._player.sound and not device.heard
    asyncio.run(mode.close())
    for name, value in (("audible", False), ("direct", False)):     # in the window only; a cooler that shows one picture
        mode = video_mode(films, device, sound="100")
        setattr(mode, name, value)
        asyncio.run(frames(mode, 0.5))
        assert not mode._player.sound and not device.heard
        asyncio.run(mode.close())


def test_the_mode_says_when_there_is_no_sound_to_play(films):
    mode = Video({"source": str(films.picture), "sound": "60"},
                 player=lambda *a, **k: Player(*a, speaker=lambda: Speaker(Device()), **k))
    mode.direct = mode.audible = True
    asyncio.run(frames(mode, 0.6))
    assert mode.status.endswith(" · no sound (this video has no sound)")
    asyncio.run(mode.close())


def test_tiktok_has_sound_too_and_next_cuts_it(films, monkeypatch):
    monkeypatch.setattr(video, "read_twice", lambda address: False)     # one address with both, as TikTok's are
    device = Device()

    class Source:
        def clips(self, topic):
            return [Clip(str(i), "somebody", "", 3.0, str(films.both)) for i in range(4)]

    mode = TikTok({"sound": "60"}, player=lambda *a, **k: Player(*a, speaker=lambda: Speaker(device), **k), feed=Feed(source=Source()))
    mode.direct = mode.audible = True
    asyncio.run(frames(mode, 0.8))
    assert device.heard and device.volumes[-1] == 0.6 and abs(tone_in(device.heard)[0] - TONE) < 8
    mode.act("next")
    assert device.written == 0
    asyncio.run(frames(mode, 0.6))
    assert mode._player.opened == 2 and mode._feed.index == 1 and device.written > 0
    mode.act("mute")
    assert device.volumes[-1] == 0.0
    asyncio.run(mode.close())
    assert ("mute", "Mute") in TikTok.actions and {"sound", "mute_key"} <= {s.key for s in TikTok.settings}
    assert tiktok.SOUND is video.SOUND


# ---- the engine
def test_the_engine_says_which_mode_is_on_the_cooler_and_closes_the_one_it_replaces(tmp_path):
    eng = engine.Engine(Config(tmp_path / "config.json"), use_device=False)
    eng.config.set("enabled", True)
    eng.config.set("mode", "video")
    closed = []

    async def main():
        task = asyncio.ensure_future(eng._main())
        await asyncio.sleep(0.2)
        first = eng._mode
        first.close = lambda: closed.append("video") or asyncio.sleep(0)
        assert first.audible
        eng.set_preview("tiktok")
        await asyncio.sleep(0.3)
        preview = eng._preview
        assert preview.id == "tiktok" and not preview.audible
        eng.mode_value("video", "sound", "60")                 # a live setting: the same mode, with the new value
        await asyncio.sleep(0.2)
        assert eng._mode is first and first.values["sound"] == "60" and not closed
        eng.config.set("mode", "tiktok")
        eng.reload_mode()                                      # the previewed mode goes on the cooler
        await asyncio.sleep(0.3)
        assert eng._mode is preview and preview.audible and closed == ["video"]
        preview.close = lambda: closed.append("tiktok") or asyncio.sleep(0)
        eng.stop()
        await task

    asyncio.run(main())
    assert closed == ["video", "tiktok"]
