"""Decrypting one part with jav-it, and checking the result.

jav-it is a separate program (from its developer's Patreon) with its modules next to it: DECRYPT.MOD for 2D
videos (`jav-it decrypt -i <.dcv> -o <.mkv> -t dmm`), VR.MOD for VR videos (`-t dmm-vr`, a .wsdcf into an .mp4).
It runs with your DMM login in its environment (DMM_USERNAME / DMM_PASSWORD), with no keyboard input (so it can
never wait forever for an answer), and a time limit. When it asks for the Patreon email to validate your
membership, that email is answered for it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

from . import children, mediainfo
from .credentials import Login
from .merging import tools

log = logging.getLogger(__name__)

SUCCESS_BANNER = "[ ok ] decryption complete!"
CONNECTION_ERROR = "no internet connection"
LICENSE_MARKERS = ("patreon email address:", "please validate your identity", "validate your identity")
# Older jav-it versions decrypted VR only with the licence DMM VR Player keeps after playing a file once, and said
# so ("... the user has not been authenticated", "... correct licence information ..."): trying again won't help.
VR_LICENCE_MARKERS = ("licence information", "license information", "has not been authenticated")
DURATION_TOLERANCE = 30.0
TIMEOUT = 7200
# VR: a decrypt took seconds (511 MB in 7 s), so a hang shouldn't block for hours: 5 minutes plus 1 second per 5 MB.
VR_TIMEOUT_BASE = 300.0
VR_TIMEOUT_BYTES_PER_SECOND = 5_000_000
DECRYPT_MODULE = "DECRYPT.MOD"  # jav-it's module for 2D videos
VR_MODULE = "VR.MOD"  # jav-it's module for VR videos
AUTH_FILE = "auth.json"  # jav-it's own sign-in (tokens), written in the folder it runs in: we start it in its own
ACCOUNT_FILE = "jav-it-account"  # in our settings folder: which login and jav-it the sign-in above was made with


def vr_timeout(size: int) -> float:
    return VR_TIMEOUT_BASE + size / VR_TIMEOUT_BYTES_PER_SECOND


def _duration(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= 120 else f"{seconds:.0f} s"


def forget_javit_login(javit: Path | None, state_dir: Path) -> None:
    """jav-it keeps its own sign-in next to itself (auth.json). Removed on log out and when the DMM login changes, so
    jav-it signs in again with the login it is given (DMM_USERNAME, DMM_PASSWORD) instead of an old account's tokens."""
    for path in ([javit.parent / AUTH_FILE] if javit else []) + [state_dir / ACCOUNT_FILE]:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not remove %s: %s", path, exc)


def match_javit_login(javit: Path | None, login: Login | None, state_dir: Path) -> None:
    """Before jav-it runs: when its sign-in was made with another login or another jav-it (or nobody knows), it goes."""
    if javit is None or login is None:
        return
    key = f"{login.username.strip().casefold()}\n{str(javit.parent.resolve()).casefold()}"
    wanted = hashlib.sha256(key.encode("utf-8")).hexdigest()  # not the email itself: nothing to read in the file
    marker = state_dir / ACCOUNT_FILE
    try:
        if marker.read_text(encoding="utf-8").strip() == wanted:
            return
    except OSError:
        pass
    forget_javit_login(javit, state_dir)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text(wanted + "\n", encoding="utf-8")
    except OSError as exc:  # then jav-it signs in again at every run: slower, still right
        log.warning("could not write %s: %s", marker, exc)


def vr_problem(javit: Path | None, mediainfo_path: Path | None) -> str:
    """Why VR videos can't be decrypted here (they are then saved encrypted, and this says why), or "" when they can:
    jav-it with VR.MOD next to it, and MediaInfo to check the result."""
    if javit is None:
        return "jav-it was not found"
    if not (javit.parent / VR_MODULE).exists():
        return f"{VR_MODULE} is not next to jav-it ({javit.parent})"
    if mediainfo_path is None:
        return "MediaInfo was not found (it checks the decrypted file)"
    return ""


