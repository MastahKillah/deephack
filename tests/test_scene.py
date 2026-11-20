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


# ------------------------------------------------------------------ drawn pictures (the direct drawing firmware)
ZONE = timezone(timedelta(hours=2))
RISE, SET = datetime(2026, 11, 19, 7, 13, tzinfo=ZONE), datetime(2026, 11, 19, 16, 26, tzinfo=ZONE)


def _clock(hour, minute=0, second=0):
    return datetime(2026, 11, 19, hour, minute, second, tzinfo=ZONE)


def test_light_is_on_its_way_between_the_phases():
    at = lambda t: scene.light_at(t, RISE, SET)
    assert at(_clock(3)) == scene.Light("night", "night") and at(_clock(12)).level(scene.DARK) == 0.0
    assert at(RISE).level(scene.DARK) == scene.DARK["dawn"] and at(SET).mix(scene.GREY) == scene.GREY["dusk"]
    falling = [at(SET + timedelta(minutes=m)).level(scene.DARK) for m in range(-60, 61, 5)]
    assert falling == sorted(falling) and falling[0] == 0.0 and falling[-1] == 1.0 and len(set(falling)) > 15
    halfway = at(SET - timedelta(minutes=22, seconds=30))
    assert (halfway.a, halfway.b, halfway.t) == ("day", "dusk", 0.5)
    assert halfway.mix(scene.SKY_TOP) == scene._mix(scene.SKY["day"][0], scene.SKY["dusk"][0], 0.5)
    polar = scene.light_at(_clock(12, 10), _clock(11, 30), _clock(12, 30))  # an hour of day: day for a moment only
    assert (polar.a, polar.b) == ("day", "dusk") and polar.level(scene.DARK) > 0


def test_windows_come_on_through_dusk_and_go_out_at_night():
    share = lambda t: scene.lit_share(t, RISE, SET)
    assert share(_clock(12)) == 0.0 and share(SET - scene.TWILIGHT) == 0.0
    evening = [share(SET + timedelta(minutes=m)) for m in range(-45, 46, 3)]
    assert evening == sorted(evening) and len(set(evening)) == len(evening)        # one after the other
    assert abs(share(SET + scene.TWILIGHT) - scene.LIT_EVENING) < 1e-9
    assert share(_clock(3)) < 0.2 < share(_clock(19))                               # most people are asleep at 3
    assert abs(share(_clock(3)) - share(_clock(3) + timedelta(days=0, seconds=1))) < 0.001
    assert abs(share(_clock(0)) - share(_clock(23, 59, 59))) < 0.01                 # no jump at midnight
    lit = lambda s: sum(p < s for _, _, p in scene.WINDOWS)
    assert lit(share(SET)) < lit(share(SET + timedelta(minutes=20))) < lit(share(SET + scene.TWILIGHT))


def test_traffic_glides_varies_and_never_touches():
    seen, counts = set(), {8.5: 0, 3.0: 0}
    for hour in counts:
        before = {}
        for i in range(int(600 / 0.05)):                                  # ten minutes, as the engine asks
            t = 1_800_000_000 + i * 0.05
            now = scene.vehicles_at(t, scene.busy_at(hour))
            for lane in (0, 1):
                here = sorted((x, x + scene.VEHICLES[v] * scene.PX) for l, x, v, _ in now if l == lane)
                assert all(b[0] - a[1] >= 10 * scene.PX for a, b in zip(here, here[1:]))       # a gap between two
            for lane, x, vehicle, colour in now:
                seen.add(vehicle)
                key = (lane, vehicle, colour)
                if key in before and abs(x - before[key]) < 40:           # the same one, a twentieth of a second on
                    moved = (x - before[key]) * (1 if lane == 0 else -1)
                    assert 5 <= moved <= 8, moved                         # a few real pixels, never a jump
            counts[hour] += len(now) > 0 and not before
            before = {(lane, v, c): x for lane, x, v, c in now}
    assert seen == {"car", "taxi", "van", "bus"}
    assert counts[8.5] > 2 * counts[3.0] > 0                              # rush hour, and the dead of night
    assert scene.vehicles_at(1_800_000_000, 0.0) == ()
    assert scene.HEADWAY * min(scene.LANE_SPEED) >= max(scene.VEHICLES.values()) + 10


