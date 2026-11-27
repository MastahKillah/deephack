import pytest

from deephack import device, update, ytdlp
from deephack.modes import claude_usage

from cooler import CoolerModel   # tests/ is on sys.path (pytest rootdir-relative imports)


@pytest.fixture(autouse=True)
def no_watching(monkeypatch):
    """Modes read the usage snapshot for some seconds when it was written lately, which the real one on a
    developer's PC always was. Not in tests."""
    monkeypatch.setattr(claude_usage, "WATCH_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def no_fetched_yt_dlp(monkeypatch, tmp_path_factory):
    """deephack keeps a newer yt-dlp it has fetched in the user's settings folder, and uses it in place of
    its own. Not in tests: one that stands yt-dlp in would be talking to the real thing on a PC where the
    app has fetched one (seen 24 Nov 2026, the day after the app here did)."""
    empty = tmp_path_factory.getbasetemp() / "no-settings"
    monkeypatch.setattr(ytdlp, "config_dir", lambda: empty)
    monkeypatch.setattr(ytdlp, "_loaded_from", None)


@pytest.fixture(autouse=True)
def no_real_updates(monkeypatch, tmp_path_factory):
    """deephack asks GitHub for a newer version of itself and keeps what it found in the user's settings
    folder. Not in tests: nothing is asked, and nothing found by the app on this PC is seen."""
    empty = tmp_path_factory.mktemp("no-update")             # (a new one each time: it also keeps what was said once)

    def no_net(url, largest=0):
        raise OSError("tests do not ask GitHub")

    monkeypatch.setattr(update, "config_dir", lambda: empty)
    monkeypatch.setattr(update, "fetch", no_net)
    monkeypatch.delenv(update.FEED_ENV, raising=False)


@pytest.fixture
def cooler(monkeypatch):
    """The real driver talking to a model of the cooler (tests/cooler.py). Fails the test on any violation."""
    model = CoolerModel()
    monkeypatch.setattr(device.libusb_package, "find", lambda **kwargs: model)
    for name in ("claim_interface", "release_interface"):
        monkeypatch.setattr(device.usb.util, name, lambda dev, interface: None)
    monkeypatch.setattr(device.usb.util, "dispose_resources", lambda dev: None)
    monkeypatch.setattr(device.time, "sleep", lambda seconds: None)     # the wait for the flash write
    yield model
    assert not model.violations, "the cooler model saw: " + "; ".join(model.violations)
