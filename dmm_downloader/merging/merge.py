"""Joining the parts with ffmpeg (no re-encoding) and checking the result."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import tools
from .plan import PlanItem
from .probe import MediaFile, ProbeError, probe

log = logging.getLogger(__name__)

STALL_TIMEOUT = 600.0  # seconds without progress before ffmpeg is stopped
SPACE_MARGIN = 256 * 1024 * 1024


class MergeFailed(Exception):
    """The merge didn't produce a good file."""


@dataclass
class MergeResult:
    item: PlanItem
    ok: bool
    message: str
    output: Path | None = None
    deleted: list[Path] = field(default_factory=list)


def duration_tolerance(total: float) -> float:
    """How far the merged length may be from the parts' sum."""
    return max(2.0, total * 0.005)


def content_streams(media: MediaFile) -> int:
    """Streams that must survive the merge (an MP4 adds a data track for its chapters)."""
    return sum(1 for s in media.streams if s.kind in ("video", "audio", "subtitle"))


def concat_line(path: Path) -> str:
    """One line of ffmpeg's concat list. Forward slashes work on Windows too."""
    escaped = path.resolve().as_posix().replace("'", "'\\''")
    return f"file '{escaped}'\n"


def ffmetadata_escape(text: str) -> str:
    for char in ("\\", "=", ";", "#", "\n"):
        text = text.replace(char, "\\" + char)
    return text


def metadata_text(parts: list[MediaFile], numbers: list[int], file_id: str | None) -> str:
    """Chapters "Part N" at each join, and DMM's content id kept in the comment tag."""
    lines = [";FFMETADATA1"]
    if file_id:
        lines.append("comment=" + ffmetadata_escape(json.dumps({"file_id": file_id}, separators=(",", ":"))))
    start = 0.0
    for number, part in zip(numbers, parts):
        end = start + part.duration
        lines += ["", "[CHAPTER]", "TIMEBASE=1/1000", f"START={round(start * 1000)}", f"END={round(end * 1000)}", f"title=Part {number}"]
        start = end
    return "\n".join(lines) + "\n"


def _tail(path: Path, lines: int = 12) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


def _run_ffmpeg(cmd: list, total: float, on_progress: Callable[[float], None] | None, stall_timeout: float, err_path: Path) -> int:
    """Run ffmpeg with -progress on stdout; stop it when it makes no progress for too long."""
    with open(err_path, "wb") as err:
        proc = tools.Popen(
            [str(c) for c in cmd],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=err,
            creationflags=tools.NO_WINDOW,
        )
        state = {"seen": time.monotonic(), "done": -1}

        def read_progress() -> None:
            for raw in proc.stdout:
                key, _, value = raw.decode("utf-8", "replace").strip().partition("=")
                # out_time_us and (despite its name) out_time_ms are both microseconds.
                if key in ("out_time_us", "out_time_ms") and value.isdigit() and int(value) > state["done"]:
                    state["done"] = int(value)
                    state["seen"] = time.monotonic()
                    if on_progress and total > 0:
                        on_progress(min(1.0, int(value) / 1_000_000 / total))

        reader = threading.Thread(target=read_progress, daemon=True)
        reader.start()
        try:
            while True:
                try:
                    code = proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() - state["seen"] > stall_timeout:
                        raise MergeFailed(f"ffmpeg made no progress for {round(stall_timeout / 60)} minutes and was stopped")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            reader.join(timeout=5)
            proc.stdout.close()
    return code


def merge(
    item: PlanItem,
    tool_paths: tools.Tools,
    delete_parts: bool = False,
    on_progress: Callable[[float], None] | None = None,
    stall_timeout: float = STALL_TIMEOUT,
) -> MergeResult:
    """Merge one planned group into item.target.

    ffmpeg writes "<name>.partial.<ext>"; it is checked (length, streams) and only then renamed
    to the final name, so an interrupted or bad merge never looks finished. The parts are only
    deleted (when asked) after that. on_progress is called from a background thread.
    """
    group, target = item.group, item.target
    assert target is not None and item.action == "merge"
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.stem}.partial{target.suffix}")

    free = shutil.disk_usage(target.parent).free
    needed = group.size + SPACE_MARGIN
    if free < needed:
        return MergeResult(item, False, f"not enough free space in {target.parent}: needs {needed / 1e9:.1f} GB, {free / 1e9:.1f} GB free")

    work = Path(tempfile.mkdtemp(prefix="video-merger-"))
    try:
        list_path = work / "parts.txt"
        meta_path = work / "chapters.txt"
        list_path.write_text("".join(concat_line(p.path) for p in group.parts), encoding="utf-8")
        meta_path.write_text(metadata_text(group.parts, group.numbers, group.name if group.from_comment else None), encoding="utf-8")
        cmd = [
            tool_paths.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-i", meta_path,
            "-map", "0", "-map_metadata", "1", "-map_chapters", "1",
            "-c", "copy",
        ]
        if target.suffix.lower() == ".mp4":
            cmd += ["-movflags", "+faststart"]
        cmd += ["-progress", "pipe:1", "-nostats", partial]
        log.info("merging %s: %s", group.name, " ".join(str(c) for c in cmd))

        try:
            code = _run_ffmpeg(cmd, group.duration, on_progress, stall_timeout, work / "ffmpeg.log")
            if code != 0:
                raise MergeFailed(f"ffmpeg failed (exit code {code}):\n{_tail(work / 'ffmpeg.log')}")
            try:
                merged = probe(partial, tool_paths.ffprobe)
            except ProbeError as exc:
                raise MergeFailed(f"the merged file can't be read: {exc}") from exc
            expected = group.duration
            if abs(merged.duration - expected) > duration_tolerance(expected):
                raise MergeFailed(f"the merged file is {merged.duration:.1f} s long, but the parts add up to {expected:.1f} s")
            have, want = content_streams(merged), content_streams(group.parts[0])
            if have != want:
                raise MergeFailed(f"the merged file has {have} video/audio/subtitle streams instead of {want}")
            os.replace(partial, target)
        except MergeFailed as exc:
            log.warning("merge of %s failed: %s", group.name, exc)
            return MergeResult(item, False, str(exc))
        except OSError as exc:
            log.warning("merge of %s failed: %s", group.name, exc)
            return MergeResult(item, False, f"{type(exc).__name__}: {exc}")
        finally:
            partial.unlink(missing_ok=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    result = MergeResult(item, True, f"merged {len(group.parts)} parts", output=target)
    if delete_parts:
        problems = []
        for part in group.parts:
            try:
                part.path.unlink()
                result.deleted.append(part.path)
            except OSError as exc:
                problems.append(f"{part.path}: {exc}")
        if problems:
            result.message += "; some parts could not be deleted: " + "; ".join(problems)
    log.info("merged %s -> %s", group.name, target)
    return result
