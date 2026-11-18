from __future__ import annotations

import argparse
import ctypes
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import APP_NAME, __version__
from .config import Config, config_dir
from .engine import Engine
from .modes import MODES
from .ui import style
from .ui.main_window import MainWindow
from .ui.widgets import app_icon, write_arrow


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=APP_NAME, description="Custom content for the DeepCool MYSTIQUE cooler LCD")
    ap.add_argument("--minimized", action="store_true", help="start hidden in the system tray")
    ap.add_argument("--screenshot", metavar="PNG", help="(development) save a screenshot of the window after a few seconds and exit")
    ap.add_argument("--no-device", action="store_true", help="(development) preview only, never touch the cooler")
    ap.add_argument("--config", metavar="JSON", help="(development) use another settings file")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    try:  # own taskbar identity instead of python.exe's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.app")
    except Exception:
        pass

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    arrow = config_dir() / "arrow.png"
    write_arrow(str(arrow))
    app.setStyleSheet(style.QSS.replace("@ARROW@", arrow.as_posix()))
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
        act = menu.addAction(cls.title, lambda mode_id=cls.id: window._select_mode(mode_id))
        act.setCheckable(True)
        act.setData(cls.id)
        mode_actions.append(act)
    menu.addSeparator()
    pause = menu.addAction("Active")
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

    engine.start()
    if not args.minimized:
        show_window()

    if args.screenshot:
        def grab() -> None:
            window.grab().save(args.screenshot)
            quit_app()
        QTimer.singleShot(7000, grab)

    def shutdown() -> None:
        engine.stop()
        engine.wait(8000)
        tray.hide()

    app.aboutToQuit.connect(shutdown)
    return app.exec()
