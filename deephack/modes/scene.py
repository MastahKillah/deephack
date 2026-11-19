"""Scene: a little pixel-art city under your real sky.

Dawn, day, dusk and night follow the actual sunrise and sunset, and the weather (clear, clouds, fog,
rain, snow, thunderstorms) is the current weather in the city set on the Weather page, from Open-Meteo.
Windows light up in the evening, street lamps glow, a car drives by.

Like the pet it is a short animation stored on the cooler (8 frames), rewritten only when the time of
day or the kind of weather changes: a handful of times a day. A new kind of weather has to show up in
two forecasts in a row before the scene follows it, so weather flickering between two codes costs nothing.
Everything is drawn on a 120 x 160 canvas and scaled up four times for real chunky pixels.
"""
from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw

from ..render import H, W
from . import weather
from .base import Animation, Mode, Setting

LW, LH = 120, 160
FRAMES, LOOP_MS = 8, 2400
FETCH_SECONDS, RETRY_SECONDS = 20 * 60, 5 * 60
TWILIGHT = timedelta(minutes=45)
STREET = 142                                   # top of the pavement

PHASES = ("dawn", "day", "dusk", "night")
KINDS = ("clear", "partly", "cloudy", "fog", "rain", "snow", "thunder")
SKY = {"night": ((6, 8, 26), (28, 32, 74)), "dawn": ((54, 60, 124), (250, 170, 124)),
       "day": ((60, 130, 226), (170, 212, 246)), "dusk": ((38, 26, 76), (238, 118, 92))}
GREY = {"night": (26, 28, 40), "dawn": (132, 126, 138), "day": (142, 150, 164), "dusk": (98, 86, 104)}
OVERCAST = {"clear": 0.0, "partly": 0.08, "cloudy": 0.55, "fog": 0.7, "rain": 0.62, "snow": 0.5, "thunder": 0.75}
LIT = {"night": 0.45, "dusk": 0.25, "dawn": 0.15, "day": 0.0}
WARM, RED_LIGHT = (255, 212, 120), (255, 70, 70)


def kind_for(code: int | None) -> str:
    if code in (0, 1):
        return "clear"
    if code == 2:
        return "partly"
    if code == 3 or code is None:
        return "cloudy" if code == 3 else "clear"
    return weather.WMO.get(code, ("cloudy", ""))[0]


def phase_at(now: datetime, sunrise: datetime, sunset: datetime) -> str:
    if abs(now - sunrise) <= TWILIGHT:
        return "dawn"
    if abs(now - sunset) <= TWILIGHT:
        return "dusk"
    return "day" if sunrise < now < sunset else "night"


def _mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


# ------------------------------------------------------------------ the city (the same every time)
def _skyline(seed: int, base: int, low: int, high: int, narrow: int, wide: int) -> list[tuple[int, int, int, int]]:
    rng, x, out = random.Random(seed), -random.Random(seed).randrange(0, 6), []
    while x < LW:
        w, h = rng.randrange(narrow, wide), rng.randrange(low, high)
        out.append((x, base - h, x + w, base))
        x += w + rng.randrange(0, 3)
    return out


FAR = _skyline(4, 132, 26, 66, 8, 15)
NEAR = _skyline(11, STREET, 16, 50, 11, 21)
WINDOWS = [(x, y, random.Random(x * 1000 + y).random())          # (x, y, how likely it is lit)
           for x0, y0, x1, y1 in NEAR for x in range(x0 + 2, x1 - 2, 4) for y in range(y0 + 4, y1 - 4, 5)]
FAR_LIGHTS = [(x, y, random.Random(x * 77 + y).random())
              for x0, y0, x1, y1 in FAR for x in range(x0 + 2, x1 - 1, 3) for y in range(y0 + 3, y1 - 2, 4)]
STARS = [(random.Random(i).randrange(0, LW), random.Random(i + 500).randrange(0, 96)) for i in range(48)]
LAMPS = (14, 56, 98)
TALLEST = min(NEAR, key=lambda b: b[1])


