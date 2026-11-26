"""The window: a page for each mode, and one page, Settings, for what belongs to no mode. No screen is
needed (Qt's "offscreen" platform), the engine is not started, and the registry is left alone."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect  # noqa: E402

from deephack import autostart, capture, firmware, hotkeys  # noqa: E402
from deephack.config import Config  # noqa: E402
from deephack.engine import Engine  # noqa: E402
from deephack.modes import BY_ID, MODES  # noqa: E402
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
    registered = {}                                          # Windows is not asked for any key: F9 is "taken"
    monkeypatch.setattr(main_window, "Hotkeys", lambda: hotkeys.Hotkeys(
        lambda number, modifiers, key: key != 0x78 and registered.setdefault(number, (modifiers, key)) is not None,
        lambda number: registered.pop(number, None)))
    for name in ("set_preview", "reload_mode", "reload_preview", "mode_value", "mode_action", "flash_firmware", "forget_firmware"):
        monkeypatch.setattr(engine, name, lambda *a, name=name: asked.append((name, *a)))
    win = main_window.MainWindow(config, engine)
    win.asked, win.registered = asked, registered
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
                "Screen brightness", "KEYS", "Next mode", "Previous mode", "Draw on the cooler, off and on", "FIRMWARE", "ABOUT"):
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


def test_an_update_that_waits_for_the_coolers_next_start_is_said_and_can_be_called_off(window):
    window._settings_button.click()
    window._on_state(state(9))
    assert not window._fw_forget.isVisible() and "waiting" not in window._fw_hint.text()
    window._on_state(dict(state(9), flash_wanted="direct"))
    assert window._fw_forget.isVisible() and window._fw_install.isVisible()          # (it can still be tried now)
    assert f"Version {firmware.PATCH_VERSION} is waiting to be sent" in window._fw_hint.text()
    window.asked.clear()
    window._fw_forget.click()
    assert window.asked == [("forget_firmware",)]
    window._on_state(dict(state(9), flash_wanted="stock"))
    assert "DeepCool's own firmware is waiting to be sent" in window._fw_hint.text()
    window._on_state(dict(state(9, stage="sending", progress=0.2), flash_wanted="direct"))
    assert not window._fw_forget.isVisible()                                         # it is being sent


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


# ---------------------------------------------------------------- a newer yt-dlp, from Settings
def test_youtube_links_can_be_kept_current_from_settings(window, monkeypatch):
    asked = []
    monkeypatch.setattr(main_window.ytdlp, "version", lambda: ("2027.1.15", True) if asked else ("2026.10.26", False))
    monkeypatch.setattr(main_window.ytdlp, "update", lambda: asked.append(True) or ("updated", "2027.1.15"))
    window._refresh_reader()
    window._settings_button.click()
    shown = texts(window._pages.currentWidget())
    assert "YOUTUBE LINKS" in shown and "Keep it current" in shown
    assert window._reader_title.text() == "yt-dlp 2026.10.26 · the one deephack came with"
    assert window.config.get("ytdlp_current") is False and not window._reader_auto.isChecked()      # off unless switched on
    window._reader_auto.click()
    assert window.config.get("ytdlp_current") is True and asked == []                               # the switch fetches nothing

    window._reader_button.click()
    assert not window._reader_button.isEnabled() and window._reader_result.text() == "Asking PyPI…"
    for _ in range(200):                                     # the answer comes from a thread
        QApplication.processEvents()
        if window._reader_button.isEnabled():
            break
        QApplication.instance().thread().msleep(10)
    assert asked == [True] and window._reader_result.text() == "Now using yt-dlp 2027.1.15."
    assert window._reader_title.text() == "yt-dlp 2027.1.15 · fetched by deephack"
    window._on_reader("current", "2027.1.15")
    assert window._reader_result.text() == "yt-dlp 2027.1.15 is the newest."
    window._on_reader("failed", "no net")
    assert window._reader_result.text() == "Could not: no net" and window._reader_button.isEnabled()


# ---------------------------------------------------------------- keys that work in any window
def press(window, text) -> None:
    assert window.hotkeys.pressed(window.hotkeys._texts[text])


def settled(window) -> list[str]:
    """What a mode's page says once it has been built anew: the old rows are gone, the new ones laid out."""
    QApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()
    return texts(window._settings_card)


