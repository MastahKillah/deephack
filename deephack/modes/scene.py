"""Scene: a little pixel-art city under your real sky.

Dawn, day, dusk and night follow the actual sunrise and sunset, and the weather (clear, clouds, fog,
rain, snow, thunderstorms) is the current weather in the city set on the Weather page, from Open-Meteo.
Windows light up in the evening, street lamps glow, a car drives by.

Which city: a made-up one, or one of a few real ones, drawn by their landmarks (skylines.py): by itself
the one that is set on the Weather page, if it is among them.

On DeepCool's own firmware it is, like the pet, a short animation stored on the cooler (8 frames),
rewritten only when the time of day or the kind of weather changes: a handful of times a day. A new kind
of weather has to show up in two forecasts in a row before the scene follows it, so weather flickering
between two codes costs nothing.

With the direct drawing firmware (`direct`, set by the engine) nothing is stored and the scene is not a
loop: it is drawn as the city is right now. The sky's colours, the sun and the moon move all the time,
windows come on one by one through the evening, clouds drift, lightning strikes when it likes, and cars
pass at odd times, more of them at rush hour, gliding by a pixel of the screen at a time. See "the scene
when it is drawn anew" below.

Everything is drawn on a 120 x 160 canvas and scaled up four times for real chunky pixels.
"""
from __future__ import annotations

import asyncio
import math
import random
import zlib
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from PIL import Image, ImageDraw

from ..render import H, W
from . import skylines, weather
from .base import Animation, Mode, Setting
from .skylines import City

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


# ------------------------------------------------------------------ sun and moon
# Simple astronomy, good to a few degrees, which is a pixel or two here: the sun's declination from
# the day of the year, its hour angle from the real sunrise and sunset, and the moon from its age:
# it trails the sun by about 50 minutes a day (a full moon rises at sunset) and its declination is
# roughly the sun's at new moon and the opposite at full moon.
SYNODIC = 29.530588853                                   # days from new moon to new moon
NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
HORIZON = 118                                            # where the sun and moon go down, behind the far skyline
MOON_R = 7


@dataclass(frozen=True)
class Sky:
    sun: tuple[int, int] | None          # canvas position, None while below the horizon
    moon: tuple[int, int] | None
    moon_age: float                      # days since new moon
    south: bool = False                  # southern hemisphere: sun from the right, moon lit the other way


def moon_age(t: datetime) -> float:
    return ((t - NEW_MOON).total_seconds() / 86400) % SYNODIC


def declination(t: datetime) -> float:
    return 23.44 * math.sin(math.radians(360 / 365.24 * (t.timetuple().tm_yday - 81)))


def _altitude(lat: float, dec: float, hour_angle: float) -> float:
    lat, dec, h = map(math.radians, (lat, dec, hour_angle))
    return math.degrees(math.asin(math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(h)))


def _half_arc(lat: float, dec: float) -> float:
    """Hour angle (degrees) at which a body with this declination rises and sets."""
    x = -math.tan(math.radians(lat)) * math.tan(math.radians(dec))
    return math.degrees(math.acos(max(-1.0, min(1.0, x))))


def _place(hour_angle: float, half: float, lat: float, dec: float, south: bool) -> tuple[int, int] | None:
    if half <= 0 or abs(hour_angle) >= half:
        return None
    alt = _altitude(lat, dec, hour_angle)
    x = 60 + 52 * hour_angle / half * (-1 if south else 1)       # rises on the left, sets on the right
    y = HORIZON - max(alt, 0) / 50 * (HORIZON - 14)          # stretched: a 23° winter sun clears the rooftops
    return round(x), round(max(y, 14))


def sky_at(t: datetime, sunrise: datetime, sunset: datetime, lat: float) -> Sky:
    noon = sunrise + (sunset - sunrise) / 2
    south = lat < 0
    dec = declination(t)
    sun = _place((t - noon).total_seconds() / 240, (sunset - sunrise).total_seconds() / 480, lat, dec, south)
    age = moon_age(t)
    transit = noon + timedelta(days=age / SYNODIC)                 # the moon trails the sun
    moon_angle = ((t - transit).total_seconds() / 240 + 180) % 360 - 180
    moon_dec = dec * math.cos(2 * math.pi * age / SYNODIC)
    moon = _place(moon_angle, _half_arc(lat, moon_dec), lat, moon_dec, south)
    return Sky(sun, moon, age, south)


def default_sky(phase: str) -> Sky:
    """Where the sun and moon are drawn when nothing is known: a picture, not astronomy."""
    return Sky({"dawn": (24, 108), "day": (84, 36), "dusk": (96, 104), "night": None}[phase],
               (26, 28) if phase == "night" else None, 10.0)


# ------------------------------------------------------------------ the city (the same every time)
# Which city: skylines.py. These are the made-up one's, the scene's first, under the names they had.
ANYWHERE = skylines.ANYWHERE
FAR, NEAR, WINDOWS, FAR_LIGHTS = list(ANYWHERE.far), list(ANYWHERE.near), list(ANYWHERE.windows), list(ANYWHERE.far_lights)
STARS = [(random.Random(i).randrange(0, LW), random.Random(i + 500).randrange(0, 96)) for i in range(48)]
LAMPS = (14, 56, 98)
SNOW_WHITE = (236, 240, 250)


