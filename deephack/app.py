from __future__ import annotations

import argparse
import ctypes
import json
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import APP_NAME, __version__, update
from .config import Config, config_dir
from .engine import Engine
from .modes import MODES
from .ui import style
from .ui.main_window import MainWindow
from .ui.widgets import app_icon, write_arrow, write_check


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=APP_NAME, description="Custom content for the DeepCool MYSTIQUE cooler LCD")
    ap.add_argument("--minimized", action="store_true", help="start hidden in the system tray")
    ap.add_argument("--screenshot", metavar="PNG", help="(development) save a screenshot of the window after a few seconds and exit")
    ap.add_argument("--screenshot-after", metavar="SECONDS", type=float, default=7.0, help="(development) ... after this many seconds")
    ap.add_argument("--no-device", action="store_true", help="(development) preview only, never touch the cooler")
    ap.add_argument("--settings", action="store_true", help="(development) open the Settings page")
    ap.add_argument("--config", metavar="JSON", help="(development) use another settings file")
    ap.add_argument("--update-now", action="store_true", help="(development) look for a newer deephack at the start "
                    "and install it without asking")
    # how an older deephack starts a newer one that is to take its place: see update.py, and keep it as it is
    ap.add_argument("--apply-update", metavar="FOLDER", help=argparse.SUPPRESS)
    ap.add_argument("--wait-pid", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--pass-on", default="[]", help=argparse.SUPPRESS)
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    if args.apply_update:
        return update.apply(Path(args.apply_update), args.wait_pid, json.loads(args.pass_on))

    try:  # own taskbar identity instead of python.exe's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.app")
    except Exception:
        pass

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    arrow, check = config_dir() / "arrow.png", config_dir() / "check.png"
    write_arrow(str(arrow))
    write_check(str(check))
    app.setStyleSheet(style.QSS.replace("@ARROW@", arrow.as_posix()).replace("@CHECK@", check.as_posix()))
    icon = app_icon()
    app.setWindowIcon(icon)

    lock = QLockFile(str(config_dir() / "app.lock"))
    lock.setStaleLockTime(0)
    if not args.no_device and not lock.tryLock(100):
        QMessageBox.information(None, APP_NAME, "deephack is already running. Look for its icon in the system tray.")
        return 0

    config = Config(Path(args.config) if args.config else None)
    engine = Engine(config, use_device=not args.no_device)
    window = MainWindow(config, engine)
    window.pass_on = (["--no-device"] if args.no_device else []) + (["--config", args.config] if args.config else [])
    app.installNativeEventFilter(window.hotkeys)     # keys that work in any window (hotkeys.py)

    def show_window() -> None:
        window.show()
        window.raise_()
        window.activateWindow()

    def quit_app() -> None:
        window.quitting = True
        app.quit()

    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("deephack")
    menu = QMenu()
    menu.addAction("Open deephack", show_window)
    menu.addSeparator()
    mode_actions: list[QAction] = []
    for cls in MODES:
        act = menu.addAction(cls.title, lambda mode_id=cls.id: window.activate_mode(mode_id))
        act.setCheckable(True)
        act.setData(cls.id)
        mode_actions.append(act)
    menu.addSeparator()
    pause = menu.addAction("Draw on the cooler")
    pause.setCheckable(True)
    pause.triggered.connect(window.set_enabled)
    menu.addSeparator()
    menu.addAction("Quit", quit_app)

    def sync_menu() -> None:
        for act in mode_actions:
            act.setChecked(act.data() == config.get("mode"))
        pause.setChecked(bool(config.get("enabled")))

    menu.aboutToShow.connect(sync_menu)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: show_window() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()

    said = {"stage": None}

    def say_flash(state: dict) -> None:
        """A firmware update that deephack sends by itself (one that waited for a freshly started cooler:
        engine.py, _send_wanted) is said in the tray: nobody asked for it just now, and the window may be shut."""
        flash = state.get("flash") or {}
        stage = flash.get("stage") if state.get("flash_remembered") else None
        if stage == said["stage"] or stage not in ("sending", "done", "failed"):
            return
        said["stage"] = stage
        tray.showMessage("deephack", "Sending the firmware update that was waiting for the cooler. Do not shut the PC down "
                         "or let it sleep for a minute." if stage == "sending" else flash.get("message", ""))

    engine.stateChanged.connect(say_flash)

    # what Settings has news about, said once in the tray; a click on it leads there
    about = {"what": ""}

    def notice(what: str, text: str) -> None:
        about["what"] = what
        tray.showMessage("deephack", text)

    def notice_clicked() -> None:
        what, about["what"] = about["what"], ""
        if what:
            show_window()
            window.show_notice(what)

    window.noticed.connect(notice)
    tray.messageClicked.connect(notice_clicked)

    def after_update() -> None:
        """How an update of the app went, if this start is the end of one, and away with what it left behind."""
        went = update.outcome()
        if went:
            tray.showMessage("deephack", f"This is deephack {went[1]} now." if went[0] == "updated" else
                             f"The update did not work: {went[1]}. This is deephack {__version__} still.")
        threading.Thread(target=update.tidy, daemon=True).start()

    def look() -> None:
        if config.get("update_check") and update.due():
            window.check_update(quiet=True)

    update.arrived()
    QTimer.singleShot(6000, after_update)
    if args.update_now:
        QTimer.singleShot(3000, lambda: window.check_update(quiet=True, install=True))
    elif not args.screenshot:                        # (a screenshot is a trial run: it asks nobody anything)
        QTimer.singleShot(15000, look)
        hourly = QTimer(app)
        hourly.timeout.connect(look)
        hourly.start(3600 * 1000)
    engine.start()
    if args.settings:
        window.open_settings()
    if not args.minimized:
        show_window()

    if args.screenshot:
        def grab() -> None:
            window.grab().save(args.screenshot)
            quit_app()
        QTimer.singleShot(round(args.screenshot_after * 1000), grab)

    def shutdown() -> None:
        window.hotkeys.clear()
        engine.stop()
        engine.wait(8000)
        tray.hide()

    app.aboutToQuit.connect(shutdown)
    return app.exec()