def test_clouds_drift_and_lightning_strikes_at_odd_times():
    assert scene.clouds_at(0, "clear") == () and len(scene.clouds_at(0, "partly")) == 4
    a, b = scene.clouds_at(1000.0, "rain"), scene.clouds_at(1010.0, "rain")
    assert len(a) == 11 and sum(c[3] for c in a) == 6                      # a dark row in front of a lighter one
    assert all(q[0] - p[0] in (5, 3, 5 - 180, 3 - 190) for p, q in zip(a, b))       # to the right, or round again
    assert all(-scene.CLOUD_LEAD <= x and x + w <= 180 for x, _, w, _ in a)
    strikes, gaps, last = 0, set(), None
    for i in range(6000):                                                  # ten minutes
        bolt = scene.strike_at(5000 + i * 0.1)
        if bolt and last is None:
            strikes += 1
            assert bolt[1] and len(bolt[0]) > 3
        if bolt and last is None and strikes > 1:
            gaps.add(round(i * 0.1 - began))
        if bolt and last is None:
            began = i * 0.1
        last = bolt
    assert 15 < strikes < 60 and len(gaps) > 8                             # not on a beat


def test_ground_follows_light_windows_and_weather():
    sky = scene.Sky((60, 40), None, 10.0)
    small = lambda **how: scene.render_ground(**{"light": scene.Light("night", "night"), "kind": "clear", "sky": sky, "lit": 0.45, **how})
    night = small()
    assert night.size == (scene.LW, scene.LH)
    on_canvas = [w for w in scene.WINDOWS if w[0] < scene.LW]              # the last building is cut off by the edge
    x, y, p = min(on_canvas, key=lambda w: w[2])                           # the window most likely to be lit
    i = scene.WINDOWS.index((x, y, p))
    assert night.getpixel((x, y)) == scene.WARM and small(lit=0.0).getpixel((x, y)) != scene.WARM
    assert small(away=frozenset({i})).getpixel((x, y)) != scene.WARM
    warm = lambda img: sum(img.getpixel((wx, wy)) == scene.WARM for wx, wy, _ in on_canvas)
    assert warm(small(lit=0.1)) < warm(small(lit=0.2)) < warm(night)
    dusk = [small(light=scene.Light("day", "dusk", t)).getpixel((3, 3)) for t in (0.0, 0.5, 1.0)]
    assert len(set(dusk)) == 3                                             # the sky's colour moves with the light
    assert small(twinkle=1).tobytes() != night.tobytes()                   # stars
    assert small(beacon=True).tobytes() != night.tobytes()                 # the mast's light
    bolt = (((60, 12), (55, 28), (62, 44), (58, 60)), True)
    assert small(kind="thunder", bolt=bolt).getpixel((3, 3)) != small(kind="thunder").getpixel((3, 3))      # the flash
    for a, b in zip(scene.PHASES, scene.PHASES[1:] + scene.PHASES[:1]):
        for kind in scene.KINDS:
            scene.render_ground(scene.Light(a, b, 0.4), kind, sky, 0.3, scene.clouds_at(77.0, kind))


def test_stored_and_drawn_scenes_look_alike_at_a_phase():
    """A drawn scene at the middle of a phase is the stored one's picture, but for what moves."""
    stored = scene.render_frame(5, "day", "clear", 0, scene.Sky((60, 40), None, 10.0)).resize((scene.LW, scene.LH), scene.Image.NEAREST)
    drawn = scene.render_ground(scene.Light("day", "day"), "clear", scene.Sky((60, 40), None, 10.0), 0.0)
    same = sum(stored.getpixel((x, y)) == drawn.getpixel((x, y)) for x in range(scene.LW) for y in range(scene.STREET))
    assert same == scene.LW * scene.STREET                                 # above the street: the very same


def _drawn_scene(kind="clear", time="live"):
    m = scene.Scene({"weather": kind, "time": time})
    m.direct = True
    return m, lambda when, t: m._drawn(when, RISE, SET, 47.0, kind, time if time in scene.PHASES else None, t)


def _quiet(start=1_800_000_000.0):
    """A moment with no car on the street, and none for the next two seconds."""
    return next(t for t in (start + i for i in range(3600)) if not any(scene.vehicles_at(t + s / 10, 1.0) for s in range(20)))


def _busy(start=1_800_000_000.0):
    """A moment with a car well on the picture."""
    return next(t for t in (start + i / 4 for i in range(3600)) if any(100 < x < 300 for _, x, _, _ in scene.vehicles_at(t, 0.5)))


