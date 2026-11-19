import asyncio
from datetime import datetime, timedelta, timezone

from deephack import device
from deephack.modes import auto, scene


def test_kind_for_weather_codes():
    assert [scene.kind_for(c) for c in (0, 1, 2, 3, 45, 61, 80, 71, 95, None)] == \
        ["clear", "clear", "partly", "cloudy", "fog", "rain", "rain", "snow", "thunder", "clear"]


def test_phase_at():
    day = datetime(2026, 11, 19, tzinfo=timezone.utc)
    rise, set_ = day.replace(hour=7), day.replace(hour=16, minute=30)
    at = lambda h, m=0: scene.phase_at(day.replace(hour=h, minute=m), rise, set_)
    assert [at(5), at(6, 20), at(7, 40), at(12), at(16), at(17, 10), at(17, 20), at(23)] == \
        ["night", "dawn", "dawn", "day", "dusk", "dusk", "night", "night"]


def test_every_scene_animates():
    for phase in scene.PHASES:
        for kind in scene.KINDS:
            anim = scene.render_animation(phase, kind)
            assert len(anim.frames) == scene.FRAMES and anim.duration_ms <= device.MAX_GIF_DURATION_MS
            assert all(f.size == (device.WIDTH, device.HEIGHT) for f in anim.frames)
            assert len({f.tobytes() for f in anim.frames}) == scene.FRAMES, (phase, kind)   # the car moves


def test_mode_with_fixed_settings_redraws_only_on_change():
    m = scene.Scene({"weather": "rain", "time": "night"})
    first = asyncio.run(m.frame())
    assert first is not None and len(first.frames) == scene.FRAMES and m.status == "Night · rain · no city on the Weather page"
    assert asyncio.run(m.frame()) is None
    m.values["weather"] = "snow"
    assert asyncio.run(m.frame()) is not None


def test_live_weather_has_to_show_up_twice():
    m = scene.Scene({}, city="Lisbon")
    m._update_kind("rain")
    assert m._kind == "rain"                                  # the first forecast counts straight away
    m._update_kind("clear")
    assert m._kind == "rain"
    m._update_kind("clear")
    assert m._kind == "clear"


def test_live_mode_fetches_the_sky(monkeypatch):
    now = datetime.now().astimezone()
    rise, set_ = now - timedelta(hours=3), now + timedelta(hours=3)
    monkeypatch.setattr(scene, "fetch_sky", lambda city: ("Lisbon", 61, rise, set_, 38.7))
    m = scene.Scene({}, city="Lisbon")
    anim = asyncio.run(m.frame())
    assert anim is not None and m._shown[:2] == ("day", "rain") and m.status == "Day · rain · Lisbon"


def test_fetch_sky_reads_sunrise_in_the_places_time_zone(monkeypatch):
    monkeypatch.setattr(scene.weather, "locate", lambda city: ("Lisbon", 38.7, -9.1))
    monkeypatch.setattr(scene.weather, "_get_json", lambda url, params: {
        "utc_offset_seconds": 3600, "current": {"weather_code": 3},
        "daily": {"sunrise": ["2026-11-19T07:40"], "sunset": ["2026-11-19T17:15"]}})
    name, code, rise, set_, lat = scene.fetch_sky("Lisbon")
    assert (name, code) == ("Lisbon", 3) and rise.utcoffset() == timedelta(hours=1)
    assert rise.astimezone(timezone.utc).hour == 6


def test_scene_reads_the_weather_city_also_in_auto():
    a = auto.Auto({"order": "", "fallback": "scene"}, lambda mode_id: {"weather": {"city": "Chisinau"}}.get(mode_id, {}))
    assert a.children["scene"].city == "Chisinau"


def test_traffic_is_light_varied_and_never_collides():
    mixes = set()
    for seed in range(200):
        near, far = scene.traffic(seed)
        assert 1 <= len(near) <= 2 and len(far) == 1
        assert all(v != "bus" for _, v, _ in near)                   # buses only in the far lane
        if len(near) == 2:
            gap = (near[1][0] - near[0][0]) % scene.TRAFFIC_SPAN
            assert min(gap, scene.TRAFFIC_SPAN - gap) >= max(scene.VEHICLES.values()) + 10
        mixes.add(tuple(v for _, v, _ in near + far))
    assert len(mixes) > 5


def test_moon_age_and_declination():
    full = datetime(2024, 1, 25, 17, 54, tzinfo=timezone.utc)          # a full moon
    assert abs(scene.moon_age(full) - scene.SYNODIC / 2) < 0.7
    assert abs(scene.moon_age(full + timedelta(days=scene.SYNODIC / 2)) % scene.SYNODIC) < 0.7 or \
        scene.moon_age(full + timedelta(days=scene.SYNODIC / 2)) > scene.SYNODIC - 0.7
    assert abs(scene.declination(datetime(2026, 6, 21)) - 23.44) < 1
    assert abs(scene.declination(datetime(2026, 12, 21)) + 23.44) < 1
    assert abs(scene.declination(datetime(2026, 3, 20))) < 1.5


def test_sun_and_moon_paths():
    zone = timezone(timedelta(hours=2))
    rise, set_ = datetime(2026, 11, 19, 7, 13, tzinfo=zone), datetime(2026, 11, 19, 16, 26, tzinfo=zone)
    noon = rise + (set_ - rise) / 2
    at = lambda t: scene.sky_at(t, rise, set_, 47.0)
    assert at(rise - timedelta(minutes=30)).sun is None and at(set_ + timedelta(minutes=30)).sun is None
    morning, midday, afternoon = at(rise + timedelta(hours=2)).sun, at(noon).sun, at(set_ - timedelta(hours=2)).sun
    assert morning[0] < midday[0] < afternoon[0] and midday[0] == 60       # left to right, noon in the middle
    assert midday[1] < morning[1] and midday[1] < afternoon[1]             # highest at noon
    summer = scene.sky_at(datetime(2026, 6, 21, 13, 0, tzinfo=zone), datetime(2026, 6, 21, 5, 28, tzinfo=zone),
                          datetime(2026, 6, 21, 21, 0, tzinfo=zone), 47.0)
    assert summer.sun[1] < midday[1]                                       # the summer sun stands higher
    south = scene.sky_at(rise + timedelta(hours=2), rise, set_, -33.9)
    assert south.sun[0] > 60 and south.south                               # rises on the right down under


def test_full_moon_rises_at_dusk_and_is_up_at_midnight():
    zone = timezone.utc
    day = datetime(2024, 1, 25, tzinfo=zone)                              # full moon day
    rise, set_ = day.replace(hour=7), day.replace(hour=17)
    assert scene.sky_at(day.replace(hour=12), rise, set_, 47.0).moon is None
    assert scene.sky_at(day.replace(hour=23, minute=59), rise, set_, 47.0).moon is not None


def test_moon_phase_lights_the_right_side():
    def brightness(age, side):
        sky = scene.Sky(None, (60, 40), age)
        img = scene.render_frame(0, "night", "clear", 0, sky).resize((scene.LW, scene.LH), scene.Image.NEAREST)
        return sum(img.getpixel((60 + side * 4, 40)))
    assert brightness(7.4, 1) > brightness(7.4, -1) + 200                  # first quarter: lit on the right
    assert brightness(22.1, -1) > brightness(22.1, 1) + 200                # last quarter: lit on the left
    assert abs(brightness(14.8, 1) - brightness(14.8, -1)) < 30            # full
