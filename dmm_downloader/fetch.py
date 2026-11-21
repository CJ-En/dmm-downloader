"""The last download step on its own: one file from DMM's file server (stcN.dmm.com, VR: wsd-cN.dmm.com).

It runs inside the tool, or as its own program (DMM-Fetch.exe in the Windows download, or
`python -m dmm_downloader.fetch`), so a VPN's split tunneling can send only this step through the VPN
(DMM refuses its link steps from VPN addresses, its file servers don't, and they were about five times faster
through a VPN in a test). The program gets its job as one JSON line on stdin
({"url", "path", "user_agent"}: the address carries a key, so never on the command line) and answers JSON lines
on stdout: {"done", "total"} while downloading, then {"ok": size} or {"error": text, "kind": ...}.

The file is written as given ("<name>.part" in the tool) and continued where it stopped when the server allows
it (HTTP Range), otherwise started again.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import requests

from . import children

CDN_HOST_RE = re.compile(r"^(stc|wsd-c)[0-9]+\.dmm\.com$", re.IGNORECASE)
CHUNK = 1024 * 1024
TIMEOUT = (15, 120)  # connect, and longest wait for the next bytes
SPACE_MARGIN = 512 * 1024 * 1024
PROGRESS_EVERY = 0.25  # seconds between progress lines of the separate program

Progress = Callable[[int, int | None], None]  # bytes done, total bytes (None if unknown)


class DownloadError(Exception):
    """A part could not be downloaded."""


class NoSpace(DownloadError):
    """Not enough free disk space (trying again won't help)."""


class NotAFile(DownloadError):
    """The file server sent a web page or other text instead of the (binary) video file."""


class FileRefused(DownloadError):
    """The file server refused the file (HTTP 403, 404 or 410): e.g. a link it gives only to the address that asked
    DMM for it (the VR Channel's links are asked for through the Japan route)."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


REFUSED = (403, 404, 410)


def disk_full(exc: OSError) -> bool:
    return exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in (39, 112)  # Windows: handle / disk full


def _gb(size: float) -> str:
    return f"{size / 1e9:.1f} GB"


class SpaceBook:
    """What the downloads (and jav-it runs) going on at the same time still have to write, per disk. Each used to
    compare its own needs with the same free space, so four parallel downloads could together fill the disk; now
    a new one starts only when the free space covers it and what the others still need."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claims: dict[int, tuple[int, Callable[[], int]]] = {}  # claim -> (disk, bytes it still has to write)
        self._count = 0

    def others(self, folder: Path, disk: int | None = None) -> int:
        disk = os.stat(folder).st_dev if disk is None else disk
        with self._lock:
            return sum(max(0, left()) for d, left in self._claims.values() if d == disk)

    def claim(self, folder: Path, left: Callable[[], int], what: str = "download") -> "Claim":
        """Reserve what `left()` still has to write into `folder` (asked again each time, so it shrinks while the
        file grows); NoSpace when the disk can't take it next to the others."""
        disk = os.stat(folder).st_dev
        with self._lock:
            mine = max(0, left())
            others = sum(max(0, need()) for d, need in self._claims.values() if d == disk)
            free = shutil.disk_usage(folder).free
            if free < mine + others + SPACE_MARGIN:
                running = f" and the others running {_gb(others)} more" if others else ""
                raise NoSpace(f"not enough free space to {what} in {folder}: this needs {_gb(mine)}{running}, "
                              f"{_gb(free)} free")
            self._count += 1
            key = self._count
            self._claims[key] = (disk, left)
        return Claim(self, key)

    def _release(self, key: int) -> None:
        with self._lock:
            self._claims.pop(key, None)


class Claim:
    def __init__(self, book: SpaceBook, key: int) -> None:
        self.book, self.key = book, key

    def release(self) -> None:
        self.book._release(self.key)

    def __enter__(self) -> "Claim":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


SPACE = SpaceBook()  # one per program: every download and jav-it run of the tool books its space here


# Answers that are a page, not a video file (VR files get no other content check: they stay encrypted).
PAGE_TYPES = ("application/json", "application/xml", "application/xhtml+xml", "application/problem+json")
NOT_A_FILE = "DMM's file server sent a web page or text instead of the video file"
PAGE_MAX_BYTES = 10 * 1024 * 1024  # an error page is far smaller; a VR part is gigabytes


def page_type(content_type: str) -> bool:
    kind = (content_type or "").split(";")[0].strip().lower()
    return kind.startswith("text/") or kind in PAGE_TYPES


def looks_like_text(path: Path, size: int = 4096) -> bool:
    """Whether the file starts like a web page, JSON or other text instead of binary video data (an error page sent
    with "200 OK"). DMM's video files, encrypted or not, have zero bytes and non-text data in their first bytes."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(size)
    except OSError:
        return False
    if not head or b"\x00" in head:
        return False
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start < len(head) - 4:  # not just a character cut at the end of the sample
            return False
        text = head[:exc.start].decode("utf-8")
    text = text.removeprefix("﻿")  # a UTF-8 byte order mark
    printable = sum(1 for c in text if c.isprintable() or c in "\r\n\t")
    return printable >= 0.95 * len(text)


def file_server(url: str) -> bool:
    """Whether `url` is one of DMM's file servers (the only addresses this step downloads from)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and bool(CDN_HOST_RE.fullmatch(parts.hostname or "")) and port is None
            and not parts.username and not parts.password)


