"""Downloading one encrypted part (.dcv, VR: .wsdcf): DMM's two redirects, then the file from DMM's file server.

The redirects (the download link) always run in this program. The file itself (fetch.py) runs here too, or in
the separate download program (DMM-Fetch.exe) when there is one, so a VPN can carry only that step.
The file is written as "<name>.part" and renamed when complete. After a broken connection or a
stop, the next try continues where it stopped when the server allows it (HTTP Range), otherwise
it starts again. Each try asks DMM for a fresh download link (they expire).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

import requests

from .dmm import (BROWSER_USER_AGENT, DMM_HOST_RE, LIBRARY_URL, LOGIN_AGAIN, LOGIN_HOSTS, REGION_REFUSED, DmmError,
                  DmmSession)
from .fetch import (CDN_HOST_RE, NOT_A_FILE, DownloadError, FileRefused, NoSpace, NotAFile, Progress,
                    fetch_with_program, looks_like_text)
from .fetch import fetch_file as fetch
from .merging.tools import NO_WINDOW

log = logging.getLogger(__name__)

STREAM_HOST = "str.dmm.com"
VR_STREAM_HOST = "wsd-r.dmm.co.jp"  # VR downloads: DMM's own domain, so it gets the login cookies
# DMM refuses its download links from VPN addresses; its file servers don't.
VPN_HINT = ("Is a VPN on? DMM refuses its download links from VPN addresses: keep this program off the VPN "
            "(split tunneling); only the file download (DMM-Fetch.exe) may go through it")
# DMM's VR download server answers 403 once one VR video was downloaded many times (seen in a test: about 20 download
# starts of one video, then 403 for that video only, other VR videos still fine; how long it lasts is unknown, at
# least 20 minutes). Waits before the next tries, in seconds: about 17 minutes in all, then the run moves on.
# A logged-out session or a VPN address gets the same 403: the login is checked first (a VPN can't be told apart).
VR_BUSY_WAITS = (120, 300, 600)
VR_LIMIT = "DMM refuses more downloads of this VR video for now (it limits how often one VR video is downloaded"
VR_VPN = "a VPN on this program gets the same refusal from DMM"


class VrServerBusy(DownloadError):
    """DMM's VR download server refuses more downloads of this video for now (HTTP 403)."""


class LinkRefused(DownloadError):
    """str.dmm.com refuses the download link (HTTP 403/404), as it does for VPN addresses, or DMM's VR download server
    answers 404/410: trying again rarely helps."""


class NotInJapan(DownloadError):
    """The link is one DMM opens only in Japan (VR Channel) and the Japan route didn't reach Japan for DMM."""


NOT_IN_JAPAN = ("DMM answered “not available in your region” to the Japan route: check the Japan proxy, or set one "
                "(Japan route, Settings > Downloads)")


class LoginExpired(DownloadError):
    """DMM no longer sees the session as logged in: trying again won't help, logging in again will."""


class NotAllowed(DownloadError):
    """DMM didn't give the download to this (logged-in) account, e.g. a Premium link an account without Premium
    can't use. The pipeline then tries the library (source "auto")."""


LINK_REFUSED_TRIES = 2
# Renaming the finished "<name>.part": on Windows a virus scanner or the search indexer may hold a file that was just
# written for a moment, and the rename fails meanwhile. Tries, and seconds between them.
RENAME_TRIES = 5
RENAME_WAIT = 1.0


def _rename(partial: Path, target: Path) -> None:
    """The complete download gets its name, trying again for a few seconds while another program holds it."""
    for attempt in range(1, RENAME_TRIES + 1):
        try:
            partial.replace(target)
            return
        except PermissionError as exc:  # Windows: "being used by another process", or access denied for now
            if attempt == RENAME_TRIES:
                raise
            log.info("%s: rename failed (%s); trying again", partial.name, exc)
            time.sleep(RENAME_WAIT)


def _refused(session: DmmSession, label: str, what: str) -> DownloadError:
    """The error for a first link step that DMM refused (`what`): an expired login, or not for this account."""
    state = session.check_login()
    if state is False:
        return LoginExpired(f"{label}: DMM didn't give the download ({what}): {LOGIN_AGAIN}")
    if state is True:
        return NotAllowed(f"{label}: DMM didn't give the download ({what}); you are logged in, so DMM doesn't let this "
                          f"account download it this way. {VPN_HINT}")
    return NotAllowed(f"{label}: DMM didn't give the download ({what}); is your login still valid? {VPN_HINT}")


def _checked_redirect(location: str, base: str, allowed: Callable[[str], bool], label: str) -> str:
    target = urljoin(base, (location or "").strip())
    try:
        parts = urlsplit(target)
        port = parts.port
    except ValueError as exc:
        raise DownloadError(f"{label}: DMM sent an unreadable link") from exc
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not allowed(host) or parts.username or parts.password or port is not None:
        raise DownloadError(f"{label}: DMM sent the download somewhere unexpected ({host or 'no host'}); stopped")
    return target


