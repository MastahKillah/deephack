"""Choosing what the Mirror mode shows: a part of the screen, dragged out on it, or one of the open windows."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QLabel, QMenu, QPushButton, QVBoxLayout, QWidget

from .. import capture
from ..modes import mirror
from . import style

TITLE = 64                  # letters of a window's title in the list


def cursor() -> tuple[int, int]:
    """Where the mouse is, in real pixels of the screen (Qt's own places are scaled ones)."""
    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def dragged(start: QPoint, end: QPoint, upright: bool = False) -> QRect:
    """The rectangle between two points; `upright`: in the shape of the cooler's screen, 3 to 4 (or 4 to
    3 when it was dragged out wider than tall), as large as fits between them."""
    dx, dy = end.x() - start.x(), end.y() - start.y()
    if upright and dx and dy:
        ratio = 3 / 4 if abs(dy) >= abs(dx) else 4 / 3                   # width to height
        width = min(abs(dx), abs(dy) * ratio)
        dx, dy = round(width) * (1 if dx > 0 else -1), round(width / ratio) * (1 if dy > 0 else -1)
    return QRect(start.x() + min(0, dx), start.y() + min(0, dy), abs(dx), abs(dy))


def real(pressed: tuple[int, int], start: QPoint, rect: QRect, scale: float) -> tuple[int, int, int, int]:
    """A rectangle dragged out in a window's scaled pixels as real ones: the press was at `pressed` for
    real and at `start` for the window, and one of the window's pixels is `scale` real ones."""
    return (pressed[0] + round((rect.left() - start.x()) * scale), pressed[1] + round((rect.top() - start.y()) * scale),
            round(rect.width() * scale), round(rect.height() * scale))


class PartPicker(QWidget):
    """One screen, dimmed, to drag a rectangle out on. Esc or the right mouse button: not after all."""

    picked = Signal(int, int, int, int)          # left, top, width, height in real pixels
    cancelled = Signal()

    def __init__(self, screen):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(screen.geometry())
        self._scale = screen.devicePixelRatio()
        self._start: QPoint | None = None
        self._pressed = (0, 0)
        self._rect = QRect()

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(8, 9, 14, 120))
        if self._start is None:
            p.setPen(QColor(style.TEXT))
            font = p.font()
            font.setPointSizeF(13)
            p.setFont(font)
            p.drawText(self.rect().adjusted(0, 60, 0, 0), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                       "Drag over the part to show on the cooler\nShift keeps the cooler's shape  ·  Esc cancels")
            return
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        p.fillRect(self._rect, Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.setPen(QPen(QColor(style.ACCENT), 2))
        p.drawRect(self._rect)
        p.setPen(QColor(style.TEXT))
        size = f"{round(self._rect.width() * self._scale)} × {round(self._rect.height() * self._scale)}"
        below = self._rect.bottom() + 24 < self.height()
        p.drawText(self._rect.left() + 2, self._rect.bottom() + 18 if below else self._rect.top() - 8, size)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            self.cancelled.emit()
            return
        self._start, self._pressed, self._rect = event.position().toPoint(), cursor(), QRect()
        self.update()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._start is not None:
            upright = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._rect = dragged(self._start, event.position().toPoint(), upright)
            self.update()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._start is None or event.button() != Qt.MouseButton.LeftButton:
            return
        box = real(self._pressed, self._start, self._rect, self._scale)
        start, self._start = self._start, None
        if min(box[2], box[3]) < mirror.SMALLEST:            # a click, or a slip: go on
            self._rect = QRect()
            self.update()
            return
        self.picked.emit(*box)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()


class CaptureChooser(QWidget):
    """A "capture" setting: what is chosen, in words, and the two ways to choose."""

    changed = Signal(str)

    def __init__(self, value: str, parent=None):
        super().__init__(parent)
        self._value = value
        self._pickers: list[PartPicker] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._now = QLabel()
        self._now.setObjectName("chosen")
        self._now.setMinimumWidth(60)
        lay.addWidget(self._now)
        self._part = QPushButton("Choose a part of the screen…")       # one under the other: the column is narrow
        self._part.clicked.connect(lambda _=False: self.pick_part())
        self._window = QPushButton("Choose a window…")
        self._window.clicked.connect(lambda _=False: self._menu().exec(QCursor.pos()))
        lay.addWidget(self._part)
        lay.addWidget(self._window)
        self._show()

    def value(self) -> str:
        return self._value

    def _show(self) -> None:
        text = mirror.describe(self._value)
        self._now.setToolTip(text)
        self._now.setText(self._now.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, max(60, self._now.width() or 230)))

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._show()

    def _set(self, value: str) -> None:
        if value != self._value:
            self._value = value
            self._show()
            self.changed.emit(value)

    # ------------------------------------------------------------ a window
    def _menu(self) -> QMenu:
        menu = QMenu(self)
        found = capture.windows()
        for window in found:
            label = window.label if len(window.label) <= TITLE else window.label[:TITLE - 1] + "…"
            action = menu.addAction(label.replace("&", "&&"))
            action.triggered.connect(lambda _=False, w=window: self._set(mirror.window_value(w)))
        if not found:
            menu.addAction("No window is open").setEnabled(False)
        return menu

    # ------------------------------------------------------------ a part of the screen
    def pick_part(self) -> None:
        """Every screen dimmed, deephack's own window out of the way, until a part is dragged out or Esc."""
        if self._pickers:
            return
        own = self.window()
        own.hide()

        def done(box: tuple | None = None) -> None:
            pickers, self._pickers = self._pickers, []
            for picker in pickers:
                picker.close()
            own.show()
            own.raise_()
            own.activateWindow()
            if box is not None:
                self._set(mirror.part_value(*box))

        def open_pickers() -> None:                          # once the window is off the screen
            for screen in QGuiApplication.screens():
                picker = PartPicker(screen)
                picker.picked.connect(lambda *box: done(box))
                picker.cancelled.connect(lambda: done())
                picker.show()
                self._pickers.append(picker)
            if self._pickers:
                self._pickers[0].raise_()
                self._pickers[0].activateWindow()

        QTimer.singleShot(150, open_pickers)
