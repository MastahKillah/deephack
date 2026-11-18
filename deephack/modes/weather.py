"""Current weather and a three day outlook, from Open-Meteo (free, no API key)."""
from __future__ import annotations

import asyncio
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import date

from PIL import Image, ImageDraw

from .. import render
from ..render import H, W
from .base import Mode, Setting

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REFRESH_SECONDS = 15 * 60
RETRY_SECONDS = 60

# WMO weather interpretation codes -> (icon kind, description)
WMO = {
    0: ("clear", "Clear sky"), 1: ("partly", "Mostly clear"), 2: ("partly", "Partly cloudy"), 3: ("cloudy", "Overcast"),
    45: ("fog", "Fog"), 48: ("fog", "Rime fog"),
    51: ("rain", "Light drizzle"), 53: ("rain", "Drizzle"), 55: ("rain", "Heavy drizzle"),
    56: ("rain", "Freezing drizzle"), 57: ("rain", "Freezing drizzle"),
    61: ("rain", "Light rain"), 63: ("rain", "Rain"), 65: ("rain", "Heavy rain"),
    66: ("rain", "Freezing rain"), 67: ("rain", "Freezing rain"),
    71: ("snow", "Light snow"), 73: ("snow", "Snow"), 75: ("snow", "Heavy snow"), 77: ("snow", "Snow grains"),
    80: ("rain", "Light showers"), 81: ("rain", "Showers"), 82: ("rain", "Violent showers"),
    85: ("snow", "Snow showers"), 86: ("snow", "Heavy snow showers"),
    95: ("thunder", "Thunderstorm"), 96: ("thunder", "Thunderstorm, hail"), 99: ("thunder", "Thunderstorm, hail"),
}

BACKGROUNDS = {  # (kind, is_day) -> gradient top, bottom
    ("clear", True): ((40, 120, 214), (126, 190, 240)), ("clear", False): ((10, 14, 40), (38, 44, 92)),
    ("partly", True): ((58, 118, 190), (140, 180, 220)), ("partly", False): ((14, 18, 44), (44, 50, 90)),
    ("cloudy", True): ((84, 98, 120), (150, 162, 180)), ("cloudy", False): ((22, 26, 38), (56, 62, 80)),
    ("fog", True): ((104, 112, 124), (168, 174, 184)), ("fog", False): ((30, 33, 42), (70, 74, 86)),
    ("rain", True): ((48, 66, 96), (98, 120, 150)), ("rain", False): ((14, 20, 36), (44, 56, 80)),
    ("snow", True): ((96, 118, 150), (190, 204, 222)), ("snow", False): ((24, 30, 50), (76, 88, 116)),
    ("thunder", True): ((40, 42, 66), (86, 84, 116)), ("thunder", False): ((12, 12, 26), (46, 42, 72)),
}

SUN, MOON, CLOUD, CLOUD_DARK = (255, 204, 64), (236, 236, 214), (246, 248, 252), (188, 196, 210)
DROP, BOLT, FOG = (120, 190, 255), (255, 214, 70), (226, 230, 238)


def _get_json(url: str, params: dict) -> dict:
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": "deephack"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


# ------------------------------------------------------------------ icons
def _cloud(d: ImageDraw.ImageDraw, x: float, y: float, w: float, fill) -> None:
    """Cloud with bounding box of roughly (x, y, x + w, y + 0.62 w)."""
    h = w * 0.62
    d.rounded_rectangle([x, y + h * 0.48, x + w, y + h], radius=h * 0.26, fill=fill)
    d.ellipse([x + w * 0.10, y + h * 0.26, x + w * 0.50, y + h * 0.90], fill=fill)
    d.ellipse([x + w * 0.30, y, x + w * 0.76, y + h * 0.74], fill=fill)
    d.ellipse([x + w * 0.58, y + h * 0.30, x + w * 0.94, y + h * 0.88], fill=fill)


