"""Finding a video's download links (Premium page, VR Channel page or your library) and picking the best quality."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .dmm import (GRAPHQL_HEADERS, GRAPHQL_URL, LIBRARY_URL, LOGIN_AGAIN, LOGIN_HOSTS, REGION_REFUSED, DmmError,
                  DmmSession, cid_from_url, member, premium_detail_url, vrchannel_detail_url)

log = logging.getLogger(__name__)

LIBRARY_PLAY_URL = "https://www.dmm.co.jp/digital/-/mylibrary/ajax-play-url/=/cid={cid}/shop={shop}/device=pc/"
LIBRARY_DETAIL_URL = "https://www.dmm.co.jp/digital/{shop}/-/detail/=/cid={cid}/"
PROXY_URL = "https://www.dmm.co.jp/monthly/premium/-/proxy/=/product_id={pid}/transfer_type=download/rate={rate}{part}/drm=1/ftype=dcv"
LIBRARY_SHOPS = ("videoa", "videoc")  # DMM's shops for normal and amateur videos

# The account's library from DMM's API, as DMM's own library page reads it: which videos are in it, in which
# shop, VR or not, and whether DMM offers a download (not only streaming).
LIBRARY_QUERY = """
query DmmLibrary($offset: Int!, $limit: Int!) {
  user {
    __typename
    ... on Member {
      ppvLibrary {
        contentViewingRightsSummaryList(filter: {displayStatus: VISIBLE}, offset: $offset, limit: $limit,
                                        sort: VIEWING_RIGHTS_ACQUIRED_AT_DESC) {
          items {
            content { id title floor contentType }
            contentItem { latestViewingRightsAcquiredAt }
            highestDownloadQualityProductItem { id }
          }
          pageInfo { hasNext }
        }
      }
    }
  }
}
""".strip()
LIBRARY_PAGE = 100
LIBRARY_MAX_PAGES = 200
LIBRARY_MAX_AGE = 600  # seconds: an id missing from an older list makes it be read again (once)
FLOOR_SHOPS = {"AV": "videoa", "VIDEO": "videoa", "AMATEUR": "videoc", "ANIME": "anime", "CINEMA": "nikkatsu", "VR": "videovr"}
# VR qualities, best first (8k is HEVC and smaller than uhq; 12000/6000/4000 are the older encodes).
VR_QUALITY_SCORE = {"8k": 3_000_000, "uhq": 2_000_000, "hq": 1_500_000}

# Where to download from. "auto" asks each of the others in this order: Premium (2D), the VR Channel (VR videos in
# DMM's monthly VR subscription), then the account's library (bought videos).
SOURCES = ("auto", "premium", "vrchannel", "library")
SOURCE_NAMES = {"premium": "DMM Premium", "vrchannel": "DMM VR Channel", "library": "your library"}
NOT_IN_VRCHANNEL = "not in DMM's VR Channel"
NO_VRCHANNEL = "your DMM account has no VR Channel subscription (DMM's monthly VR plan)"
# DMM gives the VR Channel's qualities, download list and links only to a normal browser in Japan (the Japan route).
NEEDS_JAPAN = ("in DMM's VR Channel, but DMM gives its downloads only in Japan: set a Japan proxy (Japan route, "
               "Settings > Downloads)")
# A subscriber's VR Channel page asks for its download list here (a POST, "download-vr", with the page's qualities);
# the address is read from the page.
VRCHANNEL_PANEL_RE = re.compile(r"[\"'](/monthly/vr/-/detail/ajax-panel/=/cid=[a-z0-9_]+/)[\"']")
VR_RATE_PATTERN_RE = re.compile(r"\bvr_rate_pattern\s*=\s*(\{.*?\})\s*;", re.S)
VRCHANNEL_TOP = "https://www.dmm.co.jp/monthly/vr/"  # the Japan route's check
JAPAN = timezone(timedelta(hours=9))  # DMM's dates


def source_word(source: str) -> str:
    """A source as the steps and results name it: premium, VR Channel, library."""
    return "VR Channel" if source == "vrchannel" else source


class NoLinks(Exception):
    """DMM offers no download of this video to this account."""


class LoginExpired(NoLinks):
    """DMM's API says the session is no longer logged in."""


