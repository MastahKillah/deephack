"""The TikTok mode, with small videos made here and a stand-in for TikTok's list: nothing is fetched from
the net. And what it brought along: a mode's actions, and keys that work in any window (hotkeys.py)."""
import asyncio
import json
import time

import pytest
from PIL import Image

from deephack import engine, hotkeys
from deephack.config import Config
from deephack.modes import BY_ID, tiktok, video
from deephack.modes.tiktok import Clip, Explore, Feed, TikTok

av = pytest.importorskip("av")
COLOURS = {"red": (200, 0, 0), "green": (0, 200, 0), "blue": (0, 0, 200)}


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    """Three upright videos of one second each: red, green, blue. Their addresses are files."""
    folder, made = tmp_path_factory.mktemp("tiktok"), []
    for name, colour in COLOURS.items():
        path = folder / f"{name}.mp4"
        with av.open(str(path), "w") as out:
            stream = out.add_stream("mpeg4", rate=20)
            stream.width, stream.height, stream.pix_fmt = 180, 320, "yuv420p"
            for _ in range(20):
                for packet in stream.encode(av.VideoFrame.from_image(Image.new("RGB", (180, 320), colour))):
                    out.mux(packet)
            for packet in stream.encode():
                out.mux(packet)
        made.append(Clip(name, f"{name}_account", f"A {name} video", 1.0, str(path)))
    return made


class Source:
    """TikTok's list: hands out the given videos, a few at a call."""

    def __init__(self, clips, at_a_time=2):
        self.left, self.at_a_time, self.calls = list(clips), at_a_time, []

    def clips(self, topic):
        self.calls.append(topic)
        out, self.left = self.left[:self.at_a_time], self.left[self.at_a_time:]
        return out


def colour_of(picture) -> str:
    r, g, b = picture.getpixel((240, 320))
    return max((("red", r), ("green", g), ("blue", b)), key=lambda c: c[1])[0]


async def watch(mode, seconds, presses=()):
    """Run the mode as the engine would; what was on screen, as (seconds, colour) whenever it changed."""
    seen, started, presses = [], time.monotonic(), list(presses)
    while (now := time.monotonic() - started) < seconds:
        while presses and now >= presses[0][0]:
            mode.act(presses.pop(0)[1])
        picture = await mode.frame()
        if picture is not None and (not seen or seen[-1][1] != colour_of(picture)):
            seen.append((round(now, 2), colour_of(picture)))
        await asyncio.sleep(mode.tick or 0.05)
    return seen


def item(number="1", codecs=(("h265_hvc1", 300), ("h264", 400)), **more):
    rates = [{"CodecType": codec, "Bitrate": rate, "PlayAddr": {"UrlList": [
        f"https://v16-webapp-prime.tiktok.com/video/{codec}", f"https://www.tiktok.com/aweme/v1/play/?file={codec}{rate}"]}}
        for codec, rate in codecs]
    return {"id": number, "desc": "  A dog\n on a board ", "author": {"uniqueId": "dogs"},
            "video": {"duration": 12, "bitrateInfo": rates, "playAddr": "https://v16-webapp-prime.tiktok.com/video/x"}, **more}


# ---- TikTok's list
def test_a_video_of_the_list_is_read_for_the_address_that_plays():
    clip = tiktok.clip_of(item())
    assert clip == Clip("1", "dogs", "A dog on a board", 12.0, "https://www.tiktok.com/aweme/v1/play/?file=h264400")
    assert tiktok.clip_of(item(codecs=(("h264", 400), ("h264", 900)))).address.endswith("h264900")      # the better H.264
    assert tiktok.clip_of(item(codecs=(("h265_hvc1", 300),))).address.endswith("h265_hvc1300")          # H.265 if need be
    assert tiktok.clip_of(item(isAd=True)) is None and tiktok.clip_of(item(imagePost={"images": []})) is None
    only_servers = item()
    for rate in only_servers["video"]["bitrateInfo"]:
        rate["PlayAddr"]["UrlList"].pop()
    assert tiktok.clip_of(only_servers) is None                # TikTok's video servers refuse everybody but its own player


