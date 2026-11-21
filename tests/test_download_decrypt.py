import errno
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest import mock

from dmm_downloader import decrypt as decrypting
from dmm_downloader import download, fetch
from dmm_downloader.credentials import Login

from .fakes import MEDIA_TOOLS, MEDIAINFO, FileServer, make_clip, make_fake_javit

DATA = os.urandom(3 * 1024 * 1024 + 123)


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.partial = self.dir / "part1.dcv.part"
        # The local test server stands in for DMM's file servers (the only hosts the download step accepts).
        patcher = mock.patch.object(fetch, "file_server", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def serve(self, **kwargs) -> FileServer:
        server = FileServer(DATA, **kwargs)
        self.addCleanup(server.close)
        return server

    def test_continues_after_a_cut(self):
        server = self.serve(cut_after=1024 * 1024)
        with self.assertRaises(Exception):
            download.fetch(server.url, "ua", self.partial, None)
        self.assertEqual(self.partial.stat().st_size, 1024 * 1024)
        seen = []
        download.fetch(server.url, "ua", self.partial, lambda done, total: seen.append((done, total)))
        self.assertEqual(self.partial.read_bytes(), DATA)
        self.assertEqual(server.requests, [None, f"bytes={1024 * 1024}-"])
        self.assertEqual(seen[-1], (len(DATA), len(DATA)))

    def test_starts_again_without_range_support(self):
        server = self.serve(cut_after=1000, ranges=False)
        with self.assertRaises(Exception):
            download.fetch(server.url, "ua", self.partial, None)
        download.fetch(server.url, "ua", self.partial, None)
        self.assertEqual(self.partial.read_bytes(), DATA)

    def test_download_part_retries_and_renames(self):
        server = self.serve(cut_after=4096)
        target = self.dir / "part1.dcv"
        with mock.patch.object(download, "resolve", return_value=server.url):
            session = mock.Mock(user_agent="ua")
            result = download.download_part(session, "https://www.dmm.co.jp/x", target, "abc00123", attempts=3, retry_delay=0)
        self.assertEqual(result, target)
        self.assertEqual(target.read_bytes(), DATA)
        self.assertFalse(self.partial.exists())

    def test_a_complete_partial_file_is_kept(self):
        # The whole file was downloaded but not renamed (e.g. the rename failed): the server answers 416 to "the rest".
        server = self.serve()
        self.partial.write_bytes(DATA)
        seen = []
        self.assertEqual(download.fetch(server.url, "ua", self.partial, lambda done, total: seen.append((done, total))),
                         len(DATA))
        self.assertEqual(self.partial.read_bytes(), DATA)
        self.assertEqual(server.requests, [f"bytes={len(DATA)}-"])
        self.assertEqual(seen, [(len(DATA), len(DATA))])

        target = self.dir / "part1.dcv"
        with mock.patch.object(download, "resolve", return_value=server.url):
            download.download_part(mock.Mock(user_agent="ua"), "u", target, "abc00123", attempts=1, retry_delay=0)
        self.assertEqual(target.read_bytes(), DATA)
        self.assertFalse(self.partial.exists())

    def test_a_partial_file_longer_than_the_file_starts_again(self):
        server = self.serve()
        self.partial.write_bytes(DATA + b"more")  # not the file the server has now
        with self.assertRaises(download.DownloadError) as caught:
            download.fetch(server.url, "ua", self.partial, None)
        self.assertIn("starting again", str(caught.exception))
        self.assertFalse(self.partial.exists())

    def test_a_rename_refused_for_a_moment_is_tried_again(self):
        # Windows: a virus scanner may hold the file it just saw being written.
        server = self.serve()
        target = self.dir / "part1.dcv"
        real, calls = Path.replace, []

        def held(times):
            def replace(path, other):
                calls.append(path)
                if len(calls) <= times:
                    raise PermissionError(13, "The process cannot access the file because it is being used by another "
                                              "process")
                return real(path, other)
            return replace

        with mock.patch.object(download, "resolve", return_value=server.url), \
                mock.patch.object(Path, "replace", held(2)), mock.patch.object(download.time, "sleep") as sleep:
            download.download_part(mock.Mock(user_agent="ua"), "u", target, "abc00123", attempts=1, retry_delay=0)
        self.assertEqual(target.read_bytes(), DATA)
        self.assertEqual(len(calls), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [download.RENAME_WAIT] * 2)

        target.unlink()
        calls.clear()
        with mock.patch.object(download, "resolve", return_value=server.url), \
                mock.patch.object(Path, "replace", held(99)), mock.patch.object(download.time, "sleep"):
            with self.assertRaises(download.DownloadError) as caught:
                download.download_part(mock.Mock(user_agent="ua"), "u", target, "abc00123", attempts=1, retry_delay=0)
        self.assertIn("can't write", str(caught.exception))
        self.assertEqual(len(calls), download.RENAME_TRIES)
        self.assertEqual(self.partial.read_bytes(), DATA)  # kept: the next run only renames it (416 above)

    def test_gives_up_and_keeps_the_partial_file(self):
        with mock.patch.object(download, "resolve", side_effect=download.DownloadError("link expired")):
            with self.assertRaises(download.DownloadError) as caught:
                download.download_part(mock.Mock(user_agent="ua"), "u", self.dir / "p.dcv", "abc00123", attempts=2, retry_delay=0)
        self.assertIn("after 2 tries", str(caught.exception))

    def test_no_space_is_not_retried(self):
        server = self.serve()
        calls = []

        def resolve(*args):
            calls.append(1)
            return server.url

        with mock.patch.object(download, "resolve", side_effect=resolve), \
                mock.patch.object(fetch.shutil, "disk_usage", return_value=mock.Mock(free=10)):
            with self.assertRaises(download.NoSpace):
                download.download_part(mock.Mock(user_agent="ua"), "u", self.dir / "p.dcv", "x", attempts=3, retry_delay=0)
        self.assertEqual(len(calls), 1)

    def test_downloads_running_together_book_their_space(self):
        book = fetch.SpaceBook()
        left = {"first": 6e9}
        with mock.patch.object(fetch.shutil, "disk_usage", return_value=mock.Mock(free=10e9 + fetch.SPACE_MARGIN)):
            first = book.claim(self.dir, lambda: int(left["first"]))
            with self.assertRaises(fetch.NoSpace) as caught:
                book.claim(self.dir, lambda: int(6e9))
            self.assertIn("others running 6.0 GB more", str(caught.exception))
            left["first"] = 1e9  # the first one has written most of its file meanwhile
            book.claim(self.dir, lambda: int(6e9)).release()
            first.release()

    def test_a_download_counts_the_others_running(self):
        server = self.serve()
        with fetch.SPACE.claim(self.dir, lambda: int(9e9)), \
                mock.patch.object(fetch.shutil, "disk_usage", return_value=mock.Mock(free=9e9 + fetch.SPACE_MARGIN + 1e6)):
            with self.assertRaises(fetch.NoSpace):
                download.fetch(server.url, "ua", self.partial, None)  # 3 MB alone would fit
        download.fetch(server.url, "ua", self.partial, None)
        self.assertEqual(self.partial.read_bytes(), DATA)

    def test_a_full_disk_while_writing_is_not_retried(self):
        server = self.serve()

        class FullDisk:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def write(self, data):
                raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(download, "resolve", return_value=server.url) as resolve, \
                mock.patch.object(fetch, "open", create=True, side_effect=lambda *args, **kwargs: FullDisk()):
            with self.assertRaises(fetch.NoSpace) as caught:
                download.download_part(mock.Mock(user_agent="ua"), "u", self.dir / "p.dcv", "abc00123", attempts=5,
                                       retry_delay=0)
        self.assertEqual(resolve.call_count, 1)
        self.assertIn("disk full", str(caught.exception))

    def test_a_refused_link_is_tried_twice_only(self):
        # str.dmm.com answers 403/404 to VPN addresses: five tries would only waste a minute.
        refused = download.LinkRefused(f"abc00123: DMM's download server answered HTTP 404. {download.VPN_HINT}")
        with mock.patch.object(download, "resolve", side_effect=refused) as resolve:
            with self.assertRaises(download.DownloadError) as caught:
                download.download_part(mock.Mock(user_agent="ua"), "u", self.dir / "p.dcv", "abc00123", attempts=5, retry_delay=0)
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(str(caught.exception).count("abc00123"), 1)  # the id isn't repeated inside the reason
        self.assertIn("Is a VPN on?", str(caught.exception))

    def test_vr_refuses_a_page_by_its_type(self):
        server = self.serve(content_type="text/html; charset=utf-8")
        with self.assertRaises(fetch.NotAFile):
            download.fetch(server.url, "ua", self.partial, None, binary_only=True)
        self.assertFalse(self.partial.exists())
        download.fetch(server.url, "ua", self.partial, None)  # 2D: checked by MediaInfo later instead
        self.assertEqual(self.partial.read_bytes(), DATA)

    def test_vr_downloads_a_big_file_labelled_as_text(self):
        # DMM's label for .wsdcf files isn't known: a label alone must not refuse a real (big) file, since every
        # refused VR try counts against DMM's per-video limit. Its first bytes are binary, so it is kept.
        server = self.serve(content_type="text/plain")
        with mock.patch.object(fetch, "PAGE_MAX_BYTES", len(DATA) - 1):
            download.fetch(server.url, "ua", self.partial, None, binary_only=True)
        self.assertEqual(self.partial.read_bytes(), DATA)

    def test_vr_refuses_a_page_sent_as_a_file(self):
        page = b"<!DOCTYPE html><html><body>Sorry, this download has expired.</body></html>\n" * 20
        server = FileServer(page)
        self.addCleanup(server.close)
        target = self.dir / "part1.wsdcf"
        with mock.patch.object(download, "resolve", return_value=server.url) as resolve:
            with self.assertRaises(download.DownloadError) as caught:
                download.download_part(mock.Mock(user_agent="ua"), "u", target, "sivr00457", attempts=5, retry_delay=0,
                                       binary_only=True)
        self.assertIn("web page or text", str(caught.exception))
        self.assertEqual(resolve.call_count, 2)  # each VR try asks DMM for a new link: only twice
        self.assertFalse(target.exists())
        self.assertFalse(self.partial.with_name("part1.wsdcf.part").exists())

    def test_what_looks_like_text(self):
        samples = {b"<!doctype html><title>x</title>": True, b'{"error": "expired"}': True, b"\xef\xbb\xbfplain text\r\n": True,
                   "エラー: 期限切れ".encode("utf-8"): True, DATA[:4096]: False, b"\x00\x00\x00\x20ftypisom": False,
                   b"\x01\x10application/vnd.oma.drm.content" + bytes(range(256)): False}
        for data, text in samples.items():
            self.partial.write_bytes(data)
            self.assertEqual(fetch.looks_like_text(self.partial), text, data[:20])

    def test_only_dmm_file_servers(self):
        mock.patch.stopall()
        self.assertTrue(fetch.file_server("https://stc003.dmm.com/x/abc00123hhb1.dcv?dl_key=k"))
        self.assertTrue(fetch.file_server("https://wsd-c003.dmm.com/022/sivr00457/f.wsdcf?dl_key=k"))
        for bad in ("http://stc003.dmm.com/x", "https://evil.example/x", "https://stc003.dmm.com.evil.example/x",
                    "https://stc003.dmm.com:8443/x", "https://user:pw@stc003.dmm.com/x", "https://str.dmm.com/x"):
            self.assertFalse(fetch.file_server(bad), bad)
        with self.assertRaises(fetch.DownloadError):
            fetch.fetch_file("https://evil.example/x", "ua", self.partial)


class FetchProgramTest(unittest.TestCase):
    """The separate download program (DMM-Fetch.exe): its side, and the tool's side of the exchange."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dmm fetch "))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.partial = self.dir / "part1.dcv.part"

    def run_program(self, job_line):
        out = io.StringIO()
        with mock.patch.object(fetch.sys, "stdin", io.StringIO(job_line)), redirect_stdout(out):
            code = fetch.main()
        return code, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_program_downloads_and_answers_in_json_lines(self):
        server = FileServer(DATA)
        self.addCleanup(server.close)
        with mock.patch.object(fetch, "file_server", return_value=True):
            code, answers = self.run_program(json.dumps({"url": server.url, "path": str(self.partial), "user_agent": "ua"}) + "\n")
        self.assertEqual(code, 0)
        self.assertEqual(answers[-1], {"ok": len(DATA)})
        self.assertEqual(answers[-2], {"done": len(DATA), "total": len(DATA)})
        self.assertEqual(self.partial.read_bytes(), DATA)

    def test_program_refuses_a_page_for_vr(self):
        server = FileServer(DATA, content_type="application/json")
        self.addCleanup(server.close)
        job = {"url": server.url, "path": str(self.partial), "user_agent": "ua", "binary_only": True}
        with mock.patch.object(fetch, "file_server", return_value=True):
            code, answers = self.run_program(json.dumps(job) + "\n")
        self.assertEqual((code, answers[-1]["kind"]), (1, "notafile"))

    def test_program_refuses_other_hosts_and_bad_jobs(self):
        code, answers = self.run_program(json.dumps({"url": "https://evil.example/x", "path": str(self.partial)}) + "\n")
        self.assertEqual((code, answers[-1]["kind"]), (1, "download"))
        code, answers = self.run_program("not json\n")
        self.assertEqual((code, answers[-1]["kind"]), (2, "usage"))

    def fake_program(self, lines, exit_code=0):
        """A stand-in for DMM-Fetch.exe that checks its job and prints the given answers."""
        script = self.dir / "fake_fetch.py"
        script.write_text(
            "import json, sys\n"
            "job = json.loads(sys.stdin.readline())\n"
            "assert job['url'].startswith('https://stc') and job['user_agent'] == 'ua', job\n"
            "open(job['path'], 'wb').write(b'x' * 10)\n"
            f"for line in {lines!r}: print(json.dumps(line), flush=True)\n"
            f"sys.exit({exit_code})\n", encoding="utf-8")
        return [sys.executable, str(script)]

    def test_tool_reads_the_programs_progress_and_result(self):
        seen = []
        size = fetch.fetch_with_program(self.fake_program([{"done": 5, "total": 10}, {"done": 10, "total": 10}, {"ok": 10}]),
                                        "https://stc001.dmm.com/f.dcv?dl_key=k", "ua", self.partial,
                                        lambda done, total: seen.append((done, total)))
        self.assertEqual((size, seen), (10, [(5, 10), (10, 10)]))

    def test_the_programs_download_books_its_space_too(self):
        script = self.dir / "slow_fetch.py"
        script.write_text("import json, sys, time\nsys.stdin.readline()\n"
                          "print(json.dumps({'done': 5, 'total': 10}), flush=True)\ntime.sleep(30)\n", encoding="utf-8")
        start = time.monotonic()
        with mock.patch.object(fetch.shutil, "disk_usage", return_value=mock.Mock(free=100)):
            with self.assertRaises(fetch.NoSpace):
                fetch.fetch_with_program([sys.executable, str(script)], "https://stc001.dmm.com/f.dcv?dl_key=k", "ua",
                                         self.partial)
        self.assertLess(time.monotonic() - start, 20)  # the program was stopped, not waited for

    def test_program_reports_a_full_disk(self):
        server = FileServer(DATA)
        self.addCleanup(server.close)
        failing = mock.Mock()
        failing.__enter__ = lambda s: s
        failing.__exit__ = lambda s, *exc: False
        failing.write.side_effect = OSError(errno.ENOSPC, "No space left on device")
        job = json.dumps({"url": server.url, "path": str(self.partial), "user_agent": "ua"}) + "\n"
        with mock.patch.object(fetch, "file_server", return_value=True), \
                mock.patch.object(fetch, "open", create=True, return_value=failing):
            code, answers = self.run_program(job)
        self.assertEqual((code, answers[-1]["kind"]), (1, "nospace"))

    def test_tool_maps_the_programs_errors(self):
        url = "https://stc001.dmm.com/f.dcv?dl_key=k"
        with self.assertRaises(fetch.NoSpace):
            fetch.fetch_with_program(self.fake_program([{"error": "disk full", "kind": "nospace"}], 1), url, "ua", self.partial)
        with self.assertRaises(fetch.DownloadError):
            fetch.fetch_with_program(self.fake_program([{"error": "HTTP 500", "kind": "download"}], 1), url, "ua", self.partial)
        with self.assertRaises(fetch.DownloadError):
            fetch.fetch_with_program([str(self.dir / "missing.exe")], url, "ua", self.partial)

    def test_download_part_uses_the_program_for_the_file_only(self):
        target = self.dir / "part1.dcv"
        program = self.fake_program([{"ok": 10}])
        with mock.patch.object(download, "resolve", return_value="https://stc001.dmm.com/f.dcv?dl_key=k") as resolve, \
                mock.patch.object(download, "fetch", side_effect=AssertionError("not in this program")):
            download.download_part(mock.Mock(user_agent="ua"), "https://www.dmm.co.jp/x", target, "abc00123",
                                   attempts=1, retry_delay=0, helper=program)
        resolve.assert_called_once()  # the link steps ran here
        self.assertEqual(target.read_bytes(), b"x" * 10)


@unittest.skipUnless(MEDIA_TOOLS and MEDIAINFO, "ffmpeg/mediainfo not installed")
class DecryptTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dmm test "))
        self.javit = make_fake_javit(self.dir / "jav-it folder")
        self.work = self.dir / "work"
        self.work.mkdir()
        self.dcv = make_clip(self.work / "part1.dcv", "abc00123", 1)
        self.out = self.work / "part1.mkv"
        self.login = Login("fan@example.com", "secret", "test")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_mode(self, mode: str, **kwargs):
        with mock.patch.dict(os.environ, {"FAKE_JAVIT_MODE": mode}):
            return decrypting.decrypt(self.javit, MEDIAINFO, self.dcv, self.out, kwargs.pop("login", self.login), retry_delay=0, **kwargs)

    def calls(self) -> list[str]:
        return (self.javit.parent / "calls.log").read_text().splitlines()

    def test_success_with_login_in_environment(self):
        self.assertEqual(self.run_mode("ok"), self.out)
        self.assertTrue(self.dcv.exists())
        self.assertEqual(self.calls(), [f"ok cwd=True user=fan@example.com tmp={self.work / 'tmp'}"])
        self.assertEqual(list(self.work.glob("*.log")), [])

    def test_license_prompt_is_answered(self):
        self.run_mode("license")
        self.assertEqual(len(self.calls()), 2)

    def test_license_prompt_without_email(self):
        with self.assertRaises(decrypting.DecryptError) as caught:
            self.run_mode("license", login=None)
        self.assertIn("javit_email", str(caught.exception))

    def test_connection_error_after_retries(self):
        with self.assertRaises(decrypting.DecryptError) as caught:
            self.run_mode("connection", attempts=2)
        self.assertIn("no internet connection", str(caught.exception))
        self.assertEqual(len(self.calls()), 2)
        self.assertFalse(self.out.exists())

    def test_incomplete_output_is_refused(self):
        with self.assertRaises(decrypting.DecryptError) as caught:
            self.run_mode("small")
        self.assertIn("too small", str(caught.exception))
        self.assertTrue(caught.exception.download_again)  # the pipeline downloads the part once more
        self.assertFalse(caught.exception.corrupt_source)
        self.assertFalse(self.out.exists())

    def test_damaged_download_is_flagged(self):
        self.dcv.write_bytes(b"not a video at all" * 100)
        with self.assertRaises(decrypting.DecryptError) as caught:
            self.run_mode("ok")
        self.assertTrue(caught.exception.corrupt_source)


@unittest.skipUnless(MEDIA_TOOLS and MEDIAINFO, "ffmpeg/mediainfo not installed")
class DecryptVrTest(unittest.TestCase):
    """jav-it's VR mode: `-t dmm-vr`, a shorter time limit, the licence message, and checks that suit VR files."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dmm vr test "))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.javit = make_fake_javit(self.dir / "jav-it folder", vr=True)
        self.work = self.dir / "work"
        self.work.mkdir()
        self.wsdcf = make_clip(self.work / "part1.wsdcf", "sivr00123", 1, container="mp4")
        self.out = self.work / "part1.mp4"
        self.login = Login("fan@example.com", "secret", "test")

    def run_mode(self, mode: str, source=None, vr=True, **env):
        with mock.patch.dict(os.environ, {"FAKE_JAVIT_MODE": mode, **env}):
            return decrypting.decrypt(self.javit, MEDIAINFO, source or self.wsdcf, self.out, self.login, retry_delay=0,
                                      vr=vr)

    def calls(self) -> list[str]:
        return (self.javit.parent / "calls.log").read_text().splitlines()

    def test_vr_mode_and_its_time_limit(self):
        with mock.patch.object(decrypting, "_run", wraps=decrypting._run) as run:
            self.assertEqual(self.run_mode("ok"), self.out)
        self.assertEqual((self.javit.parent / "kinds.log").read_text().split(), ["dmm-vr"])
        self.assertEqual(run.call_args.args[6], decrypting.vr_timeout(self.wsdcf.stat().st_size))
        self.assertEqual(run.call_args.args[7], "dmm-vr")
        self.assertTrue(self.wsdcf.exists())
        self.assertEqual(decrypting.vr_timeout(0), 300)
        self.assertEqual(decrypting.vr_timeout(4_000_000_000), 1100)  # a 4 GB part: about 18 minutes

    def test_the_licence_message_is_not_tried_again(self):
        with self.assertRaises(decrypting.NeedsVrLicence) as caught:
            self.run_mode("vr-licence")
        self.assertIn("DMM VR Player's licence", str(caught.exception))
        self.assertFalse(caught.exception.download_again)
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse(self.out.exists())

    def test_an_unreadable_vr_download_is_checked_by_its_result(self):
        encrypted = self.work / "part2.wsdcf"
        encrypted.write_bytes(os.urandom(4000))
        result = make_clip(self.dir / "result.mp4", "sivr00123", 1, container="mp4")
        self.assertEqual(self.run_mode("ok", encrypted, FAKE_JAVIT_RESULT=str(result)), self.out)
        with self.assertRaises(decrypting.DecryptError) as caught:  # 2D: an unreadable download is a bad download
            self.run_mode("ok", encrypted, vr=False, FAKE_JAVIT_RESULT=str(result))
        self.assertTrue(caught.exception.corrupt_source)

    def test_dmms_tag_is_not_required_in_a_vr_result(self):
        untagged = make_clip(self.dir / "untagged.mp4", None, None, container="mp4")
        self.assertEqual(self.run_mode("ok", FAKE_JAVIT_RESULT=str(untagged)), self.out)
        with self.assertRaises(decrypting.DecryptError) as caught:
            self.run_mode("ok", vr=False, FAKE_JAVIT_RESULT=str(untagged))
        self.assertIn("lost DMM's video/part data", str(caught.exception))

    def test_a_short_vr_result_is_downloaded_again(self):
        with self.assertRaises(decrypting.DecryptError) as caught:
            self.run_mode("small")
        self.assertTrue(caught.exception.bad_output)


if __name__ == "__main__":
    unittest.main()
