"""Webcam pointed at the cooler: grab frames of the LCD so probing runs can be reviewed without a human
taking photos. Needs opencv-python-headless (not a dependency of the app)."""
import atexit
from pathlib import Path

import cv2
import numpy as np

OUT = Path(__file__).resolve().parent.parent / "captures" / "cam"
CROP = (118, 540, 355, 680)   # y0, y1, x0, x1 of the LCD in the 1280x960 frame; depends on where the camera sits


class Cam:
    """The camera's whole sensor is 4:3 (1280x960 is the most it gives): at 1280x720 it cuts the top of the
    LCD off. Its focus is fixed and the LCD is only some 280 pixels wide in the frame, so text stays soft.
    Exposure -6 suits the LCD in a dark room; anything dimmer (the side light, the case) needs -4 or so,
    which blows the LCD out."""

    def __init__(self, index=0, exposure=-6):
        self.index = index
        atexit.register(self.close)             # the exposure set here stays in the camera's driver: see close
        self.cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 960)
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

    def photo(self, name, frames=16, scale=2):
        """A picture of a screen that holds still, as good as this camera gives: the mean of several frames
        (less noise), enlarged and sharpened a little. Saved as PNG; returns its path."""
        self.frame()
        y0, y1, x0, x1 = CROP
        got = [f[y0:y1, x0:x1].astype(np.float32) for ok, f in (self.cap.read() for _ in range(frames)) if ok]
        if not got:
            return None
        mean = cv2.resize(sum(got) / len(got), None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
        sharp = cv2.addWeighted(mean, 1.6, cv2.GaussianBlur(mean, (0, 0), 2.0 * scale), -0.6, 0)
        path = OUT / f"{name}.png"
        cv2.imwrite(str(path), np.clip(sharp, 0, 255).astype(np.uint8))
        return path

    def close(self):
        """Hand the exposure back to the camera and let go of it. Also done when the program ends, and also
        when the capture was released by hand: a fixed exposure for a dark PC case stays in the driver, and
        the next program that uses the camera in a lit room gets a white picture (26 Nov 2026: half of it
        blown out, until this was run). Not done by a program that ends with os._exit."""
        if self.cap is None:
            return
        if not self.cap.isOpened():
            self.cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
        self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
        self.cap.release()
        self.cap = None


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