@dataclass
class Choice:
    source: str  # "premium", "vrchannel" or "library"
    label: str  # quality, e.g. "4k", "6000" or "4k/vp9"
    urls: list[str]  # one per part, in part order
    options: dict[str, int] = field(default_factory=dict)  # every quality offered -> number of parts
    page_cid: str | None = None  # the id DMM's page answered for, when different
    vr: bool = False  # a VR video (.wsdcf files: decrypted with VR.MOD into .mp4, else saved encrypted)
    versions: dict[str, list[str]] = field(default_factory=dict)  # every quality offered -> its links, parts in order
    referer: str = ""  # the page the download links are opened "from" ("": as download.resolve decides)
    japan: bool = False  # the links are opened through the Japan route (VR Channel), as a normal browser

    def with_label(self, label: str) -> "Choice":
        """The same video in another of its qualities (one from `options`)."""
        if label not in self.versions:
            raise NoLinks(f"quality {label!r} is not offered (offered: {', '.join(self.versions)})")
        return Choice(self.source, label, self.versions[label], self.options, self.page_cid, self.vr, self.versions,
                      self.referer, self.japan)


@dataclass(frozen=True)
class LibraryItem:
    shop: str
    vr: bool
    downloadable: bool
    title: str = ""
    bought: str = ""  # when the account got it (DMM's latestViewingRightsAcquiredAt), e.g. "2026-01-02T03:04:05Z"


def url_segment(url: str, name: str) -> str:
    match = re.search(rf"/{re.escape(name)}=([^/?#]+)", str(url or ""))
    return match.group(1) if match else ""


def extract_page_links(html: str) -> dict[str, list[str]]:
    """Download links on a DMM detail page, by rate: the real links, else the page's script data."""
    soup = BeautifulSoup(html or "", "html.parser")
    links: dict[str, list[str]] = {}
    for anchor in soup.find_all("a", href=re.compile(r"ftype=dcv")):
        href = urljoin("https://www.dmm.co.jp", str(anchor.get("href") or ""))
        rate = url_segment(href, "rate")
        if rate and href not in links.setdefault(rate, []):
            links[rate].append(href)
    if links:
        return links

    for script in soup.find_all("script"):
        text = script.string or ""
        for _, block in re.findall(r"(\d+)\s*:\s*\{([^}]+)\}", text, re.DOTALL):
            pid = re.search(r"product(?:\\u005f|_)id\s*:\s*['\"]([^'\"]+)['\"]", block)
            rate = re.search(r"(?<!_)rate\s*:\s*['\"]([^'\"]+)['\"]", block)
            volume = re.search(r"volume\s*:\s*['\"]?(\d+)['\"]?", block)
            if not (pid and rate):
                continue
            product_id = pid.group(1).replace("\\u005f", "_")
            parts = int(volume.group(1)) if volume else 1
            for number in range(1, parts + 1):
                part = f"/part={number}" if parts > 1 else ""
                url = PROXY_URL.format(pid=product_id, rate=rate.group(1), part=part)
                if url not in links.setdefault(rate.group(1), []):
                    links[rate.group(1)].append(url)
        if links:
            break
    return links


def is_vr_link(url: str) -> bool:
    lower = url.lower()
    return "content_type=vr" in lower and "transfer_type=download" in lower


def extract_json_links(payload) -> dict[str, list[str]]:
    """Download links anywhere in the library's JSON answer, by rate (VR: by quality, e.g. 8k, uhq)."""
    found: list[str] = []

    def visit(value) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
        elif isinstance(value, str) and ("ftype=dcv" in value.lower() or is_vr_link(value)):
            url = urljoin("https://www.dmm.co.jp", value.strip())
            parts = urlsplit(url)
            if parts.scheme == "https" and (parts.hostname or "").endswith(".dmm.co.jp"):
                found.append(url)

    visit(payload)
    links: dict[str, list[str]] = {}
    for url in found:
        rate = url_segment(url, "rate") or url_segment(url, "quality") or "default"
        if url not in links.setdefault(rate, []):
            links[rate].append(url)
    return links


def _rate_score(rate: str) -> int:
    text = rate.lower()
    if text in VR_QUALITY_SCORE:
        return VR_QUALITY_SCORE[text]
    if "4k" in text:
        return 1_000_000
    match = re.search(r"\d+", text)
    return int(match.group()) if match else 0