def _far_city(img: Image.Image, d: ImageDraw.ImageDraw, city: City, far: tuple, near: tuple, grey: tuple, over: float,
              dark: float, lit: float) -> None:
    """The city in the distance: landmarks behind the far buildings, those buildings with a few tiny
    lights, and the landmarks that stand in front of them."""
    def marks(layer: str) -> None:
        for mark in city.marks:
            if mark.layer == layer:
                colours = skylines.tones(mark, dark, over, near, far, grey)
                skylines.paint(img, mark, colours)
                glow = _mix(WARM, far, mark.depth * 0.5)
                for x, y, p in mark.dots:
                    if p < lit * 0.8:
                        d.point((x, y), fill=glow)

    marks("back")
    for b in city.far:
        d.rectangle(b, fill=far)
    for x, y, p in city.far_lights:
        if p < lit * 0.5:
            d.point((x, y), fill=_mix(WARM, far, 0.35))
    marks("far")


def _near_city(img: Image.Image, d: ImageDraw.ImageDraw, city: City, far: tuple, near: tuple, bottom: tuple, grey: tuple,
               over: float, dark: float, lit: float, off, beacon: bool, snow: bool) -> None:
    """The city up close: the near buildings and their windows (`off(i)`: window i is dark for a moment),
    the landmarks on the street, and the red lights on the masts while `beacon`."""
    for b in city.near:
        d.rectangle(b, fill=near)
        if snow:
            d.line([(b[0], b[1]), (b[2], b[1])], fill=SNOW_WHITE)
    for x, top, foot in city.masts:
        d.line([(x, top), (x, foot)], fill=near)
    unlit = _mix(near, bottom, 0.35)
    for i, (x, y, p) in enumerate(city.windows):
        d.rectangle([x, y, x + 1, y + 1], fill=WARM if p < lit and not off(i) else unlit)
    for mark in city.marks:
        if mark.layer == "near":
            skylines.paint(img, mark, skylines.tones(mark, dark, over, near, far, grey))
            for x, y, p in mark.dots:
                if p < lit:
                    d.point((x, y), fill=WARM)
            if snow:
                d.point(list(mark.tops), fill=SNOW_WHITE)
    if beacon:
        d.point([(x, top) for x, top, _ in city.masts] + [point for mark in city.marks for point in mark.beacons],
                fill=RED_LIGHT)


# Traffic: each lane loops seamlessly because every vehicle crosses exactly once per loop, all at the
# same speed. Lane 0 drives right in front, lane 1 comes the other way behind it. Which vehicles, in
# which colours and how far apart is picked anew whenever the scene is redrawn (a few times a day),
# so the street does not look the same all day.
TRAFFIC_MARGIN = 26                          # longer than any vehicle, so the wrap happens off screen
TRAFFIC_SPAN = LW + TRAFFIC_MARGIN - 2       # 144: divisible by the frame count
TRAFFIC_STEP = TRAFFIC_SPAN // FRAMES
VEHICLES = {"car": 14, "taxi": 14, "van": 18, "bus": 24}       # lengths
LANE_BOTTOM = (158, 150)
PAINT = ((196, 58, 58), (226, 228, 234), (62, 112, 204), (64, 150, 96), (230, 122, 52), (44, 46, 54),
         (150, 82, 170), (120, 128, 140))
TAXI = (238, 186, 40)


def traffic(seed: int) -> tuple[tuple, tuple]:
    """(near lane, far lane), each a tuple of (offset, vehicle, colour): one or two vehicles near, one far."""
    rng = random.Random(seed)
    lanes = []
    for lane, count in ((0, rng.choice((1, 2, 2))), (1, 1)):
        start = rng.randrange(TRAFFIC_SPAN)
        offsets = [start] + ([(start + rng.randrange(60, 84)) % TRAFFIC_SPAN] if count == 2 else [])
        vehicles = []
        for offset in offsets:
            vehicle = rng.choices(("car", "taxi", "van", "bus"), (6, 2, 2, 1 if lane == 1 else 0))[0]
            vehicles.append((offset, vehicle, TAXI if vehicle == "taxi" else rng.choice(PAINT)))
        lanes.append(tuple(vehicles))
    return lanes[0], lanes[1]


