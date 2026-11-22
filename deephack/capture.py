"""What is on the PC's screen, as pictures: a part of the screen, or one window (the Mirror mode).

Plain Windows calls through ctypes, nothing to install:

- **A part of the screen** is copied from the screen as Windows has put it together (`BitBlt`). Only the
  part is copied: 1 to 2 ms for 960 x 1280 on a 4K screen. (Pillow's `ImageGrab` copies the whole screen
  and cuts the part out afterwards: 67 ms there, whatever the part's size.)
- **A window** is asked to draw itself (`PrintWindow`, with the flag that makes programs which draw with
  the graphics card do so too: browsers, terminals, most of what is new). That works while another
  window covers it, and not while it is minimised: Windows does not draw a minimised window at all.

Sizes and places are real pixels of the screen. The app sees them (Qt makes the process aware of each
screen's scaling); a plain `python.exe` does not until it says so, see `aware()`.

Video that is protected against copying (Netflix and the like) comes out black, here as in every
screenshot. That is how it is meant to be.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass

from PIL import Image

SRCCOPY = 0x00CC0020
PW_CLIENTONLY, PW_RENDERFULLCONTENT = 1, 2
DWMWA_CLOAKED = 14
GWL_EXSTYLE, WS_EX_TOOLWINDOW = -20, 0x80
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
LIMIT = 8192                                     # pixels a side: nothing sane is larger

try:
    user32, gdi32, kernel32 = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32
except AttributeError:                           # not Windows: the module loads, nothing in it works
    user32 = gdi32 = kernel32 = None


class _Header(ctypes.Structure):                 # BITMAPINFOHEADER
    _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG), ("height", wintypes.LONG), ("planes", wintypes.WORD),
                ("bits", wintypes.WORD), ("compression", wintypes.DWORD), ("image", wintypes.DWORD),
                ("xppm", wintypes.LONG), ("yppm", wintypes.LONG), ("used", wintypes.DWORD), ("important", wintypes.DWORD)]


def _declare() -> None:
    handle = ctypes.c_void_p
    for function, result, arguments in (
        (user32.GetDC, handle, [handle]), (user32.ReleaseDC, ctypes.c_int, [handle, handle]),
        (gdi32.CreateCompatibleDC, handle, [handle]), (gdi32.DeleteDC, wintypes.BOOL, [handle]),
        (gdi32.CreateDIBSection, handle, [handle, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), handle, wintypes.DWORD]),
        (gdi32.SelectObject, handle, [handle, handle]), (gdi32.DeleteObject, wintypes.BOOL, [handle]),
        (gdi32.BitBlt, wintypes.BOOL, [handle, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, handle, ctypes.c_int, ctypes.c_int, wintypes.DWORD]),
        (gdi32.GdiFlush, wintypes.BOOL, []),
        (user32.PrintWindow, wintypes.BOOL, [handle, handle, wintypes.UINT]),
        (user32.IsWindow, wintypes.BOOL, [handle]), (user32.IsIconic, wintypes.BOOL, [handle]),
        (user32.IsWindowVisible, wintypes.BOOL, [handle]),
        (user32.GetClientRect, wintypes.BOOL, [handle, ctypes.POINTER(wintypes.RECT)]),
        (user32.GetWindowTextLengthW, ctypes.c_int, [handle]),
        (user32.GetWindowTextW, ctypes.c_int, [handle, wintypes.LPWSTR, ctypes.c_int]),
        (user32.GetClassNameW, ctypes.c_int, [handle, wintypes.LPWSTR, ctypes.c_int]),
        (user32.GetWindowLongW, wintypes.LONG, [handle, ctypes.c_int]),
        (user32.GetWindowThreadProcessId, wintypes.DWORD, [handle, ctypes.POINTER(wintypes.DWORD)]),
        (user32.GetSystemMetrics, ctypes.c_int, [ctypes.c_int]),
        (kernel32.OpenProcess, handle, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        (kernel32.CloseHandle, wintypes.BOOL, [handle]),
        (kernel32.QueryFullProcessImageNameW, wintypes.BOOL, [handle, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]),
    ):
        function.restype, function.argtypes = result, arguments


if user32 is not None:
    _declare()


def aware() -> None:
    """Make this process see real pixels on a scaled screen. The app is, through Qt; a script is not."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


def screen() -> tuple[int, int, int, int]:
    """All screens together: left, top, width, height."""
    return tuple(user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))


