"""The library list (DMM's API) and VR downloads (.wsdcf, DMM's VR servers; VR is only downloaded)."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dmm_downloader import download
from dmm_downloader.dmm import DmmError
from dmm_downloader.links import (NOT_IN_VRCHANNEL, LoginExpired, NoLinks, choose, extract_json_links, find, group_links,
                                  library_list)


def not_in_channel():
    """The VR Channel doesn't have the video (these tests are about Premium and the library)."""
    return mock.patch("dmm_downloader.links.vrchannel_links", side_effect=NoLinks(NOT_IN_VRCHANNEL))


VR = ("/digital/-/proxy/=/adult_flag=1/category=videoa/content_type=vr/device=pc_vr/is_past=0/"
      "product_id={pid}/part={part}/quality={q}/shop=videoa/transfer_type=download/vr_appli_type=pc/")


def vr_payload(cid="abc00123"):
    """The library's play data of a VR video, shaped like DMM's answer (2 parts, 6 qualities)."""
    qualities = {"8k": "vrv18khia", "uhq": "vrv1uhqf", "hq": "vrv1hqf", "12000": "vmb", "6000": "hhb", "4000": "hmb"}
    download_links = {q: [{"link": VR.format(pid=cid + code, part=n, q=q), "part": n} for n in (1, 2)]
                      for q, code in qualities.items()}
    stream = {"high": [{"link": f"dmmvrplayerstreaming:digital?product_id={cid}&part=1", "part": 1}]}
    return {f"{cid}dl8": {"detail": {"content_type": "vr"},
                          "play_info": {"pc": {"stream": {"links": stream}, "download": {"links": download_links}}}},
            "user_agent": "pc"}


class Answer:
    def __init__(self, payload=None, status=200, location=None):
        self.payload, self.status_code = payload, status
        self.headers = {"Location": location} if location else {}

    def json(self):
        return self.payload

    def close(self):
        pass


def library_answer(items, has_next=False):
    return {"data": {"user": {"__typename": "Member", "ppvLibrary": {"contentViewingRightsSummaryList": {
        "items": items, "pageInfo": {"hasNext": has_next}}}}}}


def entry(cid, content_type="TWO_DIMENSION", floor="AV", download=True):
    return {"content": {"id": cid, "floor": floor, "contentType": content_type},
            "highestDownloadQualityProductItem": {"id": cid + "dl7"} if download else None}


class FakeSession:
    """Answers the library list (POST) and the play data (GET) like DMM."""

    def __init__(self, pages, play=None):
        self.pages, self.play, self.cache, self.calls = list(pages), play or {}, {}, []

    def request(self, method, url, label, retries=None, **kwargs):
        self.calls.append((method, url))
        if method == "POST":
            return Answer(self.pages.pop(0))
        return self.get(url, label)

    def get(self, url, label, **kwargs):
        self.calls.append(("GET", url))
        for cid, payload in self.play.items():
            if f"cid={cid}/" in url:
                return Answer(payload)
        return Answer({"user_agent": "pc", "ppv_content_viewing_rights": []})


class VrLinksTest(unittest.TestCase):
    def test_vr_links_and_the_best_quality(self):
        links = extract_json_links(vr_payload())
        self.assertEqual(sorted(links), sorted(["8k", "uhq", "hq", "12000", "6000", "4000"]))
        self.assertEqual(sorted(group_links(links)), sorted(links))  # the quality alone names a version
        label, urls = choose(links)
        self.assertEqual(label, "8k")
        self.assertEqual([u.split("/part=")[1][0] for u in urls], ["1", "2"])
        self.assertTrue(urls[0].startswith("https://www.dmm.co.jp/digital/-/proxy/"))
        self.assertEqual(choose(links, "4000")[0], "4000")

    def test_streaming_links_are_not_downloads(self):
        payload = vr_payload()
        del payload["abc00123dl8"]["play_info"]["pc"]["download"]
        self.assertEqual(extract_json_links(payload), {})


