"""A track's music video: which YouTube video it is (musicvideo.py), the player following the music's clock,
and the Now Playing mode showing it. YouTube and Windows' media player are stood in for."""
import asyncio
import json
import time
from types import SimpleNamespace

import av
import pytest

from deephack import musicvideo
from deephack.modes import now_playing, video
from deephack.musicvideo import Candidate, Found, judge, pick

NUMB = ("Linkin Park", "Numb", 188.0)
OFFICIAL = Candidate("kXYiU", "Numb (Official Music Video) [4K UPGRADE] – Linkin Park", "Linkin Park", 187, 2_800_000_000, True)


# ---------------------------------------------------------------- which video
def test_the_artists_own_video_of_the_tracks_length_is_it():
    assert judge(OFFICIAL, *NUMB) is not None
    assert judge(Candidate("a", "Rammstein - Du Hast (Official 4K Video)", "Rammstein Official", 236, 1, True), "Rammstein", "Du hast", 234) is not None
    assert judge(Candidate("b", "Foo Fighters - Everlong", "Foo Fighters", 251, 1, False), "Foo Fighters", "Everlong", 251) is not None   # its own channel
    assert judge(Candidate("c", "Bob Marley - Is This Love (Official Music Video)", "Bob Marley", 234, 1, True),
                 "Bob Marley & The Wailers", "Is This Love", 232) is not None                # the first of the names will do
    assert judge(Candidate("d", "The Killers - Mr. Brightside (Official Music Video)", "TheKillersMusic", 225), "The Killers", "Mr. Brightside", 224) is not None
    assert judge(Candidate("e", "Some Song (Official Video)", "SomeArtistVEVO", 200), "Some Artist", "Some Song", 200) is not None


@pytest.mark.parametrize("candidate, why", [
    (Candidate("a", "Numb (Official Music Video) – Linkin Park", "Linkin Park", 170, 1, True), "shorter: a part of the song is missing"),
    (Candidate("k", "Numb (Official Music Video) – Linkin Park", "Linkin Park", 500, 1, True), "longer by more than five minutes"),
    (Candidate("b", "Linkin Park - Numb (Lyrics)", "7clouds Rock", 188, 1, True), "the words on a still picture"),
    (Candidate("c", "Linkin Park - Numb (Official Audio)", "Linkin Park", 188, 1, True), "the cover standing still"),
    (Candidate("d", "Numb", "Linkin Park - Topic", 188, 1, True), "YouTube's own copy of the album"),
    (Candidate("e", "Linkin Park - Numb (Official Music Video)", "eminemfi", 188, 10_000, False), "somebody's copy"),
    (Candidate("f", "Linkin Park - Numb", "Minimal Sounds", 188, 1, True), "a channel that plays songs to a picture"),
    (Candidate("g", "Numb (Official Video)", "Some Other Band", 188, 1, True), "another band's song of that name"),
    (Candidate("h", "Linkin Park - Numb (Live in Texas)", "Linkin Park", 188, 1, True), "a concert"),
    (Candidate("i", "Linkin Park - In The End (Official Video)", "Linkin Park", 188, 1, True), "another song"),
    (Candidate("j", "Linkin Park - Numb (Official Music Video)", "Linkin Park", 0, 1, True), "no length known"),
])
def test_what_is_not_the_tracks_video(candidate, why):
    assert judge(candidate, *NUMB) is None, why


def test_a_word_that_rules_a_video_out_does_not_when_the_track_has_it_too():
    live = Candidate("a", "Queen - Love of My Life (Live at Wembley) (Official Video)", "Queen Official", 240, 1, True)
    assert judge(live, "Queen", "Love of My Life (Live at Wembley)", 240) is not None
    assert judge(live, "Queen", "Love of My Life", 240) is None


