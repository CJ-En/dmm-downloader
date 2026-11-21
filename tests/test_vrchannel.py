"""DMM's VR Channel (the monthly VR subscription) as a source: its page, the order "auto" asks the sources in, the
messages, and the fallback to the library. Pages are made up, shaped like DMM's."""

import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dmm_downloader import config, download, links, pipeline
from dmm_downloader import fetch as fetch_module
from dmm_downloader.credentials import Login
from dmm_downloader.dmm import (BROWSER_USER_AGENT, DmmError, DmmSession, JapanRoute, valid_japan_route,
                                vrchannel_detail_url)
from dmm_downloader.fetch import FileRefused
from dmm_downloader.links import (NEEDS_JAPAN, NO_VRCHANNEL, NOT_IN_VRCHANNEL, LoginExpired, NoLinks, find,
                                  vrchannel_links)

from .fakes import MEDIA_TOOLS, MEDIAINFO, copy_to, make_clip, make_fake_javit

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CHANNEL = "https://www.dmm.co.jp/monthly/vr/-/detail/=/cid={cid}/"
PANEL = "https://www.dmm.co.jp/monthly/vr/-/detail/ajax-panel/=/cid={cid}/"
# A VR Channel download link as its download list gives it (2026-11).
VR_LINK = "/monthly/vr/-/proxy/=/product_id={pid}/transfer_type=download/rate={q}/part={part}"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class Page:
    def __init__(self, url, text, status_code=200):
        self.url, self.text, self.status_code = url, text, status_code


class ChannelSession:
    """Answers the VR Channel page like DMM: as the tool's own browser name (get) the page anywhere; through the Japan
    route (japan_request) the page with its qualities (GET) and the download list (POST); or fails as given."""

    def __init__(self, text="", url=None, error=None, logged_in=True, japan_page="", panel="", japan_error=None):
        self.text, self.url, self.error, self.logged_in = text, url, error, logged_in
        self.japan_page, self.panel, self.japan_error = japan_page, panel, japan_error
        self.asked, self.japan_asked, self.cache = [], [], {}
        self.japan = JapanRoute("http://127.0.0.1:25345")

    def get(self, url, label, **kwargs):
        self.asked.append(url)
        if self.error:
            raise self.error
        return Page(self.url or url, self.text)

    def japan_request(self, method, url, label, retries=None, **kwargs):
        self.japan_asked.append((method, url, kwargs.get("data"), kwargs.get("headers")))
        if self.japan_error:
            raise self.japan_error
        return Page(url, self.japan_page if method == "GET" else self.panel)

    def check_login(self):
        return self.logged_in


def subscriber(**kwargs):
    """A subscriber's session: the page as Googlebot (no qualities), from Japan with them, and the download list."""
    return ChannelSession(fixture("vrchannel-subscriber.html"), japan_page=fixture("vrchannel-subscriber-japan.html"),
                          panel=fixture("vrchannel-panel.html"), **kwargs)