class LibraryListTest(unittest.TestCase):
    def test_list_is_read_once_over_pages(self):
        session = FakeSession([library_answer([entry("abc00123")], has_next=True), library_answer([entry("xyz00456", "VR")])])
        items = library_list(session)
        self.assertEqual(sorted(items), ["abc00123", "xyz00456"])
        self.assertTrue(items["xyz00456"].vr)
        self.assertIs(library_list(session), items)
        self.assertEqual(sum(1 for method, _ in session.calls if method == "POST"), 2)

    def test_clear_reasons(self):
        session = FakeSession([library_answer([entry("abc00123", download=False)])])
        with self.assertRaises(NoLinks) as caught:
            find(session, "sone00995", "library")
        self.assertIn("not in your DMM library", str(caught.exception))
        with self.assertRaises(NoLinks) as caught:
            find(session, "abc00123", "library")
        self.assertIn("streaming only", str(caught.exception))
        # Known videos: only their own shop is asked, never the amateur shop's address.
        self.assertFalse(any("shop=videoc" in url for _, url in session.calls))

    def test_vr_video_from_the_library(self):
        session = FakeSession([library_answer([entry("abc00123", "VR")])], play={"abc00123": vr_payload()})
        choice = find(session, "abc00123", "library")
        self.assertTrue(choice.vr)
        self.assertEqual((choice.source, choice.label, len(choice.urls)), ("library", "8k", 2))

    def test_a_vr_video_without_a_premium_page_comes_from_the_library(self):
        # DMM's Premium page answers 404 for VR videos: "auto" must still ask the library (not in the VR Channel).
        session = FakeSession([library_answer([entry("abc00123", "VR")])], play={"abc00123": vr_payload()})
        with mock.patch("dmm_downloader.links.premium_links", side_effect=DmmError("Premium page of abc00123: DMM answered HTTP 404")), \
                not_in_channel():
            choice = find(session, "abc00123", "auto")
            self.assertEqual((choice.source, choice.vr), ("library", True))
            with self.assertRaises(NoLinks) as caught:
                find(session, "abc00123", "premium")
            self.assertIn("not on DMM Premium", str(caught.exception))

    def test_an_older_list_is_read_again_for_a_missing_id(self):
        # Bought after the list was read: an id missing from a list older than 10 minutes makes it be read again.
        session = FakeSession([library_answer([entry("abc00123")]), library_answer([entry("abc00123"), entry("new00001", "VR")])],
                              play={"new00001": vr_payload("new00001")})
        library_list(session)
        with self.assertRaises(NoLinks):
            find(session, "new00001", "library")  # the list is fresh: not read again
        self.assertEqual(sum(1 for method, _ in session.calls if method == "POST"), 1)
        session.cache["library_at"] -= 3600
        self.assertTrue(find(session, "new00001", "library").vr)
        self.assertEqual(sum(1 for method, _ in session.calls if method == "POST"), 2)
        self.assertIn("new00001", session.cache["library"])

    def test_without_the_list_the_old_way_still_works(self):
        session = FakeSession([{"errors": [{"message": "nope"}]}], play={"abc00123": vr_payload()})
        self.assertIsNone(library_list(session))
        self.assertTrue(find(session, "abc00123", "library").vr)


