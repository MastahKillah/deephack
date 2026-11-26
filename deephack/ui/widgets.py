"""Small custom widgets: toggle switch, LCD preview, status dot, mode button, app icon."""
from __future__ import annotations

import time

from PIL import Image
from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from .. import device, light
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


def _picture(image: QImage) -> Image.Image:
    """A frame of the preview as the picture it was made from, for deephack.light."""
    if image.format() != QImage.Format.Format_RGB888:
        image = image.convertToFormat(QImage.Format.Format_RGB888)
    return Image.frombuffer("RGB", (image.width(), image.height()), bytes(image.constBits()), "raw", "RGB", image.bytesPerLine())


class Preview(QWidget):
    """The cooler's LCD: a 3:4 screen in a dark bezel, and beside it the glow of the pump head's lights,
    seven along each long side, as the cooler shows them for this picture (set_lights)."""

    SCREEN = QSize(354, 466)        # the bezel
    GLOW = 40                       # pixels of glow to either side of it
    UNDER = 16                      # ... and how far the glow reaches in under the bezel's round corners
    QUICK = 0.2                     # frames that come closer together than this fade as a video's do

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image: QImage | None = None
        self._frames: list[QImage] = []
        self._index = 0
        self._dimmed = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)
        self._how: str | None = None                # who picks the lights' colours: "deephack", "cooler", None: no lights
        self._level = 1.0
        self._own: list | None = None               # what the mode has them show, where deephack would pick
        self._picked: dict[int, list] = {}          # frame index -> its 14 colours
        self._goal: list | None = None              # where the lights go, and where they are: 14 x [r, g, b]
        self._lights: list | None = None
        self._pace = 0.0                            # the share of what is left that a step covers
        self._last = 0.0                            # when the last frames came
        self._fade = QTimer(self)
        self._fade.setInterval(round(device.LIGHT_STEP * 1000))
        self._fade.timeout.connect(self._step)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.SCREEN.width() + 2 * self.GLOW, self.SCREEN.height())

    def set_frames(self, frames: list[QImage], duration_ms: int = 0, dimmed: bool = False) -> None:
        """Show a still (one frame) or loop an animation that lasts duration_ms."""
        now = time.monotonic()
        quick, self._last = len(frames) > 1 or now - self._last < self.QUICK, now
        self._frames, self._index, self._dimmed = frames, 0, dimmed
        self._image = frames[0] if frames else None
        self._picked = {}
        self._timer.stop()
        if len(frames) > 1 and duration_ms > 0:
            self._timer.start(max(20, duration_ms // len(frames)))
        self._light(quick)
        self.update()

    def _advance(self) -> None:
        self._index = (self._index + 1) % len(self._frames)
        self._image = self._frames[self._index]
        self._light(True)
        self.update()

    def set_dimmed(self, dimmed: bool) -> None:
        self._dimmed = dimmed
        self._light(False)
        self.update()

    # ---- the lights
    def set_lights(self, how: str | None, level: float = 1.0, own: list | None = None) -> None:
        """Who picks the colours of the lights beside the screen: "deephack" (deephack.light, and the cooler
        fades to them: the direct drawing firmware from 16 on), "cooler" (DeepCool's firmware: one pixel a
        light, and no fade), or None for no lights (they follow something else than the screen).
        `level`: deephack's, that much dimmer. `own`: what the mode has them show in place of what deephack
        would pick (Mode.lights), 14 colours."""
        if (how, level, own) != (self._how, self._level, self._own):
            quick = own is not None and (how, level) == (self._how, self._level)
            self._how, self._level, self._own, self._picked = how, level, own, {}
            self._light(quick)
            self.update()

    def lights(self) -> list | None:
        """The 14 colours where the lights are going, 0 to 255, in the cooler's order; None: no lights."""
        return None if self._goal is None else [tuple(colour) for colour in self._goal]

    def _light(self, quick: bool) -> None:
        """The current frame has changed, or what is asked of the lights: where they go now."""
        if self._how is None or self._image is None or self._dimmed:
            self._goal = self._lights = None
            self._fade.stop()
            return
        if self._how == "deephack" and self._own is not None:
            self._goal = [[round(c * self._level) for c in colour] for colour in self._own]
        else:
            if self._index not in self._picked:
                picture = _picture(self._image)
                self._picked[self._index] = (light.colours(picture, self._level) if self._how == "deephack"
                                             else light.cooler_colours(picture))
            self._goal = [list(colour) for colour in self._picked[self._index]]
        seconds = 0.0 if self._how == "cooler" else 0.12 if quick else 0.4       # the engine's LIGHT_FADE_FAST, LIGHT_FADE
        self._pace = device.light_pace(seconds) / 256
        if self._lights is None or not self._pace:
            self._lights = [list(map(float, colour)) for colour in self._goal]
        if self._lights != self._goal and not self._fade.isActive():
            self._fade.start()

    def _step(self) -> None:
        """The lights a step on, as the cooler moves them: a share of what is left, and the last bit at once."""
        moving = False
        for now, goal in zip(self._lights or (), self._goal or ()):
            for i in range(3):
                left = goal[i] - now[i]
                now[i] = float(goal[i]) if abs(left) * self._pace < 0.5 else now[i] + left * self._pace
                moving = moving or now[i] != goal[i]
        if not moving:
            self._fade.stop()
        self.update()

    def _glow(self, p: QPainter, bezel: QRectF) -> None:
        """The light the lights throw to either side of the pump head: brightest at its edge. An LED is
        linear and a monitor is not, so a light at a tenth of its power is drawn at a third."""
        width, height = self.GLOW + self.UNDER, round(bezel.height())
        for side, lights in (("left", self._lights[6::-1]), ("right", self._lights[7:])):          # top to bottom
            tile = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
            tile.fill(Qt.GlobalColor.transparent)
            down = QLinearGradient(0, 0, 0, height)
            for k, colour in enumerate(lights):
                shown = QColor(*(round(255 * (max(0.0, c) / 255) ** (1 / light.GAMMA)) for c in colour))
                down.setColorAt((k + 0.5) / len(lights), shown)
            t = QPainter(tile)
            t.fillRect(tile.rect(), down)
            t.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            out = QLinearGradient(0, 0, width, 0)
            edge = self.GLOW / width                    # where the bezel's edge is, from the tile's outer side
            for at, alpha in ((0.0, 0), (edge * 0.35, 30), (edge * 0.7, 110), (edge, 235), (1.0, 235)):
                out.setColorAt(at if side == "left" else 1 - at, QColor(0, 0, 0, alpha))
            t.fillRect(tile.rect(), out)
            ends = QLinearGradient(0, 0, 0, height)     # ... and it runs out towards the top and the bottom
            for at, alpha in ((0.0, 0), (0.07, 255), (0.93, 255), (1.0, 0)):
                ends.setColorAt(at, QColor(0, 0, 0, alpha))
            t.fillRect(tile.rect(), ends)
            t.end()
            x = bezel.left() - self.GLOW if side == "left" else bezel.right() - self.UNDER
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            p.drawImage(QRectF(x, bezel.top(), width, height), tile)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        bezel = QRectF(self.rect()).adjusted(self.GLOW + 1, 1, -self.GLOW - 1, -1)
        if self._lights is not None:
            self._glow(p, bezel)
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
    """A mode in the sidebar. Checked: its page is open. The green dot: it is the one on the cooler.
    Without a description it is one line high (Settings)."""

    doubleClicked = Signal()

    def __init__(self, glyph: str, title: str, description: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("modeButton")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(60 if description else 42)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(12)
        g = QLabel(glyph, objectName="modeGlyph")
        g.setFixedWidth(26)
        g.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col = QVBoxLayout()
        col.setSpacing(1)
        t = QLabel(title, objectName="modeTitle")
        self._live = StatusDot()
        self._live.setFixedSize(7, 7)
        self._live.set_color(style.OK)
        self._live.setVisible(False)
        head = QHBoxLayout()
        head.setSpacing(7)
        head.addWidget(t)
        head.addWidget(self._live, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        col.addLayout(head)
        inside = [g, t, self._live]
        if description:
            d = QLabel(description, objectName="modeDesc")
            d.setWordWrap(True)
            col.addWidget(d)
            inside.append(d)
        row.addWidget(g)
        row.addLayout(col, 1)
        for w in inside:
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_mark(self, color: str | None, tip: str = "") -> None:
        """The dot next to the title, in a colour, or none; and what the button says when pointed at."""
        if color:
            self._live.set_color(color)
        self._live.setVisible(bool(color))
        self.setToolTip(tip)

    def set_live(self, on: bool) -> None:
        self.set_mark(style.OK if on else None, "On the cooler" if on else "Double-click to show it on the cooler")

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        super().mouseDoubleClickEvent(event)
        self.doubleClicked.emit()


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
