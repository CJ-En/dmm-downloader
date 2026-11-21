"""Deciding what to do with each group: merge, skip or report a problem."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .discover import Group

AV_DRIFT_WARNING = 2.0  # seconds between a part's audio and video length

# Only WEB-DL parts are merged (DMM: H.264 or HEVC video, AAC audio). Disc Remuxes/rips and WMV are not.
WEB_VIDEO = {"h264", "hevc"}
WEB_AUDIO = {"aac"}
WMV_VIDEO = {"wmv1", "wmv2", "wmv3", "vc1", "msmpeg4v1", "msmpeg4v2", "msmpeg4v3"}
DISC_SUBTITLES = {"hdmv_pgs_subtitle", "dvd_subtitle"}
DISC_WORDS = re.compile(
    r"(?<![a-z0-9])(remux|bdremux|blu-?ray|bdrip|bdmv|bd(?:25|50|66|100)?|dvd(?:rip|remux|5|9)?|iso)(?![a-z0-9])",
    re.IGNORECASE,
)


@dataclass
class PlanItem:
    group: Group
    target: Path | None
    action: str  # "merge", "skip" or "problem"
    reason: str = ""
    warnings: tuple[str, ...] = ()


def is_vp9(group: Group) -> bool:
    return any(part.video and part.video.codec == "vp9" for part in group.parts)


def not_web_dl(group: Group) -> str | None:
    """Why these parts look like a disc Remux/rip or WMV rather than a WEB-DL, or None."""
    for part in group.parts:
        for name in (part.path.stem, part.path.parent.name):
            match = DISC_WORDS.search(name)
            if match:
                return f'"{match.group(0)}" in the name: Blu-ray/DVD Remuxes and rips are not merged'
        for stream in part.streams:
            if stream.kind == "video" and stream.codec in WMV_VIDEO:
                return f"{stream.codec} video (WMV/VC-1) is not merged"
            if stream.kind == "video" and stream.codec not in WEB_VIDEO:
                return f"{stream.codec} video is not merged (only H.264/HEVC WEB-DLs)"
            if stream.kind == "audio" and stream.codec not in WEB_AUDIO:
                return f"{stream.codec} audio looks like a Blu-ray/DVD Remux, not merged (only AAC WEB-DLs)"
            if stream.kind == "subtitle" and stream.codec in DISC_SUBTITLES:
                return f"{stream.codec} subtitles look like a Blu-ray/DVD Remux, not merged"
    return None


def stream_mismatch(group: Group) -> str | None:
    """Why the parts can't be joined without re-encoding, or None."""
    first = group.parts[0]
    for number, part in zip(group.numbers[1:], group.parts[1:]):
        if len(part.streams) != len(first.streams):
            return f"part {number} has {len(part.streams)} streams, part {group.numbers[0]} has {len(first.streams)}"
        for a, b in zip(first.streams, part.streams):
            if a.signature() != b.signature():
                return f"part {number} has {b.describe()}, part {group.numbers[0]} has {a.describe()}"
    return None


def drift_warnings(group: Group) -> tuple[str, ...]:
    warnings = []
    for number, part in zip(group.numbers, group.parts):
        video, audio = part.video, part.audio
        if video and audio and video.duration and audio.duration:
            gap = abs(video.duration - audio.duration)
            if gap > AV_DRIFT_WARNING:
                warnings.append(
                    f"part {number}: sound and picture lengths differ by {gap:.1f} s, "
                    "so the sound may be out of sync after the join"
                )
    return tuple(warnings)


def plan_group(group: Group, out_dir: Path | None, container: str, overwrite: bool) -> PlanItem:
    target = (out_dir or group.folder) / f"{group.name}.{container}"
    if is_vp9(group):
        return PlanItem(group, None, "skip", "VP9 video: the parts stay separate files")
    other = not_web_dl(group)
    if other:
        return PlanItem(group, None, "skip", other)
    if group.problems:
        return PlanItem(group, target, "problem", "; ".join(group.problems))
    mismatch = stream_mismatch(group)
    if mismatch:
        return PlanItem(group, target, "problem", f"the parts don't match ({mismatch})")
    empty = [str(n) for n, p in zip(group.numbers, group.parts) if p.duration <= 0]
    if empty:
        return PlanItem(group, target, "problem", f"part(s) {', '.join(empty)} have no length (damaged?)")
    if any(p.path.resolve() == target.resolve() for p in group.parts):
        return PlanItem(group, target, "problem", f"the output name {target.name} is one of the parts; use --out")
    if target.exists() and not overwrite:
        return PlanItem(group, target, "skip", f"already merged: {target} (use --overwrite to redo it)")
    return PlanItem(group, target, "merge", warnings=drift_warnings(group))


def build_plan(groups: list[Group], out_dir: Path | None, container: str = "mkv", overwrite: bool = False) -> list[PlanItem]:
    items = [plan_group(g, out_dir, container, overwrite) for g in groups]
    # Two groups writing the same file (same name in one output folder): refuse both.
    targets: dict[str, list[PlanItem]] = {}
    for item in items:
        if item.action == "merge":
            targets.setdefault(str(item.target).lower(), []).append(item)
    for clashing in targets.values():
        if len(clashing) > 1:
            for item in clashing:
                item.action = "problem"
                item.reason = f"{len(clashing)} different videos would be saved as {item.target}"
    return items
