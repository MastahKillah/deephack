"""TikTok: random videos, one after the other, without an account. With their sound from the PC if
wanted (video.py says how).

Where they come from: TikTok's Explore page, as a browser that is not logged in gets it. Its list
(`/api/explore/item_list/`) answers without an account and without the signature most of TikTok's other
lists want: a dozen popular videos a call, others every time, for all topics or for one. What is sent is
what any browser sends plus a made-up device number; no cookie is kept between starts.

Each video comes with several addresses. The ones on TikTok's video servers refuse anybody who is not
its own player; the one on www.tiktok.com (`/aweme/v1/play/`) plays for anyone who names tiktok.com as
the page it came from. That one is read as it plays, the way the Video mode reads a YouTube stream
(video.py: the same player, the same pictures).

Next and Previous (`act`) step through the list: the app has buttons for them, and keys that work
whatever window is in front (hotkeys.py). The list remembers the last hundred videos; an address is
good for some hours, so going far back may land on one that no longer plays, and the player moves on.

All of this is TikTok's own web page talking to its own servers, written down nowhere official: it can
stop working any day.
"""
from __future__ import annotations

import gzip
import http.cookiejar
import json
import random
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .base import Setting
from .video import MUTE_KEY, NETWORK, SOUND, Player, Video, clock_text

PAGE = "https://www.tiktok.com/explore"
LIST = "https://www.tiktok.com/api/explore/item_list/"
AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
HEADERS = f"User-Agent: {AGENT}\r\nReferer: https://www.tiktok.com/\r\n"       # what a video's address wants to hear
PLAYABLE = "/aweme/v1/play/"        # the one kind of address that plays outside TikTok's own player
ASK = 16                            # videos asked for in one call (a dozen or so come)
KEEP = 100                          # videos remembered behind the one that plays
TIMEOUT = 20
# TikTok's own topics and their numbers, from the Explore page (its "categoryList")
TOPICS = (("120", "Everything"), ("104", "Comedy"), ("113", "Animals"), ("112", "Sports"), ("111", "Food"), ("115", "Cars"),
          ("103", "Games"), ("119", "Singing & dancing"), ("100", "Anime & comics"), ("105", "Daily life"),
          ("118", "Technology"), ("116", "Education"), ("117", "Fitness & health"), ("101", "Shows"), ("108", "Drama"),
          ("106", "Family"), ("107", "Relationship"), ("114", "Society"), ("109", "Outfit"), ("102", "Beauty care"),
          ("110", "Lipsync"))


@dataclass(frozen=True)
class Clip:
    id: str
    author: str                     # the account's name, without the @
    text: str                       # what its author wrote under it
    seconds: float
    address: str


def clip_of(item: dict) -> Clip | None:
    """A video of the list as a Clip; None for what cannot be played here (an advertisement, a slide show of
    photos, a video without an address that plays). H.264 before H.265: every ffmpeg decodes it, and fast."""
    video = item.get("video") or {}
    if item.get("isAd") or item.get("imagePost") or not item.get("id"):
        return None
    choices = []
    for rate in video.get("bitrateInfo") or []:
        for address in (rate.get("PlayAddr") or {}).get("UrlList") or []:
            if PLAYABLE in address:
                choices.append((rate.get("CodecType") != "h264", -(rate.get("Bitrate") or 0), address))
    if not choices:
        return None
    return Clip(str(item["id"]), (item.get("author") or {}).get("uniqueId") or "", " ".join((item.get("desc") or "").split()),
                float(video.get("duration") or 0), min(choices)[2])


class Explore:
    """TikTok's Explore list, asked as a browser without an account asks it."""

    def __init__(self):
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._device = str(random.randint(7_250_000_000_000_000_000, 7_351_147_085_025_500_000))
        self._visited = False

    def _get(self, url: str, query: dict | None = None) -> bytes:
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
                                                       "Accept-Encoding": "gzip", "Referer": PAGE})
        with self._opener.open(request, timeout=TIMEOUT) as answer:
            body = answer.read()
            return gzip.decompress(body) if answer.headers.get("Content-Encoding") == "gzip" else body

    def clips(self, topic: str) -> list[Clip]:
        """A dozen or so videos; others at every call. Raises when TikTok gives none."""
        if not self._visited:                    # the page first, as a browser would: it hands out the cookies
            self._get(PAGE)
            self._visited = True
        query = {"aid": "1988", "app_language": "en", "app_name": "tiktok_web", "browser_language": "en-US",
                 "browser_name": "Mozilla", "browser_online": "true", "browser_platform": "Win32", "browser_version": AGENT[8:],
                 "channel": "tiktok_web", "cookie_enabled": "true", "count": str(ASK), "device_id": self._device,
                 "device_platform": "web_pc", "focus_state": "true", "from_page": "explore", "history_len": "2",
                 "is_fullscreen": "false", "is_page_visible": "true", "language": "en", "os": "windows", "priority_region": "",
                 "referer": "", "region": "US", "screen_height": "1080", "screen_width": "1920", "tz_name": "UTC",
                 "webcast_language": "en", "categoryType": topic}
        try:
            data = json.loads(self._get(LIST, query))
        except ValueError as e:
            raise RuntimeError("TikTok did not answer with its list of videos") from e
        clips = [clip for clip in map(clip_of, data.get("itemList") or []) if clip]
        if not clips:
            raise RuntimeError("TikTok handed out no videos")
        return clips