class VrResolveTest(unittest.TestCase):
    def test_vr_servers(self):
        session = mock.Mock()
        session.request.side_effect = [
            Answer(status=302, location="https://wsd-r.dmm.co.jp/022/abc00123/token"),
            Answer(status=302, location="https://wsd-c003.dmm.com/022/abc00123/abc00123hmb1.wsdcf?dl_key=k"),
        ]
        url = download.resolve(session, "https://www.dmm.co.jp/digital/-/proxy/=/content_type=vr/transfer_type=download/", "t")
        self.assertEqual(url, "https://wsd-c003.dmm.com/022/abc00123/abc00123hmb1.wsdcf?dl_key=k")
        # Both steps come from the library page (DMM's VR server answers 403 otherwise).
        self.assertEqual([call.kwargs["headers"] for call in session.request.call_args_list],
                         [{"Referer": "https://video.dmm.co.jp/mylibrary/"}] * 2)

    def test_vr_server_refusing_is_busy(self):
        session = mock.Mock()
        session.request.side_effect = [
            Answer(status=302, location="https://wsd-r.dmm.co.jp/022/abc00123/token"),
            download.DmmError("t: VR download link: DMM answered HTTP 403", 403),
        ]
        with self.assertRaises(download.VrServerBusy):
            download.resolve(session, "https://www.dmm.co.jp/digital/-/proxy/=/content_type=vr/transfer_type=download/", "t")

    def test_busy_vr_server_is_waited_for_patiently(self):
        target = Path(tempfile.mkdtemp(prefix="dmm vr wait ")) / "part1.wsdcf"
        self.addCleanup(shutil.rmtree, target.parent, True)
        busy = download.VrServerBusy("t: busy")
        notices = []
        session = mock.Mock(user_agent="ua")
        session.check_login.return_value = True
        with mock.patch.object(download, "resolve", side_effect=[busy, busy, "https://wsd-c003.dmm.com/f.wsdcf"]), \
                mock.patch.object(download, "fetch", side_effect=lambda url, ua, partial, progress, binary_only=False: partial.write_bytes(b"x")), \
                mock.patch.object(download.time, "sleep") as sleep:
            download.download_part(session, "u", target, "t", attempts=2, notice=notices.append)
        self.assertTrue(target.exists())
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [120, 300])
        self.assertEqual(len(notices), 2)
        self.assertIn("waiting 2 min", notices[0])
        self.assertIn("VPN", notices[0])

    def test_vr_limit_message_mentions_a_vpn(self):
        target = Path(tempfile.mkdtemp(prefix="dmm vr wait ")) / "part1.wsdcf"
        self.addCleanup(shutil.rmtree, target.parent, True)
        session = mock.Mock(user_agent="ua")
        session.check_login.return_value = True
        with mock.patch.object(download, "resolve", side_effect=download.VrServerBusy("t: busy")), \
                mock.patch.object(download.time, "sleep") as sleep:
            with self.assertRaises(download.DownloadError) as caught:
                download.download_part(session, "u", target, "t", attempts=1)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [120, 300, 600])
        self.assertIn("limits how often", str(caught.exception))  # the page shows "DMM limit: later"
        self.assertIn("VPN", str(caught.exception))

    def test_after_the_last_vr_wait_one_more_try_only(self):
        # Each try asks DMM for a new VR link, and DMM counts them: the tries set in Settings (here 20) don't add more.
        target = Path(tempfile.mkdtemp(prefix="dmm vr wait ")) / "part1.wsdcf"
        self.addCleanup(shutil.rmtree, target.parent, True)
        session = mock.Mock(user_agent="ua")
        session.check_login.return_value = True
        with mock.patch.object(download, "resolve", side_effect=download.VrServerBusy("t: busy")) as resolve, \
                mock.patch.object(download.time, "sleep") as sleep:
            with self.assertRaises(download.DownloadError) as caught:
                download.download_part(session, "u", target, "t", attempts=20, retry_delay=0, binary_only=True)
        self.assertEqual(resolve.call_count, 4)  # the first try, and one after each of the three waits
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [120, 300, 600])
        self.assertIn("limits how often", str(caught.exception))

    def test_a_refused_vr_file_asks_for_two_links_only(self):
        # DMM's file server refusing a VR file (403, 404, 410): each new try would ask DMM for a new VR link.
        target = Path(tempfile.mkdtemp(prefix="dmm vr refused ")) / "part1.wsdcf"
        self.addCleanup(shutil.rmtree, target.parent, True)
        session = mock.Mock(user_agent="ua")
        with mock.patch.object(download, "resolve", return_value="https://wsd-c1.dmm.com/x") as resolve, \
                mock.patch.object(download, "fetch", side_effect=download.FileRefused("HTTP 403", 403)), \
                mock.patch.object(download.time, "sleep"):
            with self.assertRaises(download.DownloadError) as caught:
                download.download_part(session, "https://www.dmm.co.jp/x/content_type=vr/transfer_type=download/",
                                       target, "t", attempts=5, retry_delay=0, binary_only=True)
        self.assertEqual(resolve.call_count, download.LINK_REFUSED_TRIES)
        self.assertIn("after 2 tries", str(caught.exception))

    def test_a_vr_link_the_server_doesnt_know_is_tried_twice_only(self):
        url = "https://www.dmm.co.jp/digital/-/proxy/=/content_type=vr/transfer_type=download/"
        for status in (404, 410):
            def answer(method, link, *args, **kwargs):
                if "wsd-r.dmm.co.jp" in link:
                    raise DmmError(f"t: VR download link: DMM answered HTTP {status}", status)
                return Answer(status=302, location="https://wsd-r.dmm.co.jp/022/abc00123/token")

            session = mock.Mock(user_agent="ua")
            session.request.side_effect = answer
            with self.assertRaises(download.LinkRefused):
                download.resolve(session, url, "t")
            target = Path(tempfile.mkdtemp(prefix="dmm vr gone ")) / "part1.wsdcf"
            self.addCleanup(shutil.rmtree, target.parent, True)
            session.request.reset_mock()
            with mock.patch.object(download.time, "sleep"):
                with self.assertRaises(download.DownloadError) as caught:
                    download.download_part(session, url, target, "t", attempts=5, retry_delay=0, binary_only=True)
            self.assertEqual(session.request.call_count, 2 * download.LINK_REFUSED_TRIES)  # two links, two steps each
            self.assertIn(f"HTTP {status}", str(caught.exception))

    def test_vr_403_with_an_expired_login_says_log_in_again(self):
        # A logged-out session gets the same 403 as DMM's per-video limit: no 17-minute wait for it.
        target = Path(tempfile.mkdtemp(prefix="dmm vr login ")) / "part1.wsdcf"
        self.addCleanup(shutil.rmtree, target.parent, True)
        session = mock.Mock(user_agent="ua")
        session.check_login.return_value = False
        with mock.patch.object(download, "resolve", side_effect=download.VrServerBusy("t: busy")), \
                mock.patch.object(download.time, "sleep") as sleep:
            with self.assertRaises(download.LoginExpired) as caught:
                download.download_part(session, "u", target, "t", attempts=5)
        sleep.assert_not_called()
        self.assertIn("log in again (Settings)", str(caught.exception))
        self.assertNotIn("limits how often", str(caught.exception))


