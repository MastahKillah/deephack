"""Days until a date you pick, with hours and then minutes on the last day.

On DeepCool's own firmware the number only changes once a day until the final 24 hours, so this is about
as gentle on the cooler's flash as a mode gets: one write a day, then one an hour, then one every five
minutes in the last hour.

With the direct drawing firmware (`direct`, set by the engine) a picture costs nothing, and the countdown
ticks: days over a clock of hours, minutes and seconds, a new picture every second; on the last day the
clock alone, large; and confetti for a while when it reaches zero.

The Christmas look (a setting) is the same countdown in a winter night: a string of lights along the
top, fir trees in the snow, one of them lit. Drawn pictures move: the lights glow and fade, each at its
own pace, and snow falls, ten pictures a second. With no date set it counts to the next 25 December,
and on the day it says "Merry Christmas" for as long as the day lasts. On DeepCool's own firmware it
is the same picture standing still, rewritten as seldom as the plain one.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter

from .. import render
from ..render import H, W
from .base import Mode, Setting

BG_TOP, BG_BOTTOM = (34, 30, 64), (12, 12, 26)
TEXT, MUTED, ACCENT = (244, 244, 252), (160, 158, 190), (150, 132, 255)
TRACK = (255, 255, 255, 20)
MINUTE_STEP = 5           # last hour: minutes are rounded up to this, so the display changes every 5 minutes
PARTY_SECONDS = 600       # drawn pictures: how long the confetti falls once the countdown is at zero
PARTY_TICK = 0.1          # ... and how often it is drawn
CONFETTI = ((150, 132, 255), (255, 214, 102), (255, 122, 144), (112, 214, 190), (244, 244, 252))
LOOKS = (("plain", "Plain"), ("christmas", "Christmas"))
# The Christmas look
NIGHT_TOP, NIGHT_BOTTOM = (7, 12, 36), (30, 46, 96)
SNOW, SNOW_FAR, INK = (240, 245, 253), (172, 190, 224), (52, 68, 112)
WARM, FROST, GOLD = (255, 248, 236), (178, 192, 224), (255, 204, 104)
BULBS = ((255, 96, 96), (255, 204, 104), (112, 218, 146), (112, 172, 255))
FIR, TRUNK = ((18, 70, 56), (26, 92, 70)), (64, 46, 38)
FIRS = ((78, 612, 156, 60), (404, 606, 120, 46), (152, 604, 62, 26), (336, 601, 76, 30))    # x, foot, height, half its width
SNOW_TICK = 0.1           # drawn pictures: how often the snow and the lights are drawn
SUPER = 2                 # the winter night is drawn this much larger and made smaller: smooth edges


def parse_target(text: str) -> tuple[datetime, bool] | None:
    """(target, has_time) from '2026-12-24', '2026-12-24 18:00' or '24.12.2026 18:00'; None if unreadable."""
    text = " ".join(text.replace("T", " ").split())
    if not text:
        return None
    for fmt, has_time in (("%Y-%m-%d %H:%M", True), ("%Y-%m-%d", False), ("%d.%m.%Y %H:%M", True), ("%d.%m.%Y", False)):
        try:
            return datetime.strptime(text, fmt), has_time
        except ValueError:
            continue
    return None


def remaining(target: datetime, now: datetime) -> tuple[str, int, str]:
    """What the big number shows: (state, value, unit), state being 'ahead', 'today' or 'past'.

    More than 24 hours away counts calendar days, the way people say "Christmas is in five days".
    Inside the last 24 hours it switches to hours, and to minutes (rounded up) in the last hour.
    """
    left = target - now
    if left.total_seconds() <= 0:
        if target.date() == now.date():
            return "today", 0, ""
        days = (now.date() - target.date()).days
        return "past", days, "day ago" if days == 1 else "days ago"
    if left >= timedelta(days=1):
        days = (target.date() - now.date()).days
        return "ahead", days, "day to go" if days == 1 else "days to go"
    if left >= timedelta(hours=1):
        hours = int(left.total_seconds() // 3600)
        return "ahead", hours, "hour to go" if hours == 1 else "hours to go"
    minutes = min(60, MINUTE_STEP * math.ceil(left.total_seconds() / 60 / MINUTE_STEP))
    return "ahead", minutes, "minutes to go"


def _when_text(target: datetime, has_time: bool) -> str:
    text = f"{target:%A} {target.day} {target:%B %Y}"
    return text + (f", {target:%H:%M}" if has_time else "")


def split(seconds: int) -> tuple[int, int, int, int]:
    """(days, hours, minutes, seconds) of a number of seconds."""
    return seconds // 86400, seconds // 3600 % 24, seconds // 60 % 60, seconds % 60


def christmas_day(now: datetime) -> datetime:
    """The 25 December that is today or comes next."""
    this_year = datetime(now.year, 12, 25)
    return this_year if now.date() <= this_year.date() else datetime(now.year + 1, 12, 25)


def colours(look: str) -> tuple[tuple, tuple, tuple]:
    """(text, quiet text, accent) of a look."""
    return (WARM, FROST, GOLD) if look == "christmas" else (TEXT, MUTED, ACCENT)


def _tiers(x: float, foot: float, height: float, half: float, tiers: int = 4):
    """A fir as its tiers from the top down: (apex y, base y, half the width of its base)."""
    top, bottom = foot - height, foot - height * 0.1
    step = (bottom - top) / tiers
    return [(top + k * step * 0.8, top + (k + 1) * step + (step * 0.22 if k < tiers - 1 else 0), half * (k + 1) / tiers)
            for k in range(tiers)]


def _garland() -> list[tuple[float, float]]:
    """The string of lights along the top, in two swags: where it hangs, left to right."""
    return [(x, 8 + 28 * math.sin(math.pi * (x % (W / 2)) / (W / 2))) for x in range(0, W + 1, 4)]


def _lights() -> tuple[tuple[float, float, tuple], ...]:
    """Every light that glows: (x, y, colour). Those on the string, then those on the tall fir."""
    lights = []
    for swag in (0, 1):
        for u in (0.1, 0.26, 0.42, 0.58, 0.74, 0.9):
            x = (swag + u) * W / 2
            lights.append((x, 8 + 28 * math.sin(math.pi * u) + 9))
    x, foot, height, half = FIRS[0]
    for k, (apex, base, width) in enumerate(_tiers(x, foot, height, half)):
        for at in ((-0.5, 0.5) if k < 2 else (-0.62, 0.02, 0.6))[k == 0:]:
            lights.append((x + at * width, base - 9 - 5 * abs(at)))
    return tuple((lx, ly, BULBS[i % len(BULBS)]) for i, (lx, ly) in enumerate(lights))


LIGHTS = _lights()
# Snow: (x, where in its fall it is at time 0, pixels a second, where in its sway, size, how white)
FLAKES = tuple((random.Random(i + 900).uniform(0, W), random.Random(i + 1000).uniform(0, H + 20),
                random.Random(i + 1100).uniform(22, 74), random.Random(i + 1200).uniform(0, 6.3),
                random.Random(i + 1300).choice((1.2, 1.6, 2.0, 2.6, 3.2)), random.Random(i + 1400).randrange(110, 236))
               for i in range(120))


@lru_cache(maxsize=1)
def _winter() -> Image.Image:
    """The winter night without anything that moves or says something: sky, stars, hills, firs, the
    string the lights hang on."""
    s = SUPER
    img = render.vertical_gradient(NIGHT_TOP, NIGHT_BOTTOM).resize((W * s, H * s), Image.Resampling.BILINEAR)
    d = ImageDraw.Draw(img)
    rnd = random.Random(25)
    for _ in range(90):
        x, y, r = rnd.uniform(0, W) * s, rnd.uniform(40, H * 0.72) * s, rnd.choice((0.6, 0.8, 1.0, 1.4)) * s
        bright = rnd.randrange(110, 230)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(bright, bright, min(255, bright + 24)))
    d.ellipse([-170 * s, 566 * s, 300 * s, 780 * s], fill=SNOW_FAR)                 # hills far off
    d.ellipse([190 * s, 578 * s, 690 * s, 820 * s], fill=SNOW_FAR)
    for x, foot, height, half in sorted(FIRS, key=lambda fir: fir[2]):               # the small ones stand behind
        d.rectangle([(x - half * 0.11) * s, (foot - height * 0.14) * s, (x + half * 0.11) * s, foot * s], fill=TRUNK)
        tiers = _tiers(x, foot, height, half)
        for k, (apex, base, width) in reversed(list(enumerate(tiers))):
            d.polygon([(x * s, apex * s), ((x - width) * s, base * s), ((x + width) * s, base * s)], fill=FIR[k % 2])
            cap = apex + (base - apex) * 0.46                                        # snow on its shoulders
            d.polygon([(x * s, apex * s), ((x - width * 0.5) * s, cap * s), ((x - width * 0.18) * s, (cap - 5) * s),
                       (x * s, (cap + 2) * s), ((x + width * 0.2) * s, (cap - 5) * s), ((x + width * 0.5) * s, cap * s)], fill=SNOW)
    x, foot, height, _ = FIRS[0]                                                     # a star on the tall one
    top = foot - height
    star = [(x + r * math.sin(i * math.pi / 5), top - 2 - r * math.cos(i * math.pi / 5))
            for i, r in ((i, 11 if i % 2 == 0 else 4.6) for i in range(10))]
    d.polygon([(px * s, py * s) for px, py in star], fill=GOLD)
    d.ellipse([-110 * s, 604 * s, 590 * s, 790 * s], fill=SNOW)                      # the snow in front
    d.line([(px * s, py * s) for px, py in _garland()], fill=(82, 96, 136), width=2 * s)
    for lx, ly, _ in LIGHTS[:12]:                                                    # what each light hangs by
        d.line([(lx * s, (ly - 9) * s), (lx * s, (ly - 4) * s)], fill=(82, 96, 136), width=2 * s)
    return img.resize((W, H), Image.Resampling.LANCZOS)


def _alive(img: Image.Image, t: float, heavy: bool = False) -> Image.Image:
    """The winter night at time `t`: the lights glowing, each at its own pace, and the snow where it has fallen to."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i, (x, y, colour) in enumerate(LIGHTS):
        glow = 0.5 + 0.5 * math.sin(t * (1.3 + i % 5 * 0.35) + i * 1.9)
        lit = 0.4 + 0.6 * glow
        d.ellipse([x - 13, y - 13, x + 13, y + 13], fill=colour + (int(14 + 50 * glow),))
        d.ellipse([x - 8, y - 8, x + 8, y + 8], fill=colour + (int(30 + 90 * glow),))
        d.ellipse([x - 4.5, y - 4.5, x + 4.5, y + 4.5], fill=tuple(int(c * lit) for c in colour) + (255,))
    for x, y0, speed, sway, size, white in FLAKES if heavy else FLAKES[:70]:
        y = (y0 + speed * t) % (H + 20) - 10
        x = (x + 14 * math.sin(t * 0.7 + sway)) % W
        d.ellipse([x - size, y - size, x + size, y + size], fill=(255, 255, 255, white))
    out = img.convert("RGBA")
    out.alpha_composite(layer)
    return out.convert("RGB")