def _total_from(response: requests.Response, offset: int) -> int | None:
    content_range = response.headers.get("Content-Range", "")
    match = re.match(r"bytes (\d+)-\d+/(\d+)", content_range)
    if response.status_code == 206 and match:
        return int(match.group(2))
    length = response.headers.get("Content-Length")
    if length and length.isdigit():
        return int(length) + (offset if response.status_code == 206 else 0)
    return None


def fetch_file(cdn_url: str, user_agent: str, partial: Path, progress: Progress | None = None,
               binary_only: bool = False, proxies: dict | None = None) -> int:
    """Download into `partial`, continuing it when possible. Returns the total size. `binary_only` (VR): an answer
    labeled as a page or text is refused before anything is written. `proxies`: through the Japan route's proxy."""
    if not file_server(cdn_url):
        raise DownloadError("not a DMM file server address; stopped")
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent": user_agent}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    total = None
    with requests.Session() as plain:  # no cookies: the file servers are dmm.com, the login is dmm.co.jp
        response = plain.get(cdn_url, headers=headers, stream=True, timeout=TIMEOUT, proxies=proxies)
        try:
            if response.status_code == 416 and offset:  # nothing after `offset`: already complete, or the file changed
                size = re.fullmatch(r"bytes \*/(\d+)", response.headers.get("Content-Range", "").strip())
                if size and int(size.group(1)) == offset:  # the whole file is here (e.g. its rename failed last time)
                    if progress:
                        progress(offset, offset)
                    return offset
                partial.unlink()
                raise DownloadError("the server refused to continue the earlier download; starting again")
            if response.status_code in REFUSED:
                raise FileRefused(f"DMM's file server answered HTTP {response.status_code}", response.status_code)
            if response.status_code not in (200, 206):
                raise DownloadError(f"DMM's file server answered HTTP {response.status_code}")
            start_match = re.match(r"bytes (\d+)-", response.headers.get("Content-Range", ""))
            if response.status_code == 206 and start_match and int(start_match.group(1)) == offset:
                mode = "ab"
            else:
                offset, mode = 0, "wb"  # no resume: start over
            total = _total_from(response, offset)
            content_type = response.headers.get("Content-Type", "")
            if binary_only and page_type(content_type):
                # A page is small. A big answer labeled as text is downloaded anyway (DMM's labels for .wsdcf files
                # aren't known, and every refused try counts against DMM's per-video limit); the check of its first
                # bytes after the download still refuses a page.
                if total is None or total < PAGE_MAX_BYTES:
                    raise NotAFile(f"{NOT_A_FILE} ({content_type})")
            done = offset
            # Booked with the other downloads running (SPACE); what this one still has to write shrinks as it goes.
            with SPACE.claim(partial.parent, lambda: (total - done) if total is not None else 0):
                try:
                    with open(partial, mode) as handle:
                        for chunk in response.iter_content(CHUNK):
                            if chunk:
                                handle.write(chunk)
                                done += len(chunk)
                                if progress:
                                    progress(done, total)
                except OSError as exc:
                    if disk_full(exc):  # trying again won't help
                        raise NoSpace(f"disk full: no space left in {partial.parent}; free some space, then start "
                                      "again (the part downloaded so far is kept)") from exc
                    raise
        finally:
            response.close()
    size = partial.stat().st_size
    if total is not None and size != total:
        raise DownloadError(f"incomplete download ({size} of {total} bytes)")
    if size == 0:
        raise DownloadError("the downloaded file is empty")
    return size


