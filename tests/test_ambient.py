import socket
import socketserver
import struct
import threading
import time

from PIL import Image

from deephack import ambient, openrgb
from deephack.modes.base import Animation, Dashboard


def _string(text):
    raw = text.encode() + b"\0"
    return struct.pack("<H", len(raw)) + raw


def _mode(name):
    return _string(name) + struct.pack("<iIIIIIIIIIII", 0, 0, 0, 0, 0, 100, 0, 0, 0, 100, 0, 0) + struct.pack("<H", 1) + bytes(4)


def _zone(name, leds, segments=()):
    out = _string(name) + struct.pack("<iIII", 1, 0, 500, leds) + struct.pack("<H", 0)
    out += struct.pack("<H", len(segments)) + b"".join(_string(s) + struct.pack("<iII", 0, 0, 1) for s in segments)
    return out


def controller(name, zones, version=4):
    """A REQUEST_CONTROLLER_DATA answer laid out like OpenRGB's."""
    body = struct.pack("<i", 0) + _string(name) + (_string("ASUS") if version >= 1 else b"")
    body += b"".join(_string(s) for s in ("desc", "1.0", "", "HID: x"))
    body += struct.pack("<Hi", 2, 0) + _mode("Direct") + _mode("Rainbow")
    body += struct.pack("<H", len(zones)) + b"".join(_zone(n, c, ("seg",) if c else ()) for n, c in zones)
    leds = sum(c for _, c in zones)
    body += struct.pack("<H", leds) + b"".join(_string(f"LED {i}") + struct.pack("<I", 0) for i in range(leds))
    body += struct.pack("<H", leds) + bytes(4 * leds)
    return struct.pack("<I", len(body) + 4) + body


class FakeOpenRGB(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, devices):
        self.devices, self.received = devices, []
        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        srv = self.server
        while True:
            head = self.request.recv(16, socket.MSG_WAITALL)
            if len(head) < 16:
                return
            dev, pid, size = struct.unpack("<III", head[4:])
            data = self.request.recv(size, socket.MSG_WAITALL) if size else b""
            srv.received.append((dev, pid, data))
            reply = None
            if pid == openrgb.REQUEST_PROTOCOL_VERSION:
                reply = struct.pack("<I", 6)
            elif pid == openrgb.REQUEST_CONTROLLER_COUNT:
                self.request.sendall(openrgb.packet(0, openrgb.DEVICE_LIST_UPDATED))   # noise to skip
                reply = struct.pack("<I", len(srv.devices))
            elif pid == openrgb.REQUEST_CONTROLLER_DATA:
                reply = srv.devices[dev]
            if reply is not None:
                self.request.sendall(openrgb.packet(dev, pid, reply))


def test_parse_controller_versions():
    zones = [("Aura Mainboard", 1), ("Addressable RGB Header 1", 0), ("Addressable RGB Header 3", 30)]
    d = openrgb.parse_controller(0, controller("ASUS ProArt", zones), 4)
    assert d.name == "ASUS ProArt" and d.leds == 31
    assert d.zones == (openrgb.Zone("Aura Mainboard", 1), openrgb.Zone("Addressable RGB Header 1", 0),
                       openrgb.Zone("Addressable RGB Header 3", 30))


def test_client_against_a_server():
    server = FakeOpenRGB([controller("Board", [("Header", 12)]), controller("Strip", [("Zone", 0)])])
    try:
        client = openrgb.Client(port=server.server_address[1])
        assert client.version == 4                                    # the lower of 6 and ours
        devices = client.devices()
        assert [(d.name, d.leds) for d in devices] == [("Board", 12), ("Strip", 0)]
        client.set_custom_mode(0)
        client.set_leds(0, [(255, 10, 0)] * 12)
        client.devices()                                              # a round trip, so the server has seen it all
        client.close()
        sent = {pid: (dev, data) for dev, pid, data in server.received}
        assert sent[openrgb.SET_CLIENT_NAME][1] == b"deephack\0"
        assert openrgb.SETCUSTOMMODE in sent
        dev, data = sent[openrgb.UPDATELEDS]
        size, count = struct.unpack_from("<IH", data)
        assert dev == 0 and count == 12 and size == len(data) and data[6:10] == bytes((255, 10, 0, 0))
    finally:
        server.shutdown()


