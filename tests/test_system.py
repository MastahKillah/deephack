import asyncio
import io
import json
import struct
import zlib

import pytest

from deephack import device
from deephack.modes import system

SNAPSHOT = {
    "CPU Temperature": {"Unit": "C", "Value": 75.5}, "CPU Usage": {"Unit": "%", "Value": 34.57},
    "CPU Power": {"Unit": "W", "Value": 143.9}, "Gpu temperature": {"Unit": "C", "Value": 0},
    "Gpu usage": {"Unit": "%", "Value": 0.0}, "Memory Load": {"Unit": "%", "Value": 51.8},
    "Memory Used": {"Unit": "MB", "Value": 33533.0}, "Memory Available": {"Unit": "MB", "Value": 31123.0},
    "Fan list": {"AIO Pump": 3341.0, "CPU": 1700.0, "CHA_FAN1": 0.0}, "WiFi Status": {"Unit": "", "Value": "on"},
}


def _answer(kind, seq, payload):
    body = json.dumps(payload).encode()
    return b"DCS2" + bytes((2, kind, 0, 0)) + struct.pack("<III", len(body), seq, zlib.crc32(body)) + body


class FakePipe:
    """The sensor service: answers each request, with an event pushed in front of the snapshot."""

    def __init__(self, snapshot_body=SNAPSHOT):
        self.out = io.BytesIO()
        self.snapshot_body = snapshot_body
        self.ops = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, data):
        kind, seq, request = system.read_frame(io.BytesIO(data))
        assert kind == 1
        self.ops.append(request["op"])
        pos = self.out.tell()
        self.out.seek(0, io.SEEK_END)
        if request["op"] == "GET_SENSOR_SNAPSHOT":
            self.out.write(_answer(3, 0, {"event": "SENSOR_SNAPSHOT", "body": {}}))
            self.out.write(_answer(2, seq, {"ok": True, "op": request["op"], "body": self.snapshot_body}))
        else:
            self.out.write(_answer(2, seq, {"ok": True, "op": request["op"], "body": {"server": "SensorBridge"}}))
        self.out.seek(pos)

    def close(self):
        pass

    def read(self, n):
        return self.out.read(min(n, 7))            # in dribs and drabs, like a pipe may


def test_frames_round_trip_and_are_checked():
    kind, seq, payload = system.read_frame(io.BytesIO(system._frame(5, "HELLO")))
    assert (kind, seq, payload) == (1, 5, {"op": "HELLO", "body": {}})
    broken = bytearray(system._frame(5, "HELLO"))
    broken[-2] ^= 1
    with pytest.raises(ValueError):
        system.read_frame(io.BytesIO(bytes(broken)))
    with pytest.raises(OSError):
        system.read_frame(io.BytesIO(system._frame(5, "HELLO")[:30]))


def test_read_bridge(monkeypatch):
    pipe = FakePipe()
    monkeypatch.setattr(system, "open", lambda *args, **kwargs: pipe, raising=False)
    assert system.read_bridge() == SNAPSHOT and pipe.ops == ["HELLO", "GET_SENSOR_SNAPSHOT"]


def test_read_bridge_unwraps_enveloped_snapshot(monkeypatch):
    """DeepCool's app 1.2.16 answers with the readings under body.snapshot.data, where 1.2.15 had them
    as the body itself: every reading was nought (#61)."""
    body = {
        "event": "SENSOR_SNAPSHOT",
        "sessionId": "test-session",
        "snapshot": {
            "collectedAtMs": 1_800_000_000_000,
            "sequence": 1,
            "data": SNAPSHOT,
        },
    }
    pipe = FakePipe(body)
    monkeypatch.setattr(system, "open", lambda *args, **kwargs: pipe, raising=False)
    assert system.read_bridge() == SNAPSHOT
    assert pipe.ops == ["HELLO", "GET_SENSOR_SNAPSHOT"]


def test_read_bridge_rejects_malformed_enveloped_snapshot(monkeypatch):
    body = {"event": "SENSOR_SNAPSHOT", "snapshot": {"data": []}}
    pipe = FakePipe(body)
    monkeypatch.setattr(system, "open", lambda *args, **kwargs: pipe, raising=False)
    assert system.read_bridge() is None


def test_read_bridge_without_the_service():
    assert system.read_bridge("\\\\.\\pipe\\deephack-test-no-such-pipe") is None


def test_parse_snapshot():
    r = system.parse_snapshot(SNAPSHOT, 100.0)
    assert (r.cpu_temp, r.cpu_load, r.cpu_power) == (75.5, 34.57, 143.9)
    assert r.gpu_temp is None and r.gpu_load == 0.0                       # 0 °C: no such sensor
    assert r.mem_load == 51.8 and r.mem_total == 33533.0 + 31123.0
    assert r.fans == (("AIO Pump", 3341.0), ("CPU", 1700.0)) and r.sensors
    assert not system.parse_snapshot({"CPU Usage": {"Value": True}, "Fan list": []}, 0).sensors


