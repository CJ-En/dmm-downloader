"""Finding video files and grouping the parts of each video in order."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .probe import MediaFile

VIDEO_EXTENSIONS = {".mkv", ".mp4"}
# Reported as ignored: WMV is never merged; disc files and other formats aren't DMM WEB-DL parts.
IGNORED_VIDEO_EXTENSIONS = {".wmv", ".avi", ".flv", ".mov", ".webm", ".ts", ".m2ts", ".mts", ".vob", ".iso", ".mpg", ".mpeg"}

# Part numbers in file names, for files without DMM's comment tag:
# "abc00123-pt2", "abc00123 part 2", "abc00123.cd2", "abc00123_2", "ABC-123-2", "ABC-123-B".
# "ABC-012" (a DVD id) must not count as part 12: a bare number needs "_", or "-" after a whole
# DVD id, and no leading zero. The name before the part number is kept exactly (no padding
# changes), so "sone096_1" and "sone00096_1" are different videos.
# Files decrypted from DMM (jav-it) are matched by their comment tag instead, whatever DMM's
# suffix in the name ("hhb1", "hhbs2", "mhb1", "2d_v1_drm_a_4k1", ...).
NAME_PATTERNS = (
    re.compile(r"^(?P<base>.*?[a-z].*?)[\s._-]*(?:part|pt|cd|disc|disk)[\s._-]*0*(?P<n>[1-9]\d{0,2})$", re.IGNORECASE),
    re.compile(r"^(?P<base>.*?[a-z].*?)_(?P<n>[1-9]\d?)$", re.IGNORECASE),
    re.compile(r"^(?P<base>[a-z0-9]*[a-z][a-z0-9]*-\d+[a-z]?)-(?P<n>[1-9]\d?)$", re.IGNORECASE),
    re.compile(r"^(?P<base>[a-z0-9]*[a-z][a-z0-9]*-\d+)[\s_-](?P<letter>[a-d])$", re.IGNORECASE),
)
SAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


@dataclass
class Group:
    """The parts of one video, in order."""

    name: str  # output file name without extension: the content id, or the shared file name
    folder: Path  # folder of the first part
    parts: list[MediaFile]
    numbers: list[int]
    from_comment: bool
    problems: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(p.size for p in self.parts)

    @property
    def duration(self) -> float:
        return sum(p.duration for p in self.parts)


def is_candidate(path: Path) -> bool:
    name = path.name
    return (
        path.suffix.lower() in VIDEO_EXTENSIONS
        and not name.startswith(".")
        and ".partial." not in name.lower()  # unfinished output of this tool
    )


def find_video_files(paths: list[Path], recursive: bool = False) -> tuple[list[Path], list[str], list[Path]]:
    """Video files given directly or inside the given folders, the paths that don't exist, and
    the video files of other formats (e.g. .wmv) that are left alone."""
    found: dict[Path, None] = {}
    ignored: dict[Path, None] = {}
    missing = []

    def consider(path: Path) -> None:
        if is_candidate(path):
            found[path.resolve()] = None
        elif path.suffix.lower() in IGNORED_VIDEO_EXTENSIONS:
            ignored[path.resolve()] = None

    for given in paths:
        if given.is_file():
            consider(given)
        elif given.is_dir():
            entries = given.rglob("*") if recursive else given.iterdir()
            for entry in sorted(entries):
                if entry.is_file():
                    consider(entry)
        else:
            missing.append(str(given))
    return list(found), missing, list(ignored)


def part_from_name(stem: str) -> tuple[str, int] | None:
    for pattern in NAME_PATTERNS:
        match = pattern.match(stem.strip())
        if match:
            base = match.group("base").rstrip(" ._-")
            if base:
                groups = match.groupdict()
                number = int(groups["n"]) if groups.get("n") else ord(groups["letter"].lower()) - ord("a") + 1
                return base, number
    return None


def safe_name(name: str) -> str:
    return SAFE_NAME_RE.sub("_", name).strip(" .") or "merged"


def group_files(files: list[MediaFile]) -> tuple[list[Group], list[MediaFile]]:
    """Group parts by DMM content id (comment tag) or else by file name.

    Returns the groups and the files that are not parts of a multi-part video. A file with a
    content id but no part number is a whole video (for example an earlier merge), not a part.
    """
    buckets: dict[tuple, list[tuple[int, MediaFile]]] = {}
    names: dict[tuple, tuple[str, bool]] = {}
    singles = []
    for media in files:
        from_name = part_from_name(media.path.stem)
        if media.file_id and (media.part or from_name):
            key = ("id", media.file_id)
            number = media.part or from_name[1]
            names[key] = (media.file_id, True)
        elif not media.file_id and from_name:
            key = ("name", str(media.path.parent).lower(), from_name[0].lower())
            number = from_name[1]
            names.setdefault(key, (safe_name(from_name[0]), False))
        else:
            singles.append(media)
            continue
        buckets.setdefault(key, []).append((number, media))

    groups = []
    for key, entries in buckets.items():
        entries.sort(key=lambda entry: (entry[0], str(entry[1].path)))
        numbers = [n for n, _ in entries]
        parts = [m for _, m in entries]
        name, from_comment = names[key]
        if numbers == [1]:
            singles.append(parts[0])  # a one-part video: nothing to merge
            continue
        group = Group(name=name, folder=parts[0].path.parent, parts=parts, numbers=numbers, from_comment=from_comment)
        seen: dict[int, list[MediaFile]] = {}
        for n, media in entries:
            seen.setdefault(n, []).append(media)
        for n, copies in seen.items():
            if len(copies) > 1:
                group.problems.append(f"part {n} was found {len(copies)} times: " + ", ".join(str(c.path) for c in copies))
        missing = sorted(set(range(1, max(numbers) + 1)) - set(numbers))
        if missing:
            group.problems.append("missing part(s) " + ", ".join(map(str, missing)))
        groups.append(group)
    groups.sort(key=lambda g: (str(g.folder).lower(), g.name.lower()))
    return groups, singles