def fetch_with_program(command: list[str], cdn_url: str, user_agent: str, partial: Path,
                       progress: Progress | None = None, creationflags: int = 0, binary_only: bool = False) -> int:
    """fetch_file() in the separate program `command` (e.g. DMM-Fetch.exe), reading its answers."""
    if not file_server(cdn_url):
        raise DownloadError("not a DMM file server address; stopped")
    try:
        # children.Popen: the program ends with the downloader, also when its window is closed.
        process = children.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8", errors="replace", creationflags=creationflags)
    except OSError as exc:
        raise DownloadError(f"the download program could not be started ({command[0]}): {exc}") from exc
    answer: dict = {}
    errors = ""
    seen = {"done": 0, "total": None}
    claim = None
    try:
        job = {"url": cdn_url, "path": str(partial), "user_agent": user_agent, "binary_only": binary_only}
        process.stdin.write(json.dumps(job) + "\n")
        process.stdin.close()
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if "done" in message:
                seen.update(done=int(message["done"]), total=message.get("total"))
                if claim is None and isinstance(seen["total"], int):
                    # Booked with the other downloads running, as soon as the size is known (the program checks
                    # only its own needs); NoSpace stops the program (below).
                    claim = SPACE.claim(partial.parent, lambda: seen["total"] - seen["done"])
                if progress:
                    progress(seen["done"], seen["total"])
            else:
                answer = message
        process.wait()
        if "ok" not in answer:
            errors = process.stderr.read()
    finally:
        if claim is not None:
            claim.release()
        if process.poll() is None:
            process.kill()
            process.wait()
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()
    if "ok" in answer:
        return int(answer["ok"])
    error = str(answer.get("error") or "").strip() or (errors.strip().splitlines() or ["no answer"])[-1]
    if answer.get("kind") == "notafile":
        raise NotAFile(error)
    if answer.get("kind") == "nospace":
        raise NoSpace(error)
    if answer.get("kind") == "refused":
        raise FileRefused(error, int(answer.get("status") or 0))
    if answer.get("kind") == "connection":
        raise requests.ConnectionError(error)
    raise DownloadError(error)


def main() -> int:
    """The separate program: one download, job on stdin, JSON lines on stdout (see the module's text)."""
    def say(message: dict) -> None:
        print(json.dumps(message), flush=True)

    try:
        job = json.loads(sys.stdin.readline() or "{}")
        url, path, user_agent = str(job["url"]), Path(str(job["path"])), str(job.get("user_agent") or "")
        binary_only = bool(job.get("binary_only"))
    except (ValueError, KeyError, TypeError):
        say({"error": "no download job given (this program is started by DMM Downloader)", "kind": "usage"})
        return 2
    last = 0.0

    def progress(done: int, total: int | None) -> None:
        nonlocal last
        now = time.monotonic()
        if now - last >= PROGRESS_EVERY or (total and done >= total):
            last = now
            say({"done": done, "total": total})

    try:
        say({"ok": fetch_file(url, user_agent, path, progress, binary_only)})
        return 0
    except NoSpace as exc:
        say({"error": str(exc), "kind": "nospace"})
    except NotAFile as exc:
        say({"error": str(exc), "kind": "notafile"})
    except FileRefused as exc:
        say({"error": str(exc), "kind": "refused", "status": exc.status})
    except DownloadError as exc:
        say({"error": str(exc), "kind": "download"})
    except requests.RequestException as exc:
        say({"error": f"connection problem: {type(exc).__name__}", "kind": "connection"})
    except OSError as exc:
        say({"error": f"can't write {path}: {exc}", "kind": "write"})
    return 1


if __name__ == "__main__":
    sys.exit(main())
