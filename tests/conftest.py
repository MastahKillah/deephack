import pytest

from deephack import device
from deephack.modes import claude_usage

from cooler import CoolerModel   # tests/ is on sys.path (pytest rootdir-relative imports)


@pytest.fixture(autouse=True)
def no_watching(monkeypatch):
    """Modes read the usage snapshot for some seconds when it was written lately, which the real one on a
    developer's PC always was. Not in tests."""
    monkeypatch.setattr(claude_usage, "WATCH_SECONDS", 0.0)


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