class FirstLinkStepTest(unittest.TestCase):
    """What DMM's first answer for a download link means: go on, log in again, or not for this account."""

    URL = "https://www.dmm.co.jp/monthly/premium/-/proxy/=/product_id=abc00123/transfer_type=download/rate=4k/ftype=dcv"

    def session(self, first, logged_in=True):
        session = mock.Mock(user_agent="ua")
        session.request.side_effect = [first]
        session.check_login.return_value = logged_in
        return session

    def test_sent_to_the_login_page_means_log_in_again(self):
        session = self.session(Answer(status=302, location="https://accounts.dmm.co.jp/service/login/password/"))
        with self.assertRaises(download.LoginExpired) as caught:
            download.resolve(session, self.URL, "abc00123")
        self.assertIn("log in again (Settings)", str(caught.exception))

    def test_refused_with_an_expired_login(self):
        session = self.session(DmmError("abc00123: download link: DMM answered HTTP 403", 403), logged_in=False)
        with self.assertRaises(download.LoginExpired) as caught:
            download.resolve(session, self.URL, "abc00123")
        self.assertIn("log in again (Settings)", str(caught.exception))

    def test_refused_while_logged_in_is_not_for_this_account(self):
        for first in (DmmError("abc00123: download link: DMM answered HTTP 403", 403),
                      Answer(status=302, location="https://www.dmm.co.jp/monthly/premium/-/guide/"),
                      Answer(status=200)):
            with self.assertRaises(download.NotAllowed, msg=first):
                download.resolve(self.session(first), self.URL, "abc00123")

    def test_a_foreign_host_is_still_refused_as_unexpected(self):
        with self.assertRaises(download.DownloadError) as caught:
            download.resolve(self.session(Answer(status=302, location="https://evil.example/x")), self.URL, "abc00123")
        self.assertNotIsInstance(caught.exception, download.NotAllowed)
        self.assertIn("somewhere unexpected", str(caught.exception))

    def test_not_allowed_is_tried_twice_and_kept(self):
        target = Path(tempfile.mkdtemp(prefix="dmm refused ")) / "part1.dcv"
        self.addCleanup(shutil.rmtree, target.parent, True)
        refused = download.NotAllowed("abc00123: DMM didn't give the download (HTTP 403)")
        with mock.patch.object(download, "resolve", side_effect=refused) as resolve:
            with self.assertRaises(download.NotAllowed):
                download.download_part(mock.Mock(user_agent="ua"), "u", target, "abc00123", attempts=5, retry_delay=0)
        self.assertEqual(resolve.call_count, 2)

    def test_an_expired_login_is_not_tried_again(self):
        target = Path(tempfile.mkdtemp(prefix="dmm expired ")) / "part1.dcv"
        self.addCleanup(shutil.rmtree, target.parent, True)
        with mock.patch.object(download, "resolve", side_effect=download.LoginExpired("x: log in again")) as resolve:
            with self.assertRaises(download.LoginExpired):
                download.download_part(mock.Mock(user_agent="ua"), "u", target, "x", attempts=5, retry_delay=0)
        self.assertEqual(resolve.call_count, 1)


