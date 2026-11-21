"""Talking to DMM: the session (login or browser cookies), page requests with retries."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# DMM serves Googlebot outside Japan (DMM could stop this any time; settings `user_agent` changes it).
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
COOKIE_DOMAIN = ".dmm.co.jp"
LIBRARY_URL = "https://video.dmm.co.jp/mylibrary/"
GRAPHQL_URL = "https://api.video.dmm.co.jp/graphql"
LOGIN_URL = "https://accounts.dmm.co.jp/service/login/password/=/path={path}"
LOGIN_HOST = "accounts.dmm.co.jp"
GRAPHQL_HEADERS = {
    "Accept": "application/graphql-response+json, application/json",
    "Origin": "https://video.dmm.co.jp",
    "Referer": LIBRARY_URL,
}
# What a video's row (or the terminal) says when DMM no longer sees the session as logged in. The page also
# recognizes it by these words (web.py).
LOGIN_AGAIN = "your DMM login has expired: log in again (Settings)"
DMM_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*dmm\.(?:co\.jp|com)$")
LOGIN_HOSTS = ("accounts.dmm.co.jp", "accounts.dmm.com")
# What DMM gives only to visitors in Japan (the VR Channel's qualities, download list and links) is asked for as a
# normal Windows browser: DMM answers Googlebot, but leaves that out for it.
BROWSER_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36")
REGION_REFUSED = "not available in your region"  # DMM's page for visitors outside Japan
JAPAN_ROUTE_RE = re.compile(r"^https?://(?:[^\s/@]+@)?[^\s/@:]+(?::\d{1,5})?/?$", re.IGNORECASE)


class JapanRoute:
    """How the tool reaches DMM as a visitor in Japan: a proxy in Japan ("http://127.0.0.1:25345", e.g. wireproxy with
    a WireGuard config of a VPN's server in Japan), or "" = this program's own connection (a VPN app connected to Japan,
    or a computer in Japan)."""

    def __init__(self, proxy: str = "") -> None:
        self.proxy = (proxy or "").strip()

    @property
    def proxies(self) -> dict | None:
        return {"http": self.proxy, "https": self.proxy} if self.proxy else None

    @property
    def kind(self) -> str:
        return "proxy" if self.proxy else "own"

    def shown(self) -> str:
        """The route for messages: the proxy without a user name or password, or "this program's own connection"."""
        if not self.proxy:
            return "this program's own connection"
        return re.sub(r"//[^/@\s]*@", "//", self.proxy)


def valid_japan_route(value: str) -> bool:
    """Empty (this program's own connection) or an http(s) proxy address."""
    value = (value or "").strip()
    return not value or bool(JAPAN_ROUTE_RE.fullmatch(value))


class DmmError(Exception):
    """DMM could not be reached or answered something unexpected."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # DMM's HTTP answer (4xx) when it refused the request


class LoginError(DmmError):
    """Logging in to DMM failed."""


class DmmSession:
    """A requests session for www.dmm.co.jp and DMM's login and API hosts.

    Cookies are kept for .dmm.co.jp only, so they never go to the download servers (dmm.com).

    The browser page's lookups and its downloads (several threads) share one session, and the requests library
    doesn't promise that a session can be used by several threads at once: one request at a time goes through it
    (`_lock`; they are short: pages and redirects, never the files). Cookies DMM renews on the way are written back
    to the saved session (`save_path`), so it doesn't age from the login date.
    """

    def __init__(self, user_agent: str = "", retries: int = 5, retry_delay: float = 5.0) -> None:
        self.retries = max(1, retries)
        self.retry_delay = retry_delay
        self.cache: dict = {}  # answers kept for this run (the library list)
        self.save_path: Path | None = None  # the saved session these cookies came from (None: not saved)
        self.account = ""  # the DMM email this session logged in with (jav-it gets the same login)
        self.japan = JapanRoute()  # how to reach DMM as a visitor in Japan (settings `vrchannel_route`)
        self._lock = threading.RLock()
        self.http = requests.Session()
        self.http.headers.update({
            "User-Agent": user_agent or DEFAULT_USER_AGENT,
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        })
        self._base_cookies()

    @property
    def user_agent(self) -> str:
        return self.http.headers["User-Agent"]

    def _base_cookies(self) -> None:
        with self._lock:
            self.http.cookies.set("age_check_done", "1", domain=COOKIE_DOMAIN, path="/")
            self.http.cookies.set("ckcy", "1", domain=COOKIE_DOMAIN, path="/")

    def stop_saving(self) -> None:
        """This session no longer writes the saved session (another one replaced it, or logged out)."""
        with self._lock:  # waits for a write in progress
            self.save_path = None

    def close(self) -> None:
        self.stop_saving()
        self.http.close()

    def _cookie_state(self) -> frozenset:
        return frozenset((c.domain, c.path, c.name, c.value) for c in self.http.cookies if c.domain.endswith("dmm.co.jp"))

    # -- requests ---------------------------------------------------------------------------

    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        with self._lock:
            before = self._cookie_state()
            response = self.http.request(method, url, **kwargs)
            renewed = self.save_path is not None and self._cookie_state() != before
        if renewed:
            try:
                with self._lock:
                    if self.save_path is not None:  # not closed meanwhile
                        self.save(self.save_path)
            except OSError as exc:
                log.warning("the renewed DMM session could not be saved: %s", exc)
        return response

    def request(self, method: str, url: str, label: str, retries: int | None = None, **kwargs) -> requests.Response:
        """A request with retries on network errors, 429 and 5xx. Other HTTP errors fail at once."""
        attempts = max(1, retries or self.retries)
        kwargs.setdefault("timeout", (15, 60))
        last = ""
        for attempt in range(1, attempts + 1):
            try:
                response = self._send(method, url, **kwargs)
            except requests.RequestException as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    last = f"HTTP {response.status_code}"
                    wait = response.headers.get("Retry-After", "")
                    response.close()
                    if attempt < attempts:
                        time.sleep(min(60.0, float(wait)) if wait.isdigit() else self.retry_delay)
                        continue
                    break
                if response.status_code >= 400:
                    response.close()
                    raise DmmError(f"{label}: DMM answered HTTP {response.status_code}", response.status_code)
                return response
            log.warning("%s: attempt %d/%d failed: %s", label, attempt, attempts, last)
            if attempt < attempts:
                time.sleep(self.retry_delay)
        raise DmmError(f"{label}: no answer from DMM after {attempts} tries ({last})")

    def get(self, url: str, label: str, **kwargs) -> requests.Response:
        return self.request("GET", url, label, **kwargs)

    def japan_request(self, method: str, url: str, label: str, retries: int | None = None, **kwargs) -> requests.Response:
        """A request as a visitor in Japan: through the Japan route (`japan`), as a normal Windows browser, with this
        session's login. A proxy that doesn't answer says so (without its user name or password)."""
        kwargs["headers"] = {**(kwargs.get("headers") or {}), "User-Agent": BROWSER_USER_AGENT}
        if self.japan.proxies:
            kwargs["proxies"] = self.japan.proxies
        try:
            return self.request(method, url, label, retries=retries, **kwargs)
        except DmmError as exc:
            # No HTTP answer at all (not even DMM's 5xx): the proxy, or its tunnel, is down.
            if self.japan.proxy and exc.status is None and "(HTTP " not in str(exc):
                raise DmmError(f"{label}: no answer through the Japan proxy ({self.japan.shown()}): is it running and "
                               "connected?") from None
            raise

    # -- login state ------------------------------------------------------------------------

    def check_login(self) -> bool | None:
        """Asks DMM's API who we are (instead of guessing from cookie names): True = logged in, False = DMM says
        not logged in, None = DMM couldn't be asked (no answer, an error, a VPN address...)."""
        try:
            response = self.request(
                "POST", GRAPHQL_URL, "DMM login check", retries=2,
                json={"query": "query LoginCheck { user { __typename } }"},
                headers=GRAPHQL_HEADERS,
            )
            payload = response.json()
        except (DmmError, ValueError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            return None
        return member(payload["data"].get("user"))

    def is_logged_in(self) -> bool:
        return self.check_login() is True

    def login(self, username: str, password: str) -> None:
        """Log in with email and password, like the browser's login form."""
        page = self.get(LOGIN_URL.format(path=quote(LIBRARY_URL, safe="")), "DMM login page", retries=3)
        soup = BeautifulSoup(page.text, "html.parser")
        form = _login_form(soup)
        if form is None:
            raise LoginError("DMM's login page has changed (no login form found): this version can't log in any more.")
        target = urljoin(page.url, str(form.get("action") or ""))
        parts = urlsplit(target)
        if parts.scheme != "https" or parts.hostname != LOGIN_HOST:
            raise LoginError("DMM's login form points somewhere unexpected; not sending the password there.")
        data = {str(f.get("name")): f.get("value", "") for f in form.find_all("input") if f.get("name")}
        data.update({"login_id": username, "password": password, "use_auto_login": "1"})
        try:
            answer = self.request("POST", target, "DMM login", retries=1, data=data,
                                  allow_redirects=False, headers={"Referer": page.url})
        except DmmError as exc:
            raise LoginError(f"DMM refused the login ({exc}).") from exc
        # A correct login answers with a redirect (its body is just an address); a wrong one shows the form again.
        form_again = not answer.is_redirect and _login_form(BeautifulSoup(answer.text or "", "html.parser")) is not None
        if form_again or not self.is_logged_in():
            raise LoginError(
                "DMM did not accept the login. Check the email and password. If they are right, DMM wants an "
                "extra check (a captcha or a confirmation) for this login: log in once at dmm.co.jp in a browser on "
                "this computer, then try again."
            )
        self.account = username.strip()

    # -- saved session ----------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Write the cookies to `path`; from then on, cookies DMM renews are written there too."""
        with self._lock:
            cookies = [
                {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "expires": c.expires, "secure": c.secure}
                for c in self.http.cookies
                if c.domain.endswith("dmm.co.jp") and c.value
            ]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
                json.dump({"account": self.account, "cookies": cookies}, handle)
            os.replace(tmp, path)
            self.save_path = path

    def load(self, path: Path, account: str = "") -> bool:
        """Cookies from a saved session; cookies DMM renews are then written back to `path`. A session saved for
        another DMM account than `account` is not used (False): jav-it decrypts with `account`'s login, so the
        library and the download links must come from the same account. Sessions saved before the account was
        written down are taken as `account`'s."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        saved_for = str(data.get("account") or "").strip()
        if account and saved_for and saved_for.casefold() != account.strip().casefold():
            return False
        now = time.time()
        with self._lock:
            self.account = saved_for or account.strip()
            for c in data.get("cookies") or []:
                if not isinstance(c, dict) or not c.get("name") or (c.get("expires") and c["expires"] < now):
                    continue
                domain = str(c.get("domain") or COOKIE_DOMAIN)
                if not domain.endswith("dmm.co.jp"):
                    continue
                self.http.cookies.set(c["name"], str(c.get("value") or ""), domain=domain, path=c.get("path") or "/",
                                      expires=c.get("expires"), secure=bool(c.get("secure")))
            self.save_path = path
        return True


def member(user) -> bool:
    """Whether DMM's API answer for `user` is a logged-in member."""
    return isinstance(user, dict) and user.get("__typename") == "Member"


def _login_form(soup: BeautifulSoup):
    for form in soup.find_all("form"):
        names = {str(field.get("name") or "") for field in form.find_all("input")}
        if {"login_id", "password"} <= names:
            return form
    return None


def premium_detail_url(cid: str) -> str:
    return f"https://www.dmm.co.jp/monthly/premium/-/detail/=/cid={quote(cid, safe='')}/"


def vrchannel_detail_url(cid: str) -> str:
    """A video's page in DMM's VR Channel (the monthly VR subscription, 月額 VR チャンネル)."""
    return f"https://www.dmm.co.jp/monthly/vr/-/detail/=/cid={quote(cid, safe='')}/"


def cid_from_url(url: str) -> str | None:
    match = re.search(r"/cid=([^/?#]+)", str(url or ""))
    return match.group(1).lower() if match else None
