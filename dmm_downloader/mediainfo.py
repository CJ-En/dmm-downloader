"""Reading files with the MediaInfo command-line tool: errors, length, codec, DMM's part data."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .merging import tools
from .merging.probe import parse_dmm_comment

TIMEOUT = 300
# Fields whose name contains one of these and whose value is set mean a damaged file.
ERROR_WORDS = ("error", "compliance", "truncated", "invalid", "corrupt")


class MediaInfoError(Exception):
    """MediaInfo could not read a file."""


def read(path: Path, mediainfo: Path) -> list[dict]:
    """MediaInfo's tracks (General first)."""
    try:
        result = tools.run([mediainfo, "--Output=JSON", path], timeout=TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise MediaInfoError(f"MediaInfo took longer than {TIMEOUT} s on {path.name}") from exc
    except OSError as exc:
        raise MediaInfoError(f"MediaInfo could not be started: {exc}") from exc
    if result.returncode != 0:
        raise MediaInfoError(f"MediaInfo failed on {path.name} (exit code {result.returncode})")
    try:
        data = json.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise MediaInfoError(f"MediaInfo gave unreadable output for {path.name}") from exc
    media = data.get("media") if isinstance(data, dict) else None
    tracks = media.get("track") if isinstance(media, dict) else None
    if not isinstance(tracks, list) or not tracks:
        raise MediaInfoError(f"MediaInfo found nothing in {path.name}")
    return [t for t in tracks if isinstance(t, dict)]


def general(tracks: list[dict]) -> dict:
    return next((t for t in tracks if t.get("@type") == "General"), {})


def duration(tracks: list[dict]) -> float | None:
    try:
        return float(general(tracks).get("Duration"))
    except (TypeError, ValueError):
        return None


def dmm_comment(tracks: list[dict]) -> tuple[str | None, int | None]:
    info = general(tracks)
    return parse_dmm_comment(info.get("Comment") or (info.get("extra") or {}).get("Comment"))


def video_format(tracks: list[dict]) -> str:
    video = next((t for t in tracks if t.get("@type") == "Video"), {})
    return str(video.get("Format") or "")


def _bad(value) -> bool:
    if value is True:
        return True
    text = str(value).strip()
    if text.isdigit():
        return int(text) > 0
    return text == "Yes" or len(text) > 3


def problem(tracks: list[dict], full: bool = True) -> str | None:
    """Why the file looks damaged, or None. `full=False` only checks MediaInfo's error count and
    "truncated" flag (enough for the still encrypted download, whose streams can't be read)."""
    info = general(tracks)
    try:
        errors = int(info.get("Error_Count") or 0)
    except ValueError:
        errors = 0
    if errors:
        return f"MediaInfo found {errors} error(s)"
    if str(info.get("IsTruncated", "")).lower() == "yes":
        return "the file is cut short (truncated)"
    if not full:
        return None
    for track in tracks:
        fields = dict(track)
        fields.update(track.get("extra") or {})
        for key, value in fields.items():
            name = str(key).lower()
            if "errordetectiontype" in name or not any(word in name for word in ERROR_WORDS):
                continue
            if value not in (None, "", [], {}) and _bad(value):
                return f"MediaInfo reports {key}={value} ({track.get('@type', '?')} track)"
    return None
