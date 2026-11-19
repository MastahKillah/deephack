"""Small custom widgets: toggle switch, LCD preview, status dot, mode button, app icon."""
from __future__ import annotations

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from ..modes.base import parse_order
from . import style


class Switch(QAbstractButton):
    """iOS-style toggle."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(42, 24)
        self._pos = 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(130)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.toggled.connect(self._animate)

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def setChecked(self, on: bool) -> None:  # noqa: N802 - Qt naming
        super().setChecked(on)
        self._anim.stop()
        self._pos = 1.0 if on else 0.0
        self.update()

    def _get(self) -> float:
        return self._pos

    def _set(self, v: float) -> None:
        self._pos = v
        self.update()

    knob = Property(float, _get, _set)

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        off, on = QColor("#3a3d50"), QColor(style.ACCENT)
        mix = QColor(*(round(off.getRgb()[i] + (on.getRgb()[i] - off.getRgb()[i]) * self._pos) for i in range(3)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(mix)
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 12, 12)
        p.setBrush(QColor("white"))
        d = self.height() - 6
        p.drawEllipse(QRectF(3 + (self.width() - d - 6) * self._pos, 3, d, d))


class Preview(QWidget):
    """The cooler's LCD: a 3:4 screen in a dark bezel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image: QImage | None = None
        self._frames: list[QImage] = []
        self._index = 0
        self._dimmed = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(354, 466)

    def set_frames(self, frames: list[QImage], duration_ms: int = 0, dimmed: bool = False) -> None:
        """Show a still (one frame) or loop an animation that lasts duration_ms."""
        self._frames, self._index, self._dimmed = frames, 0, dimmed
        self._image = frames[0] if frames else None
        self._timer.stop()
        if len(frames) > 1 and duration_ms > 0:
            self._timer.start(max(20, duration_ms // len(frames)))
        self.update()

    def _advance(self) -> None:
        self._index = (self._index + 1) % len(self._frames)
        self._image = self._frames[self._index]
        self.update()

    def set_dimmed(self, dimmed: bool) -> None:
        self._dimmed = dimmed
        self.update()

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        bezel = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(QPen(QColor("#34374a"), 1.5))
        p.setBrush(QColor("#07080c"))
        p.drawRoundedRect(bezel, 26, 26)
        screen = bezel.adjusted(11, 11, -11, -11)
        path = QPainterPath()
        path.addRoundedRect(screen, 16, 16)
        p.setClipPath(path)
        if self._image is not None:
            p.drawImage(screen, self._image)
        else:
            p.fillRect(screen, QColor("#101118"))
        if self._dimmed:
            p.fillRect(screen, QColor(8, 9, 14, 170))


class ComboBox(QComboBox):
    """Combo box that ignores the mouse wheel unless it has focus, so scrolling the settings past it does
    not change the value."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):  # noqa: N802
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()            # let the scroll area have it


class OrderList(QListWidget):
    """Priority list for an "order" setting: drag rows to reorder, tick to include."""

    changed = Signal(str)

    def __init__(self, choices: tuple[tuple[str, str], ...], value: str, parent=None):
        super().__init__(parent)
        self.setObjectName("orderList")
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        labels = dict(choices)
        for mode_id, on in parse_order(value, list(labels)):
            item = QListWidgetItem(labels[mode_id])
            item.setData(Qt.ItemDataRole.UserRole, mode_id)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
                          | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            self.addItem(item)
        self._last = self.value()
        # a drop arrives as several model changes; report once, when it has settled
        self._debounce = QTimer(self, singleShot=True, interval=0)
        self._debounce.timeout.connect(self._report)
        for sig in (self.model().rowsMoved, self.model().rowsInserted, self.model().rowsRemoved, self.itemChanged):
            sig.connect(lambda *_: self._debounce.start())

    def showEvent(self, event):  # noqa: N802 - fit the height to the rows once the style sheet has been applied
        super().showEvent(event)
        rows = sum(self.sizeHintForRow(i) for i in range(self.count()))
        self.setFixedHeight(rows + 2 * self.frameWidth() + 10)

    def value(self) -> str:
        items = (self.item(i) for i in range(self.count()))
        return ",".join(("" if it.checkState() == Qt.CheckState.Checked else "-") + it.data(Qt.ItemDataRole.UserRole) for it in items)

    def _report(self) -> None:
        value = self.value()
        if value != self._last:
            self._last = value
            self.changed.emit(value)


class StatusDot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self._color = QColor(style.MUTED)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(self.rect())


class ModeButton(QPushButton):
    def __init__(self, glyph: str, title: str, description: str, parent=None):
        super().__init__(parent)
        self.setObjectName("modeButton")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(60)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 10, 8)
        row.setSpacing(12)
        g = QLabel(glyph, objectName="modeGlyph")
        g.setFixedWidth(26)
        g.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col = QVBoxLayout()
        col.setSpacing(1)
        t = QLabel(title, objectName="modeTitle")
        d = QLabel(description, objectName="modeDesc")
        d.setWordWrap(True)
        col.addWidget(t)
        col.addWidget(d)
        row.addWidget(g)
        row.addLayout(col, 1)
        for w in (g, t, d):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


def app_icon() -> QIcon:
    """Generated at runtime so the repo needs no binary assets: a little screen on an accent tile."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        grad = QLinearGradient(0, 0, size, size)
        grad.setColorAt(0, QColor("#8f7bff"))
        grad.setColorAt(1, QColor("#4b3fd6"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.24, size * 0.24)
        # portrait screen
        w, h = size * 0.42, size * 0.56
        screen = QRectF((size - w) / 2, (size - h) / 2, w, h)
        p.setBrush(QColor(255, 255, 255, 235))
        p.drawRoundedRect(screen, size * 0.07, size * 0.07)
        p.setBrush(QColor("#4b3fd6"))
        r = size * 0.085
        p.drawEllipse(screen.center(), r, r)
        p.end()
        icon.addPixmap(pm)
    return icon


def write_check(path: str) -> None:
    """Tick for checkboxes (style sheets can only reference image files)."""
    pm = QPixmap(24, 24)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("white"), 3.2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    tick = QPainterPath()
    tick.moveTo(5.5, 12.5)
    tick.lineTo(10, 17)
    tick.lineTo(18.5, 7.5)
    p.drawPath(tick)
    p.end()
    pm.save(path)


def write_arrow(path: str) -> None:
    """Chevron for combo boxes (style sheets can only reference image files)."""
    pm = QPixmap(24, 24)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(style.MUTED), 2.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    path_ = QPainterPath()
    path_.moveTo(5, 9)
    path_.lineTo(12, 16)
    path_.lineTo(19, 9)
    p.drawPath(path_)
    p.end()
    pm.save(path)