def test_screen_colour():
    assert max(ambient.screen_colour(Image.new("RGB", (480, 640), (200, 20, 20)))) == 255
    r, g, b = ambient.screen_colour(Image.new("RGB", (480, 640), (200, 20, 20)))
    assert r > 200 and g < 60 and b < 60
    framed = Image.new("RGB", (480, 640), (20, 40, 220))
    framed.paste((240, 40, 20), (60, 60, 420, 580))                  # red in the middle, blue at the edges
    r, g, b = ambient.screen_colour(framed)
    assert b > r                                                      # the edges count
    assert ambient.screen_colour(Image.new("RGB", (480, 640), (2, 2, 2))) == ambient.DIM
    grey = ambient.screen_colour(Image.new("RGB", (480, 640), (128, 128, 128)))
    assert grey[0] >= grey[1] >= grey[2] and grey[0] - grey[2] < 80     # soft white, not a colour


class FakeConfig:
    def __init__(self, on):
        self.on = on

    def get(self, key):
        return self.on if key == "ambient" else None


class FakeClient:
    instances = []

    def __init__(self):
        self.list_changed, self.sent, self.custom = False, [], []
        FakeClient.instances.append(self)

    def devices(self):
        return [openrgb.Device(0, "Board", (openrgb.Zone("Header", 3),), 3), openrgb.Device(1, "Empty", (), 0)]

    def set_custom_mode(self, index):
        self.custom.append(index)

    def set_leds(self, index, colours):
        self.sent.append((index, colours[0], len(colours)))

    def close(self):
        pass


def _wait(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.02)
    return cond()


def test_ambient_fades_every_lit_device_to_the_screen(monkeypatch):
    monkeypatch.setattr(ambient, "FADE_SECONDS", 0.2)
    FakeClient.instances.clear()
    a = ambient.Ambient(FakeConfig(True), connect=FakeClient)
    a.start()
    try:
        a.show(Image.new("RGB", (480, 640), (20, 200, 40)))
        target = ambient.screen_colour(Image.new("RGB", (480, 640), (20, 200, 40)))
        assert _wait(lambda: FakeClient.instances and FakeClient.instances[0].sent
                     and FakeClient.instances[0].sent[-1][1] == target)
        client = FakeClient.instances[0]
        assert client.custom == [0] and {i for i, _, _ in client.sent} == {0}   # the LED-less device is left alone
        assert all(n == 3 for _, _, n in client.sent) and len(client.sent) > 2  # it faded in steps
        assert a.status == "1 device: Board"
        # animations and the sensor screens have pictures too
        a.show(Animation([Image.new("RGB", (480, 640), (200, 30, 30))], 1000))
        assert _wait(lambda: client.sent[-1][1][0] > 200)
        a.show(Dashboard("dashboard", (5, 0, 1), {}, Image.new("RGB", (480, 640), (30, 30, 200))))
        assert _wait(lambda: client.sent[-1][1][2] > 200)
    finally:
        a.stop()


def test_ambient_off_and_unreachable(monkeypatch):
    monkeypatch.setattr(ambient, "RETRY_SECONDS", 0.05)
    a = ambient.Ambient(FakeConfig(False), connect=FakeClient)
    a.start()
    assert _wait(lambda: a.status == "off")
    a.stop()

    def refuse():
        raise ConnectionRefusedError

    b = ambient.Ambient(FakeConfig(True), connect=refuse)
    b.start()
    assert _wait(lambda: b.status == "unreachable")
    b.stop()