class PremiumOrLibraryTest(unittest.TestCase):
    def test_a_quality_premium_lacks_comes_from_the_library(self):
        premium = {"6000": ["https://www.dmm.co.jp/monthly/premium/-/proxy/=/product_id=abc00123/rate=6000/ftype=dcv"]}
        library = {"4k": ["https://www.dmm.co.jp/digital/-/proxy/=/product_id=abc00123/rate=4k/ftype=dcv"]}
        with mock.patch("dmm_downloader.links.premium_links", return_value=(premium, None)), \
                mock.patch("dmm_downloader.links.library_links", return_value=(library, False)), not_in_channel():
            choice = find(mock.Mock(), "abc00123", "auto", "4k")
            self.assertEqual((choice.source, choice.label), ("library", "4k"))
            with self.assertRaises(NoLinks):
                find(mock.Mock(), "abc00123", "premium", "4k")

    def test_a_logged_out_library_says_log_in_again(self):
        session = FakeSession([{"data": {"user": {"__typename": "Guest"}}}, library_answer([entry("abc00123")])])
        with self.assertRaises(LoginExpired) as caught:
            find(session, "abc00123", "library")
        self.assertIn("log in again (Settings)", str(caught.exception))
        self.assertNotIn("library", session.cache)  # not kept: the next lookup asks DMM again
        self.assertIn("abc00123", library_list(session))

    def test_vr_server_sending_elsewhere_is_refused(self):
        session = mock.Mock()
        session.request.side_effect = [
            Answer(status=302, location="https://wsd-r.dmm.co.jp/022/abc00123/token"),
            Answer(status=302, location="https://evil.example/abc00123hmb1.wsdcf"),
        ]
        with self.assertRaises(download.DownloadError):
            download.resolve(session, "https://www.dmm.co.jp/digital/-/proxy/=/x/", "t")


if __name__ == "__main__":
    unittest.main()
