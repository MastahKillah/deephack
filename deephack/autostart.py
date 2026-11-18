"""Start-with-Windows toggle (HKCU Run key, no admin rights needed)."""
from __future__ import annotations

import sys
import winreg
from pathlib import Path

from . import APP_NAME

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _command() -> str:
    scripts = Path(sys.executable).parent
    launcher = scripts / f"{APP_NAME}.exe"          # created by `pip install` (gui-script entry point)
    if launcher.exists():
        return f'"{launcher}" --minimized'
    pythonw = scripts / "pythonw.exe"
    return f'"{pythonw if pythonw.exists() else sys.executable}" -m {APP_NAME} --minimized'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except OSError:
                pass
