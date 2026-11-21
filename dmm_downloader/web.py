"""The browser page: a small web server on 127.0.0.1 that only this computer's browser can use.

The page (web/index.html, app.css, app.js) asks this server for the state of the download queue and sends what
the user does. Only the browser opened by the tool gets in: the first address carries a random key, which
becomes a cookie for this port; other web pages can't send requests that pass (cookie with SameSite=Strict, a
header of our own, the Host checked against DNS rebinding).

Videos are looked up on DMM when they are added (where from, which qualities), then downloaded several at a
time (settings `parallel`; VR one at a time, jav-it one at a time: pipeline.Slots).
"""

from __future__ import annotations

import collections
import dataclasses
import errno
import json
import logging
import os
import queue
import re
import secrets
import string
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, children, config, credentials, ids, links, logs, pipeline
from . import decrypt as decrypting
from .dmm import LOGIN_AGAIN, DmmError, DmmSession, JapanRoute, LoginError, valid_japan_route
from .merging import tools

log = logging.getLogger(__name__)

PORT = 8766  # any free port when it is taken
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.css": ("app.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
HEADER = "X-DMM-Downloader"
MAX_BODY = 256 * 1024  # a pasted list of a few thousand ids
FINISHED = {"done", "there", "bad", "failed"}
LOOKED_UP = {"ready", "bad", "there"}  # looked up again when the source changes
SOURCES = links.SOURCES
LIMIT_WORDS = "limits how often"  # DMM's per-video VR limit (download.py)
FETCH_PROGRAM = "DMM-Fetch.exe" if os.name == "nt" else "DMM-Fetch"


# The login comes from DMM_USERNAME and DMM_PASSWORD (e.g. a server without a password store): the page can't change it.
ENVIRONMENT_LOGIN = ("Your DMM login comes from DMM_USERNAME and DMM_PASSWORD in the environment (jav-it uses it too): "
                     "change or remove them there, then start the program again.")

class Refused(Exception):
    """The request can't be done; the message is shown on the page."""


def fetch_helper_command(settings: config.Settings) -> list[str] | None:
    """The separate download program (VPN split tunnelling), or None: downloads then run in this program."""
    value = settings.fetch_helper.strip().strip('"')
    if value.lower() in ("off", "no", "none", "false"):
        return None
    if value.lower() == "module":  # the same program run as `python -m dmm_downloader.fetch` (tests, run.bat)
        if not getattr(sys, "frozen", False):
            return [sys.executable, "-m", "dmm_downloader.fetch"]
        value = ""  # the .exe can't run modules: its own DMM-Fetch.exe
    path = Path(value).expanduser() if value else tools.program_dir() / FETCH_PROGRAM
    return [str(path)] if path.is_file() else None


def without_cid(cid: str, text: str) -> str:
    """A message for the video's row, which already shows the content id."""
    return text[len(cid) + 2:] if text.lower().startswith(f"{cid}: ") else text


def web_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", tools.program_dir())) / "dmm_downloader" / "web"
    return Path(__file__).resolve().parent / "web"


@dataclass
class Job:
    id: int
    cid: str
    source: str  # where to look: auto, premium, vrchannel, library
    state: str = "checking"  # checking, ready, bad, there, waiting, working, done, failed
    choice: links.Choice | None = None
    rate: str | None = None  # a quality picked on the page
    message: str = ""
    steps: list[str] = field(default_factory=list)
    progress: dict | None = None
    result: pipeline.Result | None = None
    dmm_id: str | None = None  # DMM's id inside the files, once a download read it (the result is named after it)


class JobReport(pipeline.Report):
    """What the pipeline says about one video: kept on the job for the page, and in the log."""

    def __init__(self, app: "App", job: Job) -> None:
        self.app, self.job = app, job
        self._first: tuple[float, float] | None = None

    def step(self, text: str) -> None:
        with self.app.lock:
            self.job.steps.append(text)
            self.job.progress = None
        self.app.note(f"{self.job.cid}: {text}")

    def progress(self, done: float, total: float | None, unit: str = "bytes") -> None:
        now = time.monotonic()
        if self._first is None:
            self._first = (now, done)
        start, first = self._first
        speed = (done - first) / (now - start) if unit == "bytes" and now - start > 0.5 else None
        eta = (total - done) / speed if speed and total else None
        self.job.progress = {"done": done, "total": total, "unit": unit, "speed": speed, "eta": eta}

    def end_progress(self) -> None:
        self.job.progress = None
        self._first = None

    def found(self, choice: links.Choice) -> None:
        with self.app.lock:
            self.job.choice, self.job.rate = choice, None  # the row now shows the library's version

    def named(self, file_id: str) -> None:
        with self.app.lock:
            self.job.dmm_id = file_id  # "Saved as" shows the final name