def _sun(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float) -> None:
    for i in range(8):
        a = math.pi * i / 4
        d.line([cx + math.cos(a) * r * 1.35, cy + math.sin(a) * r * 1.35, cx + math.cos(a) * r * 1.85, cy + math.sin(a) * r * 1.85],
               fill=SUN, width=max(2, round(r * 0.22)))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=SUN)


def _moon(img: Image.Image, cx: float, cy: float, r: float) -> None:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=MOON + (255,))
    d.ellipse([cx - r * 0.45, cy - r * 1.25, cx + r * 1.45, cy + r * 0.65], fill=(0, 0, 0, 0))
    img.alpha_composite(layer)


def icon(kind: str, size: int, is_day: bool = True) -> Image.Image:
    """Draw a weather icon as an RGBA image (supersampled for smooth edges)."""
    s = size * 3
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def celestial(cx, cy, r):
        if is_day:
            _sun(d, cx, cy, r)
        else:
            _moon(img, cx, cy, r * 1.25)

    if kind == "clear":
        celestial(s * 0.5, s * 0.5, s * 0.22)
    elif kind == "partly":
        celestial(s * 0.36, s * 0.36, s * 0.16)
        d = ImageDraw.Draw(img)
        _cloud(d, s * 0.22, s * 0.40, s * 0.70, CLOUD)
    elif kind == "cloudy":
        _cloud(d, s * 0.36, s * 0.18, s * 0.54, CLOUD_DARK)
        _cloud(d, s * 0.10, s * 0.34, s * 0.74, CLOUD)
    elif kind == "fog":
        _cloud(d, s * 0.14, s * 0.14, s * 0.72, CLOUD)
        for i, (a, b) in enumerate(((0.12, 0.88), (0.22, 0.78), (0.14, 0.70))):
            y = s * (0.68 + i * 0.10)
            d.line([s * a, y, s * b, y], fill=FOG, width=round(s * 0.045))
    else:
        _cloud(d, s * 0.12, s * 0.12, s * 0.76, CLOUD if kind == "snow" else CLOUD_DARK)
        base = s * 0.66
        if kind == "rain":
            for i in range(4):
                x = s * (0.28 + i * 0.15)
                d.line([x, base, x - s * 0.06, base + s * 0.20], fill=DROP, width=round(s * 0.04))
        elif kind == "snow":
            for i in range(4):
                x, y = s * (0.26 + i * 0.16), base + s * (0.06 if i % 2 == 0 else 0.16)
                r = s * 0.035
                d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255))
        elif kind == "thunder":
            d.polygon([(s * 0.54, base - s * 0.04), (s * 0.38, base + s * 0.16), (s * 0.48, base + s * 0.16),
                       (s * 0.42, base + s * 0.32), (s * 0.64, base + s * 0.08), (s * 0.53, base + s * 0.08),
                       (s * 0.62, base - s * 0.04)], fill=BOLT)
    return img.resize((size, size), Image.LANCZOS)