def test_the_list_is_asked_as_a_browser_without_an_account_asks(monkeypatch):
    asked = []

    def get(self, url, query=None):
        asked.append((url, query))
        return b"<html>" if query is None else json.dumps({"itemList": [item("1"), item("2", isAd=True), item("3")]}).encode()

    monkeypatch.setattr(Explore, "_get", get)
    explore = Explore()
    assert [clip.id for clip in explore.clips("113")] == ["1", "3"]
    explore.clips("113")
    assert [url for url, _ in asked] == [tiktok.PAGE, tiktok.LIST, tiktok.LIST]      # the page once, for its cookies
    query = asked[1][1]
    assert query["categoryType"] == "113" and query["count"] == str(tiktok.ASK) and query["device_id"].isdigit()
    assert not {"msToken", "X-Bogus", "sessionid", "secUid"} & set(query)             # no account, no signature


def test_a_list_without_videos_is_an_error(monkeypatch):
    answers = [b"<html>", b'{"statusCode": 10000}', b"<html>captcha</html>"]
    monkeypatch.setattr(Explore, "_get", lambda self, url, query=None: answers.pop(0))
    explore = Explore()
    with pytest.raises(RuntimeError, match="no videos"):
        explore.clips("120")
    with pytest.raises(RuntimeError, match="did not answer"):
        explore.clips("120")


def test_the_feed_gets_more_when_it_runs_out_and_steps_both_ways(clips):
    source = Source(clips + clips)                             # the second lot is nothing new
    feed = Feed("104", source)
    assert feed.current().id == "red" and source.calls == ["104"]
    feed.step(+1)
    assert feed.current().id == "green" and len(source.calls) == 1
    feed.step(+1)
    assert feed.current().id == "blue" and len(source.calls) == 2
    feed.step(-5)
    assert feed.current().id == "red"                          # not before the first
    feed.step(+3)
    with pytest.raises(RuntimeError, match="no other videos"):
        feed.current()                                         # asked three times, and only got what it had
    assert [clip.id for clip in feed.clips] == ["red", "green", "blue"]


def test_the_feed_forgets_what_is_far_behind(monkeypatch):
    monkeypatch.setattr(tiktok, "KEEP", 3)
    feed = Feed(source=Source([Clip(str(i), "a", "", 1.0, "x") for i in range(10)], at_a_time=10))
    for _ in range(7):
        feed.current()
        feed.step(+1)
    assert feed.current().id == "7" and [clip.id for clip in feed.clips][0] == "4" and feed.index == 3


# ---- the mode
def test_one_video_plays_after_the_other(clips):
    mode = TikTok({}, feed=Feed(source=Source(clips)))
    mode.direct = True
    seen = asyncio.run(watch(mode, 2.6))
    assert [colour for _, colour in seen] == ["red", "green", "blue"]
    assert 0.7 < seen[1][0] - seen[0][0] < 1.6                 # each for its second
    assert mode.status.startswith("@blue_account · 0:0") and mode.status.endswith(" of 0:01 · A blue video")
    assert BY_ID["tiktok"] is TikTok and TikTok.actions[:2] == (("previous", "Previous"), ("next", "Next"))


def test_or_the_same_one_again(clips):
    mode = TikTok({"after": "again"}, feed=Feed(source=Source(clips)))
    mode.direct = True
    seen = asyncio.run(watch(mode, 2.4))
    assert [colour for _, colour in seen] == ["red"] and mode._player.opened >= 2


def test_next_and_previous_change_the_video_at_once(clips):
    mode = TikTok({"after": "again"}, feed=Feed(source=Source(clips)))
    mode.direct = True
    seen = asyncio.run(watch(mode, 2.6, presses=[(0.5, "next"), (1.0, "next"), (1.5, "previous"), (2.0, "nothing")]))
    assert [colour for _, colour in seen] == ["red", "green", "blue", "green"]
    for (at, _), pressed in zip(seen[1:], (0.5, 1.0, 1.5)):
        assert 0 <= at - pressed < 0.4                         # the next one is on within a moment of the press


