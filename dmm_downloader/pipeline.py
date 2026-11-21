"""One video from start to finish: links, download, decrypt, check, name (and merge) the result.

2D: .dcv parts decrypted by jav-it into <id>.mkv. VR: .wsdcf parts decrypted by jav-it (VR.MOD) into <cid>.mp4, or,
without jav-it and VR.MOD, saved encrypted as <cid>.wsdcf.

Work files live in "<output>/.dmm-downloader/<cid>/" until the video is finished, so a stopped or
failed run continues where it left off: finished downloads and decrypted parts are reused,
a half-downloaded part is continued.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from . import decrypt as decrypting
from . import download, ids, links, mediainfo, worklock
from .credentials import Login
from .dmm import DmmError, DmmSession
from .fetch import SPACE, Claim, NoSpace
from .merging import merge as merging
from .merging import plan as planning
from .merging import probe as probing
from .merging import tools as merge_tools
from .merging.discover import Group

log = logging.getLogger(__name__)

WORK_FOLDER = ".dmm-downloader"
# In the work folder, kept when the videos are done: id asked for -> DMM's id in its files, when they differ.
IDS_FILE = "ids.json"
VR_EXTENSION = ".wsdcf"  # DMM's encrypted VR files
VR_DECRYPTED = ".mp4"  # jav-it writes VR as MP4, and VR players prefer it

_ids_lock = threading.Lock()  # IDS_FILE is shared by the videos running at the same time


class VideoFailed(Exception):
    """This video can't be finished (the reason is the message)."""


@dataclass
class Tools:
    javit: Path
    mediainfo: Path
    merge: merge_tools.Tools | None = None  # ffmpeg + ffprobe, only needed to join parts
    # Why VR videos can't be decrypted here (they are then saved encrypted, with this as the note), "" when they can.
    # None: decided from the files (VR.MOD next to jav-it, MediaInfo): decrypt.vr_problem().
    no_vr: str | None = None
    # Why 2D videos can't be decrypted here (jav-it or DECRYPT.MOD or MediaInfo missing): they fail with this, VR
    # videos still go on. "" when they can.
    no_2d: str = ""

    def __post_init__(self) -> None:
        if self.no_vr is None:
            self.no_vr = decrypting.vr_problem(self.javit, self.mediainfo)


class Slots:
    """Limits shared by videos running at the same time (the browser page runs several): downloads at once, VR
    downloads at once (in tests DMM allowed 4 videos at once, but VR only one), jav-it runs at once."""

    def __init__(self, downloads: int = 4, vr_downloads: int = 1, decrypts: int = 1) -> None:
        self._downloads = threading.BoundedSemaphore(max(1, downloads))
        self._vr = threading.BoundedSemaphore(max(1, vr_downloads))
        self._decrypts = threading.BoundedSemaphore(max(1, decrypts))

    @contextmanager
    def download(self, vr: bool = False):
        with self._vr if vr else nullcontext(), self._downloads:
            yield

    @contextmanager
    def decrypt(self):
        with self._decrypts:
            yield


@dataclass
class Options:
    out_dir: Path
    source: str = "auto"
    rate: str | None = None
    merge: bool = True
    keep_dcv: bool = False
    overwrite: bool = False
    retries: int = 5
    javit_email: str = ""
    retry_delay: float = 10.0
    fetch_helper: list[str] | None = None  # the separate download program for the file itself (VPN split tunnelling)
    slots: Slots | None = None  # shared limits when several videos run at once

    def download_slot(self, vr: bool = False):
        return self.slots.download(vr) if self.slots else nullcontext()

    def decrypt_slot(self):
        return self.slots.decrypt() if self.slots else nullcontext()


@dataclass
class Result:
    cid: str
    ok: bool
    message: str
    files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False
    encrypted: bool = False  # VR saved encrypted (.wsdcf): jav-it with VR.MOD wasn't there


class Report:
    """What the pipeline tells the user; the terminal and the window show it their own way."""

    def step(self, text: str) -> None: ...

    def progress(self, done: float, total: float | None, unit: str = "bytes") -> None: ...

    def end_progress(self) -> None: ...

    def found(self, choice: links.Choice) -> None:
        """The video now comes from another source than looked up (Premium refused it: the library)."""

    def named(self, file_id: str) -> None:
        """DMM's id inside the files is known: the result is saved under it."""


