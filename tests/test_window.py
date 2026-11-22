"""The window: a page for each mode, and one page, Settings, for what belongs to no mode. No screen is
needed (Qt's "offscreen" platform), the engine is not started, and the registry is left alone."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from PySide6.QtCore import QPoint, QRect  # noqa: E402

from deephack import autostart, capture, firmware  # noqa: E402
from deephack.config import Config  # noqa: E402
from deephack.engine import Engine  # noqa: E402
from deephack.modes import BY_ID  # noqa: E402
from deephack.modes import mirror  # noqa: E402
from deephack.ui import main_window, pick  # noqa: E402

BCD = next(iter(firmware.KNOWN))


def state(patch, **flash):
    return {"device": "connected", "deepcool_running": False, "mode_status": "", "preview": None, "preview_status": "",
            "firmware": {"version": firmware.KNOWN[BCD], "bcd": BCD, "patch": patch}, "flash": flash or None}


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    asked = []
    monkeypatch.setattr(autostart, "is_enabled", lambda: False)
    monkeypatch.setattr(autostart, "set_enabled", lambda on: asked.append(("autostart", on)))
    config = Config(tmp_path / "config.json")
    config.set("mode", "weather")
    engine = Engine(config, use_device=False)
    for name in ("set_preview", "reload_mode", "reload_preview", "mode_value", "mode_action", "flash_firmware"):
        monkeypatch.setattr(engine, name, lambda *a, name=name: asked.append((name, *a)))
    win = main_window.MainWindow(config, engine)
    win.asked = asked
    win.show()
    app.processEvents()
    yield win
    win.hotkeys.clear()
    win.quitting = True
    win.close()
    app.processEvents()


def texts(widget) -> list[str]:
    return [label.text() for label in widget.findChildren(QLabel) if label.isVisible()]


def test_a_mode_page_has_only_the_modes_own_settings(window):
    page = window._pages.currentWidget()
    assert window._pages.currentIndex() == 0
    assert {"Weather", "City", "Units"} <= set(texts(page))
    for elsewhere in ("FIRMWARE", "Start with Windows", "Cooler light", "Screen brightness", "Keep on screen"):
        assert elsewhere not in texts(page)


def test_settings_holds_what_belongs_to_no_mode(window):
    window.view_mode("scene")                                # a preview is running ...
    window.asked.clear()
    window._settings_button.click()
    assert window._pages.currentIndex() == 1
    assert window.asked == [("set_preview", None)]           # ... and stops: nobody is looking at it
    assert window._settings_button.isChecked()
    assert not any(b.isChecked() for b in window._mode_buttons.buttons() if b is not window._settings_button)
    shown = texts(window._pages.currentWidget())
    for row in ("APP", "Start with Windows", "Keep running when closed", "COOLER", "Keep on screen", "Cooler light",
                "Screen brightness", "FIRMWARE", "ABOUT"):
        assert row in shown
    assert any(firmware_text.startswith("deephack ") for firmware_text in shown)

    window.asked.clear()
    window._autostart.click()
    window._tray.click()
    window._keep.click()
    window._light.setCurrentIndex(window._light.findData("motherboard"))
    window._brightness.setCurrentIndex(window._brightness.findData(30))
    assert window.asked == [("autostart", True)]
    config = window.config
    assert (config.get("close_to_tray"), config.get("slideshow"), config.get("light"), config.get("brightness")) == (
        False, True, "motherboard", 30)

    window.asked.clear()
    window.view_mode("scene")                                # back to the mode: its preview runs again
    assert window._pages.currentIndex() == 0 and window.asked == [("set_preview", "scene")]
    assert not window._settings_button.isChecked()


def test_the_switch_for_drawing_is_in_the_sidebar_on_every_page(window):
    assert window._enabled.isVisible() and window._enabled.isChecked()
    window._settings_button.click()
    assert window._enabled.isVisible()
    window._enabled.click()
    assert window.config.get("enabled") is False
    window.set_enabled(True)                                 # the tray's menu
    assert window._enabled.isChecked() and window.config.get("enabled") is True


def test_a_mode_that_needs_direct_drawing_says_so_and_leads_to_the_firmware(window):
    assert BY_ID["video"].needs_direct and BY_ID["tiktok"].needs_direct and not BY_ID["weather"].needs_direct
    window._on_state(state(firmware.PATCH_VERSION))
    window.view_mode("video")
    assert not window._needs.isVisible()                     # the cooler runs it: nothing to say
    window._on_state(state(0))
    assert window._needs.isVisible() and "Video plays only with the direct drawing firmware" in window._needs_text.text()
    window.view_mode("weather")
    assert not window._needs.isVisible()                     # a mode that does without
    window.view_mode("tiktok")
    assert window._needs.isVisible() and window._needs_text.text().startswith("TikTok")
    window._needs.findChild(QPushButton).click()
    assert window._pages.currentIndex() == 1
    QApplication.processEvents()
    card = window._fw_card                                   # scrolled to, where the page is longer than the window
    assert card.visibleRegion().boundingRect().height() == card.height()
    assert window._fw_install.isVisible() and window._fw_install.text() == "Install direct drawing…"


def test_the_firmware_card_works_from_the_settings_page(window, monkeypatch):
    window._settings_button.click()
    window._on_state(state(firmware.PATCH_VERSION))
    assert not window._fw_install.isVisible() and window._fw_restore.isVisible()
    assert window._settings_button.toolTip() == ""

    class Dialog:
        stock_path = None

        def __init__(self, which, bcd, parent):
            self.which = which

        def exec(self):
            return main_window.QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window, "FlashDialog", Dialog)
    window._on_state(state(0))
    window.asked.clear()
    window._fw_install.click()
    assert window.asked == [("flash_firmware", "direct", None)]
    window._on_state(state(0, stage="sending", progress=0.5))
    assert window._fw_bar.isVisible() and not window._fw_install.isVisible()
    assert "50 %" in window._fw_title.text()


def test_a_newer_patch_puts_a_dot_on_settings(window):
    if firmware.PATCH_VERSION < 2:
        pytest.skip("there is no older patch to come from")
    window._on_state(state(firmware.PATCH_VERSION - 1))
    assert window._settings_button._live.isVisible()
    assert str(firmware.PATCH_VERSION) in window._settings_button.toolTip()
    window._on_state(state(firmware.PATCH_VERSION))
    assert not window._settings_button._live.isVisible()
    window._on_state(state(0))                               # DeepCool's own is a choice, not something overdue
    assert not window._settings_button._live.isVisible()


def test_a_setting_changed_as_settings_opens_does_not_trip(window):
    window.view_mode("weather")
    window._settings_button.click()                          # the line edit loses the focus only now, in the real window
    window.asked.clear()
    window._set_setting("weather", "city", "Porto")
    assert window.asked == [("reload_mode",)]
    assert window.config.mode_settings("weather")["city"] == "Porto"


# ---------------------------------------------------------------- choosing what Mirror shows
def test_a_rectangle_dragged_on_a_scaled_screen_is_in_real_pixels():
    assert pick.dragged(QPoint(100, 100), QPoint(400, 300)) == QRect(100, 100, 300, 200)
    assert pick.dragged(QPoint(400, 300), QPoint(100, 100)) == QRect(100, 100, 300, 200)         # dragged up and left
    upright = pick.dragged(QPoint(0, 0), QPoint(500, 800), True)
    assert (upright.width(), upright.height()) == (500, 667)                                     # 3 to 4, as much as fits
    wide = pick.dragged(QPoint(900, 700), QPoint(100, 400), True)
    assert (wide.width(), wide.height()) == (400, 300) and wide.bottomRight() == QPoint(899, 699)    # 4 to 3, from the start
    # pressed at 375,250 for real, which the window calls 300,200: a screen scaled by 1.25
    assert pick.real((375, 250), QPoint(300, 200), QRect(300, 200, 600, 450), 1.25) == (375, 250, 750, 562)
    assert pick.real((375, 250), QPoint(300, 200), QRect(100, 120, 200, 80), 1.25) == (125, 150, 250, 100)
    assert pick.real((-1500, 40), QPoint(10, 40), QRect(10, 40, 100, 100), 1.0) == (-1500, 40, 100, 100)    # a screen on the left


def test_mirror_is_chosen_with_two_buttons_and_reaches_the_running_mode(window, monkeypatch):
    film = capture.Window(77, "vlc.exe", "Qt5QWindowIcon", "holiday.mp4")
    monkeypatch.setattr(pick.capture, "windows", lambda: [film, capture.Window(78, "notepad.exe", "Notepad", "a & b")])
    window.view_mode("mirror")
    chooser = window._settings_card.findChild(pick.CaptureChooser)
    assert chooser is not None and chooser._now.toolTip() == "Nothing chosen"
    window.asked.clear()
    menu = chooser._menu()
    assert [a.text() for a in menu.actions()] == ["holiday.mp4 · vlc", "a && b · notepad"]
    menu.actions()[0].trigger()
    value = mirror.window_value(film)
    assert window.asked == [("mode_value", "mirror", "source", value)]       # no restart of the mode: it reads it as it runs
    assert window.config.mode_settings("mirror")["source"] == value and chooser._now.toolTip() == "holiday.mp4"
    window.asked.clear()
    chooser._set(mirror.part_value(10, 20, 960, 1280))
    assert window.asked == [("mode_value", "mirror", "source", "part:10,20,960,1280")]
    assert chooser._now.toolTip() == "Part of the screen · 960 × 1280"
    chooser._set(mirror.part_value(10, 20, 960, 1280))                       # the same again: nothing to tell
    assert len(window.asked) == 1


def test_the_part_picker_hands_over_what_was_dragged(window, monkeypatch):
    monkeypatch.setattr(pick, "cursor", lambda: (1000, 500))
    picker = pick.PartPicker(QApplication.primaryScreen())
    picker._scale = 1.5
    got, gone = [], []
    picker.picked.connect(lambda *box: got.append(box))
    picker.cancelled.connect(lambda: gone.append(True))

    class Mouse:
        def __init__(self, x, y, button=pick.Qt.MouseButton.LeftButton, shift=False):
            self._at, self._button, self._shift = QPoint(x, y), button, shift

        def position(self):
            from PySide6.QtCore import QPointF
            return QPointF(self._at)

        def button(self):
            return self._button

        def modifiers(self):
            return pick.Qt.KeyboardModifier.ShiftModifier if self._shift else pick.Qt.KeyboardModifier.NoModifier

    picker.mousePressEvent(Mouse(200, 100))
    picker.mouseMoveEvent(Mouse(203, 104))
    picker.mouseReleaseEvent(Mouse(203, 104))
    assert got == []                                         # a slip of the hand: go on
    picker.mousePressEvent(Mouse(200, 100))
    picker.mouseMoveEvent(Mouse(520, 340))
    picker.mouseReleaseEvent(Mouse(520, 340))
    assert got == [(1000, 500, 480, 360)]                    # from where the mouse really was, in real pixels
    picker.mousePressEvent(Mouse(5, 5, pick.Qt.MouseButton.RightButton))
    assert gone == [True]
    picker.grab()                                            # it paints, with and without a rectangle
    picker.close()