def test_windows_counters():
    counters = system.WindowsCounters()
    counters.cpu_load()
    load = counters.cpu_load()
    assert load is None or 0 <= load <= 100
    mem_load, used, total = counters.memory()
    assert 0 < mem_load <= 100 and 0 < used < total


def test_history_keeps_three_hours_and_survives_a_restart(tmp_path, monkeypatch):
    path, now, keep = tmp_path / "history.json", 1_800_000_000.0, system.KEEP_SECONDS
    monkeypatch.setattr(system.time, "time", lambda: now)
    h = system.History(path)
    for t in (-keep - 100, -keep - 50, -keep + 50):
        h.add(system.Reading(now + t, cpu_load=1.0))
    h.add(system.Reading(now, cpu_temp=60.0, fans=(("AIO Pump", 3000.0),)))
    assert [r.t - now for r in h.readings] == [-keep + 50, 0]          # older than three hours: dropped
    h.save()
    assert list(system.History(path).readings) == list(h.readings)    # fans come back as tuples too
    monkeypatch.setattr(system.time, "time", lambda: now + 100)
    assert [r.t - now for r in system.History(path).readings] == [0]  # loading drops what has aged out since
    path.write_text("not json")
    assert not system.History(path).readings


class SyncJob:
    def __init__(self, fn, *args):
        self.result = fn(*args)
        self.done = type("Done", (), {"is_set": lambda self: True})()


def test_tick_samples_every_ten_seconds(monkeypatch):
    calls = []
    monkeypatch.setattr(system, "_Job", SyncJob)
    monkeypatch.setattr(system, "take_reading", lambda windows: calls.append(1) or system.Reading(len(calls), cpu_load=10.0))
    h = system.History(None)
    for t in (0.0, 0.5, 5.0, 10.0, 10.5, 20.0):
        h.tick(1000 + t)
    h.tick(1030)
    assert len(calls) == 4 and len(h.readings) == 3                     # the 4th is collected on the next tick


def test_working_hard():
    now = 10_000.0
    busy = [system.Reading(now - i * 10, cpu_load=20.0, gpu_load=90.0) for i in range(12)]
    calm = [system.Reading(now - i * 10, cpu_load=40.0, gpu_load=10.0) for i in range(12)]
    assert system.working_hard(busy, now) and not system.working_hard(calm, now)
    assert not system.working_hard(busy[:3], now)                     # too few readings to tell


def test_gaps_break_the_lines():
    readings = [system.Reading(t, cpu_temp=v) for t, v in ((0, 50), (10, 51), (20, None), (30, 52), (200, 53), (210, 54))]
    assert system._segments(readings, "cpu_temp") == [[(0, 50), (10, 51)], [(30, 52)], [(200, 53), (210, 54)]]


def test_render():
    now = 50_000.0
    full = [system.Reading(now - 3600 + i * 10, cpu_temp=50 + i % 30, cpu_load=float(i % 100), cpu_power=90.0,
                           gpu_temp=40.0, gpu_load=5.0, gpu_power=30.0, mem_load=52.0, mem_used=33000.0, mem_total=64000.0,
                           fans=(("CPU", 1600.0), ("AIO Pump", 3300.0))) for i in range(360)]
    bare = [system.Reading(now - 10 * i, cpu_load=12.0, mem_load=50.0, mem_used=1.0, mem_total=2.0) for i in range(30)]
    for readings, span in ((full, 3600), (bare, 1800), ([], 10800)):
        img = system.render_system(readings, now, span)
        assert img.size == (device.WIDTH, device.HEIGHT) and img.mode == "RGB"
    assert system.status_line(full[-1]) == "CPU 79° 59% · GPU 40° 5% · RAM 52%"
    assert system.status_line(bare[-1]) == "CPU 12% · RAM 50%"


def test_mode_draws_at_its_interval(monkeypatch):
    monkeypatch.setattr(system, "_Job", SyncJob)
    monkeypatch.setattr(system, "take_reading", lambda windows: system.Reading(system.time.time(), cpu_temp=60.0, cpu_load=30.0))
    clock = [100.0]
    monkeypatch.setattr(system.time, "monotonic", lambda: clock[0])
    m = system.SystemMonitor({"refresh": "5"}, history=system.History(None))
    for i in range(system.MIN_POINTS):                # saved history, enough for the normal interval
        m.history.add(system.Reading(system.time.time() - 600 + i, cpu_load=30.0))

    async def run():
        first = await m.frame()                   # saved readings only: waits for a current one
        second = await m.frame()
        clock[0] += 60
        third = await m.frame()
        clock[0] += 5 * 60
        return first, second, third, await m.frame()

    first, second, third, fourth = asyncio.run(run())
    assert first is None and second is not None and third is None and fourth is not None
    assert m.status == "CPU 60° 30%" and not m.active