def _vehicle(d: ImageDraw.ImageDraw, beams: ImageDraw.ImageDraw, x: int, y: int, vehicle: str, colour,
             facing: int, glass, lights: str) -> None:
    """A car, taxi, van or bus with its wheels on row y, facing right (1) or left (-1)."""
    n = VEHICLES[vehicle]
    roof = tuple(round(c * 0.86) for c in colour)
    front, back = (x + n - 1, x) if facing == 1 else (x, x + n - 1)
    if vehicle in ("car", "taxi"):
        d.rectangle([x, y - 4, x + n - 1, y - 1], fill=colour)
        r0 = x + 3 if facing == 1 else x + 4
        d.rectangle([r0, y - 7, r0 + 6, y - 4], fill=roof)
        d.rectangle([r0 + 1, y - 6, r0 + 5, y - 5], fill=glass)
        if vehicle == "taxi":
            d.rectangle([r0 + 2, y - 8, r0 + 4, y - 8], fill=(250, 246, 220))
        top = y - 4
    else:                                    # van and bus: one tall box with a row of windows
        h = 7 if vehicle == "van" else 9
        d.rectangle([x, y - h, x + n - 1, y - 1], fill=colour)
        for wx in range(x + 2, x + n - 2, 3 if vehicle == "bus" else 5):
            d.rectangle([wx, y - h + 2, wx + 1, y - h + 3], fill=glass)
        d.rectangle([front - (1 if facing == 1 else 0), y - h + 1, front + (0 if facing == 1 else 1), y - 4], fill=glass)
        top = y - h
    d.point([(x + 2, y), (x + n - 3, y)], fill=(10, 10, 12))
    if vehicle == "bus":
        d.point((x + n // 2, y), fill=(10, 10, 12))
    if lights:
        hy = y - 2
        reach = front + 16 * facing
        beams.polygon([(front, hy), (reach, hy - 3), (reach, hy + 3)], fill=WARM + (84 if lights == "night" else 40,))
        d.point((front, hy), fill=WARM)
        d.point((back, hy), fill=RED_LIGHT)
        if top < y - 5:
            d.point((back, top + 1), fill=RED_LIGHT)


def _cloud(d: ImageDraw.ImageDraw, x: int, y: int, w: int, colour) -> None:
    h = max(4, w // 3)
    d.rectangle([x, y + h // 2, x + w, y + h], fill=colour)
    d.ellipse([x + w // 6, y, x + w // 6 + h + 2, y + h + 1], fill=colour)
    d.ellipse([x + w // 2 - 2, y - h // 3, x + w // 2 + h + 1, y + h], fill=colour)


def _moon(d: ImageDraw.ImageDraw, cx: int, cy: int, age: float, south: bool, night: bool, sky_at) -> None:
    """The moon in its phase: lit from the right while waxing, from the left while waning (the other way
    round in the southern hemisphere). At night the dark part shows faintly, as earthshine does."""
    theta = 2 * math.pi * age / SYNODIC
    waxing = theta <= math.pi
    lit_colour = (240, 234, 200) if night else _mix((226, 232, 244), sky_at(cy), 0.35)
    r = MOON_R
    for py in range(cy - r, cy + r + 1):
        ny = (py - cy + 0.5) / (r + 0.5)
        if abs(ny) > 1:
            continue
        w = math.sqrt(1 - ny * ny)
        for px in range(cx - r, cx + r + 1):
            nx = (px - cx + 0.5) / (r + 0.5)
            if abs(nx) > w:
                continue
            nx = -nx if south else nx
            lit = nx >= w * math.cos(theta) if waxing else nx <= -w * math.cos(theta)
            if lit:
                d.point((px, py), fill=lit_colour)
            elif night:
                d.point((px, py), fill=_mix(sky_at(py), (70, 74, 100), 0.35))


def render_frame(f: int, phase: str, kind: str, seed: int = 0, sky: Sky | None = None, city: City | None = None) -> Image.Image:
    sky, city = sky or default_sky(phase), city or ANYWHERE
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
    if over < 0.5:                                      # behind the clouds and the skyline, drawn first
        if sky.moon is not None and not (0.03 > sky.moon_age / SYNODIC or sky.moon_age / SYNODIC > 0.97):
            _moon(d, *sky.moon, sky.moon_age, sky.south, night, lambda y: _mix(top, bottom, min(y, STREET) / STREET))
        if sky.sun is not None and not night:
            sx, sy = sky.sun
            colour = (255, 228, 120) if sy < HORIZON - 30 else (255, 168, 92) if phase != "dusk" else (255, 132, 82)
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
    near = (14, 15, 26) if night else _mix((46, 50, 70), GREY[phase], over * 0.5)
    _far_city(img, d, city, far, near, GREY[phase], over, DARK[phase], LIT[phase])
    if kind == "thunder" and f in (3, 4):
        pts = [(70, 14), (64, 34), (72, 38), (62, 62), (70, 66), (60, 92)]
        d.line(pts, fill=(255, 255, 230) if f == 3 else (190, 190, 230), width=2 if f == 3 else 1)

    # near buildings with their windows; someone switches a light off for a moment
    _near_city(img, d, city, far, near, bottom, GREY[phase], over, DARK[phase], LIT[phase], lambda i: i % 23 == f % 8,
               phase in ("night", "dusk") and f < 4, kind == "snow")

    # street
    d.rectangle([0, STREET, LW, STREET + 3], fill=(236, 240, 250) if kind == "snow" else (70, 72, 84) if not night else (40, 41, 52))
    d.rectangle([0, STREET + 4, LW, LH], fill=(30, 31, 38) if not night else (18, 18, 24))
    for x in range(4, LW, 16):
        d.line([(x, 152), (x + 6, 152)], fill=(120, 116, 90) if not night else (70, 68, 56))
    glow = phase in ("night", "dusk") or over >= 0.6
    layer = Image.new("RGBA", (LW, LH), (0, 0, 0, 0))
    g = ImageDraw.Draw(layer)
    for x in city.lamps:
        d.line([(x, 126), (x, STREET + 3)], fill=(60, 62, 74))
        d.line([(x, 126), (x + 3, 126)], fill=(60, 62, 74))
        if glow:
            g.ellipse([x - 1, 123, x + 7, 131], fill=WARM + (60,))           # halo around the lamp
            if night:                                          # a pool of light only shows on a dark street
                g.ellipse([x - 7, STREET - 1, x + 13, STREET + 5], fill=WARM + (70,))
            if kind in ("rain", "thunder"):                 # reflections on the wet road
                g.rectangle([x + 1, STREET + 6, x + 4, LH], fill=WARM + (40,))
    img.paste(layer, (0, 0), layer)
    for x in city.lamps:
        d.point((x + 3, 127), fill=WARM if glow else (150, 150, 140))

    # traffic both ways: oncoming in the far lane first, the near lane in front of it
    beams = Image.new("RGBA", (LW, LH), (0, 0, 0, 0))
    b = ImageDraw.Draw(beams)
    glass = (120, 170, 210) if not night else (60, 70, 90)
    lanes = traffic(seed)
    for lane in (1, 0):
        for offset, vehicle, colour in lanes[lane]:
            travelled = (offset + f * TRAFFIC_STEP) % TRAFFIC_SPAN      # once across per loop: seamless
            length = VEHICLES[vehicle]
            x = travelled - TRAFFIC_MARGIN if lane == 0 else LW - travelled
            _vehicle(d, b, x, LANE_BOTTOM[lane], vehicle, colour, 1 if lane == 0 else -1, glass,
                     ("night" if night else "dim") if glow else "")
    img.paste(beams, (0, 0), beams)

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


def render_animation(phase: str, kind: str, seed: int = 0, sky: Sky | None = None, city: City | None = None) -> Animation:
    return Animation([render_frame(i, phase, kind, seed, sky, city) for i in range(FRAMES)], LOOP_MS)


# ------------------------------------------------------------------ the scene when it is drawn anew
# With the direct drawing firmware a picture costs nothing, so the scene is not a loop any more but the
# city as it is at this moment. What stands still or changes slowly (the sky, the sun and moon, clouds,
# the buildings and their windows, the street) is drawn on the small canvas as before and kept until it
# changes. What moves is put on top of the scaled-up picture, at positions in real pixels: a car that
# moved a pixel of the small canvas at a time would jump four on the screen.
#
# Everything is a function of the time, with nothing remembered from frame to frame: which car is where,
# which window is dark for a moment, when lightning strikes. Random things are picked from the time slot
# they belong to.
PX = W // LW                                   # real pixels to one of the small canvas
FAST_TICK, WEATHER_TICK, SLOW_TICK = 0.033, 0.05, 0.25   # seconds between pictures: while a car passes, while
                                                        # rain or snow falls, and when only the sky moves
LIGHT_STEP = 15                                # seconds between two looks at the sky's colours
TWINKLE, BEACON = 0.7, 1.0                     # seconds a star keeps its brightness, and the mast its light
DARK = {"night": 1.0, "dusk": 0.7, "dawn": 0.4, "day": 0.0}     # lamps come on above 0.5


def _but_night(night, other) -> dict:
    return {phase: night if phase == "night" else other for phase in PHASES}


SKY_TOP, SKY_BOTTOM = ({phase: SKY[phase][i] for phase in PHASES} for i in (0, 1))
NIGHT = _but_night(1.0, 0.0)
CLOUD = {"night": (52, 56, 78), "dawn": (236, 196, 190), "day": (248, 250, 255), "dusk": (170, 120, 140)}
FAR_TINT = _but_night((20, 22, 36), (70, 74, 96))
PAVEMENT, ROAD = _but_night((40, 41, 52), (70, 72, 84)), _but_night((18, 18, 24), (30, 31, 38))
ROAD_MARK, GLASS = _but_night((70, 68, 56), (120, 116, 90)), _but_night((60, 70, 90), (120, 170, 210))
RAIN = _but_night((90, 110, 150), (150, 176, 214))
SUN_HIGH = (255, 228, 120)
SUN_LOW = {"dawn": (255, 168, 92), "day": (255, 168, 92), "dusk": (255, 132, 82), "night": (255, 132, 82)}


@dataclass(frozen=True)
class Light:
    """The time of day as a place between two of the phases: `t` of the way from `a` to `b`."""
    a: str
    b: str
    t: float = 0.0

    def mix(self, table: dict) -> tuple:
        return _mix(table[self.a], table[self.b], self.t)

    def level(self, table: dict) -> float:
        return table[self.a] + (table[self.b] - table[self.a]) * self.t


def _between(now: datetime, marks) -> tuple:
    """The two of `marks` ((time, value), in order) that `now` lies between, and how far from the first:
    (value, value, 0.0 to 1.0). Before the first and after the last it is that one's value."""
    if now <= marks[0][0]:
        return marks[0][1], marks[0][1], 0.0
    for (t0, v0), (t1, v1) in zip(marks, marks[1:]):
        if now <= t1:
            return v0, v1, (now - t0) / (t1 - t0) if t1 > t0 else 1.0
    return marks[-1][1], marks[-1][1], 0.0


def light_at(now: datetime, sunrise: datetime, sunset: datetime) -> Light:
    """Night until 45 minutes before sunrise, dawn at sunrise, day from 45 minutes after it, and the same
    around sunset: in between, the sky is on its way from one to the other."""
    morning, evening = sunrise + TWILIGHT, sunset - TWILIGHT
    if morning > evening:                              # a day too short to ever be all day
        morning = evening = sunrise + (sunset - sunrise) / 2
    a, b, t = _between(now, ((sunrise - TWILIGHT, "night"), (sunrise, "dawn"), (morning, "day"), (evening, "day"),
                             (sunset, "dusk"), (sunset + TWILIGHT, "night")))
    return Light(a, b, round(t, 3))


LATE = 0.65                                    # this far through the night the fewest windows are lit
LIT_EVENING, LIT_LATE, LIT_EARLY = LIT["night"], 0.10, 0.16


def lit_share(now: datetime, sunrise: datetime, sunset: datetime) -> float:
    """The share of windows that are lit. They come on one by one through dusk, most are lit when the night
    begins, people go to bed, and a few are up again before sunrise."""
    day = timedelta(days=1)
    if now < sunrise + TWILIGHT:                       # the night that began yesterday
        sunset -= day
    else:
        sunrise += day
    dark, light = sunset + TWILIGHT, sunrise - TWILIGHT
    a, b, t = _between(now, ((sunset - TWILIGHT, 0.0), (sunset, LIT["dusk"]), (dark, LIT_EVENING),
                             (dark + (light - dark) * LATE, LIT_LATE), (light, LIT_EARLY), (sunrise, LIT["dawn"]),
                             (sunrise + TWILIGHT, 0.0)))
    return a + (b - a) * t


# Clouds drift to the right, each row on a track of its own that is longer than the canvas, so a cloud
# is out of sight when it starts again on the left. (x at time 0, y, width); pixels a second.
CLOUD_ROWS = {
    "partly": ((180, 0.5, ((8, 40, 26), (64, 18, 32), (90, 54, 20), (140, 30, 24))),),
    "overcast": ((180, 0.5, tuple((-8 + 30 * i, y, w) for i, (y, w) in enumerate(((6, 40), (0, 46), (8, 40), (2, 40), (5, 44), (1, 42))))),
                 (190, 0.3, tuple((6 + 38 * i, y, w) for i, (y, w) in enumerate(((24, 34), (22, 40), (28, 36), (25, 38), (23, 36)))))),
}
CLOUD_LEAD = 50                                # wider than any cloud: where a track starts, left of the canvas


def clouds_at(t: float, kind: str) -> tuple[tuple[int, int, int, bool], ...]:
    """Where the clouds are at time `t` (seconds): (x, y, width, dark), the ones behind first."""
    rows = CLOUD_ROWS["partly"] if kind == "partly" else CLOUD_ROWS["overcast"] if OVERCAST[kind] >= 0.45 else ()
    return tuple((int((x + speed * t) % track) - CLOUD_LEAD, y, w, len(rows) > 1 and row == 0)
                 for row, (track, speed, clouds) in enumerate(rows) for x, y, w in clouds)


# A window that is lit goes dark for a few seconds now and then: every ninth one, each at its own pace.
FLICKER = {i: (150 + 200 * random.Random(i).random(), 400 * random.Random(i + 1).random())
           for i in range(0, max(len(c.windows) for c in skylines.CITIES), 9)}    # window -> (seconds between two, when in them)
FLICKER_SECONDS = 6


def away_at(t: float, city: City | None = None) -> frozenset[int]:
    """The windows whose light is off for a moment at time `t`."""
    count = len((city or ANYWHERE).windows)
    return frozenset(i for i, (every, shift) in FLICKER.items() if i < count and (t + shift) % every < FLICKER_SECONDS)


STRIKE_SLOT, STRIKE_CHANCE = 7.0, 0.4          # lightning: in each slot of so many seconds, this likely
STRIKE_BRIGHT, STRIKE_SECONDS = 0.12, 0.3      # the sky is lit for the first, the bolt fades until the second


def strike_at(t: float) -> tuple[tuple, bool] | None:
    """The lightning that is in the sky at time `t`: (the bolt's points, whether it still lights the sky)."""
    slot = int(t // STRIKE_SLOT)
    rng = random.Random(slot)
    if rng.random() >= STRIKE_CHANCE:
        return None
    age = t - (slot * STRIKE_SLOT + rng.uniform(0, STRIKE_SLOT - STRIKE_SECONDS))
    if not 0 <= age < STRIKE_SECONDS:
        return None
    x, points = rng.randrange(24, 98), []
    for y in range(12, 96, 16):                        # down in a zigzag, to behind the skyline
        points.append((x, y))
        x += rng.randrange(-9, 10)
    return tuple(points), age < STRIKE_BRIGHT


# Traffic. Time is cut into slots, and in each a vehicle may set off, in each lane on its own: how
# likely depends on the hour. All vehicles of a lane drive at the same speed, and one sets off at least
# HEADWAY before the end of its slot, so they never touch. The near lane drives to the right.
TRAFFIC_SLOT = 6.0
LANE_SPEED = (36.0, 28.0)                      # small-canvas pixels a second: the near lane looks faster
BEAM = 17                                      # how far in front of a vehicle its headlights reach
HEADWAY = (max(VEHICLES.values()) + 10) / min(LANE_SPEED)
BUSY = (0.12, 0.10, 0.08, 0.08, 0.10, 0.20, 0.35, 0.55, 0.60, 0.45, 0.38, 0.38, 0.42, 0.38, 0.38, 0.42, 0.55, 0.60,
        0.55, 0.45, 0.38, 0.30, 0.22, 0.16)    # the chance of a vehicle in a slot, by the hour of the day


def busy_at(hour: float) -> float:
    h = int(hour) % 24
    return BUSY[h] + (BUSY[(h + 1) % 24] - BUSY[h]) * (hour % 1)


def vehicles_at(t: float, busy: float) -> tuple[tuple[int, int, str, tuple], ...]:
    """The vehicles on the street at time `t`: (lane, x of its left end in real pixels, kind, colour), the
    far lane's first. One counts from when its headlights could first be seen until it has left."""
    out = []
    slot = int(t // TRAFFIC_SLOT)
    for lane in (1, 0):
        speed = LANE_SPEED[lane]
        for k in range(slot - 2, slot + 2):
            rng = random.Random(k * 2 + lane)
            if rng.random() >= busy * (1.0 if lane == 0 else 0.8):
                continue
            start = k * TRAFFIC_SLOT + rng.uniform(0, TRAFFIC_SLOT - HEADWAY)
            vehicle = rng.choices(("car", "taxi", "van", "bus"), (6, 2, 2, 1 if lane == 1 else 0))[0]
            colour = TAXI if vehicle == "taxi" else rng.choice(PAINT)
            length = VEHICLES[vehicle]
            travelled = (t - start) * speed - BEAM         # of its front, from the edge it comes in at
            if not -BEAM <= travelled <= LW + length:
                continue
            left = travelled - length if lane == 0 else LW - travelled
            out.append((lane, round(left * PX), vehicle, colour))
    return tuple(out)


def in_sight(lane: int, left: int, vehicle: str, lights: bool) -> bool:
    """Whether any of a vehicle (of vehicles_at) is on the picture; with `lights`, its headlights' beams count."""
    right = left + VEHICLES[vehicle] * PX
    if lights:
        left, right = (left, right + BEAM * PX) if lane == 0 else (left - BEAM * PX, right)
    return right > 0 and left < W


def street_lights(light: Light, kind: str) -> str:
    """How lit the street is: "night", "dim" (dusk, dawn, or a day as dark as one), or "" by day."""
    if light.level(DARK) > 0.5 or OVERCAST[kind] >= 0.6:
        return "night" if light.level(NIGHT) > 0.5 else "dim"
    return ""


SPRITE_ROOM, SPRITE_ROWS = BEAM + 1, 12        # room for the headlights at either end; rows above the wheels


@lru_cache(maxsize=96)
def vehicle_sprite(vehicle: str, colour: tuple, facing: int, glass: tuple, lights: str) -> Image.Image:
    """A vehicle with its headlights' beams, scaled up, to paste where it is right now."""
    size = (VEHICLES[vehicle] + 2 * SPRITE_ROOM, SPRITE_ROWS)
    body, beams = Image.new("RGBA", size, (0, 0, 0, 0)), Image.new("RGBA", size, (0, 0, 0, 0))
    _vehicle(ImageDraw.Draw(body), ImageDraw.Draw(beams), SPRITE_ROOM, SPRITE_ROWS - 2, vehicle, colour, facing, glass, lights)
    return Image.alpha_composite(beams, body).resize((size[0] * PX, size[1] * PX), Image.NEAREST)


def render_ground(light: Light, kind: str, sky: Sky, lit: float, clouds: tuple = (), twinkle: int = 0,
                  beacon: bool = False, away: frozenset = frozenset(), bolt=None, city: City | None = None) -> Image.Image:
    """What stands still or changes slowly, on the small canvas: the sky with the sun, the moon and the
    clouds, the city with `lit` of its windows alight (but those in `away`), and the empty street."""
    city = city or ANYWHERE
    over = OVERCAST[kind]
    grey = light.mix(GREY)
    top, bottom = _mix(light.mix(SKY_TOP), grey, over), _mix(light.mix(SKY_BOTTOM), grey, over)
    if bolt and bolt[1]:                               # the flash
        top, bottom = _mix(top, (230, 230, 255), 0.6), _mix(bottom, (230, 230, 255), 0.6)
    night, dark = light.level(NIGHT), light.level(DARK)
    img = Image.new("RGB", (LW, LH))
    d = ImageDraw.Draw(img)
    for y in range(STREET):
        d.line([(0, y), (LW, y)], fill=_mix(top, bottom, y / STREET))

    def sky_colour(y):
        return _mix(top, bottom, min(y, STREET) / STREET)

    if night > 0.2 and over < 0.5:                     # the stars come out as the night comes
        for i, (x, y) in enumerate(STARS):
            here = sky_colour(y)
            faint = _mix(here, (150, 156, 200), night)
            if (twinkle + i) % 6 == 0:
                d.point([(x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)], fill=faint)
            d.point((x, y), fill=_mix(here, (230, 232, 255), night) if (twinkle + i) % 3 else faint)
    if over < 0.5:                                     # behind the clouds and the skyline, drawn first
        if sky.moon is not None and not (0.03 > sky.moon_age / SYNODIC or sky.moon_age / SYNODIC > 0.97):
            _moon(d, *sky.moon, sky.moon_age, sky.south, night > 0.5, sky_colour)
        if sky.sun is not None:
            sx, sy = sky.sun
            low = min(1.0, max(0.0, (sy - (HORIZON - 45)) / 30))       # redder the nearer the horizon
            colour = _mix(SUN_HIGH, light.mix(SUN_LOW), low)
            d.ellipse([sx - 10, sy - 10, sx + 10, sy + 10], fill=_mix(colour, bottom, 0.55))
            d.ellipse([sx - 8, sy - 8, sx + 8, sy + 8], fill=colour)

    cloud = _mix(light.mix(CLOUD), grey, over * 0.8)
    heavy = _mix(cloud, (0, 0, 0), 0.18 if kind in ("rain", "thunder") else 0.05)
    for x, y, w, is_dark in clouds:
        _cloud(d, x, y, w, heavy if is_dark else cloud)

    far = _mix(bottom, light.mix(FAR_TINT), 0.45)
    near = light.mix({phase: (14, 15, 26) if phase == "night" else _mix((46, 50, 70), GREY[phase], over * 0.5) for phase in PHASES})
    _far_city(img, d, city, far, near, grey, over, dark, lit)
    if bolt:
        d.line(list(bolt[0]), fill=(255, 255, 230) if bolt[1] else (190, 190, 230), width=2 if bolt[1] else 1)
    _near_city(img, d, city, far, near, bottom, grey, over, dark, lit, away.__contains__, beacon and dark > 0.5, kind == "snow")

    d.rectangle([0, STREET, LW, STREET + 3], fill=(236, 240, 250) if kind == "snow" else light.mix(PAVEMENT))
    d.rectangle([0, STREET + 4, LW, LH], fill=light.mix(ROAD))
    for x in range(4, LW, 16):
        d.line([(x, 152), (x + 6, 152)], fill=light.mix(ROAD_MARK))
    glow = dark > 0.5 or over >= 0.6
    layer = Image.new("RGBA", (LW, LH), (0, 0, 0, 0))
    g = ImageDraw.Draw(layer)
    for x in city.lamps:
        d.line([(x, 126), (x, STREET + 3)], fill=(60, 62, 74))
        d.line([(x, 126), (x + 3, 126)], fill=(60, 62, 74))
        if glow:
            g.ellipse([x - 1, 123, x + 7, 131], fill=WARM + (60,))
            if night > 0.5:                                # a pool of light only shows on a dark street
                g.ellipse([x - 7, STREET - 1, x + 13, STREET + 5], fill=WARM + (70,))
            if kind in ("rain", "thunder"):                # reflections on the wet road
                g.rectangle([x + 1, STREET + 6, x + 4, LH], fill=WARM + (40,))
    img.paste(layer, (0, 0), layer)
    for x in city.lamps:
        d.point((x + 3, 127), fill=WARM if glow else (150, 150, 140))
    return img


# Rain and snow: every drop and flake falls at its own speed and starts again at the top when it is
# through, so the picture as a whole never repeats. (x, y at time 0, pixels of the small canvas a second[,
# where it is in its sway]).
DROPS = tuple((random.Random(i).randrange(0, LW + 56), random.Random(i + 1000).uniform(0, LH + 8),
               random.Random(i + 2000).uniform(150, 230)) for i in range(80))
FLAKES = tuple((random.Random(i).randrange(0, LW), random.Random(i + 1000).uniform(0, LH + 4),
                random.Random(i + 2000).uniform(10, 22), random.Random(i + 3000).uniform(0, 6.3)) for i in range(90))
HAZE = ((84, 90), (100, 120), (116, 140), (130, 110))      # fog: (top of a band, how thick it is)


def haze_at(t: float) -> tuple[int, ...]:
    """How far, in real pixels, each band of fog has drifted sideways at time `t`."""
    return tuple(round(8 * math.sin(t / 5 + i * 1.7)) for i in range(len(HAZE)))


def render_live(ground: Image.Image, vehicles: tuple, kind: str, light: Light, t: float) -> Image.Image:
    """The picture for time `t`: `ground` (render_ground's, scaled up) with what moves on top of it."""
    img = ground.copy()
    lights, glass = street_lights(light, kind), light.mix(GLASS)
    for lane, left, vehicle, colour in vehicles:           # the far lane's come first
        sprite = vehicle_sprite(vehicle, colour, 1 if lane == 0 else -1, glass, lights)
        img.paste(sprite, (left - SPRITE_ROOM * PX, (LANE_BOTTOM[lane] - SPRITE_ROWS + 2) * PX), sprite)

    # weather in front of everything
    if kind in ("rain", "thunder"):
        d = ImageDraw.Draw(img)
        colour = light.mix(RAIN)
        for x, y0, speed in DROPS:
            y = int(((y0 + speed * t) % (LH + 8) - 8) * PX)
            x = x * PX - y // 3                            # slanting: the wind is from the right
            d.rectangle([x, y, x + PX - 1, y + 2 * PX - 1], fill=colour)
            d.rectangle([x - PX, y + 2 * PX, x - 1, y + 4 * PX - 1], fill=colour)
    elif kind == "snow":
        d = ImageDraw.Draw(img)
        for i, (x, y0, speed, sway) in enumerate(FLAKES):
            y = int(((y0 + speed * t) % (LH + 4) - 4) * PX)
            x = int((x + 1.5 * math.sin(t * 0.8 + sway)) * PX)
            d.rectangle([x, y, x + (2 * PX if i % 3 == 0 else PX) - 1, y + PX - 1], fill=(240, 244, 255))
    elif kind == "fog":
        d = ImageDraw.Draw(img, "RGBA")
        colour = _mix(light.mix(GREY), (255, 255, 255), 0.25)
        for (y, alpha), drift in zip(HAZE, haze_at(t)):
            d.rectangle([drift - 16, y * PX, W + 16, (y + 15) * PX - 1], fill=colour + (alpha,))
    return img


# ------------------------------------------------------------------ the mode
def fetch_sky(city: str) -> tuple[str, int | None, datetime, datetime, float]:
    """(place, current weather code, today's sunrise, today's sunset, latitude) for a city."""
    name, lat, lon = weather.locate(city)
    data = weather._get_json(weather.FORECAST_URL, {"latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1,
                                                    "current": "weather_code", "daily": "sunrise,sunset"})
    zone = timezone(timedelta(seconds=int(data.get("utc_offset_seconds", 0))))
    sun = [datetime.fromisoformat(data["daily"][k][0]).replace(tzinfo=zone) for k in ("sunrise", "sunset")]
    return name, data["current"].get("weather_code"), sun[0], sun[1], lat


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
                hint="The sun and moon follow their real paths, the moon in its real phase. With the direct drawing "
                     "firmware the scene is drawn as it is right now, many times a second while a car passes or "
                     "rain falls, and nothing is stored. On DeepCool's own firmware it is an 8-frame animation "
                     "stored on the cooler, with the sun and moon in hourly steps, rewritten when one of them "
                     "visibly moves or the weather changes; behind clouds they cost no redraws."),
        Setting("time", "Time of day", "choice", "live", (
            ("live", "Live, from sunrise and sunset"), ("dawn", "Dawn"), ("day", "Day"), ("dusk", "Dusk"), ("night", "Night"))),
        Setting("skyline", "City", "choice", "auto", (("auto", "The Weather page's, if it is one of these"),)
                + tuple((c.id, c.name) for c in skylines.CITIES),
                hint="A few real cities are drawn by their landmarks. Any other is a city of its own."),
    )

    def __init__(self, values=None, city: str = ""):
        super().__init__(values)
        self.city = city.strip()
        chosen = self.values["skyline"]
        self._follow = chosen not in skylines.BY_ID            # the skyline goes by the Weather page's city
        self.town = skylines.find(self.city) if self._follow else skylines.BY_ID[chosen]
        self._sky: tuple[str, int | None, datetime, datetime, float] | None = None
        self._kind: str | None = None          # the weather the scene follows
        self._seen: str | None = None          # the weather in the last forecast
        self._next = 0.0
        self._shown = None
        self._ground: Image.Image | None = None    # drawn pictures: what stands still, scaled up
        self._ground_of = None                     # ... what it is a picture of
        self._grounds = 0                          # ... and how many of them there have been
        self._look = None                          # what the last picture showed
        self.placeholder = ("Scene", "Looking at the sky…")

    @classmethod
    def build(cls, settings) -> Scene:
        return cls(settings(cls.id), city=settings(weather.Weather.id).get("city", ""))

    def _update_kind(self, kind: str) -> None:
        if self._kind is None or kind == self._seen:   # first forecast, or seen twice in a row
            self._kind = kind
        self._seen = kind

    def _drawn(self, when: datetime, sunrise: datetime, sunset: datetime, lat: float, kind: str,
               fixed: str | None, t: float) -> Image.Image | None:
        """The city at `when` (the time of day) and `t` (the clock everything moves by, in seconds), unless
        that is what the last picture showed. `fixed`: the time of day is one picked in the settings."""
        if fixed:
            light, lit = Light(fixed, fixed), LIT[fixed]
        else:
            calm = when - timedelta(seconds=when.timestamp() % LIGHT_STEP)
            light, lit = light_at(calm, sunrise, sunset), lit_share(calm, sunrise, sunset)
        over = OVERCAST[kind]
        if over >= 0.6:
            lit = max(lit, LIT_EARLY)                  # so dark a day that some lights are on
        sky = Sky(None, None, 0.0)                     # behind clouds the sun and moon need no redraws
        if over < 0.5:
            sky = sky_at(when, sunrise, sunset, lat)
            sky = replace(sky, moon_age=round(sky.moon_age, 1))
        stars = light.level(NIGHT) > 0.2 and over < 0.5
        of = (light, kind, sky, round(lit, 3), clouds_at(t, kind), int(t / TWINKLE) % 6 if stars else 0,
              light.level(DARK) > 0.5 and int(t / BEACON) % 2 == 0, away_at(t, self.town) if lit else frozenset(),
              strike_at(t) if kind == "thunder" else None, self.town)
        if of != self._ground_of:
            self._ground_of, self._grounds = of, self._grounds + 1
            self._ground = render_ground(*of).resize((W, H), Image.NEAREST)
        cars = vehicles_at(t, busy_at(when.hour + when.minute / 60))
        falls = kind in ("rain", "thunder", "snow")
        self.tick = FAST_TICK if cars else WEATHER_TICK if falls else SLOW_TICK       # fast from just before one
        lights = bool(street_lights(light, kind))                                    # comes into the picture
        cars = tuple(car for car in cars if in_sight(*car[:3], lights))
        look = (self._grounds, cars, t if falls else haze_at(t) if kind == "fog" else None)
        if look == self._look:
            return None
        self._look = look
        return render_live(self._ground, cars, kind, light, t)

    async def frame(self):
        live = self.city and "live" in (self.values["weather"], self.values["time"])
        if live and time.monotonic() >= self._next:
            try:
                self._sky = await asyncio.to_thread(fetch_sky, self.city)
                self._update_kind(kind_for(self._sky[1]))
                if self._follow and self.town is ANYWHERE:     # the place as the weather service spells it
                    self.town = skylines.find(self._sky[0])
                self._next = time.monotonic() + FETCH_SECONDS
            except (OSError, ValueError, KeyError, LookupError) as e:
                self.status = f"Weather unavailable ({type(e).__name__}), showing a clear sky"
                self._next = time.monotonic() + RETRY_SECONDS
        now = datetime.now().astimezone()
        if self._sky is not None:
            shift = timedelta(days=(now.date() - self._sky[2].date()).days)      # today's, also after midnight
            sunrise, sunset, lat = self._sky[2] + shift, self._sky[3] + shift, self._sky[4]
        else:                                  # no city: a rough day from 7 to 18 at a middle latitude
            sunrise = now.replace(hour=7, minute=0, second=0, microsecond=0)
            sunset, lat = sunrise.replace(hour=18), 45.0
        if self.values["time"] in PHASES:
            phase = self.values["time"]
            noon = sunrise + (sunset - sunrise) / 2
            at = {"dawn": sunrise + timedelta(minutes=20), "day": noon, "dusk": sunset - timedelta(minutes=20),
                  "night": noon + timedelta(hours=12)}[phase]
        else:
            phase = phase_at(now, sunrise, sunset)
            at = now.replace(minute=30, second=0, microsecond=0)    # the sun and moon move in hourly steps
        kind = self.values["weather"] if self.values["weather"] in KINDS else (self._kind or "clear")
        where = self._sky[0] if self._sky else (self.city or "no city on the Weather page")
        self.status = f"{phase.capitalize()} · {dict(self.settings[0].choices).get(kind, kind).lower()} · {where}"
        if self.direct:
            fixed = phase if self.values["time"] in PHASES else None
            self._shown = None
            return self._drawn(at if fixed else now, sunrise, sunset, lat, kind, fixed, now.timestamp())
        self._ground_of, self._look, self.tick = None, None, None
        sky = sky_at(at, sunrise, sunset, lat)
        visible = OVERCAST[kind] < 0.5                 # behind clouds the sun and moon need no redraws
        key = (phase, kind, sky.sun if visible and phase != "night" else None, sky.moon if visible else None,
               round(sky.moon_age / SYNODIC * 28) if visible and sky.moon else None, self.town.id)
        if key == self._shown:
            return None
        self._shown = key
        seed = zlib.crc32(f"{now.date()} {phase} {kind}".encode())     # other traffic every time it is redrawn
        return await asyncio.to_thread(render_animation, phase, kind, seed, sky, self.town)