@lru_cache(maxsize=4)
def _page(title: str, target: datetime, has_time: bool, look: str = "plain") -> Image.Image:
    """The page without its number: background, title and the target date on its card. The glow is blurred,
    which is slow, so a countdown that ticks every second draws it once."""
    f = render.font
    if look == "christmas":
        img = _winter().copy()
        d = ImageDraw.Draw(img)
        y = 62
        for line in render.wrap(d, title, f(render.SEMIBOLD, 36), W - 60, 2) if title else ():
            d.text((W / 2, y), line, font=f(render.SEMIBOLD, 36), fill=WARM, anchor="ma")
            y += 46
        when = render.fit(d, _when_text(target, has_time), f(render.SEMIBOLD, 19), W - 150)
        d.text((W / 2, 621), when, font=f(render.SEMIBOLD, 19), fill=INK, anchor="mm")      # on the snow
        return img
    img = render.vertical_gradient(BG_TOP, BG_BOTTOM).convert("RGBA")

    # soft glow behind the number
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W / 2 - 200, 150, W / 2 + 200, 470], fill=ACCENT + (34,))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(60)))
    d = ImageDraw.Draw(img)
    d.text((W / 2, 56), "COUNTDOWN", font=f(render.BOLD, 17), fill=MUTED, anchor="mm")
    y = 96
    for line in render.wrap(d, title or "Countdown", f(render.SEMIBOLD, 36), W - 60, 2):
        d.text((W / 2, y), line, font=f(render.SEMIBOLD, 36), fill=TEXT, anchor="ma")
        y += 46

    # the target date on a card at the bottom
    card = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle([40, 520, W - 40, 590], radius=18, fill=TRACK)
    img.alpha_composite(card)
    d = ImageDraw.Draw(img)
    when = render.fit(d, _when_text(target, has_time), f(render.REGULAR, 22), W - 110)
    d.text((W / 2, 555), when, font=f(render.REGULAR, 22), fill=TEXT, anchor="mm")
    return img.convert("RGB")