class App:
    def __init__(self, settings: config.Settings) -> None:
        self.lock = threading.RLock()
        self.settings = settings
        self.jobs: list[Job] = []
        self.next_id = 1
        self.lines: collections.deque = collections.deque(maxlen=2000)
        self.line_no = 0
        self.run_options = {"quality": "best", "keep_dcv": False, "overwrite": False}
        self.running = False
        self.stop_after = False
        self.workers = 0
        self.session: DmmSession | None = None
        self.login: credentials.Login | None = None  # the DMM session's login, which jav-it decrypts with
        self.typed_login: credentials.Login | None = None  # typed on the page and not saved (no store): this run only
        self.session_error = "Starting…"
        self.javit: Path | None = None
        self.mediainfo: Path | None = None
        self.no_vr = "not checked yet"  # why VR videos can't be decrypted (saved encrypted then), "" when they can
        self.merge: tools.Tools | None = None
        self.fetch_helper: list[str] | None = None
        self.tool_errors: dict[str, str] = {}
        # The Japan route's last check (VR Channel downloads): state unknown / ok / region / error.
        self.japan_state = {"state": "unknown", "message": "", "checked": ""}
        self.library: dict[str, links.LibraryItem] | None = None
        self.library_version = 0
        self.library_error = ""
        self.checks: queue.Queue[int] = queue.Queue()
        self.quit = threading.Event()
        self.ready = threading.Event()  # tools found and the DMM session opened
        threading.Thread(target=self._checker, name="lookups", daemon=True).start()

    def startup(self) -> None:
        try:
            self.find_tools()
            self.open_session()
            self.read_library()
        finally:
            self.ready.set()
        if self.settings.vrchannel_route or self.settings.source == "vrchannel":  # else only when asked ("Check")
            self.check_japan()

    # -- log -------------------------------------------------------------------------------

    def note(self, text: str) -> None:
        text = logs.redact(text)
        log.info("%s", text)
        with self.lock:
            self.line_no += 1
            self.lines.append((self.line_no, time.strftime("%H:%M:%S"), text))

    # -- setup -----------------------------------------------------------------------------

    def find_tools(self) -> None:
        s, errors = self.settings, {}
        try:
            self.javit = tools.find_tool("jav-it", s.jav_it or None)
            if not (self.javit.parent / decrypting.DECRYPT_MODULE).exists():
                errors["decrypt_mod"] = f"DECRYPT.MOD is not next to jav-it ({self.javit.parent})"
            if not (self.javit.parent / decrypting.VR_MODULE).exists():
                errors["vr_mod"] = f"VR.MOD is not next to jav-it ({self.javit.parent})"
        except tools.ToolError:
            self.javit, errors["javit"] = None, "not found"
        try:
            self.mediainfo = tools.find_tool("mediainfo", s.mediainfo or None)
        except tools.ToolError:
            self.mediainfo, errors["mediainfo"] = None, "not found"
        self.no_vr = decrypting.vr_problem(self.javit, self.mediainfo)
        try:
            self.merge = tools.find_tools(s.ffmpeg or None, s.ffprobe or None)
        except tools.ToolError:
            self.merge, errors["ffmpeg"] = None, "not found"
        self.fetch_helper = fetch_helper_command(s)
        if s.fetch_helper and s.fetch_helper.lower() not in ("off", "no", "none", "false") and not self.fetch_helper:
            errors["fetch_helper"] = f"not found: {s.fetch_helper}"
        self.tool_errors = errors

    def open_session(self) -> None:
        """The saved DMM session, or a new login with the saved email and password."""
        s = self.settings
        session = DmmSession(s.user_agent, s.retries)
        session.japan = JapanRoute(s.vrchannel_route)
        # jav-it decrypts with this same login, so there is no DMM session without one: typed on the page (kept for
        # this run when there is no password store), else from the environment or the password store.
        login = self.typed_login or credentials.find(interactive=False)
        error = ""
        try:
            if login is None:
                error = "Not logged in to DMM yet (Settings > DMM account)."
            elif not (session.load(config.session_path(), login.username) and session.is_logged_in()):
                session.login(login.username, login.password)  # also when the saved session is another account's
                session.save(config.session_path())
        except (LoginError, DmmError) as exc:
            error = str(exc)
        with self.lock:
            old = self.session
            self.session, self.login, self.session_error = (None if error else session), login, error
            self.library, self.library_error = None, ""
        if old is not None:
            old.stop_saving()  # only the new session writes the saved session from now on
            if not self.running:
                old.close()
        if error:
            session.close()
            self.note(f"DMM: {error}")
        else:
            self.note(f"DMM: logged in (login from the {login.source})")

    def login_lost(self, session: DmmSession | None) -> None:
        """DMM no longer sees `session` as logged in (a lookup or a download said so): the header says to log in
        again, and videos not started yet fail at once instead of each asking DMM."""
        with self.lock:
            if session is None or self.session is not session:
                return
            self.session, self.session_error = None, f"{LOGIN_AGAIN[0].upper()}{LOGIN_AGAIN[1:]}."
        self.note(f"DMM: {LOGIN_AGAIN}")

    def _library_from(self, session: DmmSession) -> None:
        """A lookup read the library list again (an id missing from an older list): the My library tab shows it."""
        items = session.cache.get("library")
        with self.lock:
            if items is not None and session is self.session and items is not self.library:
                self.library, self.library_error = items, ""
                self.library_version += 1

    def read_library(self) -> None:
        session = self.session
        if session is None:
            return
        session.cache.pop("library", None)
        try:
            items = links.library_list(session)
        except links.LoginExpired as exc:
            with self.lock:
                self.library_error = str(exc)
            self.login_lost(session)
            return
        with self.lock:
            self.library = items
            self.library_error = "" if items is not None else "DMM didn't answer the library list."
            self.library_version += 1
        if items is not None:
            self.note(f"library read from DMM: {len(items)} video(s)")

    def out_dir(self) -> Path:
        folder = Path(self.settings.output_dir).expanduser() if self.settings.output_dir else config.default_output_dir()
        return folder.resolve()

    # -- queue -----------------------------------------------------------------------------

    def add(self, entries: list[str], source: str | None = None) -> dict:
        found, invalid = ids.parse_lines(entries)
        added = 0
        with self.lock:
            busy = {j.cid for j in self.jobs if j.state not in FINISHED}
            for cid in found:
                if cid in busy:
                    continue
                job = Job(self.next_id, cid, source or self.settings.source)
                self.next_id += 1
                self.jobs.append(job)
                self.checks.put(job.id)
                added += 1
        self.note(f"added {added} video(s)" + (f", {len(found) - added} already in the list" if len(found) > added else "")
                  + (f"; not content ids: {', '.join(invalid[:5])}" if invalid else ""))
        return {"added": added, "invalid": invalid}

    def import_file(self, path: str) -> dict:
        file = Path(path.strip().strip('"')).expanduser()
        try:
            text = file.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            raise Refused(f"Can't read {file} ({exc.strerror or exc}).") from None
        return self.add(text.splitlines())

    def job(self, job_id) -> Job:
        with self.lock:
            for job in self.jobs:
                if job.id == job_id:
                    return job
        raise Refused("That video is no longer in the list.")

    def _checker(self) -> None:
        self.ready.wait()
        while not self.quit.is_set():
            try:
                job_id = self.checks.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.look_up(self.job(job_id))
            except Refused:
                continue
            except Exception:  # never let one video stop the lookups
                log.exception("lookup failed")

    def look_up(self, job: Job) -> None:
        with self.lock:
            job.state, job.message, job.choice, job.steps, job.result = "checking", "", None, [], None
            session = self.session
        out = self.out_dir()
        job.dmm_id = job.dmm_id or pipeline.known_dmm_id(out, job.cid)
        # Already saved under the id typed, or DMM's id for it (read in the files by an earlier download). A VR video
        # saved encrypted isn't, when VR is decrypted here: its files are decrypted when it is started.
        saved = pipeline.saved_files(out, [job.cid, job.dmm_id], wsdcf=bool(self.no_vr))
        if not self.run_options["overwrite"] and saved:
            job.state, job.message = "there", f"already in {out}: {', '.join(f.name for f in saved)}"
            return
        if session is None:
            job.state, job.message = "bad", "Not logged in to DMM (Settings)."
            return
        try:
            choice = links.find(session, job.cid, job.source)
        except links.NoLinks as exc:
            job.state, job.message = "bad", str(exc)
            if isinstance(exc, links.LoginExpired):
                self.login_lost(session)
        except DmmError as exc:
            job.state, job.message = "bad", f"DMM problem: {exc}"
        else:
            saved = pipeline.saved_files(out, [choice.page_cid], wsdcf=bool(self.no_vr), asked=job.cid)  # Premium page's id
            with self.lock:
                if saved and not self.run_options["overwrite"]:
                    job.state, job.message = "there", f"already in {out}: {', '.join(f.name for f in saved)}"
                else:
                    job.choice, job.state = choice, "ready"
                    if job.rate and job.rate not in choice.versions:
                        job.rate = None
        finally:
            self._library_from(session)
        if job.state == "bad":
            self.note(f"{job.cid}: {job.message}")

    def pick(self, data: dict) -> None:
        job = self.job(data.get("id"))
        rate = str(data.get("rate") or "")
        with self.lock:
            if job.state != "ready" or job.choice is None:
                raise Refused("The quality can be chosen while the video waits to start.")
            if rate not in job.choice.versions:
                raise Refused(f"DMM doesn't offer {rate} for {job.cid}.")
            job.rate = None if rate == job.choice.label else rate
        self.note(f"{job.cid}: quality {rate}")

    def retry(self, job_id) -> None:
        job = self.job(job_id)
        with self.lock:
            if job.state not in ("bad", "failed", "there"):
                raise Refused("Only a video that failed or wasn't available can be looked up again.")
            if any(j is not job and j.cid == job.cid and j.state not in FINISHED for j in self.jobs):
                raise Refused(f"{job.cid} is already in the list (another row).")
            job.state = "checking"
        self.checks.put(job.id)

    def remove(self, job_id) -> None:
        job = self.job(job_id)
        with self.lock:
            if job.state in ("waiting", "working"):
                raise Refused("Wait until this video is done.")
            self.jobs.remove(job)

    def clear(self) -> None:
        with self.lock:
            self.jobs = [j for j in self.jobs if j.state not in FINISHED]

    # -- running ---------------------------------------------------------------------------

    def options(self) -> pipeline.Options:
        s, run = self.settings, self.run_options
        return pipeline.Options(
            out_dir=self.out_dir(), source=s.source, merge=s.merge, keep_dcv=run["keep_dcv"], overwrite=run["overwrite"],
            retries=s.retries, javit_email=s.javit_email, fetch_helper=self.fetch_helper,
            slots=pipeline.Slots(downloads=s.parallel),
        )

    def start(self) -> int:
        with self.lock:
            if self.running:
                raise Refused("Already working.")
            if self.session is None:
                raise Refused(f"Not logged in to DMM: {self.session_error}")
            todo = [j for j in self.jobs if j.state == "ready" and j.choice is not None]
            if not todo:
                raise Refused("No video is ready.")
            needs_tools = any(not j.choice.vr for j in todo)  # 2D: jav-it with DECRYPT.MOD, and MediaInfo
            # VR is decrypted when jav-it has VR.MOD (and MediaInfo is there), else saved encrypted: no tools needed.
            if needs_tools and (self.javit is None or "decrypt_mod" in self.tool_errors):
                raise Refused("jav-it with DECRYPT.MOD is needed to decrypt 2D videos (Settings > jav-it).")
            if needs_tools and self.mediainfo is None:
                raise Refused("MediaInfo is missing: download it (links in Settings > Programs).")
            decrypting.match_javit_login(self.javit, self.login, config.config_dir())  # another account: jav-it's goes
            for job in todo:
                job.state, job.steps, job.message, job.result = "waiting", [], "", None
            self.running, self.stop_after = True, False
            opts = self.options()
            count = max(1, min(self.settings.parallel, len(todo)))
            self.workers = count
        for number in range(count):
            threading.Thread(target=self._work, args=(opts,), name=f"download-{number + 1}", daemon=True).start()
        self.note(f"downloading {len(todo)} video(s), {count} at a time"
                  + ("; files through " + Path(self.fetch_helper[0]).name if self.fetch_helper else ""))
        return len(todo)

    def _work(self, opts: pipeline.Options) -> None:
        try:
            while not self.stop_after and not self.quit.is_set():
                with self.lock:
                    job = next((j for j in self.jobs if j.state == "waiting"), None)
                    if job is None:
                        break
                    job.state = "working"
                    session = self.session
                self.run_job(job, session, opts)
        finally:
            with self.lock:
                self.workers -= 1
                last = self.workers == 0
                if last:
                    for job in self.jobs:
                        if job.state == "waiting":
                            job.state = "ready"
                    self.running = False
            if last:
                self.note("stopped" if self.stop_after else "queue finished")

    def run_job(self, job: Job, session: DmmSession | None, opts: pipeline.Options) -> None:
        report = JobReport(self, job)
        tool_paths = pipeline.Tools(self.javit or Path("jav-it"), self.mediainfo or Path("mediainfo"),
                                    self.merge if opts.merge else None, no_vr=self.no_vr)
        try:
            if session is None:
                raise pipeline.VideoFailed("not logged in to DMM")
            result = pipeline.process(job.cid, session, self.login, tool_paths, dataclasses.replace(opts, rate=job.rate),
                                      report, choice=job.choice)
        except Exception as exc:  # never let one video stop the others
            log.exception("download failed")
            result = pipeline.Result(job.cid, False, f"unexpected error: {exc}")
        with self.lock:
            job.result, job.progress = result, None
            job.state = ("there" if result.skipped else "done") if result.ok else "failed"
            job.message = without_cid(job.cid, result.message)
            if self.library and job.state in ("done", "there"):
                self.library_version += 1  # "saved" changes in the library list
        self.note(f"{job.cid}: {'saved: ' if result.ok else 'failed: '}{result.message}")
        if not result.ok and LOGIN_AGAIN in result.message:
            self.login_lost(session)

    # -- what the page shows ----------------------------------------------------------------

    def job_view(self, job: Job) -> dict:
        choice = job.choice
        view = {"id": job.id, "cid": job.cid, "state": job.state, "message": job.message, "steps": job.steps[-14:],
                "progress": job.progress, "source": job.source}
        if job.state == "failed" and LIMIT_WORDS in job.message:
            view["state"] = "limit"
        if choice is not None:
            label = job.rate or choice.label
            qualities = links.quality_order(choice.versions)
            urls = choice.versions.get(label, choice.urls)
            parts = len(urls)
            # 2D results are named after DMM's id inside the files: known once downloaded, else the Premium
            # page's id, else the one typed. VR keeps the id typed.
            name = job.cid if choice.vr else (job.dmm_id or choice.page_cid or job.cid)
            view.update(found=choice.source, vr=choice.vr, label=label, best=choice.label, qualities=qualities, parts=parts,
                        page_cid=choice.page_cid or "", names=self.planned_names(name, choice.vr, label, urls),
                        japan=choice.japan)
            if choice.vr and not self.no_vr and job.state in ("ready", "waiting"):
                # Encrypted files saved earlier (by a version that didn't decrypt VR): decrypted instead of downloaded.
                saved = pipeline.saved_vr_parts(self.out_dir(), job.cid, urls)
                if saved:
                    view["saved_encrypted"] = [path.name for path in saved.values()]
        if job.result is not None:
            view.update(files=[str(f) for f in job.result.files], warnings=job.result.warnings,
                        encrypted=job.result.encrypted)
        return view

    def planned_names(self, name: str, vr: bool, label: str, urls: list[str]) -> list[str]:
        parts = len(urls)
        if vr:  # never joined; named by DMM's part numbers, like the pipeline (pipeline.vr_name)
            extension = pipeline.VR_EXTENSION if self.no_vr else pipeline.VR_DECRYPTED  # no_vr: saved encrypted
            return [pipeline.vr_name(name, urls, n, extension) for n in range(1, parts + 1)]
        if parts > 1 and ("vp9" in label.lower() or not self.settings.merge):
            return [ids.output_name(name, n) for n in range(1, parts + 1)]
        return [ids.output_name(name)]

    def library_view(self) -> list[dict]:
        out = self.out_dir()
        known = pipeline.known_dmm_ids(out)  # saved under DMM's id inside the files, when an earlier run read it
        rows = []
        for cid, item in (self.library or {}).items():
            saved = pipeline.saved_files(out, [cid, known.get(cid)], bool(self.no_vr))
            rows.append({"cid": cid, "title": item.title, "vr": item.vr, "download": item.downloadable,
                         "bought": item.bought[:10], "saved": bool(saved)})
        rows.sort(key=lambda r: (r["bought"], r["cid"]), reverse=True)
        return rows

    def state(self, after: int, library_version: int) -> dict:
        s = self.settings
        with self.lock:
            jobs = []
            for job in self.jobs:
                try:
                    jobs.append(self.job_view(job))
                except Exception as exc:
                    log.exception("showing %s failed", job.cid)
                    jobs.append({"id": job.id, "cid": job.cid, "state": "failed", "message": f"can't be shown ({exc})",
                                 "steps": [], "progress": None, "source": job.source})
            lines = [list(line) for line in self.lines if line[0] > after]
            library_changed = library_version != self.library_version
        state = {
            "version": __version__, "running": self.running, "stopping": self.stop_after, "jobs": jobs, "log": lines,
            "starting": not self.ready.is_set(),
            "dmm": {"logged_in": self.session is not None, "error": self.session_error,
                    "account": self.login.username if self.login and self.session is not None else "",
                    "login_source": self.login.source if self.login else "", "store": credentials.store_available(),
                    "library_count": len(self.library) if self.library is not None else None},
            "options": {"source": s.source, "merge": s.merge, "output_dir": str(self.out_dir()), **self.run_options},
            "settings": {"parallel": s.parallel, "retries": s.retries, "javit_email": s.javit_email, "jav_it": s.jav_it,
                         "mediainfo": s.mediainfo, "ffmpeg": s.ffmpeg, "ffprobe": s.ffprobe, "fetch_helper": s.fetch_helper,
                         "vrchannel_route": s.vrchannel_route,
                         "config_path": str(config.config_path()), "log_dir": str(config.log_dir())},
            "japan": {**self.japan_state, "route": JapanRoute(s.vrchannel_route).kind,
                      "shown": JapanRoute(s.vrchannel_route).shown()},
            "tools": {"javit": str(self.javit or ""), "mediainfo": str(self.mediainfo or ""),
                      "ffmpeg": str(self.merge.ffmpeg) if self.merge else "", "ffprobe": str(self.merge.ffprobe) if self.merge else "",
                      "fetch_helper": " ".join(self.fetch_helper or []), "fetch_program": FETCH_PROGRAM,
                      "vr": not self.no_vr, "no_vr": self.no_vr, "errors": self.tool_errors},
            "library_version": self.library_version, "library_error": self.library_error,
        }
        if library_changed:
            state["library"] = self.library_view() if self.library is not None else None
        return state

    # -- settings --------------------------------------------------------------------------

    def set_options(self, data: dict) -> None:
        s = self.settings
        # Every value is checked before any is used: a refused change changes nothing.
        source = str(data.get("source", s.source))
        if source not in SOURCES:
            raise Refused("Unknown source.")
        folder = str(data.get("output_dir") or "").strip().strip('"') if "output_dir" in data else s.output_dir
        if "output_dir" in data and not folder:
            raise Refused("Choose a folder for the videos.")
        quality = data.get("quality", self.run_options["quality"])
        if quality not in ("best", "pick"):
            raise Refused("Unknown quality choice.")
        with self.lock:
            changed_source, s.source = source != s.source, source
            if "merge" in data:
                s.merge = bool(data["merge"])
            s.output_dir = folder
            self.run_options["quality"] = quality
            for name in ("keep_dcv", "overwrite"):
                if name in data:
                    self.run_options[name] = bool(data[name])
            relook = [j for j in self.jobs if j.state in LOOKED_UP]
            for job in relook:
                if changed_source:
                    job.source = s.source
                job.state = "checking"
        config.save(s)
        for job in relook:  # where it comes from, or whether it's already saved, may have changed
            self.checks.put(job.id)
        if "output_dir" in data:
            with self.lock:
                self.library_version += 1

    def save_settings(self, data: dict) -> None:
        if self.running:
            raise Refused("Wait until the downloads are done.")
        s = self.settings
        # Every value is checked before any is used: a refused save changes nothing, in use or in the file.
        try:
            parallel = int(data["parallel"]) if "parallel" in data else s.parallel
            retries = int(data["retries"]) if "retries" in data else s.retries
        except (TypeError, ValueError):
            raise Refused("Use whole numbers.") from None
        if not 1 <= parallel <= 8:
            raise Refused("Videos at the same time: 1 to 8.")
        if not 1 <= retries <= 20:
            raise Refused("Tries: 1 to 20.")
        texts = {name: str(data[name] or "").strip().strip('"')
                 for name in ("javit_email", "jav_it", "mediainfo", "ffmpeg", "ffprobe", "fetch_helper", "vrchannel_route")
                 if name in data}
        if not valid_japan_route(texts.get("vrchannel_route", "")):
            raise Refused("Japan route: an http proxy like http://127.0.0.1:25345, or empty for this program's own "
                          "connection.")
        route_changed = "vrchannel_route" in texts and texts["vrchannel_route"] != s.vrchannel_route
        s.parallel, s.retries = parallel, retries
        for name, value in texts.items():
            setattr(s, name, value)
        config.save(s)
        self.find_tools()
        if route_changed:
            with self.lock:
                if self.session is not None:
                    self.session.japan = JapanRoute(s.vrchannel_route)
                self.japan_state = {"state": "unknown", "message": "", "checked": ""}
            threading.Thread(target=self.check_japan, daemon=True).start()
        self.note("settings saved")

    def check_japan(self) -> dict:
        """Whether the Japan route reaches DMM as a visitor in Japan (links.japan_check), for the page."""
        s = self.settings
        session = DmmSession(s.user_agent, 1)  # no login needed: the VR Channel's start page
        session.japan = JapanRoute(s.vrchannel_route)
        try:
            reaches, why = links.japan_check(session)
        finally:
            session.close()
        state = {"state": "ok" if reaches else "region" if reaches is False else "error", "message": why,
                 "checked": time.strftime("%H:%M")}
        with self.lock:
            if s.vrchannel_route == session.japan.proxy:  # not changed meanwhile
                self.japan_state = state
        self.note(f"Japan route ({session.japan.shown()}): " + ("reaches Japan (VR Channel downloads possible)" if reaches
                                                                 else why))
        return dict(state)

    def log_in(self, data: dict) -> None:
        email, password = str(data.get("email") or "").strip(), str(data.get("password") or "")
        if not email or not password:
            raise Refused("Type the email and the password of your DMM account.")
        if self.running:
            raise Refused("Wait until the downloads are done.")
        if credentials.from_environment():
            raise Refused(ENVIRONMENT_LOGIN)
        session = DmmSession(self.settings.user_agent, self.settings.retries)
        try:
            session.login(email, password)
            session.save(config.session_path())
        except (LoginError, DmmError) as exc:
            session.close()
            raise Refused(str(exc)) from None
        session.close()
        login = credentials.Login(email, password, "typed")
        # Saved, it replaces the account saved before (jav-it gets it from the next video on). Not saved (no store, or
        # the store failed), it is kept for this run, and an older account's saved login never comes back later.
        saved = credentials.store_available() and credentials.save(login)
        if not saved:
            credentials.forget()
        self.typed_login = None if saved else login
        self.open_session()
        self.note("DMM login checked" + (" and saved in the password store; jav-it uses it too" if saved else
                                         " (not saved: no password store here; it works until this program closes, "
                                         "set DMM_USERNAME and DMM_PASSWORD to keep it)"))
        threading.Thread(target=self.read_library, daemon=True).start()

    def log_out(self) -> None:
        if self.running:
            raise Refused("Wait until the downloads are done.")
        if credentials.from_environment():
            raise Refused(ENVIRONMENT_LOGIN)
        credentials.forget()
        decrypting.forget_javit_login(self.javit, config.config_dir())  # jav-it's own sign-in (auth.json) too
        with self.lock:
            old, self.session, self.login, self.typed_login = self.session, None, None, None
            self.session_error, self.library = "Logged out.", None
            self.library_version += 1
        if old is not None:
            old.close()  # first: a lookup still running must not write the session again
        try:
            config.session_path().unlink()
        except OSError:
            pass
        self.note("logged out: saved login and DMM session removed (jav-it has no login either now)")

    def doctor(self) -> None:
        if self.running:
            raise Refused("Wait until the downloads are done.")
        self.find_tools()
        self.open_session()
        found = ", ".join(n for n, ok in (("jav-it", self.javit), ("MediaInfo", self.mediainfo), ("FFmpeg", self.merge)) if ok)
        self.note(f"setup checked: {found or 'no program'} found; VR videos "
                  + (f"are saved encrypted ({self.no_vr})" if self.no_vr else "are decrypted (VR.MOD found)")
                  + "; DMM " + ("logged in" if self.session else self.session_error))


