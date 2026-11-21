import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from dmm_downloader import cli, config, credentials, logs, pipeline


def run(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(list(args))
    return code, out.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for target, value in ((config, "config_dir"), (config, "log_dir")):
            patcher = mock.patch.object(target, value, return_value=self.dir / value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"DMM_USERNAME": "", "DMM_PASSWORD": ""})
        env.start()
        self.addCleanup(env.stop)
        store = mock.patch.object(credentials, "from_store", return_value=None)
        store.start()
        self.addCleanup(store.stop)

    def tearDown(self):
        import logging

        root = logging.getLogger()
        for handler in [h for h in root.handlers if getattr(h, "dmm_downloader", False)]:  # the tool's log (and -v)
            root.removeHandler(handler)
            handler.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_get_is_the_default_command(self):
        seen = {}

        def fake_get(args, settings, interactive):
            seen.update(ids=args.ids, out=args.out, verbose=args.verbose)
            return 0

        with mock.patch.dict(cli.COMMAND_FUNCTIONS, {"get": fake_get}):
            self.assertEqual(run("-v", "abc00123", "-o", "D:/x")[0], 0)
        self.assertEqual(seen, {"ids": ["abc00123"], "out": Path("D:/x"), "verbose": True})

    def test_verbose_anywhere_on_the_line(self):
        seen = []

        def fake_get(args, settings, interactive):
            seen.append((args.ids, args.source, args.verbose))
            return 0

        with mock.patch.dict(cli.COMMAND_FUNCTIONS, {"get": fake_get}):
            run("--source", "library", "-v", "abc00123")
            run("abc00123", "--verbose")
            run("get", "abc00123")
            run("-v", "get", "abc00123")
        self.assertEqual(seen, [
            (["abc00123"], "library", True),
            (["abc00123"], None, True),
            (["abc00123"], None, False),
            (["abc00123"], None, True),
        ])

    def test_ids_file_and_invalid_entries(self):
        ids_file = self.dir / "ids.txt"
        ids_file.write_text("\ufeffabc00123\nSSIS-123 # dvd id\nxyz00456\n", encoding="utf-8")
        args = cli.build_parser().parse_args(["get", "-i", str(ids_file), "def00789"])
        out = io.StringIO()
        with redirect_stdout(out):
            found = cli.read_ids(args)
        self.assertEqual(found, ["def00789", "abc00123", "xyz00456"])
        self.assertIn("SSIS-123", out.getvalue())

    def test_doctor_says_whether_vr_is_decrypted(self):
        javit = self.dir / "tools" / "jav-it"
        javit.parent.mkdir()
        with mock.patch.object(cli, "find_javit", return_value=javit), \
                mock.patch.object(cli, "find_mediainfo", return_value=self.dir / "mediainfo"), \
                mock.patch.object(cli, "find_merge_tools", return_value=None), \
                mock.patch.object(cli.links, "japan_check", return_value=(False, "DMM answered “not available in your region”")) as check:
            code, output = run("doctor")
            self.assertEqual(code, 0)
            self.assertIn("VR videos: saved encrypted (.wsdcf), VR.MOD is not next to jav-it", output)
            (javit.parent / "VR.MOD").write_text("")
            code, output = run("doctor")
        self.assertIn("VR videos: decrypted (VR.MOD found next to jav-it)", output)
        # The Japan route (VR Channel downloads), asked through the route set in the settings (none: this connection).
        self.assertIn("Japan route: this program's own connection: not in Japan", output)
        self.assertEqual(check.call_args.args[0].japan.proxy, "")

    def test_no_login_is_a_setup_problem(self):
        javit = self.dir / "jav-it"
        with mock.patch.object(cli, "find_javit", return_value=javit), \
                mock.patch.object(cli, "find_mediainfo", return_value=javit), \
                mock.patch.object(cli.DmmSession, "is_logged_in", return_value=False), \
                mock.patch.object(credentials.sys.stdin, "isatty", return_value=False):
            code, output = run("get", "abc00123")
        self.assertEqual(code, 2)
        self.assertIn("No DMM login", output)

    def test_get_reports_and_writes_failed_ids(self):
        out_dir = self.dir / "out"
        results = [pipeline.Result("abc00123", True, "premium, 4k: abc00123.mkv", [out_dir / "abc00123.mkv"]),
                   pipeline.Result("xyz00456", False, "not on DMM Premium")]
        with mock.patch.object(cli, "find_javit", return_value=Path("j")), \
                mock.patch.object(cli, "find_mediainfo", return_value=Path("m")), \
                mock.patch.object(cli, "open_session", return_value=(mock.Mock(), None)), \
                mock.patch.object(pipeline, "process", side_effect=results):
            code, output = run("abc00123", "xyz00456", "-o", str(out_dir))
        self.assertEqual(code, 1)
        self.assertIn("Finished: 1  Already there: 0  Failed: 1", output)
        self.assertEqual((out_dir / "failed_ids.txt").read_text().split(), ["xyz00456"])

    def test_get_without_jav_it_still_saves_vr_videos(self):
        # As on the page: a missing program fails only the videos that need it (VR is then saved encrypted).
        out_dir = self.dir / "out"
        no_javit = cli.SetupError("jav-it was not found. Put jav-it (and DECRYPT.MOD, for VR also VR.MOD) next to it.")
        seen = []

        def process(cid, session, login, tools, opts, report):
            seen.append((tools.no_2d, tools.no_vr))
            if cid.startswith("sivr"):
                return pipeline.Result(cid, True, "library, VR 8k: sivr00457.wsdcf", encrypted=True)
            if cid == "none00001":
                return pipeline.Result(cid, False, "not on DMM Premium")
            return pipeline.Result(cid, False, tools.no_2d)  # what the pipeline answers for a 2D video then

        with mock.patch.object(cli, "find_javit", side_effect=no_javit), \
                mock.patch.object(cli.tools, "find_tool", side_effect=cli.tools.ToolError("not found")), \
                mock.patch.object(cli, "find_mediainfo", return_value=Path("m")), \
                mock.patch.object(cli, "find_merge_tools", return_value=None), \
                mock.patch.object(cli, "open_session", return_value=(mock.Mock(), None)), \
                mock.patch.object(pipeline, "process", side_effect=process):
            code, output = run("sivr00457", "abc00123", "-o", str(out_dir))
            self.assertEqual(code, 2)  # only the missing program stopped a video: a setup problem, as before
            self.assertEqual(seen[0], (str(no_javit), "jav-it was not found"))
            self.assertIn("Note: 2D videos can't be decrypted here, they fail: jav-it was not found", output)
            self.assertIn("VR videos are saved encrypted (.wsdcf): jav-it was not found.", output)
            self.assertIn("Done: library, VR 8k: sivr00457.wsdcf", output)
            self.assertIn(f"FAILED: {no_javit}", output)
            self.assertIn("Finished: 1  Already there: 0  Failed: 1", output)
            code, _ = run("abc00123", "none00001", "-o", str(out_dir))
        self.assertEqual(code, 1)  # another failure too

    def test_an_unexpected_error_on_one_video_doesnt_stop_the_others(self):
        out_dir = self.dir / "out"
        results = [RuntimeError("the file is in use"), pipeline.Result("xyz00456", True, "premium, 4k: xyz00456.mkv")]
        with mock.patch.object(cli, "find_javit", return_value=Path("j")), \
                mock.patch.object(cli, "find_mediainfo", return_value=Path("m")), \
                mock.patch.object(cli, "open_session", return_value=(mock.Mock(), None)), \
                mock.patch.object(pipeline, "process", side_effect=results) as process:
            code, output = run("abc00123", "xyz00456", "-o", str(out_dir))
        self.assertEqual(process.call_count, 2)
        self.assertEqual(code, 1)
        self.assertIn("FAILED: unexpected error: the file is in use", output)
        self.assertIn("Finished: 1  Already there: 0  Failed: 1", output)
        self.assertEqual((out_dir / "failed_ids.txt").read_text().split(), ["abc00123"])

    def test_finished_ids_leave_the_failed_list(self):
        out_dir = self.dir / "out"
        out_dir.mkdir()
        (out_dir / "failed_ids.txt").write_text("xyz00456\ndef00789\n", encoding="utf-8")
        with mock.patch.object(cli, "find_javit", return_value=Path("j")), \
                mock.patch.object(cli, "find_mediainfo", return_value=Path("m")), \
                mock.patch.object(cli, "open_session", return_value=(mock.Mock(), None)), \
                mock.patch.object(pipeline, "process", side_effect=[pipeline.Result("xyz00456", True, "done")]):
            self.assertEqual(run("xyz00456", "-o", str(out_dir))[0], 0)
        self.assertEqual((out_dir / "failed_ids.txt").read_text().split(), ["def00789"])
        with mock.patch.object(cli, "find_javit", return_value=Path("j")), \
                mock.patch.object(cli, "find_mediainfo", return_value=Path("m")), \
                mock.patch.object(cli, "open_session", return_value=(mock.Mock(), None)), \
                mock.patch.object(pipeline, "process", side_effect=[pipeline.Result("def00789", True, "done")]):
            run("def00789", "-o", str(out_dir))
        self.assertFalse((out_dir / "failed_ids.txt").exists())

    def test_config_file_is_created_and_read(self):
        code, output = run("config")
        self.assertEqual(code, 0)
        path = config.config_path()
        self.assertTrue(path.exists())
        path.write_text(path.read_text().replace("source = auto", "source = library").replace("merge = yes", "merge = no"))
        settings = config.load()
        self.assertEqual((settings.source, settings.merge, settings.retries), ("library", False, 5))

    def test_default_folder_of_the_windows_program_is_next_to_it(self):
        self.assertEqual(config.default_output_dir(), Path("downloads"))  # from source: where it is started
        exe = self.dir / "DMM-Downloader" / "DMM-Downloader.exe"
        with mock.patch.object(config.sys, "frozen", True, create=True), mock.patch.object(config.sys, "executable", str(exe)):
            self.assertEqual(config.default_output_dir(), exe.resolve().parent / "downloads")

    def test_log_file_hides_secrets(self):
        path = logs.setup()
        import logging

        logging.getLogger("t").info("login password=hunter2 at https://stc1.dmm.com/x.dcv?dl_key=abc")
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("hunter2", text)
        self.assertNotIn("dl_key=abc", text)


class CredentialsTest(unittest.TestCase):
    def test_environment_first(self):
        with mock.patch.dict(os.environ, {"DMM_USERNAME": "fan@example.com", "DMM_PASSWORD": "pw"}):
            login = credentials.find(interactive=False)
        self.assertEqual((login.username, login.source), ("fan@example.com", "environment"))

    def test_nothing_without_a_terminal(self):
        with mock.patch.dict(os.environ, {"DMM_USERNAME": "", "DMM_PASSWORD": ""}), \
                mock.patch.object(credentials, "from_store", return_value=None), \
                mock.patch.object(credentials.sys.stdin, "isatty", return_value=False):
            self.assertIsNone(credentials.find())


if __name__ == "__main__":
    unittest.main()
