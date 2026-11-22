# What deephack is built on

deephack's own code is under the MIT licence (`LICENSE`). The repository holds nothing but that code.

The **standalone app** (the zip of a release) also carries the programs below, each under its own
licence, unchanged and as its authors publish it on PyPI. They are separate files in the app's
`_internal` folder and can be replaced there by other builds of the same version. To build the app
yourself from source, see "Releasing" in the README.

| Part | Licence | Where it comes from |
|---|---|---|
| Python 3.12 | PSF-2.0 | https://www.python.org |
| OpenSSL, SQLite, libffi, Microsoft's Visual C++ runtime, which come with Python | Apache-2.0, public domain, MIT, Microsoft's terms for passing it on | https://www.openssl.org, https://sqlite.org, https://github.com/libffi/libffi, https://visualstudio.microsoft.com |
| setuptools | MIT | https://github.com/pypa/setuptools |
| PySide6 and shiboken6 (Qt 6: the window) | LGPL-3.0 | https://code.qt.io/cgit/pyside/pyside-setup.git, https://www.qt.io |
| PyAV (reads videos) | BSD-3-Clause | https://github.com/PyAV-Org/PyAV |
| FFmpeg, inside PyAV | LGPL-3.0-or-later | https://ffmpeg.org, built by https://github.com/PyAV-Org/pyav-ffmpeg |
| x264 and x265, next to FFmpeg | GPL-2.0-or-later | https://www.videolan.org/developers/x264.html, https://www.x265.org |
| dav1d, libvpx, Opus, libwebp, SVT-AV1, libvmaf, libvpl, LAME, zlib, next to FFmpeg | BSD-2-Clause, BSD-3-Clause, BSD-3-Clause, BSD-3-Clause, BSD-3-Clause-Clear, BSD-2-Clause-Patent, MIT, LGPL-2.0-or-later, Zlib | see pyav-ffmpeg above |
| yt-dlp (finds a YouTube video's stream; the app can fetch a newer one from PyPI when asked to, into its settings folder) | Unlicense | https://github.com/yt-dlp/yt-dlp |
| Pillow (draws the pictures) | MIT-CMU | https://github.com/python-pillow/Pillow |
| pyusb | BSD-3-Clause | https://github.com/pyusb/pyusb |
| libusb-package, with libusb in it | Apache-2.0, LGPL-2.1-or-later | https://github.com/pyocd/libusb-package, https://libusb.info |
| winrt (PyWinRT: what Windows knows about the music) | MIT | https://github.com/pywinrt/pywinrt |
| icalendar | BSD-2-Clause | https://github.com/collective/icalendar |
| recurring-ical-events, x-wr-timezone | LGPL-3.0-or-later | https://github.com/niccokunzmann/python-recurring-ical-events, https://github.com/niccokunzmann/x-wr-timezone |
| python-dateutil | Apache-2.0 or BSD-3-Clause | https://github.com/dateutil/dateutil |
| click, six, typing_extensions, importlib_resources | BSD-3-Clause, MIT, PSF-2.0, Apache-2.0 | https://pypi.org |
| tzdata (time zones) | Apache-2.0 | https://github.com/python/tzdata |
| PyInstaller's bootloader (starts the app) | GPL-2.0-or-later with an exception that allows any licence for the app it starts | https://pyinstaller.org |

x264 and x265 make videos; deephack only plays them and never calls either. They are in the folder
because the FFmpeg that PyAV ships is built to load them.

Not in the repository and not in the app: anything of DeepCool's. The direct drawing firmware is made
on your PC from the firmware file DeepCool's own app installed there; deephack stores only the bytes
it changes (`deephack/firmware_patch.py`).