class ChannelPageTest(unittest.TestCase):
    def test_the_page_address(self):
        self.assertEqual(vrchannel_detail_url("mdvr00244"), CHANNEL.format(cid="mdvr00244"))

    def test_links_by_quality_and_part(self):
        session = subscriber()
        found, page_cid = vrchannel_links(session, "abcd00123")
        self.assertIsNone(page_cid)
        self.assertEqual(sorted(found), ["6000", "hq", "uhq"])
        label, urls = links.choose(found)
        self.assertEqual(label, "uhq")  # the channel's best for this video (no 8k)
        self.assertEqual([links.part_of(u) for u in urls], [1, 2, 3])
        self.assertEqual(urls[0], "https://www.dmm.co.jp/monthly/vr/-/proxy/=/product_id=abcd00123dl6/"
                                  "transfer_type=download/rate=uhq/part=1")
        # Only the qualities and the list went through the Japan route: the page from there, then the list.
        page = CHANNEL.format(cid="abcd00123")
        self.assertEqual(session.asked, [page])
        (get, post) = session.japan_asked
        self.assertEqual(get[:2], ("GET", page))
        method, url, data, headers = post
        self.assertEqual((method, url), ("POST", PANEL.format(cid="abcd00123")))
        self.assertEqual(headers["Referer"], page)
        self.assertEqual(headers["X-Requested-With"], "XMLHttpRequest")
        # The page's qualities, sent as its jQuery sends them.
        self.assertEqual(data[0], ("type", "download-vr"))
        self.assertIn(("vr_rate_pattern[uhq][product_id]", "abcd00123dl6"), data)
        self.assertIn(("vr_rate_pattern[6000][parts]", "3"), data)
        self.assertIn(("vr_rate_pattern[hq][shop_full_name]", "shop_month_vr"), data)

    def test_the_form_as_jquery_sends_it(self):
        self.assertEqual(links.jquery_form("p", {"a": {"b": 1, "c": None}, "d": [True, "x"]}),
                         [("p[a][b]", "1"), ("p[a][c]", ""), ("p[d][0]", "true"), ("p[d][1]", "x")])

    def test_links_elsewhere_are_ignored(self):
        panel = ('<a href="https://evil.example/monthly/vr/-/proxy/=/product_id=x/transfer_type=download/rate=uhq/part=1">x</a>'
                 '<a href="/monthly/vr/-/proxy/=/product_id=x/transfer_type=stream/rate=uhq/part=1">streaming</a>'
                 '<a href="http://www.dmm.co.jp/monthly/vr/-/proxy/=/product_id=x/transfer_type=download/rate=hq/part=1">http</a>')
        session = subscriber()
        session.panel = panel
        with self.assertRaises(NoLinks) as caught:
            vrchannel_links(session, "abcd00123")
        self.assertIn("gave no download link", str(caught.exception))

    def test_outside_japan(self):
        # The route isn't in Japan for DMM: a normal browser gets "not available in your region".
        session = subscriber()
        session.japan_page = fixture("vrchannel-region.html")
        with self.assertRaises(NoLinks) as caught:
            vrchannel_links(session, "abcd00123")
        self.assertEqual(str(caught.exception), NEEDS_JAPAN)
        self.assertEqual(len(session.japan_asked), 1)  # no download list asked for
        session = subscriber()
        session.panel = fixture("vrchannel-region.html")
        with self.assertRaises(NoLinks) as caught:
            vrchannel_links(session, "abcd00123")
        self.assertEqual(str(caught.exception), NEEDS_JAPAN)

    def test_no_qualities(self):
        # From Japan too, a page without its qualities (as Googlebot gets it): nothing to ask the list for.
        session = subscriber()
        session.japan_page = fixture("vrchannel-subscriber.html")
        with self.assertRaises(NoLinks) as caught:
            vrchannel_links(session, "abcd00123")
        self.assertIn("gave no qualities", str(caught.exception))

    def test_a_route_that_is_down(self):
        session = subscriber(japan_error=DmmError("VR Channel page of abcd00123 (from Japan): no answer through the "
                                                  "Japan proxy (http://127.0.0.1:25345): is it running and connected?"))
        with self.assertRaises(NoLinks) as caught:
            vrchannel_links(session, "abcd00123")
        self.assertTrue(str(caught.exception).startswith("DMM's VR Channel through the Japan route: "))
        self.assertIn("is it running", str(caught.exception))

    def test_not_in_the_channel(self):
        # DMM answers 404 for a video the channel doesn't have (seen for a 2D video), from anywhere.
        session = ChannelSession(error=DmmError("VR Channel page of abc00123: DMM answered HTTP 404", 404))
        with self.assertRaises(NoLinks) as caught:
            vrchannel_links(session, "abc00123")
        self.assertEqual(str(caught.exception), NOT_IN_VRCHANNEL)
        self.assertEqual(session.japan_asked, [])  # the Japan route isn't needed to know that
        # DMM sending the request to the channel's top page (no video) means the same.
        with self.assertRaises(NoLinks) as caught:
            vrchannel_links(ChannelSession("<html></html>", url="https://www.dmm.co.jp/monthly/vr/"), "abc00123")
        self.assertEqual(str(caught.exception), NOT_IN_VRCHANNEL)

    def test_no_subscription(self):
        # Logged in, the video's page offers to join the channel: the account has no VR Channel plan.
        session = ChannelSession(fixture("vrchannel-join.html"))
        with self.assertRaises(NoLinks) as caught:
            vrchannel_links(session, "abcd00123")
        self.assertEqual(str(caught.exception), NO_VRCHANNEL)
        self.assertNotIsInstance(caught.exception, LoginExpired)
        self.assertEqual(session.japan_asked, [])

    def test_a_video_the_channel_offers_later(self):
        session = ChannelSession(fixture("vrchannel-later.html"))
        with self.assertRaises(NoLinks) as caught:
            vrchannel_links(session, "abcd00123")
        self.assertEqual(str(caught.exception), "in DMM's VR Channel from 2099-12-15 (not yet)")
        self.assertEqual(session.japan_asked, [])

    def test_a_logged_out_session_says_log_in_again(self):
        # Logged out, DMM shows the same "join" offer: the login is checked before saying "no subscription".
        for page in ("vrchannel-join.html", "vrchannel-later.html"):
            with self.assertRaises(LoginExpired):
                vrchannel_links(ChannelSession(fixture(page).replace("2099/", "2001/"), logged_in=False), "abcd00123")
        with self.assertRaises(LoginExpired):
            vrchannel_links(ChannelSession("", url="https://accounts.dmm.co.jp/service/login/password/"), "abcd00123")

    def test_other_dmm_errors_are_not_hidden(self):
        with self.assertRaises(DmmError):
            vrchannel_links(ChannelSession(error=DmmError("VR Channel page: no answer from DMM after 2 tries")), "x00001")