class Grabber:
    """Takes pictures of one size, into memory it keeps between them. Not for two threads at once."""

    def __init__(self):
        self._size = (0, 0)
        self._dc = self._bitmap = self._before = None
        self._bits = ctypes.c_void_p()
        self.raw: bytes | None = None            # the last picture as it was taken: to tell whether the next one differs

    def _room(self, width: int, height: int) -> None:
        if (width, height) == self._size and self._dc:
            return
        self.close()
        if not (0 < width <= LIMIT and 0 < height <= LIMIT):
            raise OSError(f"no picture of {width} x {height}")
        header = _Header(ctypes.sizeof(_Header), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)      # top line first
        shown = user32.GetDC(None)
        try:
            self._dc = gdi32.CreateCompatibleDC(shown)
            self._bitmap = gdi32.CreateDIBSection(shown, ctypes.byref(header), 0, ctypes.byref(self._bits), None, 0)
        finally:
            user32.ReleaseDC(None, shown)
        if not self._dc or not self._bitmap:
            self.close()
            raise OSError("Windows gave no memory for the picture")
        self._before = gdi32.SelectObject(self._dc, self._bitmap)
        self._size = (width, height)

    def _picture(self) -> Image.Image:
        gdi32.GdiFlush()
        width, height = self._size
        data = self.raw = ctypes.string_at(self._bits, width * height * 4)
        return Image.frombuffer("RGB", (width, height), data, "raw", "BGRX", 0, 1)

    def part(self, left: int, top: int, width: int, height: int) -> Image.Image:
        """That part of the screen, as it looks right now."""
        self._room(width, height)
        shown = user32.GetDC(None)
        try:
            if not gdi32.BitBlt(self._dc, 0, 0, width, height, shown, left, top, SRCCOPY):
                raise OSError("the screen could not be copied")
        finally:
            user32.ReleaseDC(None, shown)
        return self._picture()

    def window(self, hwnd: int) -> Image.Image | None:
        """What is inside that window (without its title bar and frame). None: it is gone, minimised,
        or has no size right now."""
        if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
            return None
        rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)) or rect.right <= 0 or rect.bottom <= 0:
            return None
        self._room(min(rect.right, LIMIT), min(rect.bottom, LIMIT))
        if not user32.PrintWindow(hwnd, self._dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT):
            return None
        return self._picture()

    def close(self) -> None:
        if self._dc:
            if self._before:
                gdi32.SelectObject(self._dc, self._before)
            gdi32.DeleteDC(self._dc)
        if self._bitmap:
            gdi32.DeleteObject(self._bitmap)
        self._dc = self._bitmap = self._before = None
        self._bits, self._size = ctypes.c_void_p(), (0, 0)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------- windows
@dataclass(frozen=True)
class Window:
    hwnd: int
    program: str                                 # "chrome.exe"
    kind: str                                    # the window's class: tells a program's windows apart
    title: str

    @property
    def label(self) -> str:
        name = self.program.rsplit(".", 1)[0]
        return f"{self.title} · {name}" if name and name.lower() not in self.title.lower() else self.title


def _text(function, hwnd, room: int) -> str:
    text = ctypes.create_unicode_buffer(room)
    function(hwnd, text, room)
    return text.value


def _program(hwnd: int) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not process:
        return ""
    try:
        path, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        return os.path.basename(path.value) if kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(process)


def _cloaked(hwnd: int) -> bool:
    """There but not shown: windows of other virtual desktops, and of apps Windows keeps asleep."""
    value = wintypes.DWORD()
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(hwnd), DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
    except (AttributeError, OSError):
        return False
    return bool(value.value)


def windows() -> list[Window]:
    """The windows somebody could want to see: those with a title that are on screen or minimised. The
    front one first."""
    found: list[Window] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, wintypes.LPARAM)
    def each(hwnd, _):
        if (user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd)
                and not user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW and not _cloaked(hwnd)):
            kind = _text(user32.GetClassNameW, hwnd, 256)
            if kind not in ("Progman", "WorkerW", "Shell_TrayWnd"):          # the desktop and the task bar
                title = _text(user32.GetWindowTextW, hwnd, user32.GetWindowTextLengthW(hwnd) + 1)
                found.append(Window(hwnd, _program(hwnd), kind, title))
        return True

    user32.EnumWindows(each, 0)
    return found


def find(program: str, kind: str, title: str, among: list[Window] | None = None) -> Window | None:
    """The window that was chosen, found again: after a restart of its program it is another window
    with another number. The same program and kind with the same title; failing that, with any title
    (a browser's title is its tab's, and changes all the time)."""
    among = windows() if among is None else among
    same = [w for w in among if w.program.lower() == program.lower() and w.kind == kind]
    return next((w for w in same if w.title == title), same[0] if same else None)


def minimised(hwnd: int) -> bool:
    return bool(user32.IsIconic(hwnd))


def alive(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))
