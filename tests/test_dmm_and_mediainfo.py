import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from dmm_downloader import mediainfo
from dmm_downloader.dmm import DmmSession
from dmm_downloader.download import DownloadError, _checked_redirect
from dmm_downloader.logs import redact

FIXTURES = Path(__file__).parent / "fixtures"


def tracks(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["media"]["track"]


class SessionTest(unittest.TestCase):
    """The DMM session shared by the page's lookups and downloads (one request at a time), and renewed cookies."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dmm session "))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "session.json"
        self.session = DmmSession(retry_delay=0)
        self.addCleanup(self.session.close)

    def answer_setting(self, name, value, pause=0.0, seen=None):
        """A stand-in for the web request: DMM answers 200 and sets a cookie on its own domain."""
        lock = threading.Lock()

        def request(method, url, **kwargs):
            if seen is not None:
                with lock:
                    seen["now"] += 1
                    seen["most"] = max(seen["most"], seen["now"])
            time.sleep(pause)
            self.session.http.cookies.set(name(url) if callable(name) else name, value, domain=".dmm.co.jp", path="/")
            if seen is not None:
                with lock:
                    seen["now"] -= 1
            return mock.Mock(status_code=200, headers={})
        return request

    def saved(self):
        return {c["name"]: c["value"] for c in json.loads(self.path.read_text(encoding="utf-8"))["cookies"]}

    def cookie(self, value, session=None):
        (session or self.session).http.cookies.set("INT_SESID", value, domain=".dmm.co.jp", path="/")

    def test_renewed_cookies_are_saved(self):
        self.cookie("first")
        self.session.save(self.path)
        with mock.patch.object(self.session.http, "request", side_effect=self.answer_setting("INT_SESID", "renewed")):
            self.session.get("https://www.dmm.co.jp/", "test")
        self.assertEqual(self.saved()["INT_SESID"], "renewed")
        self.session.stop_saving()  # replaced by another session, or logged out: never written again
        with mock.patch.object(self.session.http, "request", side_effect=self.answer_setting("INT_SESID", "later")):
            self.session.get("https://www.dmm.co.jp/", "test")
        self.assertEqual(self.saved()["INT_SESID"], "renewed")

    def test_a_session_saved_for_another_account_is_not_used(self):
        self.cookie("first")
        self.session.account = "Fan@Example.com"
        self.session.save(self.path)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["account"], "Fan@Example.com")
        other = DmmSession(retry_delay=0)
        self.addCleanup(other.close)
        self.assertFalse(other.load(self.path, "someone@example.com"))  # jav-it would decrypt with another login
        self.assertIsNone(other.http.cookies.get("INT_SESID"))
        self.assertIsNone(other.save_path)
        same = DmmSession(retry_delay=0)
        self.addCleanup(same.close)
        self.assertTrue(same.load(self.path, "fan@example.com"))  # the same email, written differently
        self.assertEqual(same.http.cookies.get("INT_SESID"), "first")

    def test_a_session_saved_by_an_older_version_is_taken_as_the_logins(self):
        self.path.write_text(json.dumps({"cookies": [{"name": "INT_SESID", "value": "old", "domain": ".dmm.co.jp"}]}),
                             encoding="utf-8")
        self.assertTrue(self.session.load(self.path, "fan@example.com"))
        self.assertEqual(self.session.account, "fan@example.com")
        self.session.save(self.path)  # from now on it says whose it is
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["account"], "fan@example.com")
        self.path.write_text("[]", encoding="utf-8")
        self.assertFalse(DmmSession(retry_delay=0).load(self.path, "fan@example.com"))

    def test_a_session_never_saved_is_not_written(self):
        self.cookie("first")
        with mock.patch.object(self.session.http, "request", side_effect=self.answer_setting("INT_SESID", "renewed")):
            self.session.get("https://www.dmm.co.jp/", "test")
        self.assertFalse(self.path.exists())

    def test_one_request_at_a_time_and_no_cookie_lost(self):
        self.session.save(self.path)
        seen = {"now": 0, "most": 0}
        fake = self.answer_setting(lambda url: url.rsplit("/", 1)[1], "1", pause=0.02, seen=seen)
        with mock.patch.object(self.session.http, "request", side_effect=fake):
            threads = [threading.Thread(target=self.session.get, args=(f"https://www.dmm.co.jp/c{n}", "t")) for n in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(seen["most"], 1)
        self.assertTrue({f"c{n}" for n in range(8)} <= set(self.saved()))


class RedirectTest(unittest.TestCase):
    def test_only_dmm_download_hosts(self):
        ok = _checked_redirect("https://str.dmm.com/x?y=1", "https://www.dmm.co.jp/a", lambda h: h == "str.dmm.com", "t")
        self.assertEqual(ok, "https://str.dmm.com/x?y=1")
        for bad in ("http://str.dmm.com/x", "https://evil.com/x", "https://user:pw@str.dmm.com/x", "https://str.dmm.com:8443/x"):
            with self.assertRaises(DownloadError, msg=bad):
                _checked_redirect(bad, "https://www.dmm.co.jp/a", lambda h: h == "str.dmm.com", "t")


class RedactTest(unittest.TestCase):
    def test_secrets_removed(self):
        text = redact("GET https://stc12.dmm.com/digital/abc.dcv?dl_key=SECRET Cookie: INT_SESID=SECRET password=SECRET http://u:SECRET@proxy:8080")
        self.assertNotIn("SECRET", text)
        self.assertIn("stc12.dmm.com", text)

    def test_vr_and_other_dmm_addresses(self):
        lines = [
            "GET https://wsd-r.dmm.co.jp/022/sivr00457/SECRET-TOKEN failed",
            "redirect to https://wsd-c003.dmm.com/022/sivr00457/sivr00457vrv18khia1.wsdcf?dl_key=SECRET&e=SECRET",
            "HTTPSConnectionPool(host='wsd-r.dmm.co.jp', port=443): Max retries exceeded with url: "
            "/022/sivr00457/SECRET-TOKEN (Caused by NameResolutionError('no address'))",
            "HTTPSConnectionPool(host='video.dmm.co.jp', port=443): Read timed out. with url: /av/?sig=SECRET",
            "403 Client Error: Forbidden for url: https://video.dmm.co.jp/av/content/?id=abc00123&sig=SECRET",
            "https://www.dmm.co.jp/digital/-/proxy/=/product_id=abc00123/part=1/?t=SECRET#x",
        ]
        for line in lines:
            text = redact(line)
            self.assertNotIn("SECRET", text, line)
        self.assertIn("wsd-r.dmm.co.jp", redact(lines[0]))
        self.assertIn("NameResolutionError", redact(lines[2]))
        self.assertIn("product_id=abc00123/part=1/", redact(lines[5]))  # the path stays readable


class MediaInfoTest(unittest.TestCase):
    def test_files_like_dmms_are_fine(self):
        # MediaInfo's answers for files shaped like DMM's (ids, sizes and lengths made up).
        for name, fmt, comment in (
            ("mediainfo-h264.json", "AVC", ("abc00033", None)),
            ("mediainfo-4k-single.json", "VP9", ("xyz00891", None)),
            ("mediainfo-vp9-part1.json", "VP9", ("1abc00010", 1)),
        ):
            t = tracks(name)
            self.assertIsNone(mediainfo.problem(t), name)
            self.assertEqual(mediainfo.video_format(t), fmt)
            self.assertEqual(mediainfo.dmm_comment(t), comment)
            self.assertGreater(mediainfo.duration(t), 60)

    def test_damage_found(self):
        t = tracks("mediainfo-h264.json")
        t[0]["Error_Count"] = "2"
        self.assertIn("2 error", mediainfo.problem(t))
        t = tracks("mediainfo-h264.json")
        t[0]["IsTruncated"] = "Yes"
        self.assertIn("truncated", mediainfo.problem(t, full=False))
        t = tracks("mediainfo-h264.json")
        t[1].setdefault("extra", {})["Errors_Corrupt_Frames"] = "12"
        self.assertIn("Errors_Corrupt_Frames", mediainfo.problem(t))
        self.assertIsNone(mediainfo.problem(t, full=False))


if __name__ == "__main__":
    unittest.main()
