"""Where settings, the saved DMM session and logs live, and the settings file itself."""

from __future__ import annotations

import configparser
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .dmm import valid_japan_route

APP = "DMMDownloader"
SLUG = "dmm-downloader"
SECTION = "settings"

TEMPLATE = """\
# dmm-downloader settings. Empty values use the default. Command-line options win over these.

[settings]
# Folder for finished videos (default: "downloads" in the folder you start the program from;
# the Windows program: "downloads" next to DMM-Downloader.exe).
output_dir =

# jav-it program (default: next to dmm-downloader, then PATH). DECRYPT.MOD must sit next to it; with VR.MOD
# next to it too, VR videos are decrypted (without it they are saved encrypted, .wsdcf).
jav_it =

# Email jav-it asks for when it validates your Patreon membership (default: your DMM login email).
javit_email =

# mediainfo, ffmpeg and ffprobe (default: next to dmm-downloader, then PATH).
mediainfo =
ffmpeg =
ffprobe =

# Where to download from: auto (Premium, then the VR Channel, then your library), premium, vrchannel or library.
source = auto

# Join H.264 parts into one file: yes or no. VP9 parts always stay separate.
merge = yes

# Tries per request and per download before giving up.
retries = 5

# Videos downloaded at the same time on the browser page (VR videos: always one at a time).
parallel = 4

# The separate program that downloads the files themselves, so a VPN can carry only that step (split tunnelling):
# empty = DMM-Fetch.exe next to the program when it is there; "off" = never; or its path.
fetch_helper =

# Browser name sent to DMM (default: Googlebot, which DMM serves outside Japan).
user_agent =

# Japan route for the VR Channel: DMM gives the VR Channel's downloads only to a browser in Japan. Empty = this
# program's own connection (a VPN app connected to a server in Japan, or a computer in Japan); or an http proxy in
# Japan, e.g. http://127.0.0.1:25345 (wireproxy with a WireGuard config of a VPN server in Japan). Only the VR
# Channel's download list and links use it (and its files when DMM's file server refuses them elsewhere).
vrchannel_route =
"""


def config_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / SLUG


def log_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP / "Logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / SLUG


def default_output_dir() -> Path:
    """Where videos go when no folder is set: "downloads" in the folder the program is started from; for the
    Windows program, next to DMM-Downloader.exe (README-windows.txt), wherever it was started from."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "downloads"
    return Path("downloads")


def config_path() -> Path:
    return config_dir() / "config.ini"


def session_path() -> Path:
    return config_dir() / "session.json"


@dataclass
class Settings:
    output_dir: str = ""
    jav_it: str = ""
    javit_email: str = ""
    mediainfo: str = ""
    ffmpeg: str = ""
    ffprobe: str = ""
    source: str = "auto"
    merge: bool = True
    retries: int = 5
    user_agent: str = ""
    parallel: int = 4
    fetch_helper: str = ""
    vrchannel_route: str = ""  # the Japan route: "" = this program's own connection, or an http proxy in Japan


def ensure_config_file() -> Path:
    path = config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEMPLATE, encoding="utf-8")
    return path


def load(path: Path | None = None) -> Settings:
    path = path or config_path()
    parser = configparser.ConfigParser(interpolation=None)
    if path.exists():
        parser.read(path, encoding="utf-8")
    values = parser[SECTION] if parser.has_section(SECTION) else {}
    settings = Settings()

    def text(key: str) -> str:
        return str(values.get(key, "") or "").strip().strip('"')

    for key in ("output_dir", "jav_it", "javit_email", "mediainfo", "ffmpeg", "ffprobe", "user_agent", "fetch_helper"):
        setattr(settings, key, text(key))
    if valid_japan_route(text("vrchannel_route")):  # anything else: this program's own connection
        settings.vrchannel_route = text("vrchannel_route")
    source = text("source").lower()
    if source in ("auto", "premium", "vrchannel", "library"):
        settings.source = source
    if text("merge"):
        settings.merge = text("merge").lower() in ("yes", "true", "1", "on")
    try:
        settings.retries = max(1, min(20, int(text("retries") or settings.retries)))
    except ValueError:
        pass
    try:
        settings.parallel = max(1, min(8, int(text("parallel") or settings.parallel)))
    except ValueError:
        pass
    return settings


def save(settings: Settings, path: Path | None = None) -> Path:
    """Write the settings into the settings file, keeping its comments (the page's Settings tab uses this)."""
    path = path or ensure_config_file()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = TEMPLATE.splitlines()
    values = {key: ("yes" if value else "no") if isinstance(value, bool) else str(value) for key, value in vars(settings).items()}
    written = set()
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith(("#", ";")) and key in values:
            lines[index] = f"{key} = {values[key]}"
            written.add(key)
    if f"[{SECTION}]" not in (line.strip() for line in lines):
        lines.append(f"[{SECTION}]")
    lines += [f"{key} = {value}" for key, value in values.items() if key not in written]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path