def _codec_score(codec: str) -> int:
    text = codec.lower()
    return 2 if "vp9" in text else 1 if ("h264" in text or "avc" in text) else 0


def _part_number(url: str) -> int:
    return part_of(url) or 1


def part_of(url: str) -> int | None:
    """DMM's part number in a download link (".../part=2/..."), or None."""
    try:
        number = int(url_segment(url, "part"))
    except ValueError:
        return None
    return number if number > 0 else None


def part_numbers(urls: list[str]) -> list[int]:
    """The parts' numbers for naming: DMM's own when every link has a different one, else 1, 2, 3..."""
    numbers = [part_of(url) for url in urls]
    if all(numbers) and len(set(numbers)) == len(numbers):
        return numbers
    return list(range(1, len(urls) + 1))


def group_links(links: dict[str, list[str]]) -> dict[str, list[str]]:
    """One entry per downloadable version (rate, codec, product), parts in order."""
    groups: dict[tuple, list[str]] = {}
    for fallback_rate, urls in links.items():
        for url in urls:
            # A VR quality is its own product at DMM: the quality alone names the version.
            product = "" if is_vr_link(url) else url_segment(url, "product_id")
            key = (url_segment(url, "rate") or fallback_rate, url_segment(url, "codec"), product)
            if url not in groups.setdefault(key, []):
                groups[key].append(url)
    products = {key[2] for key in groups}
    result: dict[str, list[str]] = {}
    for (rate, codec, product), urls in groups.items():
        label = rate + (f"/{codec}" if codec else "") + (f" ({product})" if len(products) > 1 and product else "")
        result[label] = sorted(urls, key=_part_number)
    return result


def quality_order(labels) -> list[str]:
    """Quality labels best first, as choose() ranks them (VR 8k, uhq, hq; then 4K; then the highest rate)."""
    def rank(label: str) -> tuple:
        rate, _, rest = label.partition("/")
        return (_rate_score(rate.split(" ")[0]), _codec_score(rest))
    return sorted(labels, key=rank, reverse=True)


def choose(links: dict[str, list[str]], wanted: str | None = None) -> tuple[str, list[str]]:
    """The best version (4K first, then the highest rate, VP9 before H.264), or the wanted one."""
    versions = group_links(links)
    if not versions:
        raise NoLinks("no download links")
    if wanted:
        matches = [label for label in versions if label == wanted or label.split("/")[0].split(" ")[0] == wanted]
        if not matches:
            raise NoLinks(f"quality {wanted!r} is not offered (offered: {', '.join(versions)})")
        versions = {label: versions[label] for label in matches}

    def rank(label: str) -> tuple:
        rate, _, rest = label.partition("/")
        return (_rate_score(rate.split(" ")[0]), _codec_score(rest), len(versions[label]))

    best = max(versions, key=rank)
    return best, versions[best]


def premium_links(session: DmmSession, cid: str) -> tuple[dict[str, list[str]], str | None]:
    response = session.get(premium_detail_url(cid), f"Premium page of {cid}")
    page_cid = cid_from_url(response.url)
    return extract_page_links(response.text), page_cid if page_cid and page_cid != cid else None


def extract_vrchannel_links(html: str) -> dict[str, list[str]]:
    """The download links in the VR Channel's download list, by quality: one list per quality
    (<ul id="download_rate_uhq">), one link per part ("/monthly/vr/-/proxy/=/product_id=.../transfer_type=download/
    rate=uhq/part=1"). Only DMM's own https links count."""
    links: dict[str, list[str]] = {}
    for anchor in BeautifulSoup(html or "", "html.parser").find_all("a", href=True):
        url = urljoin("https://www.dmm.co.jp", str(anchor["href"]).strip())
        parts = urlsplit(url)
        if parts.scheme != "https" or not (parts.hostname or "").endswith(".dmm.co.jp"):
            continue
        if "/-/proxy/" not in parts.path or "transfer_type=download" not in parts.path:
            continue
        rate = url_segment(url, "rate")
        if rate and url not in links.setdefault(rate, []):
            links[rate].append(url)
    return links


