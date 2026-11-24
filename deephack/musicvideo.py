"""The music video of a track: which YouTube video it is, if there is one that fits.

For the Now Playing mode, which can play it where the cover is. A search for the artist and the title
gives a handful of videos; the right one is picked by rule, and when no video passes, there is none: a
cover is better than a wrong video.

- **Its length is the track's**, to within a few seconds, **or it is longer** by up to five minutes: a
  scene before the song, a pause in it, credits after it. One of the track's own length plays from the
  track's position at once. A longer one would run beside the music that way, so it plays only once
  deephack has found where in it the song is, by its sound (songplace.py); the same listening tells a
  video whose song is another recording, which is then not shown. Where both kinds are found, the one
  of the track's length is taken. Shorter videos are not: a part of the song is missing from them.
- **It is the artist's or the label's own.** The channel is one YouTube has verified, a VEVO one, or
  carries the artist's name and nothing else. Of fifty tracks tried, a third of what passed without
  this rule was somebody's copy of the video: one with subtitles burnt in, one a "spotlight" channel.
- **It is not something else of the same name**: a list of words (lyrics, audio, visualizer, cover,
  karaoke, live ...) keeps out the uploads that are the album's cover standing still.
- **It is this song**: the title's words are in the video's title.

What was found is kept (`Found`), also that nothing was: a track is searched for once, not at every play.
"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import ytdlp

SEARCH = 8                  # videos asked of the search
CLOSE = 3.5                 # seconds a video's length may differ from the track's to count as "of its length"
LONGER = 300.0              # seconds a video may be longer than its track and still be its video (see `longer`)
RULES = 2                   # how many times these rules have changed: a "nothing fits" by older rules is not believed
NOT_IT = ("lyric", "letra", "audio", "visualizer", "visualiser", "karaoke", "cover", "live", "remix", "reaction", "sped up",
          "slowed", "8d", "instrumental", "behind the scenes", "making of", "teaser", "trailer", "tutorial", "lesson",
          "1 hour", "10 hours", "loop", "nightcore", "mashup", "parody", "full album", "review", "extended")
KEEP, FORGET_NONE = 600, 7 * 86400.0        # tracks remembered; a "nothing found" is believed for a week


@dataclass(frozen=True)
class Candidate:
    id: str
    title: str
    channel: str
    seconds: float
    views: int = 0
    verified: bool = False                      # YouTube's mark on the channel: the artist's or a label's own

    @property
    def link(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"


def plain(text: str) -> str:
    """Small letters without accents, and everything that is not a letter or a digit as a space."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split())


def words(title: str) -> list[str]:
    """What a track's title is known by: its words, without what stands in brackets or after a dash
    ("Remastered 2011", "feat. somebody", "Radio Edit")."""
    title = re.split(r"\s[-–]\s|\(|\[", title or "", maxsplit=1)[0]
    return plain(title).split()


def longer(video_seconds: float, seconds: float) -> bool:
    """The video is longer than its track: it must be listened into place before it is shown."""
    return video_seconds - seconds > CLOSE


def judge(candidate: Candidate, artist: str, title: str, seconds: float) -> float | None:
    """How well a video fits a track: None when it does not, else more for a better one. Any video of the
    track's own length is better than any longer one."""
    over = candidate.seconds - seconds
    if not candidate.seconds or over < -CLOSE or over > LONGER:
        return None
    its, channel, wanted = plain(candidate.title), plain(candidate.channel), plain(title)
    if candidate.channel.strip().lower().endswith("- topic"):           # the album's cover, standing still
        return None
    if any(re.search(rf"\b{re.escape(word)}", its) and not re.search(rf"\b{re.escape(word)}", wanted) for word in NOT_IT):
        return None
    need = words(title)
    if not need or sum(word in its.split() for word in need) < max(1, round(len(need) * 0.7)):
        return None
    who = plain(re.split(r"\s(?:&|and|feat\.?|ft\.?|x|with)\s|,", artist, maxsplit=1, flags=re.IGNORECASE)[0]).split()
    name = "".join(word for word in who if word != "the")
    bare = re.sub(r"^(?:the)?(.*?)(?:official|vevo|music|tv|band|channel)*$", r"\1", channel.replace(" ", ""))
    own = bool(name) and bare == name                                    # "Rammstein Official", "TheKillersMusic"
    vevo = "vevo" in channel.replace(" ", "")
    names_it = "video" in its.split() or "clip" in its.split() or "videoclip" in its
    if not (own or vevo or candidate.verified and names_it):
        return None                                                      # somebody's copy, or a channel that only plays
    if name and name not in its.replace(" ", "") and name not in channel.replace(" ", ""):          # the song to a picture
        return None                                                      # somebody else's song of that name
    fits = 3.0 * names_it + 2.0 * (own or vevo) + ("official" in its.split())
    return fits - over / LONGER if longer(candidate.seconds, seconds) else 100.0 + fits - abs(over) / CLOSE


