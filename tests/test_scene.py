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
    monkeypatch.setattr(scene, "fetch_sky", lambda city: ("Lisbon", 61, rise, set_))
    m = scene.Scene({}, city="Lisbon")
    anim = asyncio.run(m.frame())
    assert anim is not None and m._shown == ("day", "rain") and m.status == "Day · rain · Lisbon"


def test_fetch_sky_reads_sunrise_in_the_places_time_zone(monkeypatch):
    monkeypatch.setattr(scene.weather, "locate", lambda city: ("Lisbon", 38.7, -9.1))
    monkeypatch.setattr(scene.weather, "_get_json", lambda url, params: {
        "utc_offset_seconds": 3600, "current": {"weather_code": 3},
        "daily": {"sunrise": ["2026-11-19T07:40"], "sunset": ["2026-11-19T17:15"]}})
    name, code, rise, set_ = scene.fetch_sky("Lisbon")
    assert (name, code) == ("Lisbon", 3) and rise.utcoffset() == timedelta(hours=1)
    assert rise.astimezone(timezone.utc).hour == 6


def test_scene_reads_the_weather_city_also_in_auto():
    a = auto.Auto({"order": "", "fallback": "scene"}, lambda mode_id: {"weather": {"city": "Chisinau"}}.get(mode_id, {}))
    assert a.children["scene"].city == "Chisinau"