# -- folders ------------------------------------------------------------------------------------


def drives() -> list[str]:
    """Windows' drive letters, without touching the drives (an empty card reader would make us wait)."""
    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        mask = (1 << 26) - 1
    return [f"{letter}:\\" for bit, letter in enumerate(string.ascii_uppercase) if mask >> bit & 1]


def browse(path: str, files: str = "") -> dict:
    """Folders (and .txt files when `files` is "txt") in `path`; the drives on Windows, or home, when empty."""
    if not path:
        if os.name == "nt":
            return {"path": "", "parent": None, "entries": [{"name": d, "path": d, "dir": True} for d in drives()]}
        path = str(Path.home())
    folder = Path(path).expanduser()
    if not folder.is_dir():
        raise Refused(f"Not a folder: {folder}")
    try:
        children = list(folder.iterdir())
    except OSError as exc:
        raise Refused(f"Can't open {folder} ({exc.strerror or exc})") from None
    entries = []
    for child in children:
        if child.name.startswith((".", "$")):
            continue
        try:
            if getattr(child.stat(), "st_file_attributes", 0) & 6:  # Windows' hidden and system files and folders
                continue
            is_dir = child.is_dir()
            if not is_dir and not (files == "txt" and child.suffix.lower() == ".txt"):
                continue
            entries.append({"name": child.name, "path": str(child), "dir": is_dir, "size": None if is_dir else child.stat().st_size})
        except OSError:
            continue
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    parent = str(folder.parent) if folder.parent != folder else ("" if os.name == "nt" else None)
    return {"path": str(folder), "parent": parent, "entries": entries}