def existing_outputs(out_dir: Path, cid: str, wsdcf: bool = True) -> list[Path]:
    """A video's results in the output folder: <cid>.mkv (2D) or <cid>.mp4 (VR), or their parts <cid>-ptN; with
    `wsdcf`, also a VR video saved encrypted (<cid>.wsdcf, <cid>-ptN.wsdcf). Without it (VR is decrypted here) an
    encrypted VR file isn't a result: it is decrypted when the video is added again (saved_vr_parts)."""
    for extensions in ((".mkv", VR_DECRYPTED), (VR_EXTENSION,) if wsdcf else ()):
        for extension in extensions:
            single = out_dir / ids.output_name(cid, extension=extension)
            if single.exists():
                return [single]
        parts = sorted(path for extension in extensions for path in out_dir.glob(f"{cid}-pt*{extension}"))
        if parts:
            return parts
    return []


def unfinished(out_dir: Path, cid: str) -> bool:
    """Whether a run of `cid` (the id asked for) left its state in the work folder: it stopped before the end, or
    its result wasn't saved."""
    return (out_dir / WORK_FOLDER / cid / "state.json").is_file()


def saved_files(out_dir: Path, cids, wsdcf: bool = True, asked: str | None = None) -> list[Path]:
    """A video's results already in the output folder, under any of its ids (the one asked for, DMM's). Parts
    (<id>-ptN) don't count while a run of the video asked for (`asked`, else the first id) is unfinished: it may have
    saved only some of them."""
    cids = list(dict.fromkeys(c for c in cids if c))
    asked = asked or (cids[0] if cids else None)
    for cid in cids:
        found = existing_outputs(out_dir, cid, wsdcf)
        if found and found[0].stem != cid and asked and unfinished(out_dir, asked):
            continue
        if found:
            return found
    return []


def vr_name(cid: str, urls: list[str], index: int, extension: str) -> str:
    """The file name of VR part `index` (1, 2, ...): <cid><extension> for a video in one part, else <cid>-ptN with
    DMM's part number N (a quality missing a part at DMM then shows as a gap: -pt1, -pt3)."""
    return ids.output_name(cid, links.part_numbers(urls)[index - 1] if len(urls) > 1 else None, extension)


def vr_copies(out_dir: Path, cid: str) -> list[Path]:
    """The encrypted VR files of `cid` in the output folder (<cid>.wsdcf, <cid>-ptN.wsdcf), whatever their parts."""
    single = out_dir / ids.output_name(cid, extension=VR_EXTENSION)
    return sorted(([single] if single.is_file() else []) + [p for p in out_dir.glob(f"{cid}-pt*{VR_EXTENSION}")
                                                           if p.is_file()])


def saved_vr_parts(out_dir: Path, cid: str, urls: list[str]) -> dict[int, Path]:
    """Encrypted VR parts saved in the output folder earlier (<cid>.wsdcf, <cid>-ptN.wsdcf, as DMM numbers the parts
    in `urls`): part index -> file. They are decrypted instead of downloaded again, but only all together, when the
    saved files are exactly the parts of this choice: other files (another quality's parts, a part missing) may be
    another encode, which is never mixed with new downloads (vr_copies: what is there then)."""
    expected = {vr_name(cid, urls, index, VR_EXTENSION): index for index in range(1, len(urls) + 1)}
    try:
        there = {path.name: path for path in vr_copies(out_dir, cid)}
        if set(there) != set(expected) or not all(path.stat().st_size > 0 for path in there.values()):
            return {}
    except OSError:
        return {}
    return {index: there[name] for name, index in expected.items()}


def known_dmm_ids(out_dir: Path) -> dict[str, str]:
    """Ids asked for -> DMM's id inside their files, where they differ, as earlier runs read them (IDS_FILE)."""
    data = _load_state(out_dir / WORK_FOLDER / IDS_FILE)
    return {asked: file_id for asked, file_id in data.items()
            if isinstance(file_id, str) and ids.CID_RE.fullmatch(asked) and ids.CID_RE.fullmatch(file_id)}