def resolve(session: DmmSession, url: str, label: str, referer: str = "", japan: bool = False) -> str:
    """DMM's link -> str.dmm.com -> stcN.dmm.com (the file); VR: -> wsd-r.dmm.co.jp -> wsd-cN.dmm.com.
    Cookies only go to DMM's own domain (dmm.co.jp), never to the download servers (dmm.com). `referer`: the page the
    link was found on, when it isn't the library (the VR Channel's page of the video). `japan`: the steps go through
    the Japan route as a normal browser (VR Channel links, which DMM opens only in Japan)."""
    ask = session.japan_request if japan else session.request
    # VR: wsd-r.dmm.co.jp answers 403 unless both steps come "from" the page with the link (the library page for
    # bought videos), like the browser's.
    vr_from = referer or LIBRARY_URL
    vr_link = japan or "content_type=vr" in url.lower()
    headers = {"Referer": vr_from} if vr_link else {"Referer": referer} if referer else {}
    try:
        first = ask("GET", url, f"{label}: download link", retries=2, allow_redirects=False, headers=headers)
    except DmmError as exc:
        if exc.status and 400 <= exc.status < 500:
            raise _refused(session, label, f"HTTP {exc.status}") from exc
        raise
    try:
        status, location = first.status_code, first.headers.get("Location", "")
        # DMM's "not available in your region" page instead of the redirect: the route isn't in Japan for DMM.
        outside = japan and status == 200 and REGION_REFUSED in str(getattr(first, "text", "") or "")
    finally:
        first.close()
    if outside:
        raise NotInJapan(f"{label}: {NOT_IN_JAPAN}")
    if not 300 <= status < 400:
        raise _refused(session, label, f"HTTP {status}")
    try:
        host = (urlsplit(urljoin(url, location.strip())).hostname or "").lower()
    except ValueError:
        host = ""
    if host in LOGIN_HOSTS:  # sent to DMM's login page
        raise LoginExpired(f"{label}: DMM asks to log in before the download: {LOGIN_AGAIN}")
    if DMM_HOST_RE.fullmatch(host) and host not in (STREAM_HOST, VR_STREAM_HOST):
        raise _refused(session, label, f"DMM sent it to {host}")  # e.g. a page offering Premium
    stream = _checked_redirect(location, url, lambda h: h in (STREAM_HOST, VR_STREAM_HOST), label)
    if urlsplit(stream).hostname == VR_STREAM_HOST:
        try:
            second = ask("GET", stream, f"{label}: VR download link", retries=2, allow_redirects=False,
                         headers={"Referer": vr_from})
        except DmmError as exc:
            if exc.status == 403:
                raise VrServerBusy(f"{label}: DMM's VR download server answered HTTP 403") from exc
            if exc.status in (404, 410):
                raise LinkRefused(f"{label}: DMM's VR download server answered HTTP {exc.status}") from exc
            raise
        try:
            if not 300 <= second.status_code < 400:
                raise DownloadError(f"{label}: DMM's VR download server answered HTTP {second.status_code}")
            return _checked_redirect(second.headers.get("Location", ""), stream, lambda h: bool(CDN_HOST_RE.fullmatch(h)), label)
        finally:
            second.close()
    with requests.Session() as plain:  # no cookies for dmm.com
        agent, proxies = (BROWSER_USER_AGENT, session.japan.proxies) if japan else (session.user_agent, None)
        try:
            second = plain.get(stream, headers={"User-Agent": agent}, allow_redirects=False, timeout=(15, 60),
                               proxies=proxies)
        except requests.RequestException as exc:
            raise DownloadError(f"{label}: {type(exc).__name__}: {exc}") from exc
        try:
            if second.status_code in (403, 404):
                raise LinkRefused(f"{label}: DMM's download server answered HTTP {second.status_code}. {VPN_HINT}")
            if not 300 <= second.status_code < 400:
                raise DownloadError(f"{label}: DMM's download server answered HTTP {second.status_code}")
            return _checked_redirect(second.headers.get("Location", ""), stream, lambda h: bool(CDN_HOST_RE.fullmatch(h)), label)
        finally:
            second.close()