# -- HTTP ---------------------------------------------------------------------------------------


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # Linux/macOS: a quick restart gets the same port; Windows would share it

    app: App

    def __init__(self, address, token: str) -> None:
        super().__init__(address, Handler)
        self.token = token
        self.port = self.server_address[1]
        self.cookie = f"dmm_downloader_{self.port}"


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = "dmm-downloader"
    sys_version = ""

    def log_message(self, format, *args) -> None:  # noqa: A002 (the base class's name)
        pass

    def _headers(self, status: int, content_type: str, length: int, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                                    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _send(self, status: int, payload, content_type: str = "application/json", extra: dict | None = None) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self._headers(status, content_type, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _host_ok(self) -> bool:
        # Only this computer's names (another site's name = DNS rebinding); any port, for SSH tunnels.
        host = (self.headers.get("Host") or "").lower()
        return re.fullmatch(r"(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?", host) is not None

    def _cookie_ok(self) -> bool:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == self.server.cookie and secrets.compare_digest(value, self.server.token):
                return True
        return False

    def _refuse(self, text: str) -> None:
        self._send(HTTPStatus.FORBIDDEN, f"<!doctype html><meta charset=utf-8><title>DMM Downloader</title>"
                   f"<p>{text}</p>".encode(), "text/html; charset=utf-8")

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._refuse("Wrong address.")
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/" and "key" in query:
            if not secrets.compare_digest(query["key"][0], self.server.token):
                return self._refuse("This link is from an earlier start. Use the link in the downloader's window.")
            cookie = f"{self.server.cookie}={self.server.token}; Path=/; HttpOnly; SameSite=Strict"
            return self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": "/", "Set-Cookie": cookie})
        if not self._cookie_ok():
            return self._refuse("Open the downloader with the link shown in its window.")
        if url.path in STATIC:
            name, content_type = STATIC[url.path]
            try:
                return self._send(HTTPStatus.OK, (web_dir() / name).read_bytes(), content_type)
            except OSError:
                return self._send(HTTPStatus.NOT_FOUND, {"error": "missing file"})
        if url.path.startswith("/api/") and self.headers.get(HEADER) != "1":
            # The cookie alone isn't enough: cookies aren't kept apart by port, so a page of another program on this
            # computer would send it too. Our header needs our own page (other pages can't add it).
            return self._send(HTTPStatus.FORBIDDEN, {"error": "refused"})
        app = self.server.app
        try:
            if url.path == "/api/state":
                after = int((query.get("after") or ["0"])[0] or 0)
                library = int((query.get("lib") or ["-1"])[0] or -1)
                return self._send(HTTPStatus.OK, app.state(after, library))
            if url.path == "/api/browse":
                return self._send(HTTPStatus.OK, browse((query.get("path") or [""])[0], (query.get("files") or [""])[0]))
        except Refused as exc:
            return self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
        except ValueError:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "bad request"})
        self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._host_ok() or not self._cookie_ok() or self.headers.get(HEADER) != "1":
            return self._send(HTTPStatus.FORBIDDEN, {"error": "refused"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON only"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "bad length"})
        if length > MAX_BODY:
            return self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too large"})
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "bad JSON"})
        app = self.server.app
        path = urlparse(self.path).path
        actions = {
            "/api/add": lambda: app.add(str(data.get("text") or "").splitlines()),
            "/api/import": lambda: app.import_file(str(data.get("path") or "")),
            "/api/library/add": lambda: app.add([str(c) for c in data.get("cids") or []], "library"),
            "/api/library/refresh": lambda: threading.Thread(target=app.read_library, daemon=True).start(),
            "/api/pick": lambda: app.pick(data),
            "/api/retry": lambda: app.retry(data.get("id")),
            "/api/remove": lambda: app.remove(data.get("id")),
            "/api/clear": app.clear,
            "/api/options": lambda: app.set_options(data),
            "/api/start": lambda: {"started": app.start()},
            "/api/stop": lambda: setattr(app, "stop_after", True),
            "/api/login": lambda: app.log_in(data),
            "/api/logout": app.log_out,
            "/api/settings": lambda: app.save_settings(data),
            "/api/doctor": app.doctor,
            "/api/japan/check": app.check_japan,
            "/api/quit": self._quit,
        }
        action = actions.get(path)
        if action is None:
            return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
        try:
            answer = action()
        except Refused as exc:
            return self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
        except Exception as exc:
            log.exception("request %s failed", path)
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"unexpected error: {logs.redact(exc)}"})
        self._send(HTTPStatus.OK, answer if isinstance(answer, dict) else {"ok": True})

    do_HEAD = do_GET

    def _quit(self) -> None:
        if self.server.app.running:
            raise Refused("Downloads are running: stop them first (or close the window).")
        self.server.app.quit.set()
        threading.Thread(target=self.server.shutdown, daemon=True).start()