def test_the_best_of_what_fits_is_picked_and_a_missing_channel_is_looked_up():
    copy = Candidate("x", "Linkin Park - Numb (Official Music Video)", "fanchannel", 188, 5_000_000, False)
    plain = Candidate("y", "Linkin Park - Numb", "Linkin Park", 188, 9, True)
    assert pick([copy, plain, OFFICIAL], *NUMB) is OFFICIAL                   # says it is the video, on the band's channel
    assert pick([copy], *NUMB) is None
    assert pick([], *NUMB) is None

    looked = []

    def look(candidate):
        looked.append(candidate.id)
        return Candidate(candidate.id, candidate.title, "Linkin Park", candidate.seconds, 7, True)

    unnamed = [Candidate(str(i), "Numb (Official Music Video) – Linkin Park", "", 188) for i in range(3)]
    far = Candidate("z", "Numb (Official Music Video) – Linkin Park", "", 600)
    assert pick([far, *unnamed], *NUMB, look).id == "0" and looked == ["0", "1"]          # two at most, and none that is too long
    assert pick(unnamed, *NUMB) is None                                                    # nobody to ask: unknown stays unknown


def test_a_longer_video_will_do_where_there_is_none_of_the_tracks_length():
    scene = Candidate("s", "Numb (Official Music Video) – Linkin Park", "Linkin Park", 240, 9_000_000, True)    # a scene before the song
    credits = Candidate("c", "Linkin Park - Numb (Official Music Video)", "Linkin Park", 420, 9_000_000_000, True)
    plain = Candidate("y", "Linkin Park - Numb", "Linkin Park", 188, 9, True)
    assert judge(scene, *NUMB) is not None and musicvideo.longer(scene.seconds, 188.0) and not musicvideo.longer(190.0, 188.0)
    assert pick([credits, scene], *NUMB) is scene                             # the one that is less long
    assert pick([credits, scene, plain], *NUMB) is plain                      # one of the track's length before any longer one,
    assert pick([scene, OFFICIAL, plain], *NUMB) is OFFICIAL                  # however little it says for itself
    assert judge(Candidate("b", "Linkin Park - Numb (Lyrics)", "7clouds Rock", 240, 1, True), *NUMB) is None    # the other rules hold


def test_the_search_is_asked_once_and_tracks_without_enough_to_go_by_not_at_all():
    asked = []

    def search(artist, title):
        asked.append((artist, title))
        return [OFFICIAL]

    assert musicvideo.find(*NUMB, search=search) is OFFICIAL and asked == [("Linkin Park", "Numb")]
    assert OFFICIAL.link == "https://www.youtube.com/watch?v=kXYiU"
    for artist, title, seconds in (("", "Numb", 188), ("Linkin Park", "", 188), ("Linkin Park", "Numb", 12)):
        assert musicvideo.find(artist, title, seconds, search=search) is None
    assert len(asked) == 1
    assert musicvideo.words("Bohemian Rhapsody - Remastered 2011") == ["bohemian", "rhapsody"]
    assert musicvideo.words("Hips Don't Lie (feat. Wyclef Jean)") == ["hips", "don", "t", "lie"]
    assert musicvideo.plain("Zdob și Zdub — Moldovenii s-au născut!") == "zdob si zdub moldovenii s au nascut"


def test_what_was_found_is_kept_and_nothing_found_is_believed_for_a_week(tmp_path):
    found = Found(tmp_path / "found.json")
    assert found.get(*NUMB) is None                                           # never looked for
    found.put(*NUMB, OFFICIAL.link, now=1000.0)
    found.put("Oasis", "Wonderwall", 259, "", now=1000.0)
    again = Found(tmp_path / "found.json")                                    # after a restart
    assert again.get("LINKIN PARK", "numb", 188.3, now=9e9) == OFFICIAL.link  # however it is written, and for good
    assert again.get("Oasis", "Wonderwall", 259, now=2000.0) == ""            # nothing fits: not asked again ...
    assert again.get("Oasis", "Wonderwall", 259, now=1000.0 + musicvideo.FORGET_NONE + 1) is None      # ... for a week
    assert again.get("Linkin Park", "Numb", 215) is None                      # another version of the track
    for i in range(musicvideo.KEEP + 5):
        again.put("A", f"Song {i}", 100, "")
    assert len(Found(tmp_path / "found.json")._known) == musicvideo.KEEP
    assert Found(tmp_path / "missing" / "found.json").get(*NUMB) is None      # no file, no folder: nothing known