class DecryptError(Exception):
    def __init__(self, message: str, corrupt_source: bool = False, bad_output: bool = False) -> None:
        super().__init__(message)
        self.corrupt_source = corrupt_source  # the .dcv itself is bad: download it again
        # jav-it's file failed the checks: the .dcv may be bad in a way MediaInfo can't see, so download it again too
        self.bad_output = bad_output

    @property
    def download_again(self) -> bool:
        return self.corrupt_source or self.bad_output


class NeedsVrLicence(DecryptError):
    """jav-it wants DMM VR Player's licence for this VR file: trying again (or downloading again) won't help."""


def required_ratio(size: int) -> float:
    """Smallest decrypted/encrypted size ratio that is still a complete file."""
    mb = 1024 * 1024
    if size < 500 * mb:
        return 0.95
    if size < 1024 * mb:
        return 0.96
    if size < 1536 * mb:
        return 0.97
    return 0.99


def _environment(login: Login | None, temp: Path) -> dict[str, str]:
    env = dict(os.environ)
    if login:
        env["DMM_USERNAME"] = login.username
        env["DMM_PASSWORD"] = login.password
    temp.mkdir(parents=True, exist_ok=True)
    for key in ("TMP", "TEMP", "TMPDIR"):
        env[key] = str(temp)  # jav-it's temporary files stay in the work folder
    return env


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_bytes()[-limit:]
    except OSError:
        return ""
    return data.decode("utf-8", "replace")


