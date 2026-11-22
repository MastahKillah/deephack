# PyInstaller recipe for the standalone app: a folder with deephack.exe that runs without Python installed.
#
#     pip install -e .[package]
#     pyinstaller --noconfirm packaging/deephack.spec        (makes dist/deephack/)
#
# A folder, not a single file: a one-file app unpacks itself to a temporary folder on every start, which is
# slow for an app that starts with Windows, and virus scanners like it even less.
import os
import shutil

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hidden = [], [], []
for package in ("libusb_package",          # carries libusb's DLL as a data file
                "tzdata"):                 # time zones for the calendars; pure data, nothing imports it
    found = collect_all(package)
    datas, binaries, hidden = datas + found[0], binaries + found[1], hidden + found[2]
hidden += collect_submodules("winrt")      # namespace packages of compiled modules, imported inside functions

a = Analysis(
    ["run.py"],
    pathex=[os.path.dirname(SPECPATH)],    # the repository's root, where the deephack package is
    datas=datas,
    binaries=binaries,
    hiddenimports=hidden,
    excludes=["tkinter", "numpy", "cv2", "unicorn", "capstone", "keystone", "pytest"],     # tools/ and tests only
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="deephack", console=False)
app = COLLECT(exe, a.binaries, a.datas, name="deephack")
for name in ("LICENSE", "THIRD-PARTY.md"):          # next to deephack.exe: the app carries other people's work
    shutil.copy(os.path.join(os.path.dirname(SPECPATH), name), app.name)