def search(artist: str, title: str) -> list[Candidate]:
    """What YouTube's search gives for a track. Asks the net; a second or two."""
    yt_dlp = ytdlp.module()
    query = f"ytsearch{SEARCH}:{artist} {title} official video"
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}) as ydl:
        found = ydl.extract_info(query, download=False) or {}
    return [Candidate(str(e["id"]), e.get("title") or "", e.get("channel") or e.get("uploader") or "", float(e.get("duration") or 0),
                      int(e.get("view_count") or 0), bool(e.get("channel_is_verified")))
            for e in found.get("entries") or [] if e and e.get("id")]


def look(candidate: Candidate) -> Candidate:
    """A video looked at by itself: the search leaves out the channel of some. A second or two."""
    yt_dlp = ytdlp.module()
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}) as ydl:
        e = ydl.extract_info(candidate.link, download=False, process=False) or {}
    return Candidate(candidate.id, e.get("title") or candidate.title, e.get("channel") or e.get("uploader") or "",
                     float(e.get("duration") or candidate.seconds), int(e.get("view_count") or 0), bool(e.get("channel_is_verified")))


def pick(candidates: list[Candidate], artist: str, title: str, seconds: float, look=None) -> Candidate | None:
    """The best of the candidates that fit, or None. `look` is asked about a video whose channel the
    search left out, if it would fit otherwise (at most two of them)."""
    looked = 0
    for i, candidate in enumerate(candidates):
        if look is not None and not candidate.channel and looked < 2 and -CLOSE <= candidate.seconds - seconds <= LONGER:
            looked += 1
            try:
                candidates = [*candidates[:i], look(candidate), *candidates[i + 1:]]
            except Exception:                                            # noqa: BLE001  (then it stays unknown)
                pass
    judged = [(score, candidate.views, candidate) for candidate in candidates
              if (score := judge(candidate, artist, title, seconds)) is not None]
    return max(judged, key=lambda j: j[:2])[2] if judged else None


def find(artist: str, title: str, seconds: float, search=search, look=look) -> Candidate | None:
    """The track's music video, or None. Raises what the search raises (no net, YouTube saying no)."""
    if not title or not artist or seconds < 30:
        return None
    return pick(search(artist, title), artist, title, seconds, look)


class Found:
    """What was found for which track, kept in a file: a track is searched for once."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        try:
            self._known: dict[str, list] = json.loads(path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            self._known = {}

    @staticmethod
    def key(artist: str, title: str, seconds: float) -> str:
        return f"{plain(artist)}|{plain(title)}|{round(seconds)}"

    def get(self, artist: str, title: str, seconds: float, now: float | None = None) -> str | None:
        """The link kept for a track; "" for "nothing fits"; None when it has not been looked for (lately,
        or by the rules as they are now)."""
        with self._lock:
            kept = self._known.get(self.key(artist, title, seconds))
        if kept is None:
            return None
        link, when, *more = kept
        rules = more[1] if len(more) > 1 else 1
        if not link and (rules < RULES or (time.time() if now is None else now) - when > FORGET_NONE):
            return None                                                  # perhaps there is one by now
        return link

    def length(self, artist: str, title: str, seconds: float) -> float:
        """How long the video kept for a track is, in seconds; 0 where that was not kept (it is of the
        track's own length then: the only kind there was)."""
        with self._lock:
            kept = self._known.get(self.key(artist, title, seconds))
        return float(kept[2]) if kept and len(kept) > 2 else 0.0

    def put(self, artist: str, title: str, seconds: float, link: str, now: float | None = None, length: float = 0.0) -> None:
        with self._lock:
            self._known[self.key(artist, title, seconds)] = [link, time.time() if now is None else now, length, RULES]
            while len(self._known) > KEEP:
                self._known.pop(next(iter(self._known)))
            try:
                self.path.write_text(json.dumps(self._known), encoding="utf8")
            except OSError:
                pass