def test_the_videos_length_is_kept_and_a_nothing_by_older_rules_is_looked_for_again(tmp_path):
    path = tmp_path / "found.json"
    path.write_text(json.dumps({Found.key("Oasis", "Wonderwall", 259): ["", 1000.0],            # as 0.14 kept them
                                Found.key(*NUMB): [OFFICIAL.link, 1000.0]}), encoding="utf8")
    found = Found(path)
    assert found.get("Oasis", "Wonderwall", 259, now=2000.0) is None          # longer videos did not count then
    assert found.get(*NUMB) == OFFICIAL.link and found.length(*NUMB) == 0.0   # what was found stays (of the track's length)
    found.put("Oasis", "Wonderwall", 259, "https://www.youtube.com/watch?v=w", now=2000.0, length=280.0)
    found.put("Miles Davis", "So What", 548, "", now=2000.0)
    again = Found(path)
    assert again.length("Oasis", "Wonderwall", 259) == 280.0 and again.length("Nobody", "Nothing", 100) == 0.0
    assert again.get("Miles Davis", "So What", 548, now=3000.0) == ""         # by today's rules: believed for a week


# ---------------------------------------------------------------- the player follows the music
@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """Six seconds of video in which every frame is brighter than the one before."""
    from fractions import Fraction

    path = tmp_path_factory.mktemp("clip") / "clip.mp4"
    with av.open(str(path), "w") as out:
        stream = out.add_stream("mpeg4", rate=20)
        stream.width, stream.height, stream.pix_fmt = 160, 120, "yuv420p"
        for i in range(120):
            frame = av.VideoFrame(160, 120, "yuv420p")
            for plane, value in zip(frame.planes, (i * 2, 128, 128)):
                plane.update(bytes([value]) * plane.buffer_size)
            frame.pts, frame.time_base = i, Fraction(1, 20)
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)
    return path


def watch(player, seconds, music):
    """Ask for frames for a while: (how far the video was from the music + the lead, at each new frame)."""
    off, seen, until = [], None, time.monotonic() + seconds
    while time.monotonic() < until:
        player.ask()
        seq, frame = player.latest()
        if frame is not None and seq != seen:
            seen = seq
            off.append(player.position - (music() + video.LEAD))
        time.sleep(0.005)
    return off


def test_the_picture_goes_by_a_clock_from_outside(clip):
    how = {"began": time.monotonic(), "frozen": None}

    def music():                                                             # the music is a second in, and plays
        return how["frozen"] if how["frozen"] is not None else 1.0 + (time.monotonic() - how["began"])

    player = video.Player(str(clip), speaker=None, follow=music)
    off = watch(player, 1.2, music)
    assert len(off) > 10 and all(abs(o) < 0.12 for o in off[2:])             # every frame where the music is
    how["began"] -= 1.5                                                      # the music is suddenly 1.5 s further
    off = watch(player, 1.0, music)
    assert abs(off[-1]) < 0.12 and player.opened == 1                        # caught up by leaving frames out
    how["frozen"] = music()
    still = watch(player, 0.6, music)                                        # the music pauses: so does the picture
    assert len(still) <= 4
    player.stop()


def test_a_jump_too_large_for_that_opens_the_video_anew_there(clip):
    player = video.Player(str(clip), speaker=None, follow=lambda: None)      # (no clock: it plays by its own)
    watch(player, 0.4, lambda: 0.0)
    assert player.opened == 1 and player.position < 1.0
    player.seek(4.0)
    watch(player, 0.8, lambda: 0.0)
    assert player.opened == 2 and 4.0 <= player.position < 5.2
    player.seek(-3.0)                                                        # before its beginning: the beginning
    watch(player, 0.6, lambda: 0.0)
    assert player.opened == 3 and player.position < 1.0
    player.stop()


def test_a_frame_is_made_a_picture_of_any_size():
    frame = av.VideoFrame(640, 360, "yuv420p")
    for plane in frame.planes:
        plane.update(bytes(plane.buffer_size))
    assert video.picture(frame, "fill", (392, 392)).size == (392, 392)
    assert video.picture(frame, "fill").size == (480, 640)                   # the screen's, when none is named