def test_first_start_redraws_early(monkeypatch):
    monkeypatch.setattr(system, "_Job", SyncJob)
    monkeypatch.setattr(system, "take_reading", lambda windows: system.Reading(system.time.time(), cpu_load=30.0))
    clock = [100.0]
    monkeypatch.setattr(system.time, "monotonic", lambda: clock[0])
    m = system.SystemMonitor({"refresh": "30"}, history=system.History(None))

    async def run():
        await m.frame()
        assert await m.frame() is not None                # a nearly empty graph...
        clock[0] += system.EARLY_REDRAW + 1
        return await m.frame()

    assert asyncio.run(run()) is not None                 # ...is redrawn after two minutes, not thirty


# ------------------------------------------------------------------ live: the direct drawing firmware
class FakeFeed:
    """The once-a-second readings, handed out by the test."""

    def __init__(self):
        self.kept = []

    def latest(self):
        return self.kept[-1] if self.kept else None

    def recent(self):
        return list(self.kept)

    def push(self, **values):
        t = 70_000.0 + len(self.kept)
        self.kept.append((t, system.Reading(t, **values)))


def _live_mode(monkeypatch, values):
    monkeypatch.setattr(system, "_Job", SyncJob)
    monkeypatch.setattr(system, "take_reading", lambda windows: system.Reading(system.time.time(), cpu_load=30.0))
    drawn = []
    real = system.render_system
    monkeypatch.setattr(system, "render_system", lambda *a: drawn.append(a) or real(*a))
    m = system.SystemMonitor(values, history=system.History(None), feed=FakeFeed())
    return m, drawn


def test_drawn_pictures_follow_every_reading(monkeypatch):
    m, drawn = _live_mode(monkeypatch, {"window": "5"})
    m.direct = True
    assert asyncio.run(m.frame()) is None                             # no reading yet
    m.feed.push(cpu_temp=61.0, cpu_load=20.0)
    first = asyncio.run(m.frame())
    assert first.size == (device.WIDTH, device.HEIGHT) and m.status == "CPU 61° 20%"
    assert asyncio.run(m.frame()) is None                             # the same reading: nothing new to show
    m.feed.push(cpu_temp=64.0, cpu_load=80.0)
    second = asyncio.run(m.frame())
    assert second is not None and second.tobytes() != first.tobytes() and m.status == "CPU 64° 80%"
    readings, now, span, live = drawn[-1]
    assert [r.cpu_temp for r in readings] == [61.0, 64.0] and (now, span, live) == (70_001.0, 300, True)


def test_a_long_graph_is_the_trend_with_the_line_drawn_up_to_this_second(monkeypatch):
    m, drawn = _live_mode(monkeypatch, {"window": "60"})
    m.direct = True
    for i in range(3):
        m.history.add(system.Reading(70_000.0 - 30 + 10 * i, cpu_temp=50.0 + i))
    m.history.add(system.Reading(70_000.0 - 2 * 3600, cpu_temp=10.0))  # older than the graph
    m.feed.push(cpu_temp=61.0)
    assert asyncio.run(m.frame()) is not None
    readings, now, span, live = drawn[-1]
    assert [r.cpu_temp for r in readings][-4:] == [50.0, 51.0, 52.0, 61.0] and 10.0 not in [r.cpu_temp for r in readings]
    assert (span, live) == (3600, True)


def test_stored_pictures_keep_their_interval_and_show_half_an_hour_at_least(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(system.time, "monotonic", lambda: clock[0])
    m, drawn = _live_mode(monkeypatch, {"window": "1", "refresh": "5"})
    m.feed.push(cpu_temp=61.0)                                        # not looked at: pictures are stored here

    async def run():
        await m.frame()
        first = await m.frame()
        clock[0] += 60
        return first, await m.frame()

    first, second = asyncio.run(run())
    assert first is not None and second is None
    assert drawn[-1][2] == 30 * 60 and len(drawn[-1]) == 3            # not live: no seconds on the clock


def test_the_live_page_shows_seconds():
    now = 50_000.0
    readings = [system.Reading(now - 60 + i, cpu_temp=50.0, cpu_load=10.0) for i in range(60)]
    assert system.render_system(readings, now, 60, True).tobytes() != system.render_system(readings, now, 60).tobytes()


def test_feed_keeps_the_newest_readings_and_stops_when_unused(monkeypatch):
    import time
    monkeypatch.setattr(system, "FEED_IDLE", 1.0)       # (long enough for a busy PC to start the thread in)
    count = iter(range(1000))
    feed = system.Feed(None, read=lambda: (next(count),), keep=2)
    nap = time.sleep
    monkeypatch.setattr(system.time, "sleep", lambda seconds: nap(0.01))   # a reading every moment, not every second
    assert feed.recent() == [] and feed.latest() is None       # (in this order: asking for the latest starts the readings)
    deadline = time.monotonic() + 10
    while len(feed.recent()) < 2 and time.monotonic() < deadline:
        feed.latest()
        nap(0.02)
    recent = feed.recent()
    assert len(recent) == 2 and recent[1][1] == recent[0][1] + 1 and feed.latest()[1] >= recent[1][1]
    thread = feed._thread
    thread.join(5)
    assert not thread.is_alive()
