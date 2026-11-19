"""Put the pictures and animations uploaded with the official DeepCool app back on the cooler, in their
original order, after a wipe (commands 13 and 14 both erase everything stored). The app keeps the
originals in %APPDATA%/DeepCool/Pictures/<usb serial>/{jpg,gif}/<file id>."""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image  # noqa: E402

from deephack import device  # noqa: E402

base = Path(os.environ["APPDATA"]) / "DeepCool" / "Pictures"
by_age = lambda kind: sorted((p for p in base.glob(f"*/{kind}/*") if p.is_dir()), key=lambda p: p.stat().st_ctime)  # noqa: E731

with device.Mystique() as m:
    m.set_mode(device.MODE_MOTION)
    for d in by_age("jpg"):
        m.upload_jpeg(d.name, device.encode_jpeg(Image.open(d / "cropped.jpg")))
        time.sleep(1.5)
        print("jpg", d.name, flush=True)
    for d in by_age("gif"):
        frames = [f.read_bytes() for f in sorted(d.glob("frame_*.jpg"))]
        m.upload_gif(d.name, frames, json.loads((d / "metadata.json").read_text())["duration"])
        time.sleep(1 + len(frames) * 0.1)      # the device is still writing to flash
        print("gif", d.name, len(frames), "frames", flush=True)
    m.show_slot(0, device.TYPE_JPG)