class JapanRouteTest(unittest.TestCase):
    def test_the_route(self):
        own = JapanRoute("")
        self.assertEqual((own.kind, own.proxies, own.shown()), ("own", None, "this program's own connection"))
        proxy = JapanRoute(" http://127.0.0.1:25345 ")
        self.assertEqual((proxy.kind, proxy.proxy), ("proxy", "http://127.0.0.1:25345"))
        self.assertEqual(proxy.proxies, {"http": "http://127.0.0.1:25345", "https": "http://127.0.0.1:25345"})
        self.assertEqual(JapanRoute("http://user:secret@proxy.example:8080").shown(), "http://proxy.example:8080")
        for good in ("", "http://127.0.0.1:25345", "https://proxy.example:8443/", "http://user:pw@10.0.0.1:3128"):
            self.assertTrue(valid_japan_route(good), good)
        for bad in ("127.0.0.1:25345", "socks5://127.0.0.1:1080", "http://", "http://host/path", "ftp://x"):
            self.assertFalse(valid_japan_route(bad), bad)

    def test_requests_go_as_a_browser_through_the_route(self):
        session = DmmSession("ua", retries=1, retry_delay=0)
        session.japan = JapanRoute("http://127.0.0.1:25345")
        self.addCleanup(session.close)
        with mock.patch.object(session, "request", return_value="answer") as request:
            self.assertEqual(session.japan_request("POST", "https://www.dmm.co.jp/x", "x", headers={"Referer": "r"}),
                             "answer")
        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["headers"], {"Referer": "r", "User-Agent": BROWSER_USER_AGENT})
        self.assertEqual(kwargs["proxies"], session.japan.proxies)
        session.japan = JapanRoute("")
        with mock.patch.object(session, "request", return_value="answer") as request:
            session.japan_request("GET", "https://www.dmm.co.jp/x", "x")
        self.assertNotIn("proxies", request.call_args.kwargs)  # this program's own connection

    def test_a_proxy_that_does_not_answer(self):
        session = DmmSession("ua", retries=1, retry_delay=0)
        session.japan = JapanRoute("http://user:secret@127.0.0.1:9")
        self.addCleanup(session.close)
        down = DmmError("x: no answer from DMM after 1 tries (ProxyError: cannot connect to proxy)")
        with mock.patch.object(session, "request", side_effect=down):
            with self.assertRaises(DmmError) as caught:
                session.japan_request("GET", "https://www.dmm.co.jp/x", "x")
        self.assertIn("no answer through the Japan proxy (http://127.0.0.1:9): is it running", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        refused = DmmError("x: DMM answered HTTP 403", 403)  # DMM answered: not the proxy's fault
        with mock.patch.object(session, "request", side_effect=refused):
            with self.assertRaises(DmmError) as caught:
                session.japan_request("GET", "https://www.dmm.co.jp/x", "x")
        self.assertIs(caught.exception, refused)

    def test_the_check(self):
        session = ChannelSession()
        session.japan_page = "<html>VRch</html>"
        self.assertEqual(links.japan_check(session), (True, ""))
        session.japan_page = fixture("vrchannel-region.html")
        reaches, why = links.japan_check(session)
        self.assertIs(reaches, False)
        self.assertIn("not available in your region", why)
        session.japan_error = DmmError("Japan route check: no answer through the Japan proxy")
        self.assertEqual(links.japan_check(session), (None, "Japan route check: no answer through the Japan proxy"))
        self.assertEqual({url for _, url, _, _ in session.japan_asked}, {links.VRCHANNEL_TOP})


def premium_404(cid="mdvr00244"):
    return mock.patch.object(links, "premium_links", side_effect=DmmError(f"Premium page of {cid}: DMM answered HTTP 404", 404))


def channel(result):
    """vrchannel_links answering `result`: (links, page id), or an exception."""
    if isinstance(result, Exception):
        return mock.patch.object(links, "vrchannel_links", side_effect=result)
    return mock.patch.object(links, "vrchannel_links", return_value=result)


def library(result):
    if isinstance(result, Exception):
        return mock.patch.object(links, "library_links", side_effect=result)
    return mock.patch.object(links, "library_links", return_value=result)


def vr_links(cid="mdvr00244", qualities=("8k", "uhq")):
    """Channel links as DMM's list gives them: one product id for every quality (e.g. mdvr00244dl6), two parts."""
    return {q: [f"https://www.dmm.co.jp{VR_LINK.format(pid=cid + 'dl6', part=n, q=q)}" for n in (1, 2)]
            for q in qualities}


class FindTest(unittest.TestCase):
    """links.find: auto = Premium, then the VR Channel, then the library."""

    def test_auto_takes_a_vr_video_from_the_channel(self):
        with premium_404(), channel((vr_links(), None)), library(NoLinks("not in your DMM library")) as lib:
            choice = find(mock.Mock(), "mdvr00244", "auto")
        self.assertEqual((choice.source, choice.vr, choice.label, len(choice.urls)), ("vrchannel", True, "8k", 2))
        self.assertEqual(choice.referer, CHANNEL.format(cid="mdvr00244"))  # the download links are opened "from" it
        self.assertEqual(choice.options, {"8k": 2, "uhq": 2})
        lib.assert_not_called()
        self.assertEqual(choice.with_label("uhq").referer, choice.referer)

    def test_premium_comes_first(self):
        premium = {"4k": ["https://www.dmm.co.jp/monthly/premium/-/proxy/=/product_id=abc00123/rate=4k/ftype=dcv"]}
        with mock.patch.object(links, "premium_links", return_value=(premium, None)), channel((vr_links(), None)) as ch:
            self.assertEqual(find(mock.Mock(), "abc00123", "auto").source, "premium")
        ch.assert_not_called()

    def test_auto_goes_on_to_the_library(self):
        bought = ({"8k": vr_links()["8k"]}, True)
        for reason in (NOT_IN_VRCHANNEL, NO_VRCHANNEL):
            with premium_404(), channel(NoLinks(reason)), library(bought):
                choice = find(mock.Mock(), "mdvr00244", "auto")
            self.assertEqual((choice.source, choice.vr, choice.referer), ("library", True, ""))

    def test_messages_when_nothing_has_it(self):
        # A VR video (no Premium page): every reason is said.
        with premium_404(), channel(NoLinks(NO_VRCHANNEL)), library(NoLinks("not in your DMM library")):
            with self.assertRaises(NoLinks) as caught:
                find(mock.Mock(), "mdvr00244", "auto")
        self.assertEqual(str(caught.exception), "not on DMM Premium (or your account has no Premium); "
                                                f"{NO_VRCHANNEL}; not in your DMM library")
        with premium_404(), channel(NoLinks(NOT_IN_VRCHANNEL)), library(NoLinks("not in your DMM library")):
            with self.assertRaises(NoLinks) as caught:
                find(mock.Mock(), "mdvr00244", "auto")
        self.assertIn(NOT_IN_VRCHANNEL, str(caught.exception))
        # A 2D video (its Premium page exists, without a download): "not in the VR Channel" would only confuse.
        with mock.patch.object(links, "premium_links", return_value=({}, None)), channel(NoLinks(NOT_IN_VRCHANNEL)), \
                library(NoLinks("not in your DMM library")):
            with self.assertRaises(NoLinks) as caught:
                find(mock.Mock(), "abc00123", "auto")
        self.assertEqual(str(caught.exception), "not on DMM Premium (or your account has no Premium); not in your DMM library")

    def test_vr_channel_only(self):
        with mock.patch.object(links, "premium_links") as premium, channel((vr_links(), None)), library(({}, True)) as lib:
            self.assertEqual(find(mock.Mock(), "mdvr00244", "vrchannel").source, "vrchannel")
        premium.assert_not_called()
        lib.assert_not_called()
        for reason in (NOT_IN_VRCHANNEL, NO_VRCHANNEL):
            with channel(NoLinks(reason)), library(({}, True)) as lib:
                with self.assertRaises(NoLinks) as caught:
                    find(mock.Mock(), "mdvr00244", "vrchannel")
            self.assertEqual(str(caught.exception), reason)
            lib.assert_not_called()

    def test_a_quality_the_channel_lacks(self):
        bought = ({"12000": vr_links(qualities=("12000",))["12000"]}, True)
        with premium_404(), channel((vr_links(), None)), library(bought):
            self.assertEqual(find(mock.Mock(), "mdvr00244", "auto", "12000").source, "library")
            with self.assertRaises(NoLinks) as caught:
                find(mock.Mock(), "mdvr00244", "vrchannel", "12000")
        self.assertIn("not offered", str(caught.exception))

    def test_an_expired_login_stops_the_lookup(self):
        with premium_404(), channel(LoginExpired("DMM says you are not logged in")), library(({}, True)) as lib:
            with self.assertRaises(LoginExpired):
                find(mock.Mock(), "mdvr00244", "auto")
        lib.assert_not_called()

    def test_a_channel_that_doesnt_answer(self):
        with premium_404(), channel(DmmError("VR Channel page of mdvr00244: no answer from DMM after 2 tries")), \
                library(NoLinks("not in your DMM library")):
            with self.assertRaises(NoLinks) as caught:
                find(mock.Mock(), "mdvr00244", "auto")
        self.assertIn("DMM VR Channel didn't answer", str(caught.exception))

    def test_skipped_sources(self):
        with mock.patch.object(links, "premium_links") as premium, channel((vr_links(), None)) as ch, \
                library(({"8k": vr_links()["8k"]}, True)):
            choice = find(mock.Mock(), "mdvr00244", "auto", skip=("premium", "vrchannel"))
        self.assertEqual(choice.source, "library")
        premium.assert_not_called()
        ch.assert_not_called()


class Answer:
    def __init__(self, status=200, location=None, text=""):
        self.status_code, self.text = status, text
        self.headers = {"Location": location} if location else {}

    def close(self):
        pass


class VrChannelResolveTest(unittest.TestCase):
    LINK = "https://www.dmm.co.jp" + VR_LINK.format(pid="mdvr00244dl6", part=1, q="uhq")

    def test_both_steps_go_through_the_japan_route_from_the_channel_page(self):
        session = mock.Mock()
        session.japan_request.side_effect = [
            Answer(302, "https://wsd-r.dmm.co.jp/020/mdvr00244/token"),
            Answer(302, "https://wsd-c001.dmm.com/020/mdvr00244/mdvr00244vrv1uhqf1.wsdcf?dl_key=k"),
        ]
        page = CHANNEL.format(cid="mdvr00244")
        url = download.resolve(session, self.LINK, "t", page, True)
        self.assertEqual(url, "https://wsd-c001.dmm.com/020/mdvr00244/mdvr00244vrv1uhqf1.wsdcf?dl_key=k")
        self.assertEqual([call.kwargs["headers"] for call in session.japan_request.call_args_list], [{"Referer": page}] * 2)
        session.request.assert_not_called()  # nothing of it over the tool's own route

    def test_outside_japan_the_link_is_refused_at_once(self):
        session = mock.Mock()
        session.japan_request.return_value = Answer(200, text=fixture("vrchannel-region.html"))
        with self.assertRaises(download.NotInJapan) as caught:
            download.resolve(session, self.LINK, "t", CHANNEL.format(cid="mdvr00244"), True)
        self.assertIn("not available in your region", str(caught.exception))
        target = Path(tempfile.mkdtemp(prefix="dmm channel ")) / "part1.wsdcf"
        self.addCleanup(shutil.rmtree, target.parent, True)
        with mock.patch.object(download, "resolve", side_effect=download.NotInJapan("t: not in Japan")) as resolve, \
                mock.patch.object(download.time, "sleep"):
            with self.assertRaises(download.NotInJapan):
                download.download_part(mock.Mock(user_agent="ua"), "u", target, "t", attempts=5, japan=True)
        self.assertEqual(resolve.call_count, 1)  # each try would ask DMM for a new VR link: none wasted

    def test_the_referer_and_route_reach_resolve(self):
        target = Path(tempfile.mkdtemp(prefix="dmm channel ")) / "part1.wsdcf"
        self.addCleanup(shutil.rmtree, target.parent, True)
        seen = []
        with mock.patch.object(download, "resolve", return_value="https://wsd-c003.dmm.com/f.wsdcf") as resolve, \
                mock.patch.object(download, "fetch", side_effect=lambda url, ua, partial, progress, binary_only=False,
                                  **kw: seen.append(ua) or partial.write_bytes(b"\x00" * 64)):
            download.download_part(mock.Mock(user_agent="ua"), "u", target, "t", binary_only=True, referer="page",
                                   japan=True)
        self.assertEqual(resolve.call_args.args[3:], ("page", True))
        self.assertEqual(seen, [BROWSER_USER_AGENT])  # the file is asked for as the same browser


class FileFallbackTest(unittest.TestCase):
    """A VR Channel file refused on the normal connection is downloaded through the Japan route (once)."""

    def setUp(self):
        self.target = Path(tempfile.mkdtemp(prefix="dmm channel ")) / "part1.wsdcf"
        self.addCleanup(shutil.rmtree, self.target.parent, True)
        patcher = mock.patch.object(download.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_part(self, route, helper=None, refusals=1):
        session = mock.Mock(user_agent="ua")
        session.japan = JapanRoute(route)
        calls, notices = [], []

        def fake_fetch(url, ua, partial, progress, binary_only=False, proxies=None):
            calls.append(("fetch", proxies))
            if sum(1 for c in calls) <= refusals and proxies is None:
                raise FileRefused("DMM's file server answered HTTP 404", 404)
            partial.write_bytes(b"\x00" * 64)

        def fake_program(command, url, ua, partial, progress=None, flags=0, binary_only=False):
            calls.append(("program", None))
            raise FileRefused("DMM's file server answered HTTP 403", 403)

        with mock.patch.object(download, "resolve", return_value="https://wsd-c001.dmm.com/f.wsdcf") as resolve, \
                mock.patch.object(download, "fetch", side_effect=fake_fetch), \
                mock.patch.object(download, "fetch_with_program", side_effect=fake_program):
            try:
                download.download_part(session, "u", self.target, "t", attempts=2, notice=notices.append,
                                       helper=helper, binary_only=True, japan=True)
                ok = True
            except download.DownloadError as exc:
                ok = str(exc)
        return ok, calls, notices, resolve.call_count

    def test_through_the_proxy_after_a_refusal(self):
        ok, calls, notices, resolves = self.run_part("http://127.0.0.1:25345")
        self.assertIs(ok, True)
        self.assertEqual(calls, [("fetch", None), ("fetch", JapanRoute("http://127.0.0.1:25345").proxies)])
        self.assertEqual(resolves, 2)  # a fresh link for the second way
        self.assertEqual(len(notices), 1)
        self.assertIn("through the Japan route (http://127.0.0.1:25345)", notices[0])
        self.assertTrue(self.target.exists())

    def test_in_this_program_after_the_download_program_was_refused(self):
        # With a VPN app (no proxy) and DMM-Fetch outside it: the file is fetched in this program, on the VPN.
        ok, calls, notices, _ = self.run_part("", helper=["DMM-Fetch.exe"], refusals=0)
        self.assertIs(ok, True)
        self.assertEqual(calls, [("program", None), ("fetch", None)])
        self.assertEqual(len(notices), 1)

    def test_no_other_way_left(self):
        # No proxy and no download program: the file already came over the Japan route's own connection.
        ok, calls, notices, _ = self.run_part("", refusals=5)
        self.assertIn("HTTP 404", ok)
        self.assertEqual(calls, [("fetch", None), ("fetch", None)])
        self.assertEqual(notices, [])

    def test_library_files_never_switch(self):
        session = mock.Mock(user_agent="ua")
        session.japan = JapanRoute("http://127.0.0.1:25345")
        with mock.patch.object(download, "resolve", return_value="https://wsd-c001.dmm.com/f.wsdcf"), \
                mock.patch.object(download, "fetch", side_effect=FileRefused("HTTP 404", 404)) as fetch:
            with self.assertRaises(download.DownloadError):
                download.download_part(session, "u", self.target, "t", attempts=2, binary_only=True)
        self.assertTrue(all("proxies" not in call.kwargs for call in fetch.call_args_list))

    def test_the_download_program_reports_a_refusal(self):
        answer = io.StringIO()
        with mock.patch.object(fetch_module, "fetch_file", side_effect=FileRefused("DMM's file server answered HTTP 410", 410)), \
                mock.patch.object(fetch_module.sys, "stdin", io.StringIO('{"url": "https://wsd-c001.dmm.com/f", "path": "x"}\n')), \
                mock.patch.object(fetch_module.sys, "stdout", answer):
            self.assertEqual(fetch_module.main(), 1)
        message = json.loads(answer.getvalue().splitlines()[-1])
        self.assertEqual((message["kind"], message["status"]), ("refused", 410))


class SettingsTest(unittest.TestCase):
    def test_the_source_can_be_vrchannel(self):
        folder = Path(tempfile.mkdtemp(prefix="dmm settings "))
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "config.ini"
        path.write_text(f"[{config.SECTION}]\nsource = vrchannel\n", encoding="utf-8")
        self.assertEqual(config.load(path).source, "vrchannel")
        path.write_text(f"[{config.SECTION}]\nsource = elsewhere\n", encoding="utf-8")
        self.assertEqual(config.load(path).source, "auto")
        self.assertIn("vrchannel", links.SOURCES)

    def test_the_japan_route(self):
        folder = Path(tempfile.mkdtemp(prefix="dmm settings "))
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "config.ini"
        self.assertEqual(config.Settings().vrchannel_route, "")  # this program's own connection
        path.write_text(f"[{config.SECTION}]\nvrchannel_route = http://127.0.0.1:25345\n", encoding="utf-8")
        self.assertEqual(config.load(path).vrchannel_route, "http://127.0.0.1:25345")
        path.write_text(f"[{config.SECTION}]\nvrchannel_route = socks5://127.0.0.1:1080\n", encoding="utf-8")
        self.assertEqual(config.load(path).vrchannel_route, "")  # not an http proxy: ignored
        settings = config.load(path)
        settings.vrchannel_route = "http://127.0.0.1:25345"
        config.save(settings, path)
        self.assertEqual(config.load(path).vrchannel_route, "http://127.0.0.1:25345")
        self.assertIn("vrchannel_route", config.TEMPLATE)


@unittest.skipUnless(MEDIA_TOOLS and MEDIAINFO, "ffmpeg/mediainfo not installed")
class ChannelPipelineTest(unittest.TestCase):
    """A VR Channel video goes the library's VR way: .wsdcf parts, jav-it's "dmm-vr", <cid>-ptN.mp4, never joined."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dmm channel "))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.src = self.dir / "source"
        self.src.mkdir()
        self.out = self.dir / "out"
        self.javit = make_fake_javit(self.dir / "tools", vr=True)
        self.tools = pipeline.Tools(self.javit, MEDIAINFO, MEDIA_TOOLS)
        self.login = Login("fan@example.com", "secret", "test")
        patcher = mock.patch.dict(os.environ, {"FAKE_JAVIT_MODE": "ok"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clips = [make_clip(self.src / f"p{n}.mp4", "mdvr00244", None, container="mp4") for n in (1, 2)]
        urls = vr_links()["8k"]
        self.choice = links.Choice("vrchannel", "8k", urls, {"8k": 2}, vr=True, versions={"8k": urls},
                                   referer=CHANNEL.format(cid="mdvr00244"), japan=True)
        self.by_url = dict(zip(urls, self.clips))

    def test_downloaded_decrypted_and_named_by_part(self):
        seen = []

        def fake_download(session, url, target, label, attempts=5, progress=None, retry_delay=10.0, **kwargs):
            seen.append((kwargs.get("binary_only"), kwargs.get("referer"), kwargs.get("japan")))
            return copy_to(self.by_url[url])(session, url, target, label)

        opts = pipeline.Options(out_dir=self.out, retry_delay=0, merge=True)
        with mock.patch.object(download, "download_part", side_effect=fake_download):
            result = pipeline.process("mdvr00244", mock.Mock(), self.login, self.tools, opts, Report(), choice=self.choice)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["mdvr00244-pt1.mp4", "mdvr00244-pt2.mp4"])
        self.assertTrue(result.message.startswith("VR Channel, VR 8k: "), result.message)
        self.assertEqual(seen, [(True, CHANNEL.format(cid="mdvr00244"), True)] * 2)  # links through the Japan route
        self.assertEqual((self.javit.parent / "kinds.log").read_text().split(), ["dmm-vr", "dmm-vr"])

    def test_a_refused_channel_link_falls_back_to_the_library(self):
        # "auto": the channel lists the video, but its link isn't for this account; the library has it.
        bought = links.Choice("library", "8k", ["https://www.dmm.co.jp/digital/x/part=1/", "https://www.dmm.co.jp/digital/x/part=2/"],
                              {"8k": 2}, vr=True)
        by_url = dict(zip(bought.urls, self.clips))

        def fake_download(session, url, target, label, attempts=5, progress=None, retry_delay=10.0, **kwargs):
            if "/monthly/vr/" in url:
                raise download.NotAllowed(f"{label}: DMM didn't give the download (DMM sent it to www.dmm.co.jp)")
            return copy_to(by_url[url])(session, url, target, label)

        for source, ok in (("auto", True), ("vrchannel", False)):
            report = Report()
            opts = pipeline.Options(out_dir=self.dir / source, retry_delay=0, source=source)
            with mock.patch.object(links, "find", return_value=bought) as find, \
                    mock.patch.object(download, "download_part", side_effect=fake_download):
                result = pipeline.process("mdvr00244", mock.Mock(), self.login, self.tools, opts, report, choice=self.choice)
            self.assertEqual(result.ok, ok, result.message)
            if ok:
                self.assertEqual(find.call_args.args[2:], ("auto", None))
                self.assertEqual(find.call_args.kwargs["skip"], ("vrchannel",))
                self.assertIn("DMM VR Channel didn't give the download to this account; trying your library", report.steps)
                self.assertTrue(result.message.startswith("library, VR 8k"), result.message)
            else:
                find.assert_not_called()
                self.assertIn("didn't give the download", result.message)


class Report(pipeline.Report):
    def __init__(self):
        self.steps = []

    def step(self, text):
        self.steps.append(text)


if __name__ == "__main__":
    unittest.main()
