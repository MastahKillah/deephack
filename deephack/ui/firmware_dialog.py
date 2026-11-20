"""The question before a firmware update: what it does, what can go wrong, and a tick that it was read."""
from __future__ import annotations

from PySide6.QtCore import QDir, Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .. import firmware

WHAT = {
    "direct": ("Install the direct drawing firmware",
               "This replaces the firmware of the cooler's screen with DeepCool's version 2.70 plus a small patch. "
               "With it deephack draws its pictures straight from USB: nothing is written to the cooler's flash "
               "any more, and pictures can move. Everything else the firmware does stays as it is."),
    "stock": ("Go back to DeepCool's firmware",
              "This replaces the firmware of the cooler's screen with DeepCool's own version 2.70, as the DeepCool "
              "app ships it. deephack then stores every picture on the cooler's flash again before it shows it."),
}
RISK = ("<b>It can go wrong for good.</b> The cooler writes the new firmware itself after a restart, and nothing "
        "can be done from the PC while it does. If the new firmware does not start, the screen stays dark and "
        "there is no way to bring it back over USB. It has worked every time on the one cooler it was tried on, "
        "four times in all; nobody knows how it goes on another.")
NO_WAY_BACK = ("<b>Your cooler runs firmware {version}, and this is 2.70.</b> There is no file of {version} anywhere, "
               "and no way to read it out of the cooler: once this is done you can never go back to {version}.")
MEANWHILE = ("It takes about 20 seconds, and the screen is dark for most of them. Until deephack says it is "
             "done: do not unplug the cooler, and do not shut the PC down or let it sleep.")


class FlashDialog(QDialog):
    """Asks before `which` ("direct" or "stock") is flashed. Accepted: `stock_path` is the file to use (None: the
    one deephack found by itself)."""

    def __init__(self, which: str, bcd: int, parent=None):
        super().__init__(parent)
        title, what = WHAT[which]
        self.stock_path: str | None = None
        self.setWindowTitle(title)
        self.setModal(True)
        self.setFixedWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(12)

        def text(html: str, name: str = "") -> QLabel:
            lab = QLabel(html, objectName=name)
            lab.setWordWrap(True)
            lab.setTextFormat(Qt.TextFormat.RichText)
            lay.addWidget(lab)
            return lab

        text(title, "h1")
        text(what)
        text(RISK, "risk")
        if firmware.version(bcd) != "2.70":
            text(NO_WAY_BACK.format(version=firmware.version(bcd)), "risk")
        text(MEANWHILE)

        row = QHBoxLayout()
        self._file = QLabel("", objectName="hint")
        self._file.setWordWrap(True)
        choose = QPushButton("Choose file…")
        choose.clicked.connect(self._choose)
        row.addWidget(self._file, 1)
        row.addWidget(choose, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(row)

        self._understood = QCheckBox("I have read this, and I accept that the cooler's screen may not survive it")
        self._understood.toggled.connect(self._refresh)
        lay.addSpacing(4)
        lay.addWidget(self._understood)

        buttons = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        cancel.setDefault(True)
        self._go = QPushButton("Flash the cooler", objectName="danger")
        self._go.clicked.connect(self.accept)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self._go)
        lay.addLayout(buttons)
        self._found = firmware.have_stock()
        self._refresh()

    def _choose(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, f"DeepCool's firmware file ({firmware.STOCK_NAME})", "",
                                              "Firmware (*.bin);;All files (*)")
        if not path:
            return
        try:
            firmware.read_stock(path)
        except ValueError as e:
            self.stock_path, self._found = None, firmware.have_stock()
            self._refresh(str(e))
            return
        self.stock_path, self._found = QDir.toNativeSeparators(path), True
        self._refresh()

    def _refresh(self, problem: str = "") -> None:
        if not isinstance(problem, str):               # the tick's signal passes its state
            problem = ""
        if problem:
            self._file.setText(problem)
        elif self._found:
            self._file.setText(f"DeepCool's firmware file ({firmware.STOCK_NAME}): found, and it is the right one.")
        else:
            self._file.setText(f"DeepCool's firmware file ({firmware.STOCK_NAME}) was not found. It comes with the DeepCool "
                               "app: install that, or choose the file.")
        self._go.setEnabled(self._found and self._understood.isChecked())