def test_only_the_keys_of_the_mode_on_the_cooler_are_registered_and_they_reach_it(window):
    assert window.registered == {}                           # Weather has none, and the app's own are not set
    window.activate_mode("photo_frame")
    ctrl_alt = hotkeys.MOD_CONTROL | hotkeys.MOD_ALT
    assert sorted(window.registered.values()) == [(ctrl_alt, 0x21), (ctrl_alt, 0x22)]
    window.asked.clear()
    press(window, "Ctrl+Alt+PgDown")
    press(window, "Ctrl+Alt+PgUp")
    assert window.asked == [("mode_action", "photo_frame", "next"), ("mode_action", "photo_frame", "previous")]
    buttons = [b.text() for b in window._settings_card.findChildren(QPushButton)]
    assert buttons[:2] == ["Previous", "Next"]               # and the same two as buttons on its page
    window.activate_mode("video")                            # its keys are for the one who sets them
    assert window.registered == {}
    window._set_setting("video", "pause_key", "Ctrl+Alt+Space")
    press(window, "Ctrl+Alt+Space")
    assert window.asked[-1] == ("mode_action", "video", "pause")


def test_the_apps_own_keys_change_the_mode_and_switch_drawing(window):
    ids = [cls.id for cls in MODES]
    at = ids.index("weather")
    window._set_app_key("key_next_mode", "Ctrl+Alt+Right")
    window._set_app_key("key_previous_mode", "Ctrl+Alt+Left")
    window._set_app_key("key_draw", "Ctrl+Alt+D")
    assert len(window.registered) == 3 and window.config.get("key_next_mode") == "Ctrl+Alt+Right"
    window.asked.clear()
    press(window, "Ctrl+Alt+Right")
    assert window.config.get("mode") == ids[at + 1] and ("reload_mode",) in window.asked
    assert window._viewing == ids[at + 1]                    # the page showed what is on the cooler: it follows
    assert "Ctrl+Alt+Right" in window.hotkeys._texts         # the app's keys stay, whatever mode it is
    press(window, "Ctrl+Alt+Left")
    press(window, "Ctrl+Alt+Left")
    assert window.config.get("mode") == ids[at - 1] == window._viewing

    window._settings_button.click()                          # with Settings open the page stays, the cooler changes
    press(window, "Ctrl+Alt+Right")
    assert window.config.get("mode") == "weather" and window._pages.currentIndex() == 1 and window._viewing == ""
    window.view_mode("scene")                                # ... and so does a page that previews another mode
    window.asked.clear()
    press(window, "Ctrl+Alt+Left")
    assert window.config.get("mode") == ids[at - 1] and window._viewing == "scene"
    assert ("set_preview", None) not in window.asked and BY_ID[ids[at - 1]].title in window._caption.text()

    assert window.config.get("enabled") is True
    press(window, "Ctrl+Alt+D")
    assert window.config.get("enabled") is False and not window._enabled.isChecked()
    press(window, "Ctrl+Alt+D")
    assert window.config.get("enabled") is True and window._enabled.isChecked()
    window._set_app_key("key_draw", "")
    assert len(window.registered) == 2 and "Ctrl+Alt+D" not in window.hotkeys._texts


def test_a_key_that_two_settings_ask_for_goes_to_the_first_and_the_other_says_so(window):
    window._settings_button.click()
    window._set_app_key("key_next_mode", "Ctrl+Alt+PgDown")
    window._set_app_key("key_previous_mode", "Ctrl+Alt+PgDown")
    window._set_app_key("key_draw", "F9")
    notes = window._app_key_notes
    assert not notes["key_next_mode"].isVisible()
    assert notes["key_previous_mode"].text() == "This is the key for “Next mode” in Settings already: it does that."
    assert notes["key_draw"].text().startswith("Windows will not give deephack this key")
    window._set_app_key("key_previous_mode", "")
    window._set_app_key("key_draw", "")
    assert not notes["key_previous_mode"].isVisible() and not notes["key_draw"].isVisible()

    window.view_mode("photo_frame")                          # not on the cooler, and its page says it all the same
    assert "This is the key for “Next mode” in Settings already: it does that." in settled(window)
    window.activate_mode("photo_frame")
    window.asked.clear()
    press(window, "Ctrl+Alt+PgDown")
    assert window.config.get("mode") != "photo_frame" and ("mode_action", "photo_frame", "next") not in window.asked

    window._set_app_key("key_next_mode", "")
    window.activate_mode("tiktok")                           # two of a mode's own keys
    assert not any(t.startswith("This is the key for") for t in settled(window))
    window._set_setting("tiktok", "previous_key", "Ctrl+Alt+PgDown")
    assert [t for t in settled(window) if t.startswith("This is the key for")] == [
        "This is the key for “Key for the next video” already: it does that."]
    window._set_setting("tiktok", "previous_key", "F9")
    said = settled(window)
    assert any(t.startswith("Windows will not give deephack this key") for t in said)
    assert not any(t.startswith("This is the key for") for t in said)
    assert hotkeys.same("Media Play") == hotkeys.same("Toggle Media Play/Pause") != hotkeys.same("Media Next")