def known_dmm_id(out_dir: Path, cid: str) -> str | None:
    """DMM's id for `cid` when a run learned it from the files: in the video's work folder while it isn't done,
    and in IDS_FILE (kept after the work folder is gone)."""
    value = _load_state(out_dir / WORK_FOLDER / cid / "state.json").get("file_id")
    if isinstance(value, str) and ids.CID_RE.fullmatch(value):
        return value
    return known_dmm_ids(out_dir).get(cid)


def _remember_dmm_id(out_dir: Path, cid: str, file_id: str) -> None:
    """Keeps DMM's id for `cid` (what DMM wrote into this video's files) for the next runs: "already saved" then
    finds the result under it without downloading the video again."""
    path = out_dir / WORK_FOLDER / IDS_FILE
    with _ids_lock:
        known = known_dmm_ids(out_dir)
        if known.get(cid) == (file_id if file_id != cid else None):
            return
        if file_id == cid:
            known.pop(cid, None)
        else:
            known[cid] = file_id
        tmp = path.with_name(f"{IDS_FILE}.{os.getpid()}.tmp")  # another copy of the program may write it too
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if known:
                tmp.write_text(json.dumps(known, indent=1, sort_keys=True), encoding="utf-8")
                os.replace(tmp, path)
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:  # then the id is only known while the work folder is there
            log.warning("could not write %s: %s", path, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _leftovers(out_dir: Path, cid: str) -> list[str]:
    """A note when a download of this video is still in its work folder (one that wasn't saved over a file)."""
    work = out_dir / WORK_FOLDER / cid
    if any(work.glob("part*")):
        return [f"a download of it that wasn't saved is still in {work}: delete that folder if you don't need it"]
    return []


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _space_to_decrypt(dcv: Path, mkv: Path) -> Claim:
    """jav-it writes a file about as big as the encrypted one (into the work folder): its space is booked with the
    downloads running (fetch.SPACE), so a download starting meanwhile counts it, and it counts theirs."""
    size = dcv.stat().st_size

    def left() -> int:
        try:
            return size - mkv.stat().st_size
        except OSError:
            return size

    try:
        return SPACE.claim(mkv.parent, left, "decrypt")
    except NoSpace as exc:
        raise VideoFailed(str(exc)) from None


def _place(source: Path, target: Path, replace: bool = False) -> Path:
    """Move a finished file to its place. An existing file is only replaced with `replace` (--overwrite, "Download
    again videos already saved"); otherwise FileExistsError, and the new file stays where it is."""
    if replace:
        os.replace(source, target)
        return target
    try:
        if os.name == "nt":
            os.rename(source, target)  # Windows refuses when the target exists
        else:
            try:
                os.link(source, target)  # refuses when the target exists (rename would replace it)
            except FileExistsError:
                raise
            except OSError:  # a file system without hard links
                if target.exists():
                    raise FileExistsError from None
                os.rename(source, target)
            else:
                source.unlink()
    except FileExistsError:
        raise FileExistsError(errno.EEXIST, "already exists", str(target)) from None
    return target


def _clash(existing: Path, work: Path, why: str = "") -> VideoFailed:
    return VideoFailed(f"not saved: {existing} is already there{f' ({why})' if why else ''}. The new download stays "
                       f"in {work}: compare the two and keep one, or tick \"Download again videos already saved\" "
                       f"(--overwrite) to replace it")


def process(cid: str, session: DmmSession, login: Login | None, tools: Tools, opts: Options, report: Report,
            choice: links.Choice | None = None) -> Result:
    """One video; `choice`: a lookup made before (the page looks videos up when they are added)."""
    try:
        return _process(cid, session, login, tools, opts, report, choice)
    except (VideoFailed, links.NoLinks, download.DownloadError, decrypting.DecryptError, mediainfo.MediaInfoError,
            DmmError) as exc:
        log.warning("%s failed: %s", cid, exc)
        return Result(cid, False, str(exc))


def _process(cid: str, session: DmmSession, login: Login | None, tools: Tools, opts: Options, report: Report,
             choice: links.Choice | None = None) -> Result:
    out_dir = opts.out_dir
    # "Already saved": under the id asked for, or DMM's id for it when known (an earlier run read it in the files).
    # A VR video saved encrypted earlier isn't, when VR is decrypted here: those files are decrypted now.
    existing = saved_files(out_dir, [cid, known_dmm_id(out_dir, cid)], wsdcf=bool(tools.no_vr))
    if existing and not opts.overwrite:
        return Result(cid, True, "already downloaded", existing, skipped=True, warnings=_leftovers(out_dir, cid))

    if choice is None:
        report.step("looking for the download on DMM")
        choice = links.find(session, cid, opts.source, opts.rate)
    elif opts.rate and opts.rate != choice.label:
        choice = choice.with_label(opts.rate)
    existing = saved_files(out_dir, [choice.page_cid], wsdcf=bool(tools.no_vr), asked=cid)  # the Premium page's id
    if existing and not opts.overwrite:
        return Result(cid, True, f"already downloaded as {choice.page_cid}", existing, skipped=True)
    tried: list[str] = []
    while True:
        if not choice.vr and tools.no_2d:  # before anything is downloaded
            raise VideoFailed(tools.no_2d)
        try:
            return _video(cid, choice, session, login, tools, opts, report)
        except download.NotAllowed as exc:
            # "auto": DMM listed the video on Premium or in the VR Channel, but its link isn't for this account (no
            # subscription, or not this video): the next source (the VR Channel, the library) may still have it.
            if opts.source != "auto" or choice.source == "library":
                raise
            tried.append(choice.source)
            name = links.SOURCE_NAMES.get(choice.source, choice.source)
            log.info("%s: %s refused (%s); looking further", cid, name, exc)
            try:
                choice = links.find(session, cid, "auto", opts.rate, skip=tuple(tried))
            except links.NoLinks as reason:
                raise VideoFailed(f"{exc}; {reason}") from exc
            report.step(f"{name} didn't give the download to this account; trying "
                        f"{links.SOURCE_NAMES.get(choice.source, choice.source)}")
            report.found(choice)


def _video(cid: str, choice: links.Choice, session: DmmSession, login: Login | None, tools: Tools, opts: Options,
           report: Report) -> Result:
    """Download, decrypt, check and save one video from `choice` (VR without jav-it's VR.MOD: only download it),
    alone in its work folder: another copy of the program, or another row of the page, can't work on the same video
    meanwhile."""
    work = opts.out_dir / WORK_FOLDER / cid
    work.mkdir(parents=True, exist_ok=True)
    try:
        lock = worklock.acquire(work, cid)
    except worklock.Busy as exc:
        raise VideoFailed(str(exc)) from None
    with lock:
        existing = saved_files(opts.out_dir, [cid, known_dmm_id(opts.out_dir, cid), choice.page_cid], bool(tools.no_vr))
        if existing and not opts.overwrite:  # the run that held the lock just saved it
            return Result(cid, True, "already downloaded", existing, skipped=True)
        return _video_locked(cid, choice, work, session, login, tools, opts, report)


def _video_locked(cid: str, choice: links.Choice, work: Path, session: DmmSession, login: Login | None, tools: Tools,
                  opts: Options, report: Report) -> Result:
    out_dir = opts.out_dir
    count = len(choice.urls)
    warnings = []
    if choice.page_cid:
        warnings.append(f"DMM shows this video as {choice.page_cid}")
    report.step(f"{links.source_word(choice.source)}, {'VR, ' if choice.vr else ''}quality {choice.label}, {count} part(s)")

    state_path = work / "state.json"
    state = _load_state(state_path)
    version = {"source": choice.source, "label": choice.label, "parts": count}
    if {k: state.get(k) for k in version} != version:
        if state:
            report.step("a different version than last time: starting this video again")
        for old in work.glob("part*"):
            old.unlink()
        state = dict(version, cid=cid, decrypted=[])
        _save_state(state_path, state)

    if choice.vr and tools.no_vr:
        return _vr_encrypted(cid, choice, work, session, opts, report, warnings, tools.no_vr)
    if choice.vr:
        return _vr_decrypted(cid, choice, work, state, session, login, tools, opts, report, warnings)

    mkvs: list[Path] = []
    for number, url in enumerate(choice.urls, 1):
        label = f"{cid} part {number}/{count}" if count > 1 else cid
        dcv = work / f"part{number}.dcv"
        mkv = work / f"part{number}.mkv"
        if number in state["decrypted"] and mkv.exists():
            report.step(f"part {number}/{count}: already decrypted")
            mkvs.append(mkv)
            continue
        # A bad download, or a decrypted file that fails the checks, is downloaded once more (round 2).
        for round_ in (1, 2):
            if not dcv.exists():
                report.step(f"part {number}/{count}: downloading" + (" again" if round_ == 2 else ""))
                with opts.download_slot():
                    download.download_part(session, url, dcv, label, opts.retries,
                                           lambda done, total: report.progress(done, total), opts.retry_delay,
                                           helper=opts.fetch_helper)
                report.end_progress()
            report.step(f"part {number}/{count}: decrypting with jav-it")
            try:
                with opts.decrypt_slot(), _space_to_decrypt(dcv, mkv):
                    decrypting.decrypt(tools.javit, tools.mediainfo, dcv, mkv, login, opts.javit_email,
                                       retry_delay=opts.retry_delay)
                break
            except decrypting.DecryptError as exc:
                if not exc.download_again:
                    raise
                if round_ == 2:
                    raise VideoFailed(f"{label}: {exc}, also after downloading it again (jav-it or DMM's file may "
                                      "be at fault: try again later)") from exc
                report.step(f"part {number}/{count}: {exc}; downloading it again and decrypting once more")
                dcv.unlink(missing_ok=True)
        state["decrypted"].append(number)
        _save_state(state_path, state)
        mkvs.append(mkv)

    # DMM's own video id and part number inside each file decide the name and the order.
    tracks = [mediainfo.read(mkv, tools.mediainfo) for mkv in mkvs]
    comments = [mediainfo.dmm_comment(t) for t in tracks]
    file_ids = {fid for fid, _ in comments if fid}
    if len(file_ids) > 1:
        raise VideoFailed(f"the parts belong to different videos ({', '.join(sorted(file_ids))})")
    file_id = file_ids.pop() if file_ids else cid
    if file_id != cid:
        warnings.append(f"saved as {file_id}, the id DMM wrote into the file")
    if state.get("file_id") != file_id:  # remembered: "already saved" also looks for this id next time
        state["file_id"] = file_id
        _save_state(state_path, state)
    if any(fid for fid, _ in comments):  # DMM's own word for this id (also after the work folder is gone)
        _remember_dmm_id(out_dir, cid, file_id)
    report.named(file_id)
    numbers = [part or n for n, (_, part) in enumerate(comments, 1)]
    if sorted(numbers) != list(range(1, count + 1)):
        raise VideoFailed(f"the files carry part numbers {sorted(numbers)}, but DMM offers {count} part(s)")
    ordered = [mkv for _, mkv in sorted(zip(numbers, mkvs))]
    out_dir.mkdir(parents=True, exist_ok=True)

    # "Keep the encrypted downloads": next to the result, <id>.dcv or <id>-ptN.dcv in DMM's part order.
    dcvs = [(mkv.with_suffix(".dcv"), out_dir / ids.output_name(file_id, n if count > 1 else None, ".dcv"))
            for n, mkv in enumerate(ordered, 1)] if opts.keep_dcv else []

    # An existing file is never replaced unless asked (--overwrite): e.g. one saved earlier under DMM's id while
    # this video was asked for with another one.
    existing = existing_outputs(out_dir, file_id) + [target for _, target in dcvs if target.exists()]
    if existing and not opts.overwrite:
        raise _clash(existing[0], work, f"DMM's id inside the new download is {file_id}")
    try:
        if count == 1:
            files = [_place(ordered[0], out_dir / ids.output_name(file_id), opts.overwrite)]
        else:
            files = _multi_part(file_id, ordered, tracks, tools, opts, report, warnings)
        kept = [_place(dcv, target, opts.overwrite) for dcv, target in dcvs if dcv.exists()]
    except FileExistsError as exc:  # appeared meanwhile (another copy of the program)
        raise _clash(Path(exc.filename or out_dir), work, f"DMM's id inside the new download is {file_id}") from exc
    if kept:
        warnings.append("encrypted downloads kept: " + ", ".join(f.name for f in kept))

    _remove_work(work, out_dir)
    message = f"{links.source_word(choice.source)}, {choice.label}: " + ", ".join(f.name for f in files)
    return Result(cid, True, message, files, warnings)


def _remove_work(work: Path, out_dir: Path) -> None:
    shutil.rmtree(work, ignore_errors=True)
    try:
        (out_dir / WORK_FOLDER).rmdir()  # only when no other video is left in it
    except OSError:
        pass


def _numbering_note(numbers: list[int], warnings: list[str]) -> None:
    if len(numbers) > 1 and numbers != list(range(1, len(numbers) + 1)):
        warnings.append(f"DMM offers parts {', '.join(map(str, numbers))} of this video in this quality: the files "
                        "are named by DMM's part numbers (a part may be missing at DMM)")


def _download_vr_part(session: DmmSession, url: str, target: Path, label: str, step: str, opts: Options,
                      report: Report, referer: str = "", japan: bool = False) -> None:
    with opts.download_slot(vr=True):
        download.download_part(session, url, target, label, opts.retries, lambda done, total: report.progress(done, total),
                               opts.retry_delay, notice=lambda text: report.step(f"{step}: {text}"),
                               helper=opts.fetch_helper, binary_only=True, referer=referer,
                               japan=japan)
    report.end_progress()


def _vr_encrypted(cid: str, choice: links.Choice, work: Path, session: DmmSession, opts: Options, report: Report,
                  warnings: list[str], why: str) -> Result:
    """VR without jav-it's VR.MOD (`why`): the encrypted parts only (<cid>.wsdcf, <cid>-pt1.wsdcf, ...)."""
    count = len(choice.urls)
    for number, url in enumerate(choice.urls, 1):
        part_file = work / f"part{number}{VR_EXTENSION}"
        if part_file.exists() and download.looks_like_text(part_file):  # a page saved as a part before: again
            part_file.unlink()
        if part_file.exists():
            report.step(f"part {number}/{count}: already downloaded")
            continue
        report.step(f"part {number}/{count}: downloading")
        _download_vr_part(session, url, part_file, f"{cid} part {number}/{count}" if count > 1 else cid,
                          f"part {number}/{count}", opts, report, choice.referer, choice.japan)
    _numbering_note(links.part_numbers(choice.urls), warnings)
    opts.out_dir.mkdir(parents=True, exist_ok=True)
    existing = existing_outputs(opts.out_dir, cid)
    if existing and not opts.overwrite:
        raise _clash(existing[0], work)
    try:
        files = [_place(work / f"part{n}{VR_EXTENSION}", opts.out_dir / vr_name(cid, choice.urls, n, VR_EXTENSION),
                        opts.overwrite)
                 for n in range(1, count + 1)]
    except FileExistsError as exc:
        raise _clash(Path(exc.filename or opts.out_dir), work) from exc
    _remove_work(work, opts.out_dir)
    warnings.append(f"VR video saved encrypted (.wsdcf), not decrypted: {why}")
    names = ", ".join(f.name for f in files)
    return Result(cid, True, f"{links.source_word(choice.source)}, VR {choice.label}: {names}", files, warnings,
                  encrypted=True)


def _vr_decrypted(cid: str, choice: links.Choice, work: Path, state: dict, session: DmmSession, login: Login | None,
                  tools: Tools, opts: Options, report: Report, warnings: list[str]) -> Result:
    """VR with jav-it ("dmm-vr", VR.MOD): every part downloaded, or taken from the encrypted copies saved in the
    output folder earlier (<cid>.wsdcf, <cid>-ptN.wsdcf: all parts of this choice, saved_vr_parts), decrypted into an
    .mp4 and checked. VR parts are never joined: <cid>.mp4 for a video in one part, else <cid>-ptN.mp4 with DMM's part
    numbers. Named by the id asked for."""
    out_dir, state_path = opts.out_dir, work / "state.json"
    count = len(choice.urls)
    numbers = links.part_numbers(choice.urls)
    saved = saved_vr_parts(out_dir, cid, choice.urls)  # all of this choice's parts, or nothing
    pages = [path.name for path in saved.values() if download.looks_like_text(path)]
    if pages:  # a web page saved as a part by an old version: not a video, so that set isn't used
        warnings.append(f"{', '.join(pages)}: a web page or text, not a video")
        saved = {}
    if saved:
        report.step(f"decrypting the encrypted file(s) saved earlier ({', '.join(p.name for p in saved.values())})")
    # Encrypted files of this video saved earlier that aren't this choice's parts (another quality, a part missing):
    # never mixed with new downloads, never removed.
    others = [] if saved else vr_copies(out_dir, cid)
    if others:
        names = {vr_name(cid, choice.urls, index, VR_EXTENSION) for index in range(1, count + 1)}
        taken = [path for path in others if path.name in names]
        if taken and opts.keep_dcv and not opts.overwrite:  # before anything is downloaded
            raise VideoFailed(f"not started: {taken[0]} is already there (an encrypted part saved earlier, not of this "
                              f"quality), and \"Keep the encrypted downloads\" (--keep-dcv) would save this download's "
                              f"part under that name. Move it away, or tick \"Download again videos already saved\" "
                              f"(--overwrite) to replace it")
        warnings.append(f"not used, left as they are: {', '.join(p.name for p in others)} (saved earlier, but not "
                        f"exactly the parts of {choice.label}, {count} part(s))")
    # A part whose decrypted file fails the checks is downloaded again, once per video: each VR download asks DMM for a
    # new link, and DMM limits how often one VR video is downloaded.
    downloads_again = 1
    where = (f"The encrypted parts are kept and used again next time (downloads in {work}"
             + (", the files saved earlier in the output folder)" if saved else ")"))
    mp4s: list[Path] = []
    for index, url in enumerate(choice.urls, 1):
        label = f"{cid} part {index}/{count}" if count > 1 else cid
        step = f"part {index}/{count}"
        downloaded = work / f"part{index}{VR_EXTENSION}"
        mp4 = work / f"part{index}{VR_DECRYPTED}"
        if index in state["decrypted"] and mp4.exists():
            report.step(f"{step}: already decrypted")
            mp4s.append(mp4)
            continue
        if downloaded.exists() and download.looks_like_text(downloaded):  # a page saved as a part before: again
            downloaded.unlink()
        use_saved = True
        for round_ in (1, 2):
            source = downloaded if downloaded.exists() else saved.get(index) if use_saved else None
            if source is None:
                report.step(f"{step}: downloading" + (" again" if round_ == 2 else ""))
                _download_vr_part(session, url, downloaded, label, step, opts, report, choice.referer, choice.japan)
                source = downloaded
            report.step(f"{step}: decrypting with jav-it" + ("" if source == downloaded else f" ({source.name})"))
            try:
                with opts.decrypt_slot(), _space_to_decrypt(source, mp4):
                    decrypting.decrypt(tools.javit, tools.mediainfo, source, mp4, login, opts.javit_email,
                                       retry_delay=opts.retry_delay, vr=True)
                break
            except decrypting.DecryptError as exc:
                if not exc.download_again:  # jav-it itself failed (licence, time limit, ...): no new download
                    raise VideoFailed(f"{label}: {exc}. {where}") from exc
                if round_ == 2:
                    raise VideoFailed(f"{label}: {exc}, also after downloading it again (jav-it or DMM's file may be at "
                                      f"fault: try again later). {where}") from exc
                if not downloads_again:
                    raise VideoFailed(f"{label}: {exc}. Not downloaded again: another part of this VR video already "
                                      f"was, and DMM limits how often one VR video is downloaded (try again later). "
                                      f"{where}") from exc
                downloads_again -= 1
                report.step(f"{step}: {exc}; downloading it again and decrypting once more")
                if source == downloaded:
                    downloaded.unlink(missing_ok=True)
                use_saved = False  # a copy saved earlier is replaced by the new download when it is kept
        state["decrypted"].append(index)
        _save_state(state_path, state)
        mp4s.append(mp4)

    # One video: DMM's id inside the files, when jav-it kept it, must be the same in every part.
    tracks = [mediainfo.read(mp4, tools.mediainfo) for mp4 in mp4s]
    file_ids = {fid for fid, _ in map(mediainfo.dmm_comment, tracks) if fid}
    if len(file_ids) > 1:
        raise VideoFailed(f"the parts belong to different videos ({', '.join(sorted(file_ids))})")
    _numbering_note(numbers, warnings)
    out_dir.mkdir(parents=True, exist_ok=True)

    # The encrypted file each part came from: this run's download, else the copy saved earlier.
    sources = {}
    for index in range(1, count + 1):
        downloaded = work / f"part{index}{VR_EXTENSION}"
        source = downloaded if downloaded.exists() else saved.get(index)
        if source is not None and source.exists():
            sources[index] = source
    # "Keep the encrypted downloads": next to the result, <cid>.wsdcf or <cid>-ptN.wsdcf (DMM's part numbers). A copy
    # saved earlier stays where it is, or is replaced by this run's download of the same part.
    keep = [(sources[i], out_dir / vr_name(cid, choice.urls, i, VR_EXTENSION)) for i in sources] if opts.keep_dcv else []
    earlier = set(saved.values())
    existing = existing_outputs(out_dir, cid, wsdcf=False) + [
        target for source, target in keep if target != source and target.exists() and target not in earlier]
    if existing and not opts.overwrite:
        raise _clash(existing[0], work)
    try:
        files = [_place(mp4, out_dir / vr_name(cid, choice.urls, index, VR_DECRYPTED), opts.overwrite)
                 for index, mp4 in enumerate(mp4s, 1)]
        kept = [target if source == target else _place(source, target, opts.overwrite or target in earlier)
                for source, target in keep]
    except FileExistsError as exc:  # appeared meanwhile (another copy of the program)
        raise _clash(Path(exc.filename or out_dir), work) from exc
    if kept:
        warnings.append("encrypted downloads kept: " + ", ".join(f.name for f in kept))
    if saved and not opts.keep_dcv:  # the copies saved earlier go too, now that they are decrypted
        removed = []
        for path in saved.values():
            try:
                path.unlink()
                removed.append(path.name)
            except FileNotFoundError:
                pass
            except OSError as exc:
                warnings.append(f"{path.name} could not be removed ({exc.strerror or exc})")
        if removed:
            warnings.append("decrypted from the encrypted files saved earlier, which were removed (\"Keep the "
                            "encrypted downloads\" is off): " + ", ".join(removed))
    _remove_work(work, out_dir)
    names = ", ".join(f.name for f in files)
    return Result(cid, True, f"{links.source_word(choice.source)}, VR {choice.label}: {names}", files, warnings)


def _keep_parts(file_id: str, ordered: list[Path], opts: Options) -> list[Path]:
    return [_place(path, opts.out_dir / ids.output_name(file_id, n), opts.overwrite)
            for n, path in enumerate(ordered, 1)]


def _multi_part(file_id: str, ordered: list[Path], tracks: list[list[dict]], tools: Tools, opts: Options,
                report: Report, warnings: list[str]) -> list[Path]:
    """2D parts in order: joined into <file_id>.mkv when they can be, else kept as <file_id>-ptN.mkv. (VR parts are
    never joined: `_vr_decrypted`.)"""
    if any("vp9" in mediainfo.video_format(t).lower() for t in tracks):
        warnings.append("VP9 video: the parts stay separate files")
        return _keep_parts(file_id, ordered, opts)
    if not opts.merge:
        return _keep_parts(file_id, ordered, opts)
    if tools.merge is None:
        warnings.append("ffmpeg/ffprobe not found, so the parts were not joined (install FFmpeg to have them joined)")
        return _keep_parts(file_id, ordered, opts)

    try:
        parts = [probing.probe(path, tools.merge.ffprobe) for path in ordered]
    except probing.ProbeError as exc:  # like any failed join: the parts are kept
        warnings.append(f"parts not joined: ffprobe could not read them ({exc})")
        return _keep_parts(file_id, ordered, opts)
    work = ordered[0].parent
    group = Group(name=file_id, folder=work, parts=parts, numbers=list(range(1, len(parts) + 1)), from_comment=True)
    # Joined in the video's own work folder (FFmpeg's unfinished file too: two videos never share a name), then moved
    # into the output folder like any result. A joined file left there by an earlier run is this video's own.
    item = planning.plan_group(group, work / "joined", "mkv", overwrite=True)
    if item.action != "merge":
        warnings.append(f"parts not joined: {item.reason}")
        return _keep_parts(file_id, ordered, opts)
    warnings.extend(item.warnings)
    report.step(f"joining {len(parts)} parts")
    result = merging.merge(item, tools.merge, on_progress=lambda f: report.progress(f, 1.0, "fraction"))
    report.end_progress()
    if not result.ok:
        warnings.append(f"joining the parts failed ({result.message}); the parts were kept")
        return _keep_parts(file_id, ordered, opts)
    return [_place(result.output, opts.out_dir / ids.output_name(file_id), opts.overwrite)]