def _cloud(d: ImageDraw.ImageDraw, x: int, y: int, w: int, colour) -> None:
    h = max(4, w // 3)
    d.rectangle([x, y + h // 2, x + w, y + h], fill=colour)
    d.ellipse([x + w // 6, y, x + w // 6 + h + 2, y + h + 1], fill=colour)
    d.ellipse([x + w // 2 - 2, y - h // 3, x + w // 2 + h + 1, y + h], fill=colour)


def render_frame(f: int, phase: str, kind: str) -> Image.Image:
    over = OVERCAST[kind]
    top, bottom = (_mix(c, GREY[phase], over) for c in SKY[phase])
    if kind == "thunder" and f == 3:                    # the flash
        top, bottom = _mix(top, (230, 230, 255), 0.6), _mix(bottom, (230, 230, 255), 0.6)
    img = Image.new("RGB", (LW, LH))
    d = ImageDraw.Draw(img)
    for y in range(STREET):
        d.line([(0, y), (LW, y)], fill=_mix(top, bottom, y / STREET))
    night = phase == "night"

    if night and over < 0.5:
        for i, (x, y) in enumerate(STARS):
            if (f + i) % 6 == 0:
                d.point([(x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)], fill=(150, 156, 200))
            d.point((x, y), fill=(230, 232, 255) if (f + i) % 3 else (150, 156, 200))
    if over < 0.5:
        if night:
            mx, my = 24, 24
            d.ellipse([mx - 7, my - 7, mx + 7, my + 7], fill=(240, 234, 200))
            d.ellipse([mx - 4, my - 9, mx + 10, my + 5], fill=_mix(top, bottom, (my - 2) / STREET))
        else:
            sx, sy, colour = {"dawn": (28, 112, (255, 168, 92)), "day": (88, 30, (255, 228, 120)),
                              "dusk": (92, 108, (255, 132, 82))}[phase]
            d.ellipse([sx - 10, sy - 10, sx + 10, sy + 10], fill=_mix(colour, bottom, 0.55))
            d.ellipse([sx - 8, sy - 8, sx + 8, sy + 8], fill=colour)

    cloud = _mix({"night": (52, 56, 78), "dawn": (236, 196, 190), "day": (248, 250, 255), "dusk": (170, 120, 140)}[phase],
                 GREY[phase], over * 0.8)
    if kind == "partly":
        for x, y, w in ((8, 40, 26), (64, 18, 32), (90, 54, 20)):
            _cloud(d, x, y, w, cloud)
    elif over >= 0.45:
        dark = _mix(cloud, (0, 0, 0), 0.18 if kind in ("rain", "thunder") else 0.05)
        for i, (x, y, w) in enumerate(((-8, 6, 40), (24, 0, 46), (64, 8, 40), (92, 2, 40), (6, 24, 34), (46, 22, 40), (84, 28, 36))):
            _cloud(d, x, y, w, dark if i < 4 else cloud)

    # far buildings, hazy, with a few tiny lights
    far = _mix(bottom, (20, 22, 36) if night else (70, 74, 96), 0.45)
    for b in FAR:
        d.rectangle(b, fill=far)
    for x, y, p in FAR_LIGHTS:
        if p < LIT[phase] * 0.5:
            d.point((x, y), fill=_mix(WARM, far, 0.35))
    if kind == "thunder" and f in (3, 4):
        pts = [(70, 14), (64, 34), (72, 38), (62, 62), (70, 66), (60, 92)]
        d.line(pts, fill=(255, 255, 230) if f == 3 else (190, 190, 230), width=2 if f == 3 else 1)

    # near buildings with their windows
    near = (14, 15, 26) if night else _mix((46, 50, 70), GREY[phase], over * 0.5)
    for b in NEAR:
        d.rectangle(b, fill=near)
        if kind == "snow":
            d.line([(b[0], b[1]), (b[2], b[1])], fill=(236, 240, 250))
    tx = (TALLEST[0] + TALLEST[2]) // 2
    d.line([(tx, TALLEST[1] - 8), (tx, TALLEST[1])], fill=near)
    if phase in ("night", "dusk") and f < 4:
        d.point((tx, TALLEST[1] - 8), fill=RED_LIGHT)
    unlit = _mix(near, bottom, 0.35)
    for i, (x, y, p) in enumerate(WINDOWS):
        lit = p < LIT[phase]
        if lit and i % 23 == f % 8:                     # someone switches a light off for a moment
            lit = False
        d.rectangle([x, y, x + 1, y + 1], fill=WARM if lit else unlit)

    # street
    d.rectangle([0, STREET, LW, STREET + 3], fill=(236, 240, 250) if kind == "snow" else (70, 72, 84) if not night else (40, 41, 52))
    d.rectangle([0, STREET + 4, LW, LH], fill=(30, 31, 38) if not night else (18, 18, 24))
    for x in range(4, LW, 16):
        d.line([(x, 153), (x + 6, 153)], fill=(120, 116, 90) if not night else (70, 68, 56))
    glow = phase in ("night", "dusk") or over >= 0.6
    layer = Image.new("RGBA", (LW, LH), (0, 0, 0, 0))
    g = ImageDraw.Draw(layer)
    for x in LAMPS:
        d.line([(x, 126), (x, STREET + 3)], fill=(60, 62, 74))
        d.line([(x, 126), (x + 3, 126)], fill=(60, 62, 74))
        if glow:
            g.ellipse([x - 1, 123, x + 7, 131], fill=WARM + (60,))           # halo around the lamp
            g.ellipse([x - 7, STREET - 1, x + 13, STREET + 5], fill=WARM + (70,))   # light pool on the pavement
            if kind in ("rain", "thunder"):                 # reflections on the wet road
                g.rectangle([x + 1, STREET + 6, x + 4, LH], fill=WARM + (40,))
    img.paste(layer, (0, 0), layer)
    for x in LAMPS:
        d.point((x + 3, 127), fill=WARM if glow else (150, 150, 140))

    # a car drives past once per loop
    cx = -18 + f * 18
    d.rectangle([cx, 147, cx + 14, 151], fill=(196, 58, 58))
    d.rectangle([cx + 3, 144, cx + 10, 147], fill=(170, 46, 46))
    d.rectangle([cx + 4, 145, cx + 9, 146], fill=(120, 170, 210) if not night else (60, 70, 90))
    d.point([(cx + 3, 152), (cx + 11, 152)], fill=(10, 10, 12))
    if glow:
        beam = Image.new("RGBA", (LW, LH), (0, 0, 0, 0))
        ImageDraw.Draw(beam).polygon([(cx + 14, 148), (cx + 30, 145), (cx + 30, 152)], fill=WARM + (70,))
        img.paste(beam, (0, 0), beam)
        d.point((cx + 14, 148), fill=WARM)

    # weather in front of everything
    if kind in ("rain", "thunder"):
        rng = random.Random(7)
        for _ in range(80):
            x, y0 = rng.randrange(0, LW + 10), rng.randrange(0, LH)
            y = (y0 + f * 20) % LH                      # a whole screen height per loop: seamless
            d.line([(x, y), (x - 1, y + 3)], fill=(150, 176, 214) if not night else (90, 110, 150))
    elif kind == "snow":
        rng = random.Random(9)
        for i in range(90):
            x, band, y0 = rng.randrange(0, LW), rng.randrange(0, 4), rng.randrange(0, 40)
            y = band * 40 + (y0 + f * 5) % 40
            x += 1 if (f + i) % 4 < 2 else 0
            d.point((x, y), fill=(240, 244, 255))
            if i % 3 == 0:
                d.point((x + 1, y), fill=(240, 244, 255))
    elif kind == "fog":
        haze = Image.new("RGBA", (LW, LH), (0, 0, 0, 0))
        h = ImageDraw.Draw(haze)
        colour = _mix(GREY[phase], (255, 255, 255), 0.25)
        for i, (y, alpha) in enumerate(((84, 90), (100, 120), (116, 140), (130, 110))):
            shift = 2 if (f + i) % 4 < 2 else 0
            h.rectangle([-4 + shift, y, LW + 4, y + 14], fill=colour + (alpha,))
        img.paste(haze, (0, 0), haze)
    return img.resize((W, H), Image.NEAREST)


def render_animation(phase: str, kind: str) -> Animation:
    return Animation([render_frame(i, phase, kind) for i in range(FRAMES)], LOOP_MS)


# ------------------------------------------------------------------ the mode
def fetch_sky(city: str) -> tuple[str, int | None, datetime, datetime]:
    """(place, current weather code, today's sunrise, today's sunset) for a city."""
    name, lat, lon = weather.locate(city)
    data = weather._get_json(weather.FORECAST_URL, {"latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1,
                                                    "current": "weather_code", "daily": "sunrise,sunset"})
    zone = timezone(timedelta(seconds=int(data.get("utc_offset_seconds", 0))))
    sun = [datetime.fromisoformat(data["daily"][k][0]).replace(tzinfo=zone) for k in ("sunrise", "sunset")]
    return name, data["current"].get("weather_code"), sun[0], sun[1]


class Scene(Mode):
    id = "scene"
    title = "Scene"
    description = "A pixel city under your sky"
    glyph = ""
    reads = ("weather",)                       # the city
    settings = (
        Setting("weather", "Weather", "choice", "live", (
            ("live", "Live, for the city on the Weather page"), ("clear", "Clear"), ("partly", "Partly cloudy"),
            ("cloudy", "Overcast"), ("fog", "Fog"), ("rain", "Rain"), ("snow", "Snow"), ("thunder", "Thunderstorm")),
                hint="The scene is an 8-frame animation stored on the cooler, rewritten only when the time of day "
                     "or the kind of weather changes, a few times a day."),
        Setting("time", "Time of day", "choice", "live", (
            ("live", "Live, from sunrise and sunset"), ("dawn", "Dawn"), ("day", "Day"), ("dusk", "Dusk"), ("night", "Night"))),
    )

    def __init__(self, values=None, city: str = ""):
        super().__init__(values)
        self.city = city.strip()
        self._sky: tuple[str, int | None, datetime, datetime] | None = None
        self._kind: str | None = None          # the weather the scene follows
        self._seen: str | None = None          # the weather in the last forecast
        self._next = 0.0
        self._shown = None
        self.placeholder = ("Scene", "Looking at the sky…")

    @classmethod
    def build(cls, settings) -> Scene:
        return cls(settings(cls.id), city=settings(weather.Weather.id).get("city", ""))

    def _update_kind(self, kind: str) -> None:
        if self._kind is None or kind == self._seen:   # first forecast, or seen twice in a row
            self._kind = kind
        self._seen = kind

    async def frame(self):
        live = self.city and "live" in (self.values["weather"], self.values["time"])
        if live and time.monotonic() >= self._next:
            try:
                self._sky = await asyncio.to_thread(fetch_sky, self.city)
                self._update_kind(kind_for(self._sky[1]))
                self._next = time.monotonic() + FETCH_SECONDS
            except (OSError, ValueError, KeyError, LookupError) as e:
                self.status = f"Weather unavailable ({type(e).__name__}), showing a clear sky"
                self._next = time.monotonic() + RETRY_SECONDS
        now = datetime.now().astimezone()
        if self.values["time"] in PHASES:
            phase = self.values["time"]
        elif self._sky is not None:
            phase = phase_at(now, self._sky[2] + timedelta(days=(now.date() - self._sky[2].date()).days),
                             self._sky[3] + timedelta(days=(now.date() - self._sky[3].date()).days))
        else:                                  # no city: a rough day from 7 to 18
            phase = phase_at(now, now.replace(hour=7, minute=0, second=0, microsecond=0),
                             now.replace(hour=18, minute=0, second=0, microsecond=0))
        kind = self.values["weather"] if self.values["weather"] in KINDS else (self._kind or "clear")
        where = self._sky[0] if self._sky else (self.city or "no city on the Weather page")
        self.status = f"{phase.capitalize()} · {dict(self.settings[0].choices).get(kind, kind).lower()} · {where}"
        if (phase, kind) == self._shown:
            return None
        self._shown = (phase, kind)
        return await asyncio.to_thread(render_animation, phase, kind)