class Feed:
    """The videos to play, one after the other: where we are in them, and more of them when they run out."""

    def __init__(self, topic: str = "120", source=None):
        self.topic, self._source = topic, source or Explore()
        self.clips: list[Clip] = []
        self.index = 0
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def current(self) -> Clip:
        """The video to play now. Asks TikTok for more when the list has run out (and raises when none come)."""
        with self._lock:
            for _ in range(3):
                if self.index < len(self.clips):
                    break
                new = [clip for clip in self._source.clips(self.topic) if clip.id not in self._seen]
                self._seen.update(clip.id for clip in new)
                self.clips += new
            else:
                raise RuntimeError("TikTok has no other videos right now")
            spare = self.index - KEEP
            if spare > 0:
                del self.clips[:spare]
                self.index -= spare
            return self.clips[self.index]

    def step(self, by: int) -> None:
        with self._lock:
            self.index = max(0, min(len(self.clips), self.index + by))


class TikTok(Video):
    id = "tiktok"
    title = "TikTok"
    description = "Random videos, no account"
    glyph = ""
    actions = (("previous", "Previous"), ("next", "Next"), ("mute", "Mute"))
    settings = (
        Setting("topic", "Videos about", "choice", "120", TOPICS,
                hint="Popular videos from TikTok's Explore page, as somebody who is not logged in sees it. Which ones "
                     "you get depends on where your connection is."),
        Setting("fit", "Picture", "choice", "fill", (("fill", "Fill the screen"), ("fit", "The whole picture, with bars")),
                hint="A video filmed upright is taller than the screen: filling it cuts off the top and the bottom."),
        Setting("after", "When a video ends", "choice", "next", (("next", "Play the next one"), ("again", "Play it again"))),
        SOUND,
        Setting("next_key", "Key for the next video", "shortcut", "Ctrl+Alt+PgDown", action="next",
                hint="Works whatever window is in front, while TikTok is on the cooler. Empty: no key."),
        Setting("previous_key", "Key for the one before", "shortcut", "Ctrl+Alt+PgUp", action="previous"),
        MUTE_KEY,
    )

    def __init__(self, values: dict | None = None, player=Player, feed=None):
        super().__init__(values, player)
        self._feed = feed or Feed(self.values["topic"])
        self._playing: Clip | None = None        # the video the player was last sent to

    def _source(self) -> str:
        return "tiktok"

    def _new_player(self, source: str) -> Player:
        def resolve(_source: str) -> tuple[str, dict, str]:
            clip = self._playing = self._feed.current()
            return clip.address, {**NETWORK, "headers": HEADERS}, f"@{clip.author}"

        def ended() -> None:
            if self.values["after"] != "again":
                self._feed.step(+1)

        return self._make_player(source, resolve, on_end=ended, on_error=lambda: self._feed.step(+1))

    def _name(self, source: str) -> str:
        return self._player.title or "TikTok"

    def act(self, action: str) -> None:
        """Next or Previous (and Mute, which is the Video mode's), from the app's buttons or a key."""
        if action not in ("next", "previous"):
            return super().act(action)
        self._feed.step(+1 if action == "next" else -1)
        if self._player is not None:
            self._player.skip()

    async def frame(self):
        picture = await super().frame()
        player = self._player
        clip = self._playing
        if player is not None and clip is not None and player.state == "playing" and self.direct:
            text = f" · {clip.text}" if clip.text else ""
            self.status = (f"@{clip.author} · {clock_text(player.position)} of {clock_text(player.duration or clip.seconds)}"
                           f"{self._sound_note()}{text}")
        return picture
