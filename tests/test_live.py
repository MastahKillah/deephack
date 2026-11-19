import asyncio
import time

from PIL import Image

from deephack import device, engine
from deephack.config import Config
from deephack.modes import live
from deephack.modes.base import Dashboard

BODY = {
    "CPU Temperature": {"Unit": "C", "Value": 75.5}, "CPU Usage": {"Unit": "%", "Value": 34.6},
    "Memory Load": {"Unit": "%", "Value": 51.8}, "CPU Clock": {"Unit": "MHz", "Value": 5375.0},
    "Motherboard v3 volt": {"Unit": "V", "Value": 3.28}, "Motherboard v5 volt": {"Unit": "V", "Value": 4.98},
    "Motherboard v12 volt": {"Unit": "V", "Value": 12.08},
    "Fan list": {"AIO Pump": 3341.0, "CPU_OPT": 829.0, "CPU": 1700.0},
}


class NoWindows:
    def cpu_load(self):
        return 12.0

    def memory(self):
        return 40.0, 1.0, 2.0


def test_pick_fans():
    assert live.pick_fans(BODY["Fan list"]) == (1700.0, 3341.0)
    assert live.pick_fans({"CPU_FAN": 900, "W_PUMP+": 2500}) == (900.0, 2500.0)
    assert live.pick_fans({"CPU_OPT": 800, "Chassis 1": 600}) == (800.0, None)     # the second CPU header beats a case fan
    assert live.pick_fans({}) == (None, None)


def test_dashboard_values():
    values, sensors = live.dashboard_values(BODY, NoWindows())
    assert sensors and values["cpu_freq"] == 5.375 and values["fan_rpm"] == 1700.0 and values["pump_rpm"] == 3341.0
    assert set(values) <= set(device.SENSOR_FIELDS)
    device.sensor_payload(**values)                                   # all in range for the wire format
    values, sensors = live.dashboard_values({"CPU Temperature": {"Value": -5}}, NoWindows())
    assert values["cpu_temp"] == 0.0 and values["cpu_freq"] == 0.0     # clamped, missing as zero
    assert live.dashboard_values(None, NoWindows()) == ({"cpu_load": 12.0, "ram_usage": 40.0}, False)


def test_feed_reads_in_the_background_and_stops_when_unused(monkeypatch):
    monkeypatch.setattr(live, "FEED_IDLE", 0.3)
    feed = live.LiveFeed(read=lambda: ({"cpu_temp": 50.0}, True))
    assert feed.latest() is None
    deadline = time.monotonic() + 3
    while feed.latest() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert feed.latest()[1:] == ({"cpu_temp": 50.0}, True)
    thread = feed._thread
    thread.join(3)
    assert not thread.is_alive()                                      # nobody asked for a while: it ended


class FakeFeed:
    def __init__(self):
        self.reading = None

    def latest(self):
        return self.reading


def test_live_mode_sends_one_dashboard_per_reading():
    feed = FakeFeed()
    m = live.Live({"screen": "fan_pump", "strip": "voltage"}, feed=feed)
    assert asyncio.run(m.frame()) is None
    values, _ = live.dashboard_values(BODY, NoWindows())
    feed.reading = (1000.0, values, True)
    dash = asyncio.run(m.frame())
    assert isinstance(dash, Dashboard) and dash.screen == "dashboard"
    assert dash.layout == (device.DASH_FAN_PUMP, 0, device.STRIP_VOLTAGE) and dash.values is values
    assert dash.preview.size == (device.WIDTH, device.HEIGHT)
    assert asyncio.run(m.frame()) is None                             # same reading: nothing new to send
    assert m.status == "CPU 76° · 5.38 GHz · pump 3341 rpm · fan 1700 rpm"
    feed.reading = (1001.0, values, True)
    m.values["screen"] = "recorder_frequency"
    dash = asyncio.run(m.frame())
    assert dash.screen == "recorder" and dash.layout == (1,)


def test_every_screen_renders():
    values, _ = live.dashboard_values(BODY, NoWindows())
    recent = [(1000.0 + i, values) for i in range(30)]
    for screen in live.SCREENS:
        for strip in live.STRIPS:
            assert live.render_preview(screen, strip, values, recent, 1030.0).size == (device.WIDTH, device.HEIGHT)


# ------------------------------------------------------------------ engine: the firmware's sensor screens
class RecordingDevice:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.calls.append((name, args))


def test_engine_drives_the_sensor_screen_without_writing_flash(tmp_path, monkeypatch):
    dev = RecordingDevice()
    monkeypatch.setattr(device, "Mystique", lambda: dev)
    config = Config(tmp_path / "config.json")
    config.set("slot_index", 3)
    config.set("restore_index", 0)
    eng = engine.Engine(config)
    clock = [1000.0]
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])
    preview = Image.new("RGB", (480, 640))

    def step(frame, wait=1.0):
        clock[0] += wait
        eng._set_frame(frame)
        dev.calls.clear()
        eng._device_step(True)
        return [(name, args) for name, args in dev.calls if name not in ("set_slideshow",)]

    step(Image.new("RGB", (480, 640), "red"), wait=engine.MIN_UPLOAD_INTERVAL + 1)      # connect, an image first
    dash = Dashboard("dashboard", (device.DASH_TEMPERATURE, 0, device.STRIP_SYSTEM), {"cpu_temp": 60.0}, preview)
    calls = step(dash)
    assert [name for name, _ in calls] == ["set_time", "set_mode", "set_dashboard", "send_sensors"]
    assert calls[1][1] == (device.MODE_SITUATIONAL,) and calls[2][1] == (device.DASH_TEMPERATURE, 0, device.STRIP_SYSTEM)
    assert step(Dashboard("dashboard", dash.layout, {"cpu_temp": 61.0}, preview)) == [("send_sensors", ())]
    calls = step(Dashboard("recorder", (0,), {"cpu_temp": 61.0}, preview))
    assert [name for name, _ in calls] == ["set_time", "set_mode", "set_recorder", "send_sensors"]
    assert calls[1][1] == (device.MODE_RECORDER,)
    calls = step(Image.new("RGB", (480, 640), "red"))                 # back to the stored image: no upload
    assert calls == [("set_mode", (device.MODE_MOTION,)), ("show_slot", (3, device.TYPE_JPG))]
    step(dash)
    dev.calls.clear()
    eng._shutdown()
    assert ("set_mode", (device.MODE_MOTION,)) in dev.calls and ("show_slot", (0, device.TYPE_JPG)) in dev.calls