def jquery_form(name: str, value) -> list[tuple[str, str]]:
    """`value` as a form field the way the page's jQuery sends it: nested objects as name[key][key]=value, null as ""."""
    if isinstance(value, dict):
        return [pair for key, nested in value.items() for pair in jquery_form(f"{name}[{key}]", nested)]
    if isinstance(value, list):
        return [pair for index, nested in enumerate(value) for pair in jquery_form(f"{name}[{index}]", nested)]
    if value is None:
        return [(name, "")]
    if isinstance(value, bool):
        return [(name, "true" if value else "false")]
    return [(name, str(value))]


def vrchannel_start(soup: BeautifulSoup) -> date | None:
    """The day the VR Channel starts offering the video: its page's 配信開始日 ("2026/12/15")."""
    for term in soup.find_all("dt"):
        if "配信開始日" in term.get_text():
            value = term.find_next_sibling("dd")
            match = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", value.get_text() if value else "")
            if match:
                try:
                    return date(*map(int, match.groups()))
                except ValueError:
                    return None
    return None


def japan_check(session: DmmSession) -> tuple[bool | None, str]:
    """Whether the Japan route reaches DMM as a visitor in Japan: True, False (DMM's "not available in your region"),
    or None with the reason when it couldn't be asked (a proxy that doesn't answer, no connection)."""
    try:
        answer = session.japan_request("GET", VRCHANNEL_TOP, "Japan route check", retries=1, timeout=(10, 30))
    except DmmError as exc:
        return None, str(exc)
    if REGION_REFUSED in (answer.text or ""):
        return False, f"DMM answered “{REGION_REFUSED}” through {session.japan.shown()}"
    return True, ""


def vrchannel_links(session: DmmSession, cid: str) -> tuple[dict[str, list[str]], str | None]:
    """Download links of a video in the VR Channel (DMM's monthly VR subscription) and the id DMM's page answered for
    when it is another one. Raises NoLinks with the reason.

    1. The video's page, as the tool's own browser name, from any address: 404 = not in the channel; a "join the
       channel" offer = no subscription (or logged out); no download tab but a start date to come = not yet; a
       subscriber's page has the script that asks for the download list.
    2. Through the Japan route (`session.japan`), as a normal browser in Japan, as the page itself does: the page again
       (now with its qualities, vr_rate_pattern), then the download list (a POST, "download-vr", with those qualities).
       Outside Japan DMM answers "not available in your region" to a browser and leaves the qualities out for
       Googlebot."""
    try:
        response = session.get(vrchannel_detail_url(cid), f"VR Channel page of {cid}", retries=2)
    except DmmError as exc:
        if exc.status == 404:
            raise NoLinks(NOT_IN_VRCHANNEL) from None
        raise
    final = urlsplit(response.url)
    if (final.hostname or "").lower() in LOGIN_HOSTS:
        raise LoginExpired(f"DMM asks to log in before showing the VR Channel page: {LOGIN_AGAIN}")
    page_cid = cid_from_url(response.url)
    if "/monthly/vr/" not in final.path or not page_cid:  # DMM sent it elsewhere (the channel's top page, a shop)
        raise NoLinks(NOT_IN_VRCHANNEL)
    other = page_cid if page_cid != cid else None
    html = response.text or ""
    panel = VRCHANNEL_PANEL_RE.search(html)
    if panel:
        return vrchannel_download_list(session, cid, response.url, urljoin(response.url, panel.group(1))), other
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one(".admission-line-block") or soup.find("a", href=re.compile(r"/monthly/vr/-/basket/")):
        if session.check_login() is False:  # DMM shows the same "join" offer to a logged-out visitor
            raise LoginExpired(f"DMM says you are not logged in: {LOGIN_AGAIN}")
        raise NoLinks(NO_VRCHANNEL)
    start = vrchannel_start(soup)
    if start and start > datetime.now(JAPAN).date():
        raise NoLinks(f"in DMM's VR Channel from {start.isoformat()} (not yet)")
    if session.check_login() is False:
        raise LoginExpired(f"DMM says you are not logged in: {LOGIN_AGAIN}")
    raise NoLinks("DMM's VR Channel page of it offers no download (streaming only?)")