def download_part(
    session: DmmSession,
    url: str,
    target: Path,
    label: str,
    attempts: int = 5,
    progress: Progress | None = None,
    retry_delay: float = 10.0,
    notice: Callable[[str], None] | None = None,
    helper: list[str] | None = None,
    binary_only: bool = False,
    referer: str = "",
    japan: bool = False,
) -> Path:
    """Download one part to `target` (a .dcv or .wsdcf), trying again on errors. Keeps `<target>.part`
    on failure so the next run can continue it. `notice` shows long waits to the user. `helper`: the separate
    download program's command, which then downloads the file itself (only that step). `binary_only` (VR, which
    gets no other check): a web page or text sent instead of the file is refused and deleted. `referer`: the page
    the link was found on (resolve). `japan`: a link DMM opens only in Japan (VR Channel): its steps go through the
    Japan route; the file itself comes over the normal connection (or `helper`), and through the Japan route when
    DMM's file server refuses it there (once per part: each new link counts as a download start at DMM)."""
    if target.exists() and target.stat().st_size > 0:
        return target
    partial = target.with_name(target.name + ".part")
    last = ""
    failure: type[DownloadError] = DownloadError  # the kind of the last failure, kept for the pipeline (NotAllowed)
    busy = 0
    attempt = 0
    agent = BROWSER_USER_AGENT if japan else session.user_agent
    via_route = False  # the file itself through the Japan route (after its file server refused it elsewhere)
    while attempt < attempts:
        attempt += 1
        wait = retry_delay
        failure = DownloadError
        try:
            cdn_url = resolve(session, url, label, referer, japan) if japan else resolve(session, url, label, referer)
            log.info("%s: downloading (try %d/%d, %s bytes already%s)", label, attempt, attempts,
                     partial.stat().st_size if partial.exists() else 0, ", through the Japan route" if via_route else "")
            if via_route:
                fetch(cdn_url, agent, partial, progress, binary_only, proxies=session.japan.proxies)
            elif helper:
                fetch_with_program(helper, cdn_url, agent, partial, progress, NO_WINDOW, binary_only)
            else:
                fetch(cdn_url, agent, partial, progress, binary_only)
            if binary_only and looks_like_text(partial):  # an error page sent with "200 OK"
                partial.unlink(missing_ok=True)
                raise NotAFile(f"{label}: {NOT_A_FILE}")
            _rename(partial, target)
            return target
        except NoSpace as exc:  # trying again won't help
            raise NoSpace(f"{label}: {exc}") from exc
        except (LoginExpired, NotInJapan):
            raise  # trying again won't help
        except FileRefused as exc:
            last = str(exc)
            # A link asked for through the Japan route may be good only from there: the file through the route too.
            if japan and not via_route and (session.japan.proxy or helper):
                via_route = True
                attempts = max(attempts, attempt + 1)  # this switch doesn't use up a normal try
                message = (f"DMM's file server refused the file on this connection (HTTP {exc.status}); downloading "
                           f"it through the Japan route ({session.japan.shown()}) instead")
                log.warning("%s: %s", label, message)
                if notice:
                    notice(message)
                continue
            if binary_only:  # VR: each try asks DMM for a new link, and DMM counts VR download starts
                attempts = min(attempts, max(attempt, LINK_REFUSED_TRIES))
        except VrServerBusy as exc:
            last = str(exc)
            if session.check_login() is False:  # not DMM's per-video limit: the login has expired
                raise LoginExpired(f"{label}: DMM's VR download server refused the download (HTTP 403): {LOGIN_AGAIN}") from exc
            if busy < len(VR_BUSY_WAITS):
                wait = VR_BUSY_WAITS[busy]
                busy += 1
                # These waits don't use up the normal tries; after the last one, one more try only (each asks DMM
                # for a new VR link, and DMM counts them).
                attempts = attempt + 1 if busy == len(VR_BUSY_WAITS) else max(attempts, attempt + 1)
                message = f"{VR_LIMIT}; {VR_VPN}); waiting {wait // 60} min before trying again"
                log.warning("%s: %s", label, message)
                if notice:
                    notice(message)
                time.sleep(wait)
                continue
            attempts = min(attempts, attempt)  # still refused after the waits: stop
        except (LinkRefused, NotAFile) as exc:
            last = str(exc)
            attempts = min(attempts, max(attempt, LINK_REFUSED_TRIES))  # each VR try asks DMM for a new link
        except NotAllowed as exc:
            last, failure = str(exc), NotAllowed
            attempts = min(attempts, max(attempt, LINK_REFUSED_TRIES))
        except (DownloadError, DmmError) as exc:
            last = str(exc)
        except requests.RequestException as exc:
            last = f"connection problem: {type(exc).__name__}"
            log.info("%s: %s", label, exc)
        except OSError as exc:
            raise DownloadError(f"{label}: can't write {partial}: {exc}") from exc
        log.warning("%s: try %d/%d failed: %s", label, attempt, attempts, last)
        if attempt < attempts:
            time.sleep(wait)
    if busy:
        raise DownloadError(f"{label}: {VR_LIMIT}); run again later, e.g. tomorrow (the parts already downloaded are "
                            f"kept). If this program goes through a VPN, keep it off the VPN: {VR_VPN}")
    if last.startswith(f"{label}: "):
        last = last[len(label) + 2:]
    raise failure(f"{label}: download failed after {attempts} tries ({last})")