# ---------------------------------------------------------------- the mode
class Clip:
    """Stands in for the video's player."""

    def __init__(self, source, resolve=None, on_end=None, speaker=None, follow=None):
        self.source, self.on_end, self.follow = source, on_end, follow
        self.asked, self.sought, self.stopped, self.state, self.position, self.seq = 0, [], False, "playing", 0.0, 0
        self.keeps = True                                                    # to the music; False: it stays where it is
        self.frame = av.VideoFrame(640, 360, "yuv420p")
        for plane, value in zip(self.frame.planes, (200, 90, 200)):
            plane.update(bytes([value]) * plane.buffer_size)

    def ask(self):
        self.asked += 1
        self.seq += 1
        if self.keeps:
            self.position = self.follow()

    def latest(self):
        return self.seq, self.frame if self.seq else None

    def seek(self, at):
        self.sought.append(at)

    def stop(self):
        self.stopped = True


class Spot:
    """Stands in for songplace.Place, which listens for where in the video the song is: nothing is heard here."""

    def __init__(self, link, clock, seconds):
        self.link, self.clock, self.seconds, self.lead, self.state, self.closed = link, clock, seconds, None, "listening", False

    def close(self):
        self.closed = True


def make(tmp_path, monkeypatch, values=None, found=OFFICIAL, direct=True):
    """The mode, with a track that plays and a search that finds `found` (an exception: it raises it)."""
    world = SimpleNamespace(clock=100.0, title="Numb", artist="Linkin Park", at=30.0, since=100.0, playing=True, seconds=188.0,
                            searched=[], clips=[], spots=[])
    # the mode's clock, and only the mode's: `now_playing.time.monotonic` is the whole process's, asyncio's too
    monkeypatch.setattr(now_playing, "time", SimpleNamespace(monotonic=lambda: world.clock))

    async def track():
        if world.title is None:
            return None
        at = world.at + (world.clock - world.since if world.playing else 0.0)
        return now_playing.Track(world.title, world.artist, "", None, at, world.seconds, world.playing)

    def find(artist, title, seconds):
        world.searched.append((artist, title, seconds))
        if isinstance(found, Exception):
            raise found
        return found

    def player(*args, **kwargs):
        world.clips.append(Clip(*args, **kwargs))
        return world.clips[-1]

    mode = now_playing.NowPlaying({"video": "cover", **(values or {})})
    mode.direct = direct
    mode._track, mode._find, mode._make_player = track, find, player
    mode._make_place = lambda link, clock, seconds: world.spots.append(Spot(link, clock, seconds)) or world.spots[-1]
    mode._found = Found(tmp_path / "found.json")
    return mode, world


def test_make_keeps_the_process_monotonic_clock(tmp_path, monkeypatch):
    """A clock that stands still for the whole process stops asyncio's timers with it: `steps` waited for
    ever on a Python whose clock is finer than its 10 ms (3.13 on; 3.12's ticks every 15.6, which let it by). #62"""
    original_monotonic = time.monotonic
    _, world = make(tmp_path, monkeypatch)
    assert now_playing.time.monotonic() == world.clock
    assert time.monotonic is original_monotonic


async def steps(mode, world, count=6, each=0.05):
    """Some calls of frame() with time passing; the pictures it gave."""
    pictures = []
    for _ in range(count):
        picture = await mode.frame()
        if picture is not None:
            pictures.append(picture)
        world.clock += each
        await asyncio.sleep(0.01)                                            # the search runs in a thread
    return pictures


def test_the_music_video_is_off_unless_switched_on(tmp_path, monkeypatch):
    setting = {s.key: s for s in now_playing.NowPlaying.settings}["video"]
    assert setting.default == "off" and [value for value, _ in setting.choices] == ["off", "cover", "screen"]
    mode, world = make(tmp_path, monkeypatch, {"video": "off"})
    pictures = asyncio.run(steps(mode, world))
    assert pictures and world.searched == [] and world.clips == [] and "music video" not in mode.status
    stored, world = make(tmp_path, monkeypatch, direct=False)                # DeepCool's firmware: never
    asyncio.run(steps(stored, world))
    assert world.searched == [] and world.clips == []