def vrchannel_download_list(session: DmmSession, cid: str, page_url: str, panel_url: str) -> dict[str, list[str]]:
    """Step 2 of vrchannel_links: the qualities and the download list, through the Japan route."""
    try:
        page = session.japan_request("GET", page_url, f"VR Channel page of {cid} (from Japan)", retries=2)
        text = page.text or ""
        if REGION_REFUSED in text:
            raise NoLinks(NEEDS_JAPAN)
        match = VR_RATE_PATTERN_RE.search(text)
        try:
            qualities = json.loads(match.group(1)) if match else None
        except ValueError:
            qualities = None
        if not isinstance(qualities, dict) or not qualities:
            raise NoLinks("DMM's VR Channel page gave no qualities for it, also in Japan (try again later)")
        answer = session.japan_request(
            "POST", panel_url, f"VR Channel download list of {cid}", retries=2,
            data=[("type", "download-vr"), *jquery_form("vr_rate_pattern", qualities)],
            headers={"Referer": page_url, "X-Requested-With": "XMLHttpRequest", "Origin": "https://www.dmm.co.jp"},
        )
    except DmmError as exc:  # the route is down, or DMM refused it: the lookup goes on with the next source
        raise NoLinks(f"DMM's VR Channel through the Japan route: {exc}") from None
    if REGION_REFUSED in (answer.text or ""):
        raise NoLinks(NEEDS_JAPAN)
    links = extract_vrchannel_links(answer.text)
    if not links:
        raise NoLinks("DMM's VR Channel gave no download link for it (try again later)")
    return links


def library_list(session: DmmSession) -> dict[str, LibraryItem] | None:
    """The account's library (content id -> item), or None when DMM's API doesn't answer it. Asked once per run."""
    if "library" in session.cache:
        return session.cache["library"]
    items: dict[str, LibraryItem] | None = {}
    try:
        for page in range(LIBRARY_MAX_PAGES):
            answer = session.request(
                "POST", GRAPHQL_URL, "DMM library list", retries=2, headers=GRAPHQL_HEADERS,
                json={"query": LIBRARY_QUERY, "variables": {"offset": page * LIBRARY_PAGE, "limit": LIBRARY_PAGE}},
            ).json()
            user = answer["data"]["user"]
            if not member(user):  # DMM answered, but not for a logged-in member: not cached, the next try asks again
                raise LoginExpired(f"DMM says you are not logged in: {LOGIN_AGAIN}")
            listing = user["ppvLibrary"]["contentViewingRightsSummaryList"]
            for entry in listing["items"]:
                content = entry.get("content") or {}
                cid = str(content.get("id") or "").strip().lower()
                if cid:
                    items[cid] = LibraryItem(
                        FLOOR_SHOPS.get(str(content.get("floor") or "").upper(), "videoa"),
                        str(content.get("contentType") or "").upper() == "VR",
                        bool(entry.get("highestDownloadQualityProductItem")),
                        str(content.get("title") or ""),
                        str((entry.get("contentItem") or {}).get("latestViewingRightsAcquiredAt") or ""),
                    )
            if not listing["pageInfo"]["hasNext"] or not listing["items"]:
                break
        log.info("DMM library: %d video(s)", len(items))
    except (DmmError, ValueError, KeyError, TypeError, AttributeError) as exc:
        log.info("DMM library list not available (%s); asking per video", exc)
        items = None
    session.cache["library"] = items
    session.cache["library_at"] = time.monotonic()
    return items


