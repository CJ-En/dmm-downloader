"""One run per video: a lock file in the video's work folder.

Two copies of the program, or two rows of the page with the same id, would otherwise download and decrypt into the
same files at the same time. The lock file holds the process id and when that process started (a later process can
get the same id), so a lock left by a program that ended (closed window, crash) is taken over; two runs taking
over the same lock at once don't both get it (_take_over).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

LOCK_NAME = "lock"
NEW_LOCK = 10.0  # seconds: an unreadable lock this new is being written right now, not left behind

_guard = threading.Lock()
_held: set[str] = set()  # this program's locks (normalized paths)


class Busy(Exception):
    """Another run works on this video now (the message says which)."""


def process_start(pid: int):
    """When process `pid` started (a number only compared with itself), 0 when it runs but that can't be read, or
    None when no such process runs."""
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return 0 if ctypes.get_last_error() == 5 else None  # 5: access denied, so it runs
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:  # 259: still running
                return None
            created, ended, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended), ctypes.byref(kernel),
                                            ctypes.byref(user)):
                return 0
            return created.dwHighDateTime << 32 | created.dwLowDateTime
        finally:
            kernel32.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            return None
        return None if fields[0] in ("Z", "X") else int(fields[19])  # state, and start time (field 22)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return 0
    return 0


def _key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _owner_of(data: bytes) -> dict | None:
    try:
        owner = json.loads(data.decode("utf-8"))
        return owner if isinstance(owner, dict) and isinstance(owner.get("pid"), int) else None
    except ValueError:
        return None


def _owner(path: Path) -> dict | None:
    try:
        return _owner_of(path.read_bytes())
    except OSError:
        return None


def _look(path: Path) -> tuple[bytes, int] | None:
    """A lock file as it is now: what it holds and when it was written; None when it can't be read (or is gone)."""
    try:
        return path.read_bytes(), path.stat().st_mtime_ns
    except OSError:
        return None


def _take_over(path: Path, seen: tuple[bytes, int]) -> bool:
    """Removes a lock that was judged left behind (`seen`: how it looked then). Two runs may judge the same lock at
    the same time, and the second could remove the new lock the first just made. So the file is first renamed away
    (only one run can rename it), and removed only when it still is the lock that was judged; otherwise it is a new
    lock of another run: it is put back, and False says so."""
    aside = path.with_name(f"{LOCK_NAME}.{os.getpid()}-{threading.get_ident()}.old")
    try:
        os.replace(path, aside)
    except OSError:  # gone (taken over by another run), or (Windows) being written right now: look again
        return True
    if _look(aside) == seen:
        aside.unlink(missing_ok=True)
        return True
    try:
        if os.name == "nt":
            os.rename(aside, path)  # Windows refuses when a lock is there again
        else:
            try:
                os.link(aside, path)  # refuses when a lock is there again (rename would replace it)
            except FileExistsError:
                raise
            except OSError:  # a file system without hard links
                if path.exists():
                    raise FileExistsError from None
                os.rename(aside, path)
            else:
                aside.unlink()
    except OSError:  # yet another lock is there now: that one counts
        aside.unlink(missing_ok=True)
    return False


def _running(owner: dict) -> bool:
    start = process_start(owner["pid"])
    if start is None:
        return False  # that program has ended
    recorded = owner.get("start")
    return not (start and recorded and start != recorded)  # another process got the same id later


class Lock:
    def __init__(self, path: Path, key: str, me: dict) -> None:
        self.path, self.key, self.me = path, key, me

    def release(self) -> None:
        with _guard:
            _held.discard(self.key)
            owner = _owner(self.path)
            if owner and owner.get("pid") == self.me["pid"] and owner.get("start") == self.me["start"]:
                self.path.unlink(missing_ok=True)

    def __enter__(self) -> "Lock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def acquire(work: Path, what: str) -> Lock:
    """Lock the video's work folder `work` (it must exist) for this run; Busy when another run holds it."""
    path = work / LOCK_NAME
    me = {"pid": os.getpid(), "start": process_start(os.getpid())}
    with _guard:
        key = _key(path)
        if key in _held:
            raise Busy(f"{what} is already being downloaded by this program (another row of the list): "
                       "wait until that one is done")
        for _ in range(3):
            try:
                handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                seen = _look(path)
                if seen is None:
                    continue  # removed meanwhile: try again
                owner = _owner_of(seen[0])
                if owner is None:
                    if time.time() - seen[1] / 1e9 < NEW_LOCK:  # another run is writing it right now
                        raise Busy(f"{what} is being started by another copy of the downloader: wait until it is "
                                   "done, or close that copy") from None
                elif owner["pid"] != me["pid"] and _running(owner):
                    raise Busy(f"{what} is already being downloaded by another copy of the downloader (process "
                               f"{owner['pid']}): wait until it is done, or close that copy") from None
                # Left by a program that ended (or by this one earlier): take it over.
                if not _take_over(path, seen):
                    raise Busy(f"{what} was just started by another copy of the downloader: wait until it is done, "
                               "or close that copy") from None
                continue
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(dict(me, video=what, since=time.strftime("%Y-%m-%d %H:%M:%S")), file)
            _held.add(key)
            return Lock(path, key, me)
    raise Busy(f"{what}: another copy of the downloader keeps taking this video's lock; close it and try again")