def _run(javit: Path, dcv: Path, out: Path, env: dict, answer: str | None, logs: Path, timeout: float,
         kind: str = "dmm") -> tuple[int, str]:
    cmd = [str(javit), "decrypt", "-i", str(dcv), "-o", str(out), "-t", kind]
    log.info("running: %s", " ".join(cmd))
    with open(logs, "wb") as output:
        process = children.Popen(  # ends with the downloader, also when its window is closed
            cmd,
            cwd=str(javit.parent),  # jav-it finds its modules (DECRYPT.MOD, VR.MOD) next to itself
            env=env,
            stdin=subprocess.PIPE if answer else subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=tools.NO_WINDOW,
            start_new_session=os.name != "nt",
        )
        try:
            process.communicate(answer.encode() if answer else None, timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop_tree(process)
            raise
    return process.returncode, _tail(logs)


def _stop_tree(process: subprocess.Popen) -> None:
    """Stop jav-it and the helper process it may start (a plain kill would leave the helper running)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True,
                           creationflags=tools.NO_WINDOW, timeout=60)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
        process.wait(timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def verify(dcv: Path, out: Path, mediainfo_path: Path, vr: bool = False) -> None:
    """The decrypted file is complete: size, MediaInfo errors, length and DMM's part data. A failed check means
    bad_output: the part is downloaded again (pipeline.py). VR: size, damage and length only (whether jav-it keeps
    DMM's tags in a VR file isn't known, and a false alarm would cost a VR download); MediaInfo may not read the
    encrypted .wsdcf, then only the decrypted file is checked (its size against the download's, its damage, that it
    has a length)."""
    if not out.exists() or out.stat().st_size == 0:
        raise DecryptError("jav-it reported success but wrote no file", bad_output=True)
    size_in, size_out = dcv.stat().st_size, out.stat().st_size
    if size_out / size_in < required_ratio(size_in):
        raise DecryptError(f"the decrypted file is too small ({size_out} of {size_in} bytes)", bad_output=True)
    try:
        source = mediainfo.read(dcv, mediainfo_path)
    except mediainfo.MediaInfoError as exc:
        if not vr:
            raise DecryptError(f"the decrypted file can't be checked ({exc})", bad_output=True) from exc
        source = []
    try:
        result = mediainfo.read(out, mediainfo_path)
    except mediainfo.MediaInfoError as exc:
        raise DecryptError(f"the decrypted file can't be checked ({exc})", bad_output=True) from exc
    damage = mediainfo.problem(result)
    if damage:
        raise DecryptError(f"the decrypted file is damaged: {damage}", bad_output=True)
    length_in, length_out = mediainfo.duration(source), mediainfo.duration(result)
    if vr and length_in is None and length_out:
        return  # MediaInfo can't read this .wsdcf's length: the decrypted file's own checks decide
    if length_in is None or length_out is None:
        raise DecryptError("MediaInfo could not read the length of the files", bad_output=True)
    if abs(length_in - length_out) > DURATION_TOLERANCE:
        raise DecryptError(f"the decrypted file is {length_out:.0f} s long, the download {length_in:.0f} s", bad_output=True)
    expected = (None, None) if vr else mediainfo.dmm_comment(source)
    if any(expected) and mediainfo.dmm_comment(result) != expected:
        raise DecryptError(f"the decrypted file lost DMM's video/part data (expected {expected})", bad_output=True)


def decrypt(
    javit: Path,
    mediainfo_path: Path,
    dcv: Path,
    out: Path,
    login: Login | None,
    javit_email: str = "",
    attempts: int = 3,
    retry_delay: float = 10.0,
    timeout: float | None = None,
    vr: bool = False,
) -> Path:
    """Decrypt `dcv` into `out` and verify it. The encrypted file is kept. 2D: a .dcv into an .mkv (jav-it's "dmm");
    vr: a .wsdcf into an .mp4 (jav-it's "dmm-vr"), with a shorter time limit (vr_timeout)."""
    if not vr:  # MediaInfo may not read a VR .wsdcf: jav-it and the checks of its result decide then
        try:
            source = mediainfo.read(dcv, mediainfo_path)
        except mediainfo.MediaInfoError as exc:
            raise DecryptError(f"the download can't be read ({exc})", corrupt_source=True) from exc
        damage = mediainfo.problem(source, full=False)
        if not damage and not mediainfo.duration(source):
            damage = "MediaInfo sees no video in it"
        if damage:
            raise DecryptError(f"the download is damaged ({damage})", corrupt_source=True)

    if timeout is None:
        timeout = vr_timeout(dcv.stat().st_size) if vr else TIMEOUT
    env = _environment(login, out.parent / "tmp")
    email = javit_email or (login.username if login else "")
    answer = None
    last = ""
    attempt = 0
    while attempt < attempts:
        attempt += 1
        out.unlink(missing_ok=True)
        logs = out.with_name(f"{out.stem}.jav-it-{attempt}.log")
        try:
            code, text = _run(javit, dcv, out, env, answer, logs, timeout, "dmm-vr" if vr else "dmm")
        except subprocess.TimeoutExpired:
            out.unlink(missing_ok=True)
            raise DecryptError(f"jav-it took longer than {_duration(timeout)} on {dcv.name} and was stopped "
                               f"(its output is in {logs})")
        except OSError as exc:
            raise DecryptError(f"jav-it could not be started: {exc}") from exc
        lower = text.lower()
        if code == 0 and SUCCESS_BANNER in lower:
            try:
                verify(dcv, out, mediainfo_path, vr)
            except DecryptError:
                out.unlink(missing_ok=True)
                raise
            for old in out.parent.glob(f"{out.stem}.jav-it-*.log"):
                old.unlink(missing_ok=True)
            return out
        out.unlink(missing_ok=True)
        if vr and any(marker in lower for marker in VR_LICENCE_MARKERS):
            raise NeedsVrLicence(f"jav-it says it needs DMM VR Player's licence to decrypt {dcv.name}: this jav-it "
                                 f"can't decrypt VR without the player (newer versions can: update jav-it and "
                                 f"VR.MOD). Its output is in {logs}")
        if any(marker in lower for marker in LICENSE_MARKERS):
            if not email:
                raise DecryptError(
                    "jav-it asks for your Patreon email to validate your membership: set javit_email "
                    "in the settings file (dmm-downloader config) or run jav-it once yourself"
                )
            if answer is None:
                answer = f"{email}\n{email}\n"  # some versions ask twice
                attempts = max(attempts, attempt + 2)
                log.info("jav-it asked for the Patreon email; answering and trying again")
                continue
            last = "jav-it did not accept the Patreon email (is javit_email your Patreon email?)"
        elif CONNECTION_ERROR in lower:
            last = "jav-it could not reach its server (no internet connection)"
        else:
            last = f"jav-it failed (exit code {code}): {text.strip().splitlines()[-1] if text.strip() else 'no output'}"
        log.warning("decrypt %s: try %d/%d: %s (log: %s)", dcv.name, attempt, attempts, last, logs)
        if attempt < attempts:
            time.sleep(retry_delay)
    raise DecryptError(f"{last}. jav-it's output is in {logs}")