def test_a_tracks_video_plays_where_the_cover_is(tmp_path, monkeypatch):
    async def main():
        mode, world = make(tmp_path, monkeypatch)
        first = [await mode.frame()]                                         # the cover, while the video is looked for
        assert first[0] is not None and mode.tick == now_playing.WAIT_TICK
        pictures = await steps(mode, world, 8)
        assert world.searched == [("Linkin Park", "Numb", 188.0)] and len(world.clips) == 1
        clip = world.clips[0]
        assert clip.source == OFFICIAL.link and clip.sought and 30.0 < clip.sought[0] < 30.0 + now_playing.OPENING + 1
        assert mode.status == "Linkin Park — Numb · music video" and mode.tick == video.TICK
        box = now_playing.cover_box("full", True)
        middle = (box[0] + box[2] // 2, box[1] + box[2] // 2)
        assert pictures[-1].getpixel(middle) != first[0].getpixel(middle)    # the video is where the cover was ...
        assert pictures[-1].getpixel((240, 560)) == first[0].getpixel((240, 560))        # ... and nowhere else
        assert pictures[-1].size == (480, 640)
        await mode.close()
        assert clip.stopped
        return mode

    asyncio.run(main())
    again, world = make(tmp_path, monkeypatch)                               # the next time the track plays
    asyncio.run(steps(again, world, 4))
    assert world.searched == [] and len(world.clips) == 1                    # it is known: no search


def test_over_the_whole_screen_it_is_the_video_and_nothing_else(tmp_path, monkeypatch):
    mode, world = make(tmp_path, monkeypatch, {"video": "screen"})
    pictures = asyncio.run(steps(mode, world, 8))
    assert pictures[-1].getpixel((5, 5)) == pictures[-1].getpixel((470, 630)) != (0, 0, 0)       # the frame, edge to edge


@pytest.mark.parametrize("found", [None, OSError("no net")])
def test_no_video_that_fits_is_the_cover_as_ever(tmp_path, monkeypatch, found):
    mode, world = make(tmp_path, monkeypatch, found=found)
    pictures = asyncio.run(steps(mode, world, 8))
    assert pictures and world.clips == [] and "music video" not in mode.status and len(world.searched) == 1
    kept = mode._found.get(*NUMB)
    assert kept == ("" if found is None else None)                           # no net is not "there is none"


def test_the_video_follows_pause_a_jump_and_the_next_track(tmp_path, monkeypatch):
    async def main():
        mode, world = make(tmp_path, monkeypatch)
        await steps(mode, world, 8)
        clip = world.clips[0]
        assert abs(clip.follow() - (world.at + world.clock - world.since)) < 0.05          # the clock it is given is the music's

        world.at, world.since, world.playing = world.at + world.clock - world.since, world.clock, False
        world.clock += now_playing.POLL
        await steps(mode, world, 3)
        asked, frozen = clip.asked, clip.follow()
        world.clock += 5.0
        await steps(mode, world, 3)
        assert clip.asked == asked and clip.follow() == frozen               # paused: the clock stands, nobody asks

        world.since, world.playing = world.clock, True
        world.clock += now_playing.POLL
        await steps(mode, world, 3)
        assert clip.asked > asked and len(clip.sought) == 1

        clip.keeps = False                                                   # the video stays where it is ...
        world.at += 90.0                                                     # ... while the listener jumps ahead
        world.clock += now_playing.POLL + 4.0
        await mode.frame()
        assert len(clip.sought) == 2 and clip.sought[1] > 120.0              # so the video is opened there
        await mode.frame()
        assert len(clip.sought) == 2                                         # once: opening takes its time
        clip.keeps = True

        world.title, world.seconds, world.at, world.since = "In the End", 216.0, 0.0, world.clock
        world.clock += now_playing.POLL
        await steps(mode, world, 8)
        assert clip.stopped and len(world.clips) == 2 and world.searched[-1][1] == "In the End"

        world.clips[1].on_end()                                              # the video is over before the track is
        await steps(mode, world, 3)
        assert "music video" not in mode.status and world.spots[-1].closed   # and nobody listens for its place any more

        world.title = None                                                   # nothing plays any more
        world.clock += now_playing.POLL
        await steps(mode, world, 2)
        assert mode._clip is None

    asyncio.run(main())


def test_a_video_of_the_tracks_length_plays_at_once_and_is_set_right_by_ear(tmp_path, monkeypatch):
    async def main():
        mode, world = make(tmp_path, monkeypatch)
        await steps(mode, world, 8)
        clip, spot = world.clips[0], world.spots[0]
        assert len(world.spots) == 1 and spot.link == OFFICIAL.link and spot.seconds == 188.0
        music = world.at + world.clock - world.since
        assert abs(clip.follow() - music) < 0.05 and abs(spot.clock(world.clock) - music) < 0.05     # until it is heard
        spot.lead = 0.86                                                     # the video's song begins that much later
        assert abs(clip.follow() - (music + 0.86)) < 0.05
        await steps(mode, world, 2)
        assert clip.sought == clip.sought[:1]                                # too little to open it anew: its player catches up
        spot.lead = None                                                     # the place is lost (the song's end): the video stays
        music = world.at + world.clock - world.since
        assert abs(clip.follow() - (music + 0.86)) < 0.05
        world.title, world.seconds, world.at, world.since = "In the End", 216.0, 0.0, world.clock
        world.clock += now_playing.POLL
        await steps(mode, world, 8)
        assert spot.closed and len(world.spots) == 2 and world.spots[1].lead is None
        assert abs(world.clips[1].follow() - (world.clock - world.since)) < 0.2      # the next track starts without a lead
        await mode.close()
        assert world.spots[1].closed

    asyncio.run(main())


def test_a_longer_video_is_shown_once_its_place_is_heard(tmp_path, monkeypatch):
    scene = Candidate("s", "Numb (Official Music Video) – Linkin Park", "Linkin Park", 240, 9, True)        # 52 s longer

    async def main():
        mode, world = make(tmp_path, monkeypatch, found=scene)
        await steps(mode, world, 8)
        assert world.clips == [] and len(world.spots) == 1                   # found, and listened for: no picture yet
        assert mode.status == "Linkin Park — Numb · listening for where its video begins"
        assert mode._found.length(*NUMB) == 240.0
        spot = world.spots[0]
        spot.lead = 22.5                                                     # the song begins 22.5 s into the video
        await steps(mode, world, 4)
        clip = world.clips[0]
        music = world.at + world.clock - world.since
        assert 22.5 + 30.0 < clip.sought[0] < music + 22.5 + now_playing.OPENING + 0.1               # opened there
        assert abs(clip.follow() - (music + 22.5)) < 0.05 and mode.status == "Linkin Park — Numb · music video"
        spot.lead = 25.1                                                     # the video put a pause in: 2.6 s more
        assert abs(clip.follow() - (music + 25.1)) < 0.05
        spot.state, spot.lead = "failed", None
        await steps(mode, world, 2)
        assert "music video" in mode.status                                  # it goes on from the place that was known
        return mode

    asyncio.run(main())
    again, world = make(tmp_path, monkeypatch, found=scene)                  # the next time the track plays: no search,
    asyncio.run(steps(again, world, 6))                                      # and it is known to be a longer one
    assert world.searched == [] and world.clips == [] and "listening" in again.status
    world.spots[0].state = "failed"                                          # its sound cannot be read: the cover, and no word
    asyncio.run(steps(again, world, 2))
    assert world.clips == [] and again.status == "Linkin Park — Numb"


def test_a_video_that_will_not_play_gives_way_to_the_cover(tmp_path, monkeypatch):
    async def main():
        mode, world = make(tmp_path, monkeypatch)
        await steps(mode, world, 8)
        world.clips[0].state = "failed"
        await steps(mode, world, 3)
        assert world.clips[0].stopped and "music video" not in mode.status and len(world.clips) == 1      # not tried again

    asyncio.run(main())


def test_the_videos_address_is_asked_of_youtube_once_in_a_while(tmp_path, monkeypatch):
    mode, world = make(tmp_path, monkeypatch)
    asked = []
    mode._resolve = lambda link, lines: (asked.append((link, lines)), ("address", {}, "title", None))[1]
    assert mode._address("link") == ("address", {}, "title", None) == mode._address("link")
    assert asked == [("link", 480)]                                          # the cover's place needs no more lines
    world.clock += now_playing.ADDRESS_GOOD + 1
    mode.values["video"] = "screen"
    mode._address("link")
    assert asked[-1] == ("link", 720)