def _clock(d: ImageDraw.ImageDraw, text: str, y: float, font, fill) -> None:
    """A running clock, centred: every digit gets the same room, so it does not shiver as they change."""
    digit = max(d.textlength(c, font=font) for c in "0123456789")
    widths = [digit if c.isdigit() else d.textlength(c, font=font) + digit * 0.1 for c in text]
    x = W / 2 - sum(widths) / 2
    for c, width in zip(text, widths):
        d.text((x + width / 2, y), c, font=font, fill=fill, anchor="mm")
        x += width


def render_ticking(title: str, target: datetime, has_time: bool, seconds: int, look: str = "plain", t: float = 0.0) -> Image.Image:
    """The countdown with `seconds` to go, to the second: days over a clock, or on the last day the clock
    alone. `t` is the moment, for what moves in a look."""
    img = _page(title, target, has_time, look).copy()
    d = ImageDraw.Draw(img)
    f = render.font
    TEXT, MUTED, ACCENT = colours(look)                      # noqa: N806  (this look's, under the names used below)
    days, hours, minutes, secs = split(seconds)
    clock = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    if days:
        big = f"{days:,}".replace(",", " ")
        size = 200
        while size > 60 and d.textlength(big, font=f(render.LIGHT, size)) > W - 70:
            size -= 10
        d.text((W / 2, 290), big, font=f(render.LIGHT, size), fill=TEXT, anchor="mm")
        d.text((W / 2, 398), "day" if days == 1 else "days", font=f(render.SEMIBOLD, 30), fill=ACCENT, anchor="mm")
        _clock(d, clock, 456, f(render.SEMIBOLD, 46), TEXT)
    else:
        _clock(d, clock, 310, f(render.LIGHT, 116), TEXT)
        for x, label in ((W / 2 - 152, "hours"), (W / 2, "minutes"), (W / 2 + 152, "seconds")):
            d.text((x, 392), label, font=f(render.REGULAR, 19), fill=MUTED, anchor="mm")
        d.text((W / 2, 452), "to go", font=f(render.SEMIBOLD, 30), fill=ACCENT, anchor="mm")
    return _alive(img, t) if look == "christmas" else img


