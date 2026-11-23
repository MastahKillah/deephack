"""The Scene's cities: a made-up one, and real ones by their landmarks."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from deephack.modes import scene, skylines
from deephack.modes.skylines import Art, place

REAL = [c for c in skylines.CITIES if c.id != "anywhere"]
SKY = scene.Sky((92, 30), (24, 26), 11.0)


def ground(city, phase="night", kind="clear", lit=0.45, beacon=False, **how):
    return scene.render_ground(scene.Light(phase, phase), kind, SKY, lit, (), 0, beacon, frozenset(), None, city, **how)


def test_the_cities_there_are():
    assert [c.id for c in skylines.CITIES] == ["anywhere", "chisinau", "paris", "london", "new_york", "tokyo"]
    assert skylines.ANYWHERE.marks == () and len(skylines.ANYWHERE.masts) == 1          # the scene's first city, as it was
    for city in REAL:
        assert city.marks and city.known_as and city.name
        assert all(0 <= x < skylines.LW for x in city.lamps)


def test_a_place_is_known_by_its_name_however_it_is_written():
    assert skylines.find("Chișinău").id == skylines.find("Chisinau").id == skylines.find("chisinau, Moldova").id == "chisinau"
    assert skylines.find("Kishinev").id == "chisinau"
    assert skylines.find("PARIS").id == "paris" and skylines.find("New York").id == "new_york" and skylines.find("Tokyo").id == "tokyo"
    for elsewhere in ("Lisbon", "", "Parish", "Londonderry", None):
        assert skylines.find(elsewhere) is skylines.ANYWHERE


@pytest.mark.parametrize("city", REAL, ids=lambda c: c.id)
def test_every_city_is_drawn_in_any_light_and_weather(city):
    seen = set()
    for phase in scene.PHASES:
        for kind in ("clear", "cloudy", "snow", "thunder"):
            picture = ground(city, phase, kind, scene.LIT[phase])
            assert picture.size == (scene.LW, scene.LH)
            seen.add(picture.tobytes())
    assert len(seen) == 16                                   # and none of them like another
    assert ground(city).tobytes() != ground(skylines.ANYWHERE).tobytes()
    frames = scene.render_animation("dusk", "rain", 1, None, city).frames         # the stored kind, for DeepCool's firmware
    assert len(frames) == scene.FRAMES and frames[0].size == (480, 640)
    for mark in city.marks:                                  # nothing stands wholly outside the picture
        assert mark.x < skylines.LW and mark.x + mark.size[0] > 0 and 0 < mark.base < skylines.LH


@pytest.mark.parametrize("city", REAL, ids=lambda c: c.id)
def test_windows_and_beacons_of_landmarks_come_on_with_the_citys(city):
    dark, lit = ground(city, lit=0.0), ground(city, lit=0.9)
    assert dark.tobytes() != lit.tobytes()
    dots = city.dots
    if dots:
        x, y, p = min(dots, key=lambda dot: dot[2])          # the one most likely lit
        assert lit.getpixel((x, y)) != dark.getpixel((x, y))
    beacons = [point for mark in city.marks for point in mark.beacons]
    if beacons:
        on = ground(city, beacon=True)
        assert on.getpixel(beacons[0]) == scene.RED_LIGHT != ground(city).getpixel(beacons[0])
        assert ground(city, "day", beacon=True).getpixel(beacons[0]) != scene.RED_LIGHT      # not by day


def test_a_floodlit_landmark_shows_at_night_and_another_is_a_shape():
    art = Art(4, 4)
    art.box(0, 0, 3, 3, "S")
    art.dot(1, 1, "L")
    lit, unlit = place(art, 10, flood=True), place(art, 10, flood=False)
    silhouette, haze, grey = (14, 15, 26), (20, 22, 36), (26, 28, 40)
    bright = lambda c: sum(c)
    night_lit, night_unlit = (skylines.tones(m, 1.0, 0.0, silhouette, haze, grey) for m in (lit, unlit))
    assert bright(night_lit["S"]) > 2 * bright(night_unlit["S"])
    assert night_lit["L"] == night_unlit["L"] == skylines.GLOW["L"]                       # a lamp glows either way
    day = skylines.tones(lit, 0.0, 0.0, silhouette, haze, grey)
    assert bright(day["S"]) > bright(night_lit["S"]) and day["T"] != night_lit["T"]
    far = skylines.tones(place(art, 10, depth=0.6), 0.0, 0.0, silhouette, (200, 200, 200), grey)
    assert abs(far["S"][0] - 200) < abs(day["S"][0] - 200)                                # the haze is over what is far


def test_art_is_materials_and_windows_keep_to_the_walls():
    art = Art(9, 9)
    art.poly([(4, 0), (0, 8), (8, 8)], "S")                  # something that narrows
    art.windows(0, 1, 8, 8, 2, 2)
    data = art.img.load()
    for y in range(9):
        for x in range(9):
            if data[x, y] == ord("o"):
                assert 0 < x < 8 and data[x - 1, y] and data[x + 1, y]                    # a wall on both sides
    mark = place(art, 20, 100)
    assert {m for m, _ in mark.masks} == {"S", "o"} and mark.size == (9, 9)
    assert all(20 <= x < 29 and 92 <= y <= 100 for x, y, _ in mark.dots) and len(mark.dots) > 3
    mirrored = art.mirrored()
    assert mirrored.img.getpixel((0, 8)) == art.img.getpixel((8, 8))
    assert all(material in skylines.DAY for material in "SsHhWwDZBbEegqkRGTtMFKoLNC")


def test_snow_lies_on_the_landmarks_of_the_street():
    city = skylines.BY_ID["chisinau"]
    cathedral = max((m for m in city.marks if m.layer == "near"), key=lambda m: m.size[0])
    x, y = cathedral.tops[len(cathedral.tops) // 2]
    assert ground(city, "day", "snow", 0.0).getpixel((x, y)) == scene.SNOW_WHITE != ground(city, "day", "clear", 0.0).getpixel((x, y))


def test_the_scene_takes_the_weather_pages_city_or_the_one_chosen(monkeypatch):
    assert scene.Scene({}, city="Chișinău").town.id == "chisinau"                         # by itself
    assert scene.Scene({}, city="Lisbon").town is skylines.ANYWHERE
    assert scene.Scene({"skyline": "paris"}, city="Chișinău").town.id == "paris"          # chosen
    assert scene.Scene({"skyline": "anywhere"}, city="Chișinău").town is skylines.ANYWHERE
    setting = {s.key: s for s in scene.Scene.settings}["skyline"]
    assert setting.default == "auto" and [value for value, _ in setting.choices][:2] == ["auto", "anywhere"]

    zone = timezone(timedelta(hours=9))
    rise = datetime.now(zone).replace(hour=6, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(scene, "fetch_sky", lambda place: ("Tokyo", 0, rise, rise + timedelta(hours=11), 35.7))
    mode = scene.Scene({}, city="東京")                       # typed as the weather service understands it, not as we do
    assert mode.town is skylines.ANYWHERE
    assert asyncio.run(mode.frame()) is not None and mode.town.id == "tokyo"              # ... which names it for us
    fixed = scene.Scene({"skyline": "london"}, city="東京")
    asyncio.run(fixed.frame())
    assert fixed.town.id == "london"


def test_a_drawn_scene_of_a_real_city_moves_like_any_other():
    mode = scene.Scene({"weather": "snow", "time": "night", "skyline": "chisinau"})
    mode.direct = True
    first = asyncio.run(mode.frame())
    assert first is not None and first.size == (480, 640) and mode.tick == scene.WEATHER_TICK
    other = scene.Scene({"weather": "snow", "time": "night", "skyline": "anywhere"})
    other.direct = True
    assert asyncio.run(other.frame()).tobytes() != first.tobytes()
    assert scene.away_at(400.0, skylines.BY_ID["chisinau"]) == frozenset()                # it has no plain windows to go dark
