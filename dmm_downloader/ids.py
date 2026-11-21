"""Content ids: reading them from text, files and DMM links, and naming the output files."""

from __future__ import annotations

import re
from typing import Iterable

# DMM content ids are lowercase letters, digits and "_": "abc00123", "1sdde00301", "h_1133gone00123",
# "1mist00432re01". They are used exactly as given: "sone096" and "sone00096" can be different videos.
CID_RE = re.compile(r"^[a-z0-9_]{1,80}$")
LINK_CID_RE = re.compile(r"(?:^|[/?&;])cid=([A-Za-z0-9_]{1,80})(?=$|[/?&;#])")
# DMM's newer pages (video.dmm.co.jp/av/content/?id=abc00123) name it "id" ("product_id=" etc. don't match).
LINK_ID_RE = re.compile(r"[?&;]id=([A-Za-z0-9_]{1,80})(?=$|[&;#])")


def parse_cid(text: str) -> str | None:
    """A content id from "abc00123" or a DMM link containing "cid=abc00123" (or "?id=abc00123"); None if invalid."""
    text = str(text or "").strip().strip("\"'")
    if not text:
        return None
    match = LINK_CID_RE.search(text) or LINK_ID_RE.search(text)  # cid= wins when a link has both
    if match:
        text = match.group(1)
    text = text.lower()
    return text if CID_RE.fullmatch(text) else None


def parse_lines(lines: Iterable[str]) -> tuple[list[str], list[str]]:
    """Content ids from lines of text (several per line allowed, "#" starts a comment).

    Returns the ids in order without repeats, and the entries that are not content ids.
    """
    ids: dict[str, None] = {}
    invalid = []
    for line in lines:
        for entry in re.split(r"[\s,]+", line.strip()):
            if entry.startswith("#"):
                break  # the rest of the line is a comment
            if not entry:
                continue
            cid = parse_cid(entry)
            if cid:
                ids[cid] = None
            else:
                invalid.append(entry)
    return list(ids), invalid


def output_name(file_id: str, part: int | None = None, extension: str = ".mkv") -> str:
    """The final file name: "abc00123.mkv", or "abc00123-pt2.mkv" for a part kept separate.

    DMM's own names ("abc00123hhb1.dcv", "abc000232d_v1_drm_a_4k1.dcv", ...) are never used.
    """
    if not CID_RE.fullmatch(file_id):
        raise ValueError(f"not a content id: {file_id!r}")
    return f"{file_id}-pt{part}{extension}" if part else f"{file_id}{extension}"
