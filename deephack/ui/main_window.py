from __future__ import annotations

import time

from PySide6.QtCore import QDir, Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QKeySequenceEdit, QProgressBar, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .. import autostart, firmware
from ..config import Config
from ..engine import Engine, quit_deepcool
from ..hotkeys import Hotkeys
from ..modes import BY_ID, MODES
from . import style
from .firmware_dialog import FlashDialog
from .widgets import ComboBox, ModeButton, OrderList, Preview, StatusDot, Switch


def _label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    return lab


def _scroll(inner: QWidget) -> QScrollArea:
    """Vertical scroller that is invisible until its content no longer fits."""
    area = QScrollArea(objectName="scroll")
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    inner.setObjectName("scrollInner")
    area.setWidget(inner)
    return area


def _card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame(objectName="card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(10)
    return frame, lay


class MainWindow(QMainWindow):
    def __init__(self, config: Config, engine: Engine):
        super().__init__()
        self.config, self.engine = config, engine
        self.quitting = False
        self._placeholder = True
        self._state: dict = {}
        self._viewing = ""                       # the mode whose page is open; config "mode" is the one on the cooler
        self._live_frames: tuple | None = None   # last frames of the mode on the cooler, to show again
        self.hotkeys = Hotkeys()                 # the keys of the mode on the cooler (app.py lets it hear them)
        self._keys_refused: set[str] = set()     # ... and the ones Windows would not give us
        self.setWindowTitle("deephack")
        self.setFixedSize(960, 620)

        root = QWidget()
        self.setCentralWidget(root)
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._build_sidebar())
        row.addWidget(self._build_content(), 1)

        engine.frameChanged.connect(self._on_frame)
        engine.previewChanged.connect(self._on_preview)
        engine.stateChanged.connect(self._on_state)
        if config.get("mode") not in BY_ID:
            config.set("mode", MODES[0].id)
        self._mark_live()
        self.sync_hotkeys()
        self.view_mode(config.get("mode"))

    # ------------------------------------------------------------ keys
    def sync_hotkeys(self) -> None:
        """Register the keys of the mode that is on the cooler (its settings of kind "shortcut"), and no others."""
        cls = BY_ID[self._live]
        values, keys = self.config.mode_settings(cls.id), {}
        for s in cls.settings:
            text = values.get(s.key, s.default) if s.kind == "shortcut" else ""
            if text and text not in keys:
                keys[text] = lambda mode_id=cls.id, action=s.action: self.engine.mode_action(mode_id, action)
        self._keys_refused = self.hotkeys.set(keys)

    # ------------------------------------------------------------ layout
    def _build_sidebar(self) -> QWidget:
        side = QFrame(objectName="sidebar")
        side.setFixedWidth(256)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(16, 22, 16, 18)
        lay.setSpacing(6)
        lay.addWidget(_label("deephack", "brand"))
        lay.addWidget(_label("DeepCool MYSTIQUE display", "brandSub"))
        lay.addSpacing(22)
        lay.addWidget(_label("MODES", "section"))
        lay.addSpacing(2)

        self._mode_buttons = QButtonGroup(self)
        self._mode_buttons.setExclusive(True)
        modes = QWidget()
        mode_list = QVBoxLayout(modes)
        mode_list.setContentsMargins(0, 0, 0, 0)
        mode_list.setSpacing(6)
        for cls in MODES:
            btn = ModeButton(cls.glyph, cls.title, cls.description)
            btn.clicked.connect(lambda _=False, mode_id=cls.id: self.view_mode(mode_id))
            btn.doubleClicked.connect(lambda mode_id=cls.id: self.activate_mode(mode_id))
            btn.setProperty("mode_id", cls.id)
            self._mode_buttons.addButton(btn)
            mode_list.addWidget(btn)
        mode_list.addStretch(1)
        self._mode_scroll = _scroll(modes)
        lay.addWidget(self._mode_scroll, 1)
        lay.addSpacing(8)

        status = QHBoxLayout()
        status.setSpacing(10)
        self._dot = StatusDot()
        col = QVBoxLayout()
        col.setSpacing(0)
        self._device_title = _label("Looking for the cooler…", "modeTitle")
        self._device_sub = _label("", "modeDesc")
        col.addWidget(self._device_title)
        col.addWidget(self._device_sub)
        status.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)
        status.addLayout(col, 1)
        lay.addLayout(status)
        return side

    def _build_content(self) -> QWidget:
        content = QWidget(objectName="content")
        outer = QVBoxLayout(content)
        outer.setContentsMargins(28, 22, 28, 22)
        outer.setSpacing(16)

        # banner: another program holds the USB device
        self._banner = QFrame(objectName="banner")
        b = QHBoxLayout(self._banner)
        b.setContentsMargins(14, 10, 10, 10)
        self._banner_text = _label("", wrap=True)
        self._banner_button = QPushButton("Quit DeepCool app")
        self._banner_button.clicked.connect(self._quit_deepcool)
        b.addWidget(self._banner_text, 1)
        b.addWidget(self._banner_button)
        self._banner.hide()
        outer.addWidget(self._banner)

        row = QHBoxLayout()
        row.setSpacing(28)
        outer.addLayout(row, 1)

        left = QVBoxLayout()
        left.setSpacing(8)
        self._preview = Preview()
        left.addWidget(self._preview, 0, Qt.AlignmentFlag.AlignHCenter)
        left.addSpacing(4)
        self._now = _label("", "nowLine")
        self._now.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._now.setFixedWidth(354)
        self._caption = _label("", "caption")
        self._caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        left.addWidget(self._now)
        left.addWidget(self._caption)
        left.addStretch(1)
        row.addLayout(left)

        column = QWidget()
        right = QVBoxLayout(column)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(14)
        row.addWidget(_scroll(column), 1)

        # title, and either "on the cooler" or a button to put it there
        head = QHBoxLayout()
        head.setSpacing(8)
        self._mode_title = _label("", "h1")
        self._live_dot = StatusDot()
        self._live_dot.setFixedSize(8, 8)
        self._live_dot.set_color(style.OK)
        self._live_label = _label("On the cooler", "muted")
        self._show_button = QPushButton("Show on cooler", objectName="primary")
        self._show_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._show_button.clicked.connect(lambda: self.activate_mode(self._viewing))
        head.addWidget(self._mode_title, 1)
        head.addWidget(self._live_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self._live_label)
        head.addWidget(self._show_button)
        right.addLayout(head)
        self._mode_desc = _label("", "muted", wrap=True)
        right.addWidget(self._mode_desc)

        self._settings_card, self._settings_layout = _card()
        right.addWidget(self._settings_card)

        display, dl = _card()
        dl.addWidget(_label("DISPLAY", "section"))
        self._enabled = self._switch_row(dl, "Draw on the cooler", "Off: the cooler shows your own stored image again.",
                                         bool(self.config.get("enabled")), self._set_enabled)
        self._keep = self._switch_row(dl, "Keep on screen", "Off: the cooler cycles through all images stored on it.",
                                      not self.config.get("slideshow"), lambda on: self.config.set("slideshow", not on))
        self._autostart = self._switch_row(dl, "Start with Windows", "Launches minimised to the tray.",
                                           autostart.is_enabled(), autostart.set_enabled)
        self._tray = self._switch_row(dl, "Keep running when closed", "The window hides to the tray instead of quitting.",
                                      bool(self.config.get("close_to_tray")), lambda on: self.config.set("close_to_tray", on))
        self._choice_row(dl, "Cooler light", "The light strip on the pump head.", "light",
                         (("screen", "Picks up the screen's edges"), ("motherboard", "Synced with the motherboard"),
                          ("warning", "The cooler's warning colour")))
        self._choice_row(dl, "Screen brightness", "", "brightness",
                         tuple((p, f"{p} %") for p in (100, 85, 70, 50, 30)))
        right.addWidget(display)

        # what the cooler's screen runs, and the way to the firmware that draws straight from USB
        fw, fl = _card()
        fl.addWidget(_label("FIRMWARE", "section"))
        self._fw_title = _label("")
        self._fw_hint = _label("", "hint", wrap=True)
        self._fw_result = _label("", "good", wrap=True)
        self._fw_bar = QProgressBar()
        self._fw_bar.setTextVisible(False)
        self._fw_install = QPushButton("", objectName="primary")
        self._fw_install.clicked.connect(lambda: self._ask_flash("direct"))
        self._fw_restore = QPushButton("Back to DeepCool's firmware…")
        self._fw_restore.clicked.connect(lambda: self._ask_flash("stock"))
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self._fw_install)
        buttons.addWidget(self._fw_restore)
        buttons.addStretch(1)
        for widget in (self._fw_title, self._fw_bar, self._fw_hint, self._fw_result):
            fl.addWidget(widget)
        fl.addLayout(buttons)
        right.addWidget(fw)
        self._refresh_firmware()
        right.addStretch(1)
        return content

    # ------------------------------------------------------------ firmware
    def _ask_flash(self, which: str) -> None:
        running = self._state.get("firmware")
        if not running:
            return
        dialog = FlashDialog(which, running["bcd"], self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.engine.flash_firmware(which, dialog.stock_path)

    def _refresh_firmware(self) -> None:
        running, flash = self._state.get("firmware"), self._state.get("flash") or {}
        stage = flash.get("stage")
        busy = stage in ("sending", "restarting", "checking")
        self._fw_bar.setVisible(busy)
        self._fw_result.setVisible(stage in ("done", "failed") and not busy)
        if stage in ("done", "failed"):
            self._fw_result.setObjectName("good" if stage == "done" else "bad")
            self._fw_result.setStyleSheet(f"color: {style.OK if stage == 'done' else style.ERR};")
            self._fw_result.setText(flash.get("message", ""))
        install = restore = False
        if busy:
            if stage == "sending":
                self._fw_bar.setRange(0, 1000)
                self._fw_bar.setValue(round(flash.get("progress", 0.0) * 1000))
                title = f"Sending the firmware · {round(flash.get('progress', 0.0) * 100)} %"
            else:
                self._fw_bar.setRange(0, 0)                    # no telling how far it is: the bar just moves
                title = ("Asking the cooler what it runs" if stage == "checking" else
                         f"The cooler is writing its firmware · {flash.get('seconds', 0)} s" if flash.get("off") else
                         "Waiting for the cooler to restart")
            hint = "Do not unplug the cooler, and do not shut the PC down or let it sleep."
        elif not running:
            title = "The cooler is not connected"
            hint = "Its firmware shows here once it is."
        else:
            patch, known = running["patch"], running["bcd"] in firmware.KNOWN
            restore = patch > 0 and known
            if patch == 0:
                title = f"DeepCool's firmware {running['version']}"
                hint = ("Every picture is written to the cooler's flash before it shows. The direct drawing firmware "
                        "draws them straight from USB instead: nothing is written, and pictures can move.")
                install = known
                self._fw_install.setText("Install direct drawing…")
            elif patch < firmware.PATCH_VERSION:
                title = f"Direct drawing firmware, version {patch}"
                hint = f"Pictures are drawn straight from USB. This deephack has version {firmware.PATCH_VERSION} of it, with fixes."
                install = known
                self._fw_install.setText(f"Update to version {firmware.PATCH_VERSION}…")
            else:
                title = f"Direct drawing firmware, version {patch}"
                hint = "Pictures are drawn straight from USB: nothing is written to the cooler's flash."
            if not known:
                hint = f"deephack does not know firmware {running['version']} and cannot replace it."
        self._fw_title.setText(title)
        self._fw_hint.setText(hint)
        self._fw_install.setVisible(install)
        self._fw_restore.setVisible(restore)

    def _choice_row(self, parent: QVBoxLayout, title: str, hint: str, key: str, choices) -> ComboBox:
        parent.addWidget(_label(title))
        box = ComboBox()
        box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        box.setMinimumContentsLength(12)
        for value, text in choices:
            box.addItem(text, value)
        box.setCurrentIndex(max(0, box.findData(self.config.get(key))))
        box.currentIndexChanged.connect(lambda _i: self.config.set(key, box.currentData()))
        parent.addWidget(box)
        if hint:
            parent.addWidget(_label(hint, "hint", wrap=True))
        return box

    def _switch_row(self, parent: QVBoxLayout, title: str, hint: str, checked: bool, on_toggle) -> Switch:
        row = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(_label(title))
        col.addWidget(_label(hint, "hint", wrap=True))
        sw = Switch()
        sw.setChecked(checked)
        sw.toggled.connect(on_toggle)
        row.addLayout(col, 1)
        row.addWidget(sw, 0, Qt.AlignmentFlag.AlignVCenter)
        parent.addLayout(row)
        return sw

    # ------------------------------------------------------------ modes
    @property
    def _live(self) -> str:
        return self.config.get("mode")

    def view_mode(self, mode_id: str) -> None:
        """Open a mode's page. The cooler keeps showing what it shows; the preview runs the viewed mode."""
        cls = BY_ID[mode_id]
        changed = mode_id != self._viewing
        self._viewing = mode_id
        for btn in self._mode_buttons.buttons():
            btn.setChecked(btn.property("mode_id") == mode_id)
            if btn.isChecked():   # once laid out, so the selected mode is not hidden in the scrolled list
                QTimer.singleShot(0, lambda b=btn: self._mode_scroll.ensureWidgetVisible(b, 0, 8))
        self._mode_title.setText(cls.title)
        self._mode_desc.setText(cls.description + ".")
        self._build_settings(cls)
        live = mode_id == self._live
        self._live_dot.setVisible(live)
        self._live_label.setVisible(live)
        self._show_button.setVisible(not live)
        if changed:
            self.engine.set_preview(None if live else mode_id)
            if live and self._live_frames:
                self._show_frames(*self._live_frames)
        self._on_state(self._state)

    def activate_mode(self, mode_id: str) -> None:
        """Put a mode on the cooler (and open its page)."""
        if self._live != mode_id:
            self.config.set("mode", mode_id)
            self._live_frames = None
            self.engine.reload_mode()
            self._mark_live()
            self.sync_hotkeys()
        self._viewing = ""                       # rebuild the page as the live one
        self.view_mode(mode_id)

    def _mark_live(self) -> None:
        for btn in self._mode_buttons.buttons():
            btn.set_live(btn.property("mode_id") == self._live)

    def _build_settings(self, cls) -> None:
        lay = self._settings_layout
        def clear(layout) -> None:
            while layout.count():
                item = layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
                elif item.layout():
                    clear(item.layout())
        clear(lay)
        values = self.config.mode_settings(cls.id)
        if cls.actions:                          # what the mode can be told to do: a button each
            row = QHBoxLayout()
            row.setSpacing(8)
            for action, label in cls.actions:
                button = QPushButton(label)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.clicked.connect(lambda _=False, a=action: self.engine.mode_action(cls.id, a))
                row.addWidget(button, 1)
            lay.addLayout(row)
        lay.addWidget(_label("SETTINGS", "section"))
        for s in cls.settings:
            lay.addWidget(_label(s.label))
            current = values.get(s.key, s.default)
            if s.kind == "shortcut":
                line = QHBoxLayout()
                line.setSpacing(8)
                keys = QKeySequenceEdit(QKeySequence(current))
                keys.setMaximumSequenceLength(1)
                keys.editingFinished.connect(lambda k=keys, key=s.key: self._set_setting(
                    cls.id, key, k.keySequence().toString(QKeySequence.SequenceFormat.PortableText)))
                none = QPushButton("None")
                none.clicked.connect(lambda _=False, k=keys, key=s.key: (k.clear(), self._set_setting(cls.id, key, "")))
                line.addWidget(keys, 1)
                line.addWidget(none)
                lay.addLayout(line)
                if cls.id == self._live and current in self._keys_refused:
                    note = _label("Windows will not give deephack this key: another program has it, or it is no single key.",
                                  "hint", wrap=True)
                    note.setStyleSheet(f"color: {style.WARN};")
                    lay.addWidget(note)
            elif s.kind == "order":
                order = OrderList(s.choices, current)
                order.changed.connect(lambda value, key=s.key: self._set_setting(cls.id, key, value))
                lay.addWidget(order)
            elif s.kind == "choice":
                box = ComboBox()
                box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                box.setMinimumContentsLength(12)      # long entries must not widen the column
                for value, text in s.choices:
                    box.addItem(text, value)
                box.setCurrentIndex(max(0, box.findData(current)))
                box.currentIndexChanged.connect(lambda _i, b=box, key=s.key: self._set_setting(cls.id, key, b.currentData()))
                lay.addWidget(box)
            else:
                edit = QLineEdit(current)
                edit.setPlaceholderText(s.placeholder)
                edit.setMinimumWidth(60)
                if s.kind == "secret":            # shown while typing, dots otherwise (and in screenshots)
                    edit.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
                edit.editingFinished.connect(lambda e=edit, key=s.key: self._set_setting(cls.id, key, e.text().strip()))
                if s.kind in ("folder", "file"):
                    line = QHBoxLayout()
                    line.setSpacing(8)
                    browse = QPushButton("Browse…")
                    browse.clicked.connect(lambda _=False, e=edit, key=s.key, label=s.label, kind=s.kind:
                                           self._browse(cls.id, key, e, label, kind))
                    line.addWidget(edit, 1)
                    line.addWidget(browse)
                    lay.addLayout(line)
                else:
                    lay.addWidget(edit)
            if s.hint:
                lay.addWidget(_label(s.hint, "hint", wrap=True))
        self._settings_card.setVisible(bool(cls.settings))

    def _browse(self, mode_id: str, key: str, edit: QLineEdit, label: str, kind: str = "folder") -> None:
        here = edit.text().strip()
        if kind == "file":
            picked = QFileDialog.getOpenFileName(self, label, here if "://" not in here else "")[0]
        else:
            picked = QFileDialog.getExistingDirectory(self, label, here)
        if picked:
            edit.setText(QDir.toNativeSeparators(picked))
            self._set_setting(mode_id, key, edit.text())

    def _set_setting(self, mode_id: str, key: str, value) -> None:
        if self.config.mode_settings(mode_id).get(key, None) == value:
            return
        self.config.set_mode_setting(mode_id, key, value)

        def uses(target: str) -> bool:           # Auto, for one, runs the other modes with their own settings
            return target == mode_id or mode_id in BY_ID[target].reads

        setting = next((s for s in BY_ID[mode_id].settings if s.key == key), None)
        key_setting = setting is not None and setting.kind == "shortcut"
        if setting is not None and (setting.live or key_setting):
            self.engine.mode_value(mode_id, key, value)      # the running mode goes on, with the new value
        else:
            if uses(self._live):
                self.engine.reload_mode()
            if self._viewing != self._live and uses(self._viewing):
                self.engine.reload_preview()
        if mode_id == self._live and key_setting:
            self.sync_hotkeys()
            QTimer.singleShot(0, lambda: self._viewing == mode_id and self._build_settings(BY_ID[mode_id]))

    def _set_enabled(self, on: bool) -> None:
        self.config.set("enabled", on)
        if self._viewing == self._live:
            self._preview.set_dimmed(not on)
        self._refresh_caption()

    def set_enabled(self, on: bool) -> None:
        self._enabled.setChecked(on)
        self._set_enabled(on)

    # ------------------------------------------------------------ engine events
    def _on_frame(self, frames: list, duration_ms: int, placeholder: bool) -> None:
        self._live_frames = (frames, duration_ms, placeholder)
        if self._viewing == self._live:
            self._show_frames(frames, duration_ms, placeholder)

    def _on_preview(self, mode_id: str, frames: list, duration_ms: int, placeholder: bool) -> None:
        if mode_id == self._viewing != self._live:     # late frames of a page already left are dropped
            self._show_frames(frames, duration_ms, placeholder)

    def _show_frames(self, frames: list, duration_ms: int, placeholder: bool) -> None:
        self._placeholder = placeholder
        self._preview.set_frames(frames, duration_ms, dimmed=self._viewing == self._live and not self.config.get("enabled"))
        self._refresh_caption()

    def _on_state(self, state: dict) -> None:
        self._state = state
        if not state:
            return
        dev = state["device"]
        if dev == "connected":
            self._dot.set_color(style.OK)
            self._device_title.setText("Cooler connected")
            running = state.get("firmware")
            self._device_sub.setText("MYSTIQUE · USB" if not running else f"Firmware {running['version']}"
                                     + (" · direct drawing" if running["patch"] else " · DeepCool's own"))
        elif dev == "busy":
            self._dot.set_color(style.WARN)
            self._device_title.setText("Display is in use")
            self._device_sub.setText("Another program holds it")
        elif dev == "preview":
            self._dot.set_color(style.MUTED)
            self._device_title.setText("Preview mode")
            self._device_sub.setText("The cooler is not touched")
        else:
            self._dot.set_color(style.ERR)
            self._device_title.setText("Cooler not found")
            self._device_sub.setText("Check the USB header cable")
        self._banner.setVisible(dev == "busy")
        if dev == "busy":
            running = state["deepcool_running"]
            self._banner_text.setText("The DeepCool app is running and holds the display exclusively. Quit it to let deephack draw."
                                      if running else "Another program holds the cooler's USB interface. Close it, then retry.")
            self._banner_button.setText("Quit DeepCool app" if running else "Retry")
        if self._viewing == self._live:
            now = state.get("mode_status", "")
        else:
            now = state.get("preview_status", "") if state.get("preview") == self._viewing else ""
        self._now.setText(self._now.fontMetrics().elidedText(now, Qt.TextElideMode.ElideRight, 350))
        self._refresh_caption()
        self._refresh_firmware()

    def _refresh_caption(self) -> None:
        s = self._state
        if self._viewing != self._live:
            text = f"Preview · the cooler shows {BY_ID[self._live].title}"
        elif not self.config.get("enabled"):
            text = "Paused · the cooler shows your own stored image"
        elif not s or s.get("device") != "connected":
            text = "Preview only · not connected to the cooler"
        elif self._placeholder:
            text = "Waiting for something to show"
        elif s.get("no_room"):
            text = "No room on the cooler: it keeps about 9 pictures. Delete some in the DeepCool app."
        elif s.get("storing"):
            text = "Storing frames on the cooler, once · {} of {}".format(*s["storing"])
        elif s.get("live") and s.get("mode_live") == "drawn":
            text = "Live · drawn straight from USB, nothing is stored"
        elif s.get("live") and s.get("mode_live") == "flipbook":
            text = "Live · flipping stored frames, no flash writes"
        elif s.get("live"):
            text = "Live · the cooler draws this itself, no flash writes"
        elif s.get("pending"):
            text = "Sending to the cooler…"
        elif s.get("last_upload"):
            text = "On the cooler since " + time.strftime("%H:%M", time.localtime(s["last_upload"]))
            if s.get("drawn"):
                text += " · drawn from USB, nothing stored"
        else:
            text = ""
        self._caption.setText(text)

    def _quit_deepcool(self) -> None:
        if self._state.get("deepcool_running"):
            quit_deepcool()
        self.engine.retry_device()

    # ------------------------------------------------------------ window
    def closeEvent(self, event):  # noqa: N802
        if not self.quitting and self.config.get("close_to_tray"):
            event.ignore()
            self.hide()
            self.view_mode(self._live)            # no preview running while nobody looks
        else:
            event.accept()