def library_links(session: DmmSession, cid: str) -> tuple[dict[str, list[str]], bool]:
    """Download links from the account's library and whether it is a VR video. Raises NoLinks with the reason."""
    listing = library_list(session)
    if listing is not None and cid not in listing and time.monotonic() - session.cache.get("library_at", 0) > LIBRARY_MAX_AGE:
        # Bought after the list was read? Read it once more before saying it isn't there.
        log.info("%s isn't in the library list read earlier: reading it again", cid)
        session.cache.pop("library", None)
        listing = library_list(session)
    item = listing.get(cid) if listing is not None else None
    if listing is not None:
        if item is None:
            raise NoLinks("not in your DMM library")
        if not item.downloadable:
            raise NoLinks("in your DMM library for streaming only (no download)")
    headers = {"Referer": LIBRARY_URL, "X-Requested-With": "XMLHttpRequest"}
    for shop in (item.shop,) if item else LIBRARY_SHOPS:
        url = LIBRARY_PLAY_URL.format(cid=quote(cid, safe=""), shop=shop)
        try:
            payload = session.get(url, f"library data of {cid} ({shop})", retries=2, headers=headers).json()
        except (DmmError, ValueError) as exc:
            log.info("library data of %s (%s): %s", cid, shop, exc)
            continue
        links = extract_json_links(payload)
        if links:
            vr = (item.vr if item else False) or any(is_vr_link(u) for urls in links.values() for u in urls)
            return links, vr
    if item is None:  # library list unknown: the old way, the detail pages
        for shop in LIBRARY_SHOPS:
            url = LIBRARY_DETAIL_URL.format(cid=quote(cid, safe=""), shop=shop)
            try:
                response = session.get(url, f"library page of {cid} ({shop})", retries=2, headers=headers)
            except DmmError as exc:
                log.info("library page of %s (%s): %s", cid, shop, exc)
                continue
            text = response.text
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    text = next((v for k in ("html", "body", "detail", "data") if isinstance(v := payload.get(k), str)), text)
            except ValueError:
                pass
            links = extract_page_links(text)
            if links:
                return links, False
        raise NoLinks("no download in your library (not bought, a rental, or streaming only)")
    raise NoLinks("in your DMM library, but DMM gave no download link for it (try again later)")


def find(session: DmmSession, cid: str, source: str = "auto", wanted: str | None = None, skip=()) -> Choice:
    """Where and in which quality to download a video. Raises NoLinks with the reason. `skip`: sources "auto" doesn't
    ask again (one that listed the video but didn't give this account its download)."""
    reasons = []
    no_premium_page = False  # DMM has no Premium page for it (HTTP 404), as for every VR video
    if source in ("auto", "premium") and "premium" not in skip:
        try:
            links, page_cid = premium_links(session, cid)
        except DmmError as exc:  # VR videos have no Premium page (HTTP 404): the VR Channel or library may have them
            if "HTTP 404" not in str(exc) and source == "premium":
                raise
            log.info("%s", exc)
            links, page_cid = {}, None
            no_premium_page = "HTTP 404" in str(exc)
            if not no_premium_page:
                reasons.append(f"DMM Premium didn't answer ({exc})")
        if links:
            try:
                label, urls = choose(links, wanted)
            except NoLinks as exc:  # the wanted quality isn't on Premium: "auto" looks further
                if source == "premium":
                    raise
                reasons.append(f"DMM Premium: {exc}")
            else:
                versions = group_links(links)
                return Choice("premium", label, urls, {k: len(v) for k, v in versions.items()}, page_cid, versions=versions)
        if not reasons:
            reasons.append("not on DMM Premium (or your account has no Premium)")
    if source in ("auto", "vrchannel") and "vrchannel" not in skip:
        try:
            links, page_cid = vrchannel_links(session, cid)
        except LoginExpired:
            raise
        except (NoLinks, DmmError) as exc:
            if source == "vrchannel":
                raise
            log.info("%s: VR Channel: %s", cid, exc)
            # "Not in the VR Channel" only matters for a VR video (no Premium page): 2D videos never are.
            if str(exc) != NOT_IN_VRCHANNEL or no_premium_page:
                reasons.append(str(exc) if isinstance(exc, NoLinks) else f"DMM VR Channel didn't answer ({exc})")
        else:
            try:
                label, urls = choose(links, wanted)
            except NoLinks as exc:  # the wanted quality isn't in the VR Channel: "auto" tries the library
                if source == "vrchannel":
                    raise
                reasons.append(f"DMM VR Channel: {exc}")
            else:
                versions = group_links(links)
                return Choice("vrchannel", label, urls, {k: len(v) for k, v in versions.items()}, page_cid, vr=True,
                              versions=versions, referer=vrchannel_detail_url(page_cid or cid), japan=True)
    if source in ("auto", "library") and "library" not in skip:
        try:
            links, vr = library_links(session, cid)
        except LoginExpired:
            raise  # the real reason: not "not in your library"
        except NoLinks as exc:
            reasons.append(str(exc))
        else:
            try:
                label, urls = choose(links, wanted)
            except NoLinks as exc:
                raise NoLinks("; ".join([*reasons, f"your library: {exc}"])) from None
            versions = group_links(links)
            return Choice("library", label, urls, {k: len(v) for k, v in versions.items()}, vr=vr, versions=versions)
    raise NoLinks("; ".join(reasons))
