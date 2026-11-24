from __future__ import annotations

import threading
import time

from PySide6.QtCore import QDir, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QKeySequenceEdit, QProgressBar, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget)

from .. import __version__, autostart, firmware, ytdlp
from ..config import Config
from ..engine import Engine, quit_deepcool
from ..hotkeys import Hotkeys
from ..modes import BY_ID, MODES
from . import style
from .firmware_dialog import FlashDialog
from .pick import CaptureChooser
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


REPO = "https://github.com/a7ex-turcan/deephack"
LINKS = (("Source and releases", REPO), ("Licence", REPO + "/blob/main/LICENSE"),
         ("What the app carries", REPO + "/blob/main/THIRD-PARTY.md"))
SETTINGS_GLYPH = "\ue713"                        # the cog of "Segoe Fluent Icons"


def _card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame(objectName="card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(10)
    return frame, lay


class MainWindow(QMainWindow):
    readerChecked = Signal(str, str)             # how looking for a newer yt-dlp went (ytdlp.update), from its thread

    def __init__(self, config: Config, engine: Engine):
        super().__init__()
        self.config, self.engine = config, engine
        self.quitting = False
        self._placeholder = True
        self._state: dict = {}
        self._viewing = ""                       # the mode whose page is open; config "mode" is the one on the cooler.
                                                 # Empty while the Settings page is
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

        # what belongs to no mode: one page for it, and the switch that is wanted most often right here
        self._settings_button = ModeButton(SETTINGS_GLYPH, "Settings")
        self._settings_button.clicked.connect(lambda _=False: self.open_settings())
        self._mode_buttons.addButton(self._settings_button)
        rule = QFrame(objectName="rule")
        rule.setFixedHeight(1)
        lay.addWidget(rule)
        lay.addWidget(self._settings_button)
        draw = QHBoxLayout()
        draw.setContentsMargins(12, 2, 12, 8)
        text = _label("Draw on the cooler")
        self._enabled = Switch()
        self._enabled.setChecked(bool(self.config.get("enabled")))
        self._enabled.toggled.connect(self._set_enabled)
        for widget in (text, self._enabled):
            widget.setToolTip("Off: the cooler shows your own stored image again.")
        draw.addWidget(text, 1)
        draw.addWidget(self._enabled, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(draw)

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

        self._pages = QStackedWidget()           # a mode's page, or Settings
        self._pages.addWidget(self._build_mode_page())
        self._pages.addWidget(self._build_settings_page())
        outer.addWidget(self._pages, 1)
        self._refresh_firmware()
        return content

    def _build_mode_page(self) -> QWidget:
        page = QWidget()
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(28)

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

        # a mode that needs the direct drawing firmware, on a cooler that does not run it
        self._needs = QFrame(objectName="banner")
        nl = QVBoxLayout(self._needs)
        nl.setContentsMargins(14, 12, 14, 12)
        nl.setSpacing(10)
        self._needs_text = _label("", wrap=True)
        to_firmware = QPushButton("Firmware…")
        to_firmware.clicked.connect(lambda _=False: self.open_settings(self._fw_card))
        nl.addWidget(self._needs_text)
        nl.addWidget(to_firmware, 0, Qt.AlignmentFlag.AlignLeft)
        self._needs.hide()
        right.addWidget(self._needs)

        self._settings_card, self._settings_layout = _card()
        right.addWidget(self._settings_card)
        right.addStretch(1)
        return page

    def _build_settings_page(self) -> QWidget:
        """What belongs to no mode: the app, the cooler, the cooler's firmware."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        lay.addWidget(_label("Settings", "h1"))
        column = QWidget()
        cards = QVBoxLayout(column)
        cards.setContentsMargins(0, 0, 0, 0)
        cards.setSpacing(14)
        self._settings_scroll = _scroll(column)
        lay.addWidget(self._settings_scroll, 1)

        app, al = _card()
        al.addWidget(_label("APP", "section"))
        self._autostart = self._switch_row(al, "Start with Windows", "Launches minimised to the tray.",
                                           autostart.is_enabled(), autostart.set_enabled)
        self._tray = self._switch_row(al, "Keep running when closed", "The window hides to the tray instead of quitting.",
                                      bool(self.config.get("close_to_tray")), lambda on: self.config.set("close_to_tray", on))
        cards.addWidget(app)

        cooler, cl = _card()
        cl.addWidget(_label("COOLER", "section"))
        self._keep = self._switch_row(cl, "Keep on screen", "Off: the cooler cycles through all images stored on it.",
                                      not self.config.get("slideshow"), lambda on: self.config.set("slideshow", not on))
        self._light = self._choice_row(cl, "Cooler light", "The light strip on the pump head.", "light",
                                       (("screen", "Picks up the screen's edges"), ("motherboard", "Synced with the motherboard"),
                                        ("warning", "The cooler's warning colour")))
        self._brightness = self._choice_row(cl, "Screen brightness", "Of the cooler's screen.", "brightness",
                                            tuple((p, f"{p} %") for p in (100, 85, 70, 50, 30)))
        cards.addWidget(cooler)

        # the program that finds the stream behind a YouTube link, and a newer one when YouTube has changed
        links, ll = _card()
        ll.addWidget(_label("YOUTUBE LINKS", "section"))
        self._reader_title = _label("")
        ll.addWidget(self._reader_title)
        ll.addWidget(_label("YouTube changes how its videos are handed out now and then, and links stop playing. yt-dlp, "
                            "which deephack finds them with, follows within days: deephack can fetch the newest from PyPI "
                            "and use it instead of the one it came with.", "hint", wrap=True))
        self._reader_auto = self._switch_row(
            ll, "Keep it current", "Looks once a day and when a link will not open. Off: only when you press the button. "
            "It is code from the net that deephack then runs.", bool(self.config.get("ytdlp_current")),
            lambda on: self.config.set("ytdlp_current", on))
        self._reader_button = QPushButton("Update now")
        self._reader_button.clicked.connect(lambda _=False: self._check_reader())
        self._reader_result = _label("", "hint", wrap=True)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self._reader_button)
        row.addWidget(self._reader_result, 1)
        ll.addLayout(row)
        self.readerChecked.connect(self._on_reader)
        self._refresh_reader()
        cards.addWidget(links)

        # what the cooler's screen runs, and the way to the firmware that draws straight from USB
        self._fw_card, fl = _card()
        fw = self._fw_card
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
        self._fw_forget = QPushButton("Do not send it")      # the update that waits for a freshly started cooler
        self._fw_forget.clicked.connect(lambda: self.engine.forget_firmware())
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self._fw_install)
        buttons.addWidget(self._fw_restore)
        buttons.addWidget(self._fw_forget)
        buttons.addStretch(1)
        for widget in (self._fw_title, self._fw_bar, self._fw_hint, self._fw_result):
            fl.addWidget(widget)
        fl.addLayout(buttons)
        cards.addWidget(fw)

        about, bl = _card()
        bl.addWidget(_label("ABOUT", "section"))
        bl.addWidget(_label(f"deephack {__version__}"))
        bl.addWidget(_label("Free software under the MIT licence. Not affiliated with or endorsed by DeepCool.", "hint", wrap=True))
        links = _label(" &nbsp;·&nbsp; ".join(f'<a href="{url}" style="color: {style.LINK}; text-decoration: none;">{text}</a>'
                                              for text, url in LINKS))
        links.setTextFormat(Qt.TextFormat.RichText)
        links.setOpenExternalLinks(True)
        bl.addWidget(links)
        cards.addWidget(about)
        cards.addStretch(1)
        return page

    def open_settings(self, show: QWidget | None = None) -> None:
        """Open the Settings page, scrolled to one of its cards if one is given. The cooler keeps showing
        what it shows, and a preview that was running stops: nobody is looking at it."""
        if self._viewing:
            self._viewing = ""
            self.engine.set_preview(None)
        self._settings_button.setChecked(True)
        self._pages.setCurrentIndex(1)
        if show is None:
            self._settings_scroll.verticalScrollBar().setValue(0)
        else:                                    # once laid out
            QTimer.singleShot(0, lambda: self._settings_scroll.ensureWidgetVisible(show, 0, 14))

    # ------------------------------------------------------------ yt-dlp
    def _refresh_reader(self) -> None:
        version, fetched = ytdlp.version()
        self._reader_title.setText(f"yt-dlp {version}".rstrip() + (" · fetched by deephack" if fetched else " · the one deephack came with"))

    def _check_reader(self) -> None:
        """Look for a newer yt-dlp now. It asks the net: in a thread, and the answer comes as a signal."""
        self._reader_button.setEnabled(False)
        self._reader_result.setStyleSheet("")
        self._reader_result.setText("Asking PyPI…")
        threading.Thread(target=lambda: self.readerChecked.emit(*ytdlp.update()), daemon=True).start()

    def _on_reader(self, how: str, text: str) -> None:
        self._reader_button.setEnabled(True)
        self._reader_result.setStyleSheet(f"color: {style.ERR if how == 'failed' else style.OK};")
        self._reader_result.setText({"updated": f"Now using yt-dlp {text}.", "current": f"yt-dlp {text} is the newest."}.get(
            how, f"Could not: {text}"))
        self._refresh_reader()

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
        wanted = None if busy or not running else self._state.get("flash_wanted")
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
            elif wanted:
                hint += (" " + (f"Version {firmware.PATCH_VERSION}" if wanted == "direct" else "DeepCool's own firmware")
                         + " is waiting to be sent: deephack does that by itself when it next starts on a cooler that has "
                           "just been switched on.")
        self._fw_title.setText(title)
        self._fw_hint.setText(hint)
        self._fw_install.setVisible(install)
        self._fw_restore.setVisible(restore)
        self._fw_forget.setVisible(bool(wanted))
        newer = install and not busy and running["patch"] > 0      # the card is a page away now: a dot says so
        self._settings_button.set_mark(style.WARN if newer else None,
                                       f"Version {firmware.PATCH_VERSION} of the direct drawing firmware can be installed" if newer else "")

    def _refresh_needs(self) -> None:
        """The note on the page of a mode that needs the direct drawing firmware, when the cooler runs DeepCool's."""
        cls, running = BY_ID.get(self._viewing), self._state.get("firmware")
        lacking = bool(cls and cls.needs_direct and running and not running["patch"])
        if lacking:
            self._needs_text.setText(f"{cls.title} plays only with the direct drawing firmware. This cooler runs DeepCool's own: "
                                     "it shows one picture.")
        self._needs.setVisible(lacking)

    def _choice_row(self, parent: QVBoxLayout, title: str, hint: str, key: str, choices) -> ComboBox:
        row = QHBoxLayout()
        row.setSpacing(16)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(_label(title))
        col.addWidget(_label(hint, "hint", wrap=True))
        box = ComboBox()
        box.setFixedWidth(250)
        for value, text in choices:
            box.addItem(text, value)
        box.setCurrentIndex(max(0, box.findData(self.config.get(key))))
        box.currentIndexChanged.connect(lambda _i: self.config.set(key, box.currentData()))
        row.addLayout(col, 1)
        row.addWidget(box, 0, Qt.AlignmentFlag.AlignVCenter)
        parent.addLayout(row)
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
        self._pages.setCurrentIndex(0)
        self._mode_title.setText(cls.title)
        self._mode_desc.setText(cls.description + ".")
        self._build_settings(cls)
        live = mode_id == self._live
        self._preview.set_dimmed(live and not self.config.get("enabled"))
        self._live_dot.setVisible(live)
        self._live_label.setVisible(live)
        self._show_button.setVisible(not live)
        if changed:
            self.engine.set_preview(None if live else mode_id)
            if live and self._live_frames:
                self._show_frames(*self._live_frames)
        self._refresh_needs()
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
            if btn is not self._settings_button:
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
            elif s.kind == "capture":
                chooser = CaptureChooser(current)
                chooser.changed.connect(lambda value, key=s.key: self._set_setting(cls.id, key, value))
                lay.addWidget(chooser)
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
            return target == mode_id or (target in BY_ID and mode_id in BY_ID[target].reads)

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
        self._refresh_needs()

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
