# PyInstaller: DMM Downloader as a Windows program in one folder (build.ps1 runs it), with
# DMM-Fetch.exe next to it: the file download on its own, for VPN split tunneling.
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]
HERE = ROOT / "packaging" / "windows"
ICON = str(HERE / "dmm-downloader.ico")  # made by make_icon.py

main = Analysis(
    [str(HERE / "dmm_downloader_app.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "dmm_downloader" / "web"), "dmm_downloader/web")],
    hiddenimports=collect_submodules("keyring.backends"),
    excludes=["tkinter", "_tkinter"],
    noarchive=False,
)
fetch = Analysis(
    [str(HERE / "dmm_fetch.py")],
    pathex=[str(ROOT)],
    excludes=["tkinter", "_tkinter", "keyring", "bs4"],
    noarchive=False,
)
main_exe = EXE(
    PYZ(main.pure),
    main.scripts,
    [],
    exclude_binaries=True,
    name="DMM-Downloader",
    console=True,  # the window shows the page's address; closing it stops the downloader
    icon=ICON,
    upx=False,
)
fetch_exe = EXE(
    PYZ(fetch.pure),
    fetch.scripts,
    [],
    exclude_binaries=True,
    name="DMM-Fetch",
    console=True,  # started without a window by the downloader; it talks through stdin/stdout
    icon=ICON,
    upx=False,
)
coll = COLLECT(main_exe, main.binaries, main.datas, fetch_exe, fetch.binaries, fetch.datas,
               name="DMM-Downloader", upx=False)