class PortError(Exception):
    """The port asked for (--port) can't be used; the message says what to do."""


def make_server(settings: config.Settings, port: int = 0) -> Server:
    token = secrets.token_urlsafe(32)
    server = None
    for candidate in ([port] if port else [PORT, 0]):
        try:
            server = Server(("127.0.0.1", candidate), token)
            break
        except OSError as exc:
            if not port:
                continue  # 8766 is taken (another copy?): any free port
            if exc.errno == errno.EADDRINUSE:
                raise PortError(f"Port {port} is in use, maybe by another copy of the downloader: close it, "
                                "or start without --port to use a free port.") from None
            raise PortError(f"Port {port} can't be used ({exc.strerror or exc}): start without --port to use a free port.") from None
    if server is None:
        raise OSError("no free port")
    server.app = App(settings)
    return server


def serve(settings: config.Settings, port: int = 0, open_browser: bool = True) -> int:
    server = make_server(settings, port)
    app = server.app
    threading.Thread(target=app.startup, name="startup", daemon=True).start()
    url = f"http://127.0.0.1:{server.port}/?key={server.token}"
    app.note(f"DMM Downloader {__version__} started")
    print(f"DMM Downloader {__version__} is running in your browser:\n\n    {url}\n")
    print("Keep this window open while you download. Closing it (or Ctrl+C) stops the downloader.", flush=True)
    has_screen = os.name == "nt" or sys.platform == "darwin" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if open_browser and has_screen:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    elif open_browser:
        print(f"No screen here: open the address above in a browser on your computer. Over SSH, first run there: "
              f"ssh -L {server.port}:127.0.0.1:{server.port} <you>@<this machine>", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        app.quit.set()
        server.server_close()
        children.stop_all()  # Quit or Ctrl+C: no download program, jav-it or FFmpeg keeps running
        if app.session:
            app.session.close()
    return 0