# Confetti: (x, where in its fall it is at time 0, pixels a second, where in its sway, colour, size)
PIECES = tuple((random.Random(i).randrange(0, W), random.Random(i + 100).uniform(0, H + 40), random.Random(i + 200).uniform(90, 220),
                random.Random(i + 300).uniform(0, 6.3), CONFETTI[i % len(CONFETTI)], random.Random(i + 400).choice((8, 10, 12)))
               for i in range(70))


def _merry(img: Image.Image) -> None:
    d = ImageDraw.Draw(img)
    d.text((W / 2, 262), "Merry", font=render.font(render.LIGHT, 118), fill=WARM, anchor="mm")
    d.text((W / 2, 372), "Christmas", font=render.font(render.SEMIBOLD, 76), fill=GOLD, anchor="mm")


def render_party(title: str, target: datetime, has_time: bool, t: float, look: str = "plain") -> Image.Image:
    """Zero: "Today is the day", under confetti that has been falling for `t` seconds. In the Christmas
    look: "Merry Christmas", in thicker snow."""
    if look == "christmas":                                  # the title is not said twice
        img = _page("" if title.lower() == "christmas" else title, target, has_time, look).copy()
        _merry(img)
        return _alive(img, t, heavy=True)
    img = _page(title, target, has_time, look).copy()
    d = ImageDraw.Draw(img)
    d.text((W / 2, 318), "Today", font=render.font(render.LIGHT, 150), fill=TEXT, anchor="mm")
    d.text((W / 2, 444), "is the day", font=render.font(render.SEMIBOLD, 30), fill=ACCENT, anchor="mm")
    for i, (x, y0, speed, sway, colour, size) in enumerate(PIECES):
        fallen = speed * t                                   # all of it starts above the picture
        y = (y0 + fallen) % (H + 40) - 40 if fallen > H + 40 - y0 else fallen - y0 - 40
        x += 14 * math.sin(t * 2.2 + sway)
        flat = math.sin(t * 5 + sway) > 0                    # tumbling: seen flat, then on edge
        d.rectangle([x, y, x + (size if flat else size // 2), y + (size // 2 if flat else size)], fill=colour)
    return img


def render_countdown(title: str, target: datetime, has_time: bool, state: str, value: int, unit: str,
                     look: str = "plain") -> Image.Image:
    if look == "christmas" and state == "today":
        return render_party(title, target, has_time, 0.0, look)
    img = _page(title, target, has_time, look).convert("RGBA")
    f = render.font
    TEXT, MUTED, ACCENT = colours(look)                      # noqa: N806
    d = ImageDraw.Draw(img)

    big = "Today" if state == "today" else f"{value:,}".replace(",", " ")
    size = 230
    while size > 60 and d.textlength(big, font=f(render.LIGHT, size)) > W - 70:
        size -= 10
    d.text((W / 2, 318), big, font=f(render.LIGHT, size), fill=MUTED if state == "past" else TEXT, anchor="mm")
    d.text((W / 2, 444), "is the day" if state == "today" else unit, font=f(render.SEMIBOLD, 30),
           fill=MUTED if state == "past" else ACCENT, anchor="mm")
    return _alive(img.convert("RGB"), 0.0) if look == "christmas" else img.convert("RGB")


class Countdown(Mode):
    id = "countdown"
    title = "Countdown"
    description = "Days until a date you pick"
    claims = "last day"
    glyph = ""
    settings = (
        Setting("title", "Title", "text", "", placeholder="e.g. Holidays"),
        Setting("date", "Date", "text", "", placeholder="2026-12-24 or 2026-12-24 18:00",
                hint="Counts days, then hours and minutes on the last day. On DeepCool's own firmware it rewrites the "
                     "display once a day until then. With the direct drawing firmware it ticks every second, and "
                     "nothing is stored."),
        Setting("look", "Look", "choice", "plain", LOOKS,
                hint="Christmas: a winter night with a string of lights and firs in the snow; with the direct drawing "
                     "firmware the lights glow and snow falls. With no date set it counts to the next 25 December."),
    )

    def __init__(self, values=None):
        super().__init__(values)
        self._shown = None
        self._look = None                              # drawn pictures: what the last one showed
        self.placeholder = ("Countdown", "Pick a date in the settings on the right.")

    def _drawn(self, title: str, target: datetime, has_time: bool, now: datetime) -> Image.Image | None:
        """The countdown to the second, when that is not what the last picture showed. Asks to be called
        again when the next second begins."""
        left = (target - now).total_seconds()
        theme = self.values["look"]
        snow = theme == "christmas"                    # it moves: a picture every SNOW_TICK, whatever the clock says
        t = round(now.timestamp(), 2) if snow else 0.0
        merry = snow and left <= 0 and remaining(target, now)[0] == "today"      # all of the day, not ten minutes of it
        if left > 0:
            seconds = math.ceil(left)                  # so it reads 0:00:01 until the very moment
            self.tick = SNOW_TICK if snow else max(0.02, left - (seconds - 1) + 0.01)
            look = (title, target, has_time, seconds, t)
            self.status = (f"{title}: " if title else "") + "{0} d {1:02d}:{2:02d}:{3:02d} to go".format(*split(seconds))
        elif merry:
            self.tick, look = SNOW_TICK, (title, target, has_time, "merry", t)
        elif -left < PARTY_SECONDS:
            self.tick, look = PARTY_TICK, (title, target, has_time, "party", round(-left, 2))
        else:
            self.tick, look = None, (title, target, has_time, remaining(target, now))
        if look == self._look:
            return None
        self._look = look
        if left > 0:
            return render_ticking(title, target, has_time, seconds, theme, t)
        if merry:
            return render_party(title, target, has_time, t, theme)
        if -left < PARTY_SECONDS:
            return render_party(title, target, has_time, -left, theme)
        return render_countdown(title, target, has_time, *remaining(target, now), theme)

    async def frame(self):
        now = datetime.now()
        theme, title = self.values["look"], self.values["title"].strip()
        if theme == "christmas" and not self.values["date"].strip():
            parsed, title = (christmas_day(now), False), title or "Christmas"
        else:
            parsed = parse_target(self.values["date"])
        self.active = False
        if parsed is None:
            self.status = "No date set" if not self.values["date"].strip() else "Date not understood"
            if self.values["date"].strip():
                self.placeholder = ("Date not understood", "Write it as 2026-12-24, optionally with a time: 2026-12-24 18:00.")
            return None
        target, has_time = parsed
        state, value, unit = remaining(target, now)
        self.active = state == "today" or (state == "ahead" and not unit.startswith("day"))
        self.status = ((f"{title}: " if title else "") +
                       ("today" if state == "today" else f"{value} {unit}"))
        if self.direct:
            self._shown = None
            return self._drawn(title, target, has_time, now)
        self._look, self.tick = None, None
        key = (title, target, has_time, state, value, unit, theme)
        if key == self._shown:
            return None
        self._shown = key
        return render_countdown(title, target, has_time, state, value, unit, theme)
