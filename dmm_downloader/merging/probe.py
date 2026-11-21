"""Reading a video's streams, length and DMM part data with ffprobe."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from . import tools

# DMM content ids, e.g. "abc00123", "1sdde00301", "h_51cmn001".
CONTENT_ID_RE = re.compile(r"^[a-z0-9_]{1,80}$")
PROBE_TIMEOUT = 120


class ProbeError(Exception):
    """ffprobe could not read a file."""


@dataclass(frozen=True)
class Stream:
    index: int
    kind: str  # video, audio, subtitle, ...
    codec: str
    profile: str = ""
    width: int = 0
    height: int = 0
    pix_fmt: str = ""
    frame_rate: str = ""
    sample_rate: str = ""
    channels: int = 0
    duration: float | None = None

    def signature(self) -> tuple:
        """What must be equal in every part for a merge without re-encoding."""
        if self.kind == "video":
            return (self.kind, self.codec, self.profile, self.width, self.height, self.pix_fmt, self.frame_rate)
        if self.kind == "audio":
            return (self.kind, self.codec, self.profile, self.sample_rate, self.channels)
        return (self.kind, self.codec)

    def describe(self) -> str:
        if self.kind == "video":
            fps = format_rate(self.frame_rate)
            return f"video {self.codec} {self.profile} {self.width}x{self.height} {self.pix_fmt} {fps} fps".replace("  ", " ")
        if self.kind == "audio":
            return f"audio {self.codec} {self.profile} {self.sample_rate} Hz {self.channels} ch".replace("  ", " ")
        return f"{self.kind} {self.codec}"


@dataclass(frozen=True)
class MediaFile:
    path: Path
    size: int
    duration: float
    streams: tuple[Stream, ...]
    file_id: str | None = None  # from DMM's comment tag, kept by the decrypter
    part: int | None = None

    @property
    def video(self) -> Stream | None:
        return next((s for s in self.streams if s.kind == "video"), None)

    @property
    def audio(self) -> Stream | None:
        return next((s for s in self.streams if s.kind == "audio"), None)


def parse_duration(value) -> float | None:
    """Seconds from ffprobe's "8939.798" or Matroska's tag "02:28:59.798000000"."""
    if value in (None, "", "N/A"):
        return None
    text = str(value).strip()
    try:
        if ":" in text:
            hours, minutes, seconds = text.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        return float(text)
    except ValueError:
        return None


def format_rate(rate: str) -> str:
    try:
        value = Fraction(rate)
    except (ValueError, ZeroDivisionError):
        return rate or "?"
    return f"{float(value):.3f}".rstrip("0").rstrip(".")


def parse_dmm_comment(text) -> tuple[str | None, int | None]:
    """DMM files carry {"file_id": "<content id>", "part": "2"} in their comment tag."""
    if not text:
        return None, None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    file_id = str(data.get("file_id") or "").strip().lower()
    file_id = file_id if CONTENT_ID_RE.fullmatch(file_id) else None
    try:
        part = int(str(data.get("part")).strip())
    except (TypeError, ValueError):
        part = None
    if part is not None and part < 1:
        part = None
    return file_id, part


def _lower_keys(tags) -> dict:
    return {str(k).lower(): v for k, v in (tags or {}).items()}


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_probe(path: Path, size: int, data: dict) -> MediaFile:
    fmt = data.get("format") or {}
    streams = []
    for raw in data.get("streams") or []:
        tags = _lower_keys(raw.get("tags"))
        streams.append(
            Stream(
                index=_int(raw.get("index")),
                kind=str(raw.get("codec_type") or "unknown"),
                codec=str(raw.get("codec_name") or "unknown"),
                profile=str(raw.get("profile") or ""),
                width=_int(raw.get("width")),
                height=_int(raw.get("height")),
                pix_fmt=str(raw.get("pix_fmt") or ""),
                frame_rate=str(raw.get("r_frame_rate") or ""),
                sample_rate=str(raw.get("sample_rate") or ""),
                channels=_int(raw.get("channels")),
                duration=parse_duration(raw.get("duration")) or parse_duration(tags.get("duration")),
            )
        )
    file_id, part = parse_dmm_comment(_lower_keys(fmt.get("tags")).get("comment"))
    return MediaFile(
        path=path,
        size=size,
        duration=parse_duration(fmt.get("duration")) or 0.0,
        streams=tuple(streams),
        file_id=file_id,
        part=part,
    )


def probe(path: Path, ffprobe: Path) -> MediaFile:
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path]
    try:
        result = tools.run(cmd, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe took longer than {PROBE_TIMEOUT} s") from exc
    except OSError as exc:
        raise ProbeError(f"ffprobe could not be started: {exc}") from exc
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ProbeError(message[-1] if message else f"ffprobe failed (exit code {result.returncode})")
    try:
        data = json.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise ProbeError("ffprobe gave unreadable output") from exc
    return parse_probe(path, path.stat().st_size, data)
