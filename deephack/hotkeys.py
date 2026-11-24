"""Keys that work whatever window is in front: Windows' RegisterHotKey, heard through Qt's event loop.

A mode's settings of kind "shortcut" name such keys ("Ctrl+Alt+PgDown", as Qt writes a key sequence),
each for one of the mode's actions. Of the modes' keys only those of the mode that is on the cooler are
registered, so the same key can be Next in every mode. The app has keys of its own, set on the Settings
page, that are registered whatever mode is on the cooler: the next and the previous mode, and drawing
off and on.

A key that another program has registered cannot be had: `Hotkeys.set` returns those, and the window
says so next to the setting. A key that two settings ask for goes to the first of them, the app's own
before the mode's, and the window says so next to the other (`same`, `MainWindow._claims`).
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
MODIFIERS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "meta": MOD_WIN}
KEYS = {   # Qt's names for keys (QKeySequence's portable text), and Windows' virtual key codes
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "pgup": 0x21, "pgdown": 0x22, "end": 0x23, "home": 0x24,
    "ins": 0x2D, "del": 0x2E, "space": 0x20, "tab": 0x09, "return": 0x0D, "enter": 0x0D, "esc": 0x1B, "backspace": 0x08,
    "pause": 0x13, "print": 0x2C, "scrolllock": 0x91, "capslock": 0x14, "numlock": 0x90,
    "media next": 0xB0, "media previous": 0xB1, "media stop": 0xB2, "media play": 0xB3, "toggle media play/pause": 0xB3,
    "+": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, ";": 0xBA, "=": 0xBB, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE, "`": 0xC0,
}


def parse(text: str) -> tuple[int, int] | None:
    """A key as Qt writes it ("Ctrl+Alt+PgDown") as Windows wants it: (modifiers, virtual key). None for
    nothing, for something Windows has no single key for, and for more than one key ("Ctrl+K, Ctrl+N")."""
    text = (text or "").strip()
    if not text or ", " in text:
        return None
    before, _, key = text.rpartition("+")
    if not key:                                  # the plus key itself: "Ctrl++"
        before, key = before.rstrip("+"), "+"
    modifiers = 0
    for name in before.split("+") if before else []:
        if name.strip().lower() not in MODIFIERS:
            return None
        modifiers |= MODIFIERS[name.strip().lower()]
    key = key.strip().lower()
    if len(key) == 1 and (key.isascii() and key.isalnum()):
        return modifiers, ord(key.upper())
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return modifiers, 0x70 + int(key[1:]) - 1
    if key in KEYS:
        return modifiers, KEYS[key]
    return None


def same(text: str):
    """What two settings have in common when they name the same key, however it is written."""
    return parse(text) or (text or "").strip().lower()


def _register(number: int, modifiers: int, key: int) -> bool:
    return bool(ctypes.windll.user32.RegisterHotKey(None, number, modifiers | MOD_NOREPEAT, key))


def _unregister(number: int) -> None:
    ctypes.windll.user32.UnregisterHotKey(None, number)


class Hotkeys(QAbstractNativeEventFilter):
    """The keys in force. Make it in the thread that runs Qt's event loop, and install it on the application
    (`app.installNativeEventFilter`): Windows posts a pressed key to the thread that registered it."""

    def __init__(self, register=_register, unregister=_unregister):
        super().__init__()
        self._register, self._unregister = register, unregister
        self._calls: dict[int, object] = {}          # number of a registered key -> what it does
        self._texts: dict[str, int] = {}
        self._next = 1

    def set(self, keys: dict) -> set[str]:
        """Have exactly these keys: {"Ctrl+Alt+PgDown": what to call}. Returns the ones that could not be
        had: not a key Windows knows, or taken by another program."""
        self.clear()
        refused = set()
        for text, call in keys.items():
            parsed = parse(text)
            number, self._next = self._next, self._next % 0xBFFF + 1
            if parsed is None or not self._register(number, *parsed):
                refused.add(text)
                continue
            self._calls[number], self._texts[text] = call, number
        return refused

    def clear(self) -> None:
        for number in self._calls:
            self._unregister(number)
        self._calls, self._texts = {}, {}

    def pressed(self, number: int) -> bool:
        call = self._calls.get(number)
        if call is None:
            return False
        call()
        return True

    def nativeEventFilter(self, kind, message):          # noqa: N802
        if bytes(kind) == b"windows_generic_MSG":             # (a QByteArray: it is not equal to bytes as it is)
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and self.pressed(msg.wParam):
                return True, 0
        return False, 0
