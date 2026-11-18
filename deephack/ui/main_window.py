from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton,
                               QVBoxLayout, QWidget)

from .. import autostart
from ..config import Config
from ..engine import Engine, quit_deepcool
from ..modes import BY_ID, MODES
from . import style
from .widgets import ModeButton, Preview, StatusDot, Switch


def _label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    return lab


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
        engine.stateChanged.connect(self._on_state)
        self._select_mode(config.get("mode") if config.get("mode") in BY_ID else MODES[0].id, notify=False)

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
        for cls in MODES:
            btn = ModeButton(cls.glyph, cls.title, cls.description)
            btn.clicked.connect(lambda _=False, mode_id=cls.id: self._select_mode(mode_id))
            btn.setProperty("mode_id", cls.id)
            self._mode_buttons.addButton(btn)
            lay.addWidget(btn)
        lay.addStretch(1)

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

        right = QVBoxLayout()
        right.setSpacing(14)
        row.addLayout(right, 1)

        # active toggle
        head = QHBoxLayout()
        self._mode_title = _label("", "h1")
        self._enabled = Switch()
        self._enabled.setChecked(bool(self.config.get("enabled")))
        self._enabled.toggled.connect(self._set_enabled)
        self._enabled.setToolTip("Turn off to show your own stored image again")
        head.addWidget(self._mode_title, 1)
        head.addWidget(_label("Active", "muted"))
        head.addWidget(self._enabled)
        right.addLayout(head)
        self._mode_desc = _label("", "muted", wrap=True)
        right.addWidget(self._mode_desc)

        self._settings_card, self._settings_layout = _card()
        right.addWidget(self._settings_card)

        display, dl = _card()
        dl.addWidget(_label("DISPLAY", "section"))
        self._keep = self._switch_row(dl, "Keep on screen", "Off: the cooler cycles through all images stored on it.",
                                      not self.config.get("slideshow"), lambda on: self.config.set("slideshow", not on))
        self._autostart = self._switch_row(dl, "Start with Windows", "Launches minimised to the tray.",
                                           autostart.is_enabled(), autostart.set_enabled)
        self._tray = self._switch_row(dl, "Keep running when closed", "The window hides to the tray instead of quitting.",
                                      bool(self.config.get("close_to_tray")), lambda on: self.config.set("close_to_tray", on))
        right.addWidget(display)
        right.addStretch(1)
        return content

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
    def _select_mode(self, mode_id: str, notify: bool = True) -> None:
        cls = BY_ID[mode_id]
        for btn in self._mode_buttons.buttons():
            btn.setChecked(btn.property("mode_id") == mode_id)
        self._mode_title.setText(cls.title)
        self._mode_desc.setText(cls.description + ".")
        self._build_settings(cls)
        if notify and self.config.get("mode") != mode_id:
            self.config.set("mode", mode_id)
            self.engine.reload_mode()

    def _build_settings(self, cls) -> None:
        lay = self._settings_layout
        while lay.count():
            item = lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        values = self.config.mode_settings(cls.id)
        lay.addWidget(_label("SETTINGS", "section"))
        for s in cls.settings:
            lay.addWidget(_label(s.label))
            current = values.get(s.key, s.default)
            if s.kind == "choice":
                box = QComboBox()
                for value, text in s.choices:
                    box.addItem(text, value)
                box.setCurrentIndex(max(0, box.findData(current)))
                box.currentIndexChanged.connect(lambda _i, b=box, key=s.key: self._set_setting(cls.id, key, b.currentData()))
                lay.addWidget(box)
            else:
                edit = QLineEdit(current)
                edit.setPlaceholderText(s.placeholder)
                edit.editingFinished.connect(lambda e=edit, key=s.key: self._set_setting(cls.id, key, e.text().strip()))
                lay.addWidget(edit)
            if s.hint:
                lay.addWidget(_label(s.hint, "hint", wrap=True))
        self._settings_card.setVisible(bool(cls.settings))

    def _set_setting(self, mode_id: str, key: str, value) -> None:
        if self.config.mode_settings(mode_id).get(key, None) == value:
            return
        self.config.set_mode_setting(mode_id, key, value)
        if self.config.get("mode") == mode_id:
            self.engine.reload_mode()

    def _set_enabled(self, on: bool) -> None:
        self.config.set("enabled", on)
        self._preview.set_dimmed(not on)
        self._refresh_caption()

    def set_enabled(self, on: bool) -> None:
        self._enabled.setChecked(on)
        self._set_enabled(on)

    # ------------------------------------------------------------ engine events
    def _on_frame(self, frames: list, duration_ms: int, placeholder: bool) -> None:
        self._placeholder = placeholder
        self._preview.set_frames(frames, duration_ms, dimmed=not self.config.get("enabled"))
        self._refresh_caption()

    def _on_state(self, state: dict) -> None:
        self._state = state
        dev = state["device"]
        if dev == "connected":
            self._dot.set_color(style.OK)
            self._device_title.setText("Cooler connected")
            self._device_sub.setText("MYSTIQUE · USB")
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
        self._now.setText(self._now.fontMetrics().elidedText(state["mode_status"], Qt.TextElideMode.ElideRight, 350))
        self._refresh_caption()

    def _refresh_caption(self) -> None:
        s = self._state
        if not self.config.get("enabled"):
            text = "Paused · the cooler shows your own stored image"
        elif not s or s.get("device") != "connected":
            text = "Preview only · not connected to the cooler"
        elif self._placeholder:
            text = "Waiting for something to show"
        elif s.get("pending"):
            text = "Sending to the cooler…"
        elif s.get("last_upload"):
            text = "On the cooler since " + time.strftime("%H:%M", time.localtime(s["last_upload"]))
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
        else:
            event.accept()
