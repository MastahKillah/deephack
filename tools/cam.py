"""Webcam pointed at the cooler: grab frames of the LCD so probing runs can be reviewed without a human
taking photos. Needs opencv-python-headless (not a dependency of the app)."""
from pathlib import Path

import cv2

OUT = Path(__file__).resolve().parent.parent / "captures" / "cam"
CROP = (0, 420, 325, 680)   # y0, y1, x0, x1 of the LCD in the 1280x720 frame; depends on where the camera sits


class Cam:
    def __init__(self, index=0, exposure=-6):
        self.cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
        self.cap.set(cv2.CAP_PROP_EXPOSURE, exposure)   # auto exposure blows the LCD out
        OUT.mkdir(parents=True, exist_ok=True)
        for _ in range(15):
            self.cap.read()

    def frame(self):
        for _ in range(4):          # drop buffered frames so we get the current picture
            ok, f = self.cap.read()
        y0, y1, x0, x1 = CROP
        return f[y0:y1, x0:x1] if ok else None

    def snap(self, name):
        f = self.frame()
        if f is not None:
            cv2.imwrite(str(OUT / f"{name}.jpg"), f, [cv2.IMWRITE_JPEG_QUALITY, 90])
        return f

    def close(self):
        self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
        self.cap.release()


def sheet(names, out, cols=6):
    """Contact sheet of saved snaps, each labelled with its name."""
    tiles = []
    for n in names:
        f = cv2.imread(str(OUT / f"{n}.jpg"))
        if f is None:
            continue
        cv2.putText(f, n, (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        tiles.append(f)
    if not tiles:
        return None
    blank = tiles[0] * 0
    rows = []
    for i in range(0, len(tiles), cols):
        row = tiles[i:i + cols]
        rows.append(cv2.hconcat(row + [blank] * (cols - len(row))))
    cv2.imwrite(str(OUT / out), cv2.vconcat(rows), [cv2.IMWRITE_JPEG_QUALITY, 88])
    return OUT / out


def diff(a, b):
    """Mean absolute difference between two frames (0 = identical)."""
    return float(cv2.absdiff(a, b).mean()) if a is not None and b is not None else -1.0