def test_a_video_that_will_not_play_is_passed_over(clips, tmp_path, monkeypatch):
    monkeypatch.setattr(video, "RETRY_NEXT", 0.1)
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"no video" * 200)
    mode = TikTok({}, feed=Feed(source=Source([Clip("x", "gone", "", 1.0, str(broken)), clips[1]])))
    mode.direct = True
    seen = asyncio.run(watch(mode, 1.0))
    assert [colour for _, colour in seen] == ["green"]


def test_when_tiktok_gives_nothing_the_mode_says_so():
    class Nothing:
        def clips(self, topic):
            raise RuntimeError("TikTok handed out no videos")

    mode = TikTok({}, feed=Feed(source=Nothing()))
    mode.direct = True
    assert asyncio.run(watch(mode, 0.4)) == []
    assert mode.status == "Cannot play it: TikTok handed out no videos" and mode.placeholder[0] == "TikTok"


# ---- actions and keys
def test_the_engine_passes_an_action_to_the_mode_it_is_for(tmp_path):
    eng = engine.Engine(Config(tmp_path / "config.json"), use_device=False)
    eng.config.set("mode", "tiktok")
    eng._make_mode()
    done = []
    eng._mode.act = done.append
    eng.mode_action("tiktok", "next")
    eng.mode_action("video", "next")                           # not the mode that runs: nothing happens
    while not eng._commands.empty():
        eng._commands.get_nowait()()
    assert done == ["next"]


def test_keys_are_read_the_way_qt_writes_them():
    ctrl_alt = hotkeys.MOD_CONTROL | hotkeys.MOD_ALT
    assert hotkeys.parse("Ctrl+Alt+PgDown") == (ctrl_alt, 0x22) and hotkeys.parse("Ctrl+Alt+PgUp") == (ctrl_alt, 0x21)
    assert hotkeys.parse("F9") == (0, 0x78) and hotkeys.parse("Meta+Shift+N") == (hotkeys.MOD_WIN | hotkeys.MOD_SHIFT, ord("N"))
    assert hotkeys.parse("Ctrl++") == (hotkeys.MOD_CONTROL, 0xBB) and hotkeys.parse("Alt+,") == (hotkeys.MOD_ALT, 0xBC)
    assert hotkeys.parse("Media Next") == (0, 0xB0)
    for nothing in ("", "  ", "Ctrl+K, Ctrl+N", "Ctrl+Alt+Banana", "Hyper+N"):
        assert hotkeys.parse(nothing) is None
    defaults = {s.key: s.default for s in TikTok.settings if s.kind == "shortcut"}
    assert defaults.pop("mute_key") == "" and len(defaults) == 2             # no key for Mute unless one is set
    assert all(hotkeys.parse(key) for key in defaults.values())
    assert {s.action for s in TikTok.settings if s.kind == "shortcut"} == {"next", "previous", "mute"}


def test_only_the_keys_asked_for_are_registered_and_a_taken_one_is_reported():
    registered, done = {}, []

    def register(number, modifiers, key):
        if key == 0x78:                                        # F9 belongs to another program
            return False
        registered[number] = (modifiers, key)
        return True

    keys = hotkeys.Hotkeys(register, registered.pop)
    refused = keys.set({"Ctrl+Alt+PgDown": lambda: done.append("next"), "F9": lambda: done.append("f9"), "Banana": lambda: None})
    assert refused == {"F9", "Banana"} and list(registered.values()) == [(hotkeys.MOD_CONTROL | hotkeys.MOD_ALT, 0x22)]
    number = next(iter(registered))
    assert keys.pressed(number) and done == ["next"] and not keys.pressed(number + 50)
    assert keys.set({"Ctrl+Alt+PgUp": lambda: done.append("previous")}) == set()
    assert list(registered.values()) == [(hotkeys.MOD_CONTROL | hotkeys.MOD_ALT, 0x21)]      # the old one was given back
    assert not keys.pressed(number)
    keys.clear()
    assert registered == {}
