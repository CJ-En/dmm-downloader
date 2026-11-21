"""Finding and running ffmpeg and ffprobe."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

WINDOWS = os.name == "nt"
# Keeps a console window from flashing up for every tool run when started from the GUI.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# How programs are started. A program that uses this package can put its own here, e.g. one that ends the programs
# when it ends (the DMM downloader does: dmm_downloader/merging/__init__.py).
Popen = subprocess.Popen

INSTALL_HINT = (
    "Install FFmpeg (Windows: https://www.gyan.dev/ffmpeg/builds/ , Linux: sudo apt install ffmpeg), "
    "or put ffmpeg and ffprobe next to dmm-downloader, or set ffmpeg in its config file."
)


class ToolError(Exception):
    """A required program is missing or unusable."""


@dataclass(frozen=True)
class Tools:
    ffmpeg: Path
    ffprobe: Path


def program_dir() -> Path:
    """The folder of the .exe (packaged) or of the source checkout."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _file_names(name: str) -> list[str]:
    if WINDOWS and not name.lower().endswith(".exe"):
        return [f"{name}.exe"]
    return [name]


def _usable(path: Path) -> bool:
    return path.is_file() and (WINDOWS or os.access(path, os.X_OK))


def find_tool(name: str, override: str | os.PathLike | None = None) -> Path:
    """Find a program: the given path, next to this program, then PATH.

    The current folder is never searched (unlike Windows' own lookup), so a stray ffmpeg.exe
    sitting next to the videos is not run.
    """
    if override:
        given = Path(override).expanduser()
        candidates = [given / n for n in _file_names(name)] if given.is_dir() else [given]
        for candidate in candidates:
            if _usable(candidate):
                return candidate.resolve()
        raise ToolError(f"{name} was not found at {given}.")

    folders = [program_dir(), program_dir() / "bin"]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if entry and Path(entry).is_absolute():
            folders.append(Path(entry))
    for folder in folders:
        for file_name in _file_names(name):
            candidate = folder / file_name
            if _usable(candidate):
                return candidate.resolve()
    raise ToolError(f"{name} was not found. {INSTALL_HINT}")


def find_tools(ffmpeg: str | None = None, ffprobe: str | None = None) -> Tools:
    return Tools(ffmpeg=find_tool("ffmpeg", ffmpeg), ffprobe=find_tool("ffprobe", ffprobe))


def run(cmd: list, timeout: float) -> subprocess.CompletedProcess:
    """Run a short command and capture its output (bytes). Like subprocess.run, but started with Popen above."""
    with Popen([str(part) for part in cmd], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
               creationflags=NO_WINDOW) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def version(path: Path) -> str:
    try:
        result = run([path, "-version"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"not working ({exc})"
    first_line = result.stdout.decode("utf-8", "replace").splitlines()[:1]
    return first_line[0].strip() if first_line else "unknown version"