def test_drawn_scene_draws_only_what_changed():
    m, at = _drawn_scene("clear", "day")
    t = _quiet()
    first = at(_clock(12), t)
    assert first.size == (device.WIDTH, device.HEIGHT) and m.tick == scene.SLOW_TICK
    assert at(_clock(12), t + 0.25) is None and at(_clock(12), t + 0.5) is None       # a clear day, an empty street
    m, at = _drawn_scene("clear", "live")
    t = _busy()
    noon = _clock(12)
    pictures = [at(noon, t + i * 0.05) for i in range(5)]
    assert all(p is not None for p in pictures) and m.tick == scene.FAST_TICK          # a car is passing
    assert len({p.tobytes() for p in pictures}) == 5
    grounds = m._grounds
    at(noon + timedelta(seconds=1), t + 1)
    assert m._grounds == grounds                       # ... over the same city: only the car was drawn anew
    coming = next(s for s in (t + i * 0.05 for i in range(4000))           # a car that is not on the picture yet
                  if (cars := scene.vehicles_at(s, scene.busy_at(12))) and not any(scene.in_sight(*c[:3], False) for c in cars))
    m._look = None
    assert at(noon, coming) is not None and m.tick == scene.FAST_TICK      # the pace is up before it shows
    assert at(noon, coming + 0.05) is None
    at(noon + timedelta(hours=1), t + 2)
    assert m._grounds == grounds + 1                   # an hour later the sun is elsewhere


def test_drawn_rain_snow_and_fog_keep_moving():
    for kind, tick in (("rain", scene.WEATHER_TICK), ("snow", scene.WEATHER_TICK), ("thunder", scene.WEATHER_TICK)):
        m, at = _drawn_scene(kind, "dusk")
        t = _quiet()
        pictures = [at(_clock(16), t + i * tick) for i in range(4)]
        assert m.tick == tick and len({p.tobytes() for p in pictures}) == 4, kind
    m, at = _drawn_scene("fog", "day")
    t = _quiet()
    assert at(_clock(9), t) is not None and m.tick == scene.SLOW_TICK
    assert at(_clock(9), t + 0.01) is None and at(_clock(9), t + 3.0) is not None      # it drifts, slowly
    # no two drops fall in step: the rain as a whole does not repeat
    assert len({speed for _, _, speed in scene.DROPS}) == len(scene.DROPS)


def test_cars_are_pasted_where_they_are_and_lit_at_night():
    ground = scene.render_ground(scene.Light("night", "night"), "clear", scene.Sky(None, None, 0.0), 0.4).resize((device.WIDTH, device.HEIGHT), scene.Image.NEAREST)
    car = lambda x: scene.render_live(ground, ((0, x, "car", (196, 58, 58)),), "clear", scene.Light("night", "night"), 0.0)
    body = (scene.LANE_BOTTOM[0] - 3) * scene.PX
    assert car(201).getpixel((203, body)) == (196, 58, 58) and car(201).getpixel((200, body)) != (196, 58, 58)
    assert car(202).getpixel((201, body)) != (196, 58, 58)                 # a real pixel further, not four
    beam = (201 + 14 * scene.PX + 20, (scene.LANE_BOTTOM[0] - 2) * scene.PX)
    assert car(201).getpixel(beam) != ground.getpixel(beam)                # its headlights on the road
    day = scene.render_live(ground, ((0, 201, "car", (196, 58, 58)),), "clear", scene.Light("day", "day"), 0.0)
    assert day.getpixel(beam) == ground.getpixel(beam)
    for x in (-200, -30, 470, 600):                                        # half on the picture, or off it
        assert car(x).size == ground.size
    assert scene.vehicle_sprite("bus", (1, 2, 3), -1, (4, 5, 6), "dim").size == ((24 + 2 * scene.SPRITE_ROOM) * scene.PX, scene.SPRITE_ROWS * scene.PX)


def test_scene_mode_draws_when_it_may_and_stores_when_it_must(monkeypatch):
    m = scene.Scene({"weather": "rain", "time": "night"})
    m.direct = True
    first, second = asyncio.run(m.frame()), asyncio.run(m.frame())
    assert isinstance(first, scene.Image.Image) and isinstance(second, scene.Image.Image)
    assert first.tobytes() != second.tobytes() and m.tick in (scene.FAST_TICK, scene.WEATHER_TICK)
    assert m.status == "Night · rain · no city on the Weather page"
    m.direct = False                                                       # e.g. the slide show was switched on
    stored = asyncio.run(m.frame())
    assert len(stored.frames) == scene.FRAMES and m.tick is None and asyncio.run(m.frame()) is None