# ------------------------------------------------------------------ rendering
def render_weather(place: str, data: dict, unit: str) -> Image.Image:
    cur, daily = data["current"], data["daily"]
    is_day = bool(cur.get("is_day", 1))
    kind, text = WMO.get(cur["weather_code"], ("cloudy", "Unknown"))
    img = render.vertical_gradient(*BACKGROUNDS[(kind, is_day)]).convert("RGBA")
    d = ImageDraw.Draw(img)
    white, soft = (255, 255, 255), (228, 234, 244)
    deg = "°"

    d.text((W / 2, 34), render.fit(d, place, render.font(render.SEMIBOLD, 30), W - 60), font=render.font(render.SEMIBOLD, 30), fill=white, anchor="ma")
    img.alpha_composite(icon(kind, 200, is_day), ((W - 200) // 2, 86))
    d = ImageDraw.Draw(img)
    d.text((W / 2 + 14, 284), f"{round(cur['temperature_2m'])}{deg}", font=render.font(render.LIGHT, 112), fill=white, anchor="ma")
    d.text((W / 2, 420), text, font=render.font(render.SEMIBOLD, 28), fill=white, anchor="ma")
    detail = f"Feels like {round(cur['apparent_temperature'])}{deg}   ·   H {round(daily['temperature_2m_max'][0])}{deg}  L {round(daily['temperature_2m_min'][0])}{deg}"
    d.text((W / 2, 462), detail, font=render.font(render.REGULAR, 21), fill=soft, anchor="ma")

    # three day outlook on a translucent card
    card = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle([20, 508, W - 20, H - 20], radius=20, fill=(0, 0, 0, 62))
    img.alpha_composite(card)
    d = ImageDraw.Draw(img)
    col_w = (W - 40) / 3
    for i in range(3):
        j = i + 1
        if j >= len(daily["time"]):
            break
        cx = 20 + col_w * (i + 0.5)
        day = date.fromisoformat(daily["time"][j]).strftime("%a")
        d.text((cx, 518), day, font=render.font(render.SEMIBOLD, 20), fill=white, anchor="ma")
        img.alpha_composite(icon(WMO.get(daily["weather_code"][j], ("cloudy", ""))[0], 46, True), (round(cx - 23), 544))
        d = ImageDraw.Draw(img)
        hilo = f"{round(daily['temperature_2m_max'][j])}{deg} / {round(daily['temperature_2m_min'][j])}{deg}"
        d.text((cx, 592), hilo, font=render.font(render.REGULAR, 19), fill=soft, anchor="ma")
    return img.convert("RGB")


class Weather(Mode):
    id = "weather"
    title = "Weather"
    description = "Conditions and 3-day outlook"
    glyph = ""
    settings = (
        Setting("city", "City", "text", "", placeholder="e.g. Lisbon", hint="Looked up with Open-Meteo. Refreshes every 15 minutes."),
        Setting("unit", "Units", "choice", "celsius", (("celsius", "Celsius"), ("fahrenheit", "Fahrenheit"))),
    )

    def __init__(self, values=None):
        super().__init__(values)
        self._place = None        # (name, lat, lon)
        self._next_fetch = 0.0
        self._shown = None
        self.placeholder = ("Weather", "Enter a city in the settings on the right.")

    def _fetch(self) -> tuple[str, dict]:
        if self._place is None:
            hits = _get_json(GEOCODE_URL, {"name": self.values["city"].strip(), "count": 1, "language": "en", "format": "json"}).get("results")
            if not hits:
                raise LookupError(f"No place called “{self.values['city'].strip()}” found")
            self._place = (hits[0]["name"], hits[0]["latitude"], hits[0]["longitude"])
        name, lat, lon = self._place
        data = _get_json(FORECAST_URL, {
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 4,
            "current": "temperature_2m,apparent_temperature,weather_code,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "temperature_unit": self.values["unit"],
        })
        return name, data

    async def frame(self):
        if not self.values["city"].strip():
            self.status = "No city set"
            return None
        if time.monotonic() < self._next_fetch:
            return None
        try:
            name, data = await asyncio.to_thread(self._fetch)
        except LookupError as e:
            self.status = str(e)
            self.placeholder = ("City not found", str(e))
            self._next_fetch = time.monotonic() + REFRESH_SECONDS
            return None
        except (OSError, ValueError, KeyError) as e:
            self.status = f"Weather service unreachable ({type(e).__name__})"
            self._next_fetch = time.monotonic() + RETRY_SECONDS
            return None
        self._next_fetch = time.monotonic() + REFRESH_SECONDS
        cur, daily = data["current"], data["daily"]
        kind, text = WMO.get(cur["weather_code"], ("cloudy", "Unknown"))
        self.status = f"{name}: {round(cur['temperature_2m'])}°, {text.lower()}"
        # only what is visible goes into the key, so an unchanged forecast causes no flash write
        key = (name, round(cur["temperature_2m"]), round(cur["apparent_temperature"]), cur["weather_code"], cur.get("is_day"),
               tuple(daily["weather_code"][:4]), tuple(round(v) for v in daily["temperature_2m_max"][:4]),
               tuple(round(v) for v in daily["temperature_2m_min"][:4]), daily["time"][0])
        if key == self._shown:
            return None
        self._shown = key
        return render_weather(name, data, self.values["unit"])