# ---- the lights beside the preview
def _halves(left, right):
    from PIL import Image, ImageDraw

    picture = Image.new("RGB", (480, 640), left)
    ImageDraw.Draw(picture).rectangle((240, 0, 479, 639), fill=right)
    return picture


def test_the_preview_shows_the_lights_the_cooler_would(window):
    """deephack's picking for a cooler that takes it (patch 16) and for one that is not there, the cooler's
    own one pixel a light otherwise, and none when the lights follow something else than the screen."""
    from deephack import device, light
    from deephack.engine import to_qimage

    picture = _halves((255, 0, 0), (0, 0, 255))
    preview = window._preview
    window._on_frame([to_qimage(picture)], 0, False)
    assert preview.lights() == light.colours(picture)                     # no cooler: as patch 16 would
    window._on_state(state(device.LIGHT_VERSION))
    assert preview.lights() == light.colours(picture)
    window._on_state(state(device.LIGHT_VERSION - 1))
    assert preview.lights() == light.cooler_colours(picture) == [(248, 0, 0)] * 7 + [(0, 0, 248)] * 7
    window._on_state(state(device.LIGHT_VERSION))
    window._keep.setChecked(False)                                        # a slide show: stored pictures, the cooler's picking
    assert preview.lights() == light.cooler_colours(picture)
    window._keep.setChecked(True)
    window._light_level.setCurrentIndex(window._light_level.findData(50))
    assert preview.lights() == light.colours(picture, 0.5) and preview.lights()[0] == (128, 0, 0)
    window._light.setCurrentIndex(window._light.findData("warning"))
    assert preview.lights() is None
    window._light.setCurrentIndex(window._light.findData("screen"))
    assert preview.lights() is not None
    window._on_frame([to_qimage(picture)], 0, True)                       # "nothing to show yet" is not on the cooler
    assert preview.lights() is None
    window._on_frame([to_qimage(picture)], 0, False)
    window.set_enabled(False)                                             # paused: the cooler shows a stored picture
    assert preview.lights() is None
    assert preview.grab().width() == preview.sizeHint().width() == 354 + 2 * preview.GLOW


def test_the_previews_lights_fade_as_the_coolers_do(window):
    from deephack import device, light
    from deephack.engine import to_qimage

    preview = window._preview
    red, blue = _halves((255, 0, 0), (255, 0, 0)), _halves((0, 0, 255), (0, 0, 255))
    window._on_frame([to_qimage(red)], 0, False)
    assert preview._lights[0] == [255.0, 0.0, 0.0] and not preview._fade.isActive()      # the first picture: at once
    preview._last -= 5                                                    # some seconds later: a page, not a video
    window._on_frame([to_qimage(blue)], 0, False)
    assert preview.lights() == light.colours(blue) and preview._fade.isActive()
    assert preview._fade.interval() == round(device.LIGHT_STEP * 1000)
    preview._step()
    first = preview._lights[0][2]
    assert 0 < first < 30 and preview._lights[0][0] > 225                 # a 26th of the way, as pace 10 is
    for _ in range(400):
        preview._step()
    assert preview._lights[0] == [0.0, 0.0, 255.0] and not preview._fade.isActive()
    window._on_state(state(device.LIGHT_VERSION - 1))                     # the cooler's own picking does not fade
    window._on_frame([to_qimage(red)], 0, False)
    assert preview._lights[0] == [248.0, 0.0, 0.0] and not preview._fade.isActive()
    preview.grab()                                                        # and it paints
