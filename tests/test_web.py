"""The browser page's server: who may use it, and a queue from adding to saving (DMM and the pipeline faked)."""

import dataclasses
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import requests

from dmm_downloader import config, credentials, links, pipeline, web
from dmm_downloader.dmm import DmmSession, LoginError


def choice(cid, vr=False, source="premium"):
    versions = {"8k": [f"https://www.dmm.co.jp/{cid}/p1", f"https://www.dmm.co.jp/{cid}/p2"], "4000": [f"https://www.dmm.co.jp/{cid}/q1", f"https://www.dmm.co.jp/{cid}/q2"]} if vr \
        else {"4k/vp9": [f"https://www.dmm.co.jp/{cid}/a"], "6000": [f"https://www.dmm.co.jp/{cid}/b"]}
    best = "8k" if vr else "4k/vp9"
    return links.Choice(source, best, versions[best], {k: len(v) for k, v in versions.items()}, vr=vr, versions=versions)


LIBRARY = {"sone00876": links.LibraryItem("videoa", False, True, "A title", "2026-02-01T10:00:00Z"),
           "sivr00457": links.LibraryItem("videoa", True, True, "A VR title", "2026-03-01T10:00:00Z"),
           "snos00070": links.LibraryItem("videoa", False, False, "Streaming", "2026-01-01T10:00:00Z")}


class WebTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dmm web "))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.calls = []
        self.found = {"abc00123": choice("abc00123"), "xyz00456": choice("xyz00456"), "sivr00457": choice("sivr00457", vr=True, source="library")}
        patches = [
            mock.patch.object(config, "config_dir", return_value=self.dir / "config"),
            mock.patch.object(config, "log_dir", return_value=self.dir / "logs"),
            mock.patch.object(credentials, "find", return_value=credentials.Login("fan@example.com", "pw", "password store")),
            mock.patch.object(credentials, "store_available", return_value=True),
            mock.patch.object(DmmSession, "load", return_value=True),
            mock.patch.object(DmmSession, "is_logged_in", return_value=True),
            mock.patch.object(DmmSession, "check_login", return_value=True),
            mock.patch.object(DmmSession, "login", side_effect=LoginError("no DMM in the tests")),  # never DMM
            mock.patch.object(links, "library_list", return_value=LIBRARY),
            mock.patch.object(links, "find", side_effect=self.fake_find),
            mock.patch.object(pipeline, "process", side_effect=self.fake_process),
            mock.patch.object(web.tools, "find_tool", side_effect=lambda name, override=None: self.dir / name),
            mock.patch.object(web.tools, "find_tools", side_effect=web.tools.ToolError("no ffmpeg")),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.dir / "DECRYPT.MOD").write_text("")
        self.settings = config.Settings(output_dir=str(self.dir / "out"), parallel=4)
        self.server = web.make_server(self.settings, port=0)
        self.server.app.startup()
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.server.port}"
        self.http = requests.Session()
        self.addCleanup(self.http.close)

    def stop_server(self):
        self.server.app.quit.set()
        self.server.shutdown()
        self.server.server_close()

    def fake_find(self, session, cid, source="auto", wanted=None):
        if cid not in self.found:
            raise links.NoLinks("not on DMM Premium; not in your DMM library")
        return self.found[cid]

    def fake_process(self, cid, session, login, tools, opts, report, choice=None):
        self.calls.append((cid, opts.rate, choice.label if choice else None, opts.fetch_helper))
        report.step("part 1/1: downloading")
        report.progress(50, 100)
        return pipeline.Result(cid, True, "premium, 4k: done", [Path(opts.out_dir) / f"{cid}.mkv"])

    def sign_in(self):
        answer = self.http.get(f"{self.base}/?key={self.server.token}", allow_redirects=False)
        self.assertEqual(answer.status_code, 303)
        self.assertIn("HttpOnly; SameSite=Strict", answer.headers["Set-Cookie"])

    def post(self, path, body=None, expect=200):
        answer = self.http.post(f"{self.base}{path}", json=body or {}, headers={"X-DMM-Downloader": "1"})
        self.assertEqual(answer.status_code, expect, answer.text)
        return answer.json()

    def get(self, path, **kwargs):
        return self.http.get(f"{self.base}{path}", headers={"X-DMM-Downloader": "1", **kwargs.pop("headers", {})},
                             **kwargs)

    def state(self, lib=-1):
        return self.get(f"/api/state?lib={lib}").json()

    def wait_for(self, check, seconds=20):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            state = self.state()
            if check(state):
                return state
            time.sleep(0.05)
        self.fail(f"timed out; last state: {state['jobs']}")

    def test_only_the_tools_browser_gets_in(self):
        self.assertEqual(self.get("/api/state").status_code, 403)
        self.assertEqual(self.http.get(f"{self.base}/?key=wrong").status_code, 403)
        self.sign_in()
        self.assertEqual(self.get("/api/state").status_code, 200)
        self.assertEqual(self.http.post(f"{self.base}/api/clear", json={}).status_code, 403)  # no header of ours
        # Reading too: the cookie alone would also come from another local program's page (cookies ignore the port).
        for path in ("/api/state", f"/api/browse?path={self.dir}"):
            self.assertEqual(self.http.get(f"{self.base}{path}").status_code, 403, path)
            self.assertEqual(self.get(path).status_code, 200, path)
        self.assertEqual(self.get("/api/state", headers={"Host": "evil.example"}).status_code, 403)
        self.assertIn(b"DMM Downloader", self.http.get(f"{self.base}/").content)
        self.assertIn("default-src 'none'", self.http.get(f"{self.base}/app.js").headers["Content-Security-Policy"])

    def test_add_look_up_pick_and_download(self):
        self.sign_in()
        answer = self.post("/api/add", {"text": "abc00123\nhttps://www.dmm.co.jp/digital/videoa/-/detail/=/cid=xyz00456/\nSSIS-123\nnone00001"})
        self.assertEqual(answer["added"], 3)
        self.assertEqual(answer["invalid"], ["SSIS-123"])
        state = self.wait_for(lambda s: all(j["state"] not in ("checking",) for j in s["jobs"]))
        jobs = {j["cid"]: j for j in state["jobs"]}
        self.assertEqual(jobs["abc00123"]["state"], "ready")
        self.assertEqual(jobs["abc00123"]["qualities"], ["4k/vp9", "6000"])
        self.assertEqual(jobs["abc00123"]["names"], ["abc00123.mkv"])
        self.assertEqual(jobs["none00001"]["state"], "bad")
        self.assertIn("not in your DMM library", jobs["none00001"]["message"])

        self.post("/api/pick", {"id": jobs["xyz00456"]["id"], "rate": "6000"})
        self.post("/api/pick", {"id": jobs["abc00123"]["id"], "rate": "9000"}, expect=409)
        self.assertEqual(self.post("/api/start")["started"], 2)
        state = self.wait_for(lambda s: not s["running"] and all(j["state"] in ("done", "bad") for j in s["jobs"]))
        self.assertEqual(sorted(c[:3] for c in self.calls), [("abc00123", None, "4k/vp9"), ("xyz00456", "6000", "4k/vp9")])
        done = {j["cid"]: j for j in state["jobs"]}["abc00123"]
        self.assertTrue(done["files"][0].endswith("abc00123.mkv"))
        self.post("/api/clear")
        self.assertEqual(self.state()["jobs"], [])

    def test_vr_names_and_already_saved(self):
        self.sign_in()
        out = Path(self.settings.output_dir)
        out.mkdir()
        (out / "abc00123.mkv").write_bytes(b"x")
        self.post("/api/add", {"text": "sivr00457 abc00123"})
        state = self.wait_for(lambda s: all(j["state"] != "checking" for j in s["jobs"]))
        jobs = {j["cid"]: j for j in state["jobs"]}
        self.assertEqual(jobs["sivr00457"]["names"], ["sivr00457-pt1.wsdcf", "sivr00457-pt2.wsdcf"])  # no VR.MOD here
        self.assertTrue(jobs["sivr00457"]["vr"])
        self.assertEqual(jobs["abc00123"]["state"], "there")
        self.assertFalse(state["tools"]["vr"])
        self.assertIn("VR.MOD is not next to jav-it", state["tools"]["no_vr"])

    def test_vr_is_decrypted_with_vr_mod_and_saved_files_are_used(self):
        (self.dir / "VR.MOD").write_text("")
        self.server.app.find_tools()
        self.sign_in()
        state = self.state()
        self.assertTrue(state["tools"]["vr"])
        self.assertNotIn("vr_mod", state["tools"]["errors"])
        out = Path(self.settings.output_dir)
        out.mkdir()
        for name in ("sivr00457-pt1.wsdcf", "sivr00457-pt2.wsdcf"):  # saved by a version that didn't decrypt VR
            (out / name).write_bytes(b"\x00\x01" * 100)
        self.assertFalse({r["cid"]: r for r in self.state(lib=-1)["library"]}["sivr00457"]["saved"])

        self.post("/api/add", {"text": "sivr00457"})
        job = self.wait_for(lambda s: s["jobs"][0]["state"] != "checking")["jobs"][0]
        self.assertEqual(job["state"], "ready")  # not "Already saved": the encrypted files are decrypted
        self.assertEqual(job["names"], ["sivr00457-pt1.mp4", "sivr00457-pt2.mp4"])  # two parts, never joined
        self.assertEqual(job["saved_encrypted"], ["sivr00457-pt1.wsdcf", "sivr00457-pt2.wsdcf"])

        seen = []

        def decrypted(cid, session, login, tools, opts, report, choice=None):
            seen.append(tools.no_vr)
            return pipeline.Result(cid, True, "library, VR 8k: sivr00457.mp4", [opts.out_dir / "sivr00457.mp4"])

        with mock.patch.object(pipeline, "process", side_effect=decrypted):
            self.post("/api/start")
            state = self.wait_for(lambda s: not s["running"] and s["jobs"][0]["state"] == "done")
        self.assertEqual(seen, [""])  # the pipeline was told VR is decrypted
        self.assertFalse(state["jobs"][0]["encrypted"])  # "Saved", not "VR saved (encrypted)"

    def test_the_library_finds_videos_saved_under_dmms_id(self):
        self.sign_in()
        out = Path(self.settings.output_dir)
        (out / pipeline.WORK_FOLDER).mkdir(parents=True)
        (out / "abc00876.mkv").write_bytes(b"x")  # an earlier run: DMM's file for sone00876 said abc00876 (made up)

        def saved():
            return {row["cid"]: row["saved"] for row in self.state(lib=-1)["library"]}

        self.assertFalse(saved()["sone00876"])
        (out / pipeline.WORK_FOLDER / pipeline.IDS_FILE).write_text('{"sone00876": "abc00876"}', encoding="utf-8")
        self.assertTrue(saved()["sone00876"])
        self.post("/api/library/add", {"cids": ["sone00876"]})
        job = self.wait_for(lambda s: s["jobs"][0]["state"] != "checking")["jobs"][0]
        self.assertEqual(job["state"], "there")  # not looked up on DMM, not downloaded again

        # Parts left by a run that didn't finish don't count as saved.
        for name in ("sivr00457-pt1.wsdcf", "sivr00457-pt2.wsdcf"):
            (out / name).write_bytes(b"\x00\x01" * 100)
        self.assertTrue(saved()["sivr00457"])
        (out / pipeline.WORK_FOLDER / "sivr00457").mkdir()
        (out / pipeline.WORK_FOLDER / "sivr00457" / "state.json").write_text("{}", encoding="utf-8")
        self.assertFalse(saved()["sivr00457"])

    def test_vr_names_with_vr_mod(self):
        (self.dir / "VR.MOD").write_text("")
        self.server.app.find_tools()
        app = self.server.app
        urls = ["https://www.dmm.co.jp/x/part=1/", "https://www.dmm.co.jp/x/part=3/"]
        self.assertEqual(app.planned_names("sivr00457", True, "8k", urls), ["sivr00457-pt1.mp4", "sivr00457-pt3.mp4"])
        self.assertEqual(app.planned_names("sivr00457", True, "8k", urls[:1]), ["sivr00457.mp4"])
        self.assertTrue(self.settings.merge)  # "Join parts" on: VR parts still stay separate
        self.assertEqual(app.planned_names("sivr00457", True, "8k", [urls[0], "https://www.dmm.co.jp/x/part=2/"]),
                         ["sivr00457-pt1.mp4", "sivr00457-pt2.mp4"])

    def test_one_row_per_video(self):
        self.sign_in()
        self.post("/api/add", {"text": "new00001"})
        first = self.wait_for(lambda s: s["jobs"][0]["state"] == "bad")["jobs"][0]
        self.found["new00001"] = choice("new00001")  # bought meanwhile: added again
        self.post("/api/add", {"text": "new00001"})
        self.wait_for(lambda s: len(s["jobs"]) == 2 and s["jobs"][1]["state"] == "ready")
        refused = self.post("/api/retry", {"id": first["id"]}, expect=409)  # two rows would download it at once
        self.assertIn("already in the list", refused["error"])

    def test_saved_as_and_already_saved_use_dmms_id(self):
        self.sign_in()
        self.found["abc123"] = dataclasses.replace(choice("abc123"), page_cid="abc00123")
        self.post("/api/add", {"text": "abc123"})
        state = self.wait_for(lambda s: s["jobs"][0]["state"] == "ready")
        self.assertEqual(state["jobs"][0]["names"], ["abc00123.mkv"])  # DMM's Premium page calls it abc00123

        def named(cid, session, login, tools, opts, report, choice=None):
            report.named("abc00124")  # what DMM wrote inside the file
            return pipeline.Result(cid, False, "stopped")

        with mock.patch.object(pipeline, "process", side_effect=named):
            self.post("/api/start")
            state = self.wait_for(lambda s: not s["running"] and s["jobs"][0]["state"] == "failed")
        self.assertEqual(state["jobs"][0]["names"], ["abc00124.mkv"])
        out = Path(self.settings.output_dir)
        out.mkdir()
        (out / "abc00124.mkv").write_bytes(b"x")
        self.post("/api/retry", {"id": state["jobs"][0]["id"]})
        state = self.wait_for(lambda s: s["jobs"][0]["state"] == "there")
        self.assertIn("abc00124.mkv", state["jobs"][0]["message"])

    def test_parallel_downloads_but_one_vr_at_a_time(self):
        self.sign_in()
        running = {"all": 0, "vr": 0, "max_all": 0, "max_vr": 0}
        lock = threading.Lock()

        def slow(cid, session, login, tools, opts, report, choice=None):
            with opts.download_slot(vr=choice.vr):
                with lock:
                    running["all"] += 1
                    running["vr"] += choice.vr
                    running["max_all"] = max(running["max_all"], running["all"])
                    running["max_vr"] = max(running["max_vr"], running["vr"])
                time.sleep(0.2)
                with lock:
                    running["all"] -= 1
                    running["vr"] -= choice.vr
            return pipeline.Result(cid, True, "ok", [])

        cids = [f"aaa0000{n}" for n in range(1, 6)] + [f"sivr0040{n}" for n in range(1, 4)]
        for cid in cids:
            self.found[cid] = choice(cid, vr=cid.startswith("sivr"), source="library")
        with mock.patch.object(pipeline, "process", side_effect=slow):
            self.post("/api/add", {"text": " ".join(cids)})
            self.wait_for(lambda s: len(s["jobs"]) == 8 and all(j["state"] == "ready" for j in s["jobs"]))
            self.post("/api/start")
            self.wait_for(lambda s: not s["running"] and all(j["state"] == "done" for j in s["jobs"]))
        self.assertEqual(running["max_vr"], 1)
        self.assertEqual(running["max_all"], 4)

    def test_library_and_adding_from_it(self):
        self.sign_in()
        state = self.state(lib=-1)
        self.assertEqual([r["cid"] for r in state["library"]], ["sivr00457", "sone00876", "snos00070"])
        self.assertNotIn("library", self.state(lib=state["library_version"]))  # sent only when it changed
        self.found["sone00876"] = choice("sone00876", source="library")
        self.post("/api/library/add", {"cids": ["sone00876"]})
        state = self.wait_for(lambda s: s["jobs"] and s["jobs"][0]["state"] == "ready")
        self.assertEqual(state["jobs"][0]["source"], "library")

    def test_settings_are_saved(self):
        self.sign_in()
        self.post("/api/settings", {"parallel": 3, "retries": 7, "fetch_helper": "off"})
        self.post("/api/settings", {"parallel": 99}, expect=409)
        saved = config.load(config.config_path())
        self.assertEqual((saved.parallel, saved.retries, saved.fetch_helper), (3, 7, "off"))
        self.post("/api/options", {"source": "library", "merge": False})
        self.assertEqual(config.load(config.config_path()).source, "library")
        self.post("/api/options", {"source": "vrchannel"})
        self.assertEqual(config.load(config.config_path()).source, "vrchannel")
        self.post("/api/options", {"source": "nowhere"}, expect=409)

    def test_the_japan_route_is_saved_and_checked(self):
        self.sign_in()
        self.assertEqual(self.state()["japan"], {"state": "unknown", "message": "", "checked": "", "route": "own",
                                                 "shown": "this program's own connection"})
        with mock.patch.object(links, "japan_check", return_value=(True, "")) as check:
            self.post("/api/settings", {"vrchannel_route": "http://127.0.0.1:25345"})
            state = self.wait_for(lambda s: s["japan"]["state"] == "ok")  # checked by itself after the change
        self.assertEqual(config.load(config.config_path()).vrchannel_route, "http://127.0.0.1:25345")
        self.assertEqual((state["japan"]["route"], state["settings"]["vrchannel_route"]), ("proxy", "http://127.0.0.1:25345"))
        self.assertEqual(check.call_args.args[0].japan.proxy, "http://127.0.0.1:25345")
        self.assertEqual(self.server.app.session.japan.proxy, "http://127.0.0.1:25345")  # used by the lookups
        self.post("/api/settings", {"vrchannel_route": "socks5://127.0.0.1:1080"}, expect=409)
        self.assertEqual(config.load(config.config_path()).vrchannel_route, "http://127.0.0.1:25345")
        with mock.patch.object(links, "japan_check", return_value=(False, "DMM answered “not available in your region”")):
            answer = self.post("/api/japan/check")
        self.assertEqual(answer["state"], "region")
        self.assertEqual(self.state()["japan"]["state"], "region")
        with mock.patch.object(links, "japan_check", return_value=(None, "no answer through the Japan proxy")):
            self.assertEqual(self.post("/api/japan/check")["state"], "error")
        with mock.patch.object(links, "japan_check", return_value=(True, "")):
            self.post("/api/settings", {"vrchannel_route": ""})
            state = self.wait_for(lambda s: s["japan"]["state"] == "ok")
        self.assertEqual(state["japan"]["route"], "own")

    def test_refused_settings_change_nothing(self):
        # A refused value must not stay in use until a restart (it did: 99 videos at once after a refusal).
        self.sign_in()
        self.post("/api/settings", {"parallel": 3, "retries": 7})
        for body in ({"parallel": 99}, {"parallel": 5, "retries": 50}, {"retries": "many", "parallel": 2},
                     {"parallel": 0, "javit_email": "fan@example.com"}):
            self.post("/api/settings", body, expect=409)
            app = self.server.app.settings
            self.assertEqual((app.parallel, app.retries, app.javit_email), (3, 7, ""), body)
            saved = config.load(config.config_path())
            self.assertEqual((saved.parallel, saved.retries, saved.javit_email), (3, 7, ""), body)
        self.assertEqual(self.server.app.options().slots._downloads._initial_value, 3)
        self.post("/api/options", {"source": "premium", "quality": "best"})
        self.post("/api/options", {"source": "library", "quality": "nope"}, expect=409)
        self.post("/api/options", {"source": "library", "output_dir": ""}, expect=409)
        self.assertEqual(self.server.app.settings.source, "premium")
        self.assertEqual(config.load(config.config_path()).source, "premium")

    def test_an_expired_login_shows_in_the_header(self):
        self.sign_in()

        def expired(cid, session, login, tools, opts, report, choice=None):
            return pipeline.Result(cid, False, f"{cid}: DMM didn't give the download (HTTP 403): {web.LOGIN_AGAIN}")

        with mock.patch.object(pipeline, "process", side_effect=expired):
            self.post("/api/add", {"text": "abc00123"})
            self.wait_for(lambda s: s["jobs"][0]["state"] == "ready")
            self.post("/api/start")
            state = self.wait_for(lambda s: not s["running"] and s["jobs"][0]["state"] == "failed")
        self.assertFalse(state["dmm"]["logged_in"])
        self.assertIn("log in again (Settings)", state["dmm"]["error"])
        self.assertIn("log in again", state["jobs"][0]["message"])

    def fake_store(self, available=True):
        """A password store in memory instead of the system's."""
        store = {}

        def save(login):
            if not available:
                return False
            store["login"] = credentials.Login(login.username, login.password, "password store")
            return True

        for name, fake in (("find", lambda interactive=True: store.get("login")), ("save", save),
                           ("forget", lambda: bool(store.pop("login", None))),
                           ("store_available", lambda: available), ("from_environment", lambda: None)):
            patcher = mock.patch.object(credentials, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        return store

    def logins_given_to_jav_it(self):
        """Start abc00123 and return the login the pipeline (and so jav-it) got."""
        got = []

        def process(cid, session, login, tools, opts, report, choice=None):
            got.append(login)
            return pipeline.Result(cid, True, "premium, 4k: done", [Path(opts.out_dir) / f"{cid}.mkv"])

        self.post("/api/clear")  # "Clear finished": the video is added again
        self.post("/api/add", {"text": "abc00123"})
        self.wait_for(lambda s: s["jobs"] and s["jobs"][0]["state"] == "ready")
        with mock.patch.object(pipeline, "process", side_effect=process):
            self.post("/api/start")
            self.wait_for(lambda s: not s["running"] and s["jobs"][0]["state"] == "done")
        return got

    def test_no_dmm_session_without_a_login(self):
        # A DMM session saved earlier (load and is_logged_in say yes) but no login: jav-it couldn't decrypt.
        self.sign_in()
        self.fake_store()
        self.server.app.open_session()
        state = self.state()
        self.assertFalse(state["dmm"]["logged_in"])
        self.assertIn("Not logged in to DMM yet", state["dmm"]["error"])
        self.post("/api/add", {"text": "abc00123"})
        self.wait_for(lambda s: s["jobs"][0]["state"] == "bad")
        self.assertEqual(self.calls, [])

    def test_log_in_log_out_and_another_account_reach_jav_it(self):
        self.sign_in()
        store = self.fake_store()
        auth = self.dir / pipeline.decrypting.AUTH_FILE  # jav-it's own sign-in, next to jav-it
        self.assertEqual(self.server.app.javit.parent, self.dir)
        with mock.patch.object(DmmSession, "login", return_value=None):
            self.post("/api/login", {"email": "first@example.com", "password": "pw1"})
            self.assertEqual(store["login"].username, "first@example.com")
            self.assertEqual(self.state()["dmm"]["account"], "first@example.com")
            auth.write_text("{}")  # made by a jav-it run before this program knew whose it is
            self.assertEqual([(l.username, l.password) for l in self.logins_given_to_jav_it()], [("first@example.com", "pw1")])
            self.assertFalse(auth.exists())
            auth.write_text("{}")  # jav-it signed in again, as first@example.com
            self.logins_given_to_jav_it()
            self.assertTrue(auth.exists())  # the same account: kept

            self.post("/api/login", {"email": "second@example.com", "password": "pw2"})  # another account
            self.assertEqual(store["login"].username, "second@example.com")
            self.assertEqual([(l.username, l.password) for l in self.logins_given_to_jav_it()], [("second@example.com", "pw2")])
            self.assertFalse(auth.exists())  # removed before jav-it ran with the new login
            auth.write_text("{}")

        self.post("/api/logout")
        self.assertNotIn("login", store)
        self.assertFalse(auth.exists())
        self.assertFalse((self.dir / "config" / pipeline.decrypting.ACCOUNT_FILE).exists())
        state = self.state()
        self.assertFalse(state["dmm"]["logged_in"])
        self.assertEqual(state["dmm"]["account"], "")
        self.assertIsNone(self.server.app.login)
        self.post("/api/clear")
        self.post("/api/add", {"text": "abc00123"})
        self.wait_for(lambda s: s["jobs"][0]["state"] == "bad")
        self.assertIn("Not logged in", self.post("/api/start", expect=409)["error"])

    def test_without_a_password_store_the_typed_login_lasts_this_run(self):
        self.sign_in()
        store = self.fake_store(available=False)
        with mock.patch.object(DmmSession, "login", return_value=None):
            self.post("/api/login", {"email": "fan@example.com", "password": "pw"})
        self.assertEqual(store, {})
        self.post("/api/doctor")  # "Check setup again" opens the session again: still the typed login
        state = self.state()
        self.assertTrue(state["dmm"]["logged_in"])
        self.assertEqual(state["dmm"]["login_source"], "typed")
        self.assertEqual([l.username for l in self.logins_given_to_jav_it()], ["fan@example.com"])
        self.post("/api/logout")
        self.assertIsNone(self.server.app.typed_login)
        self.assertFalse(self.state()["dmm"]["logged_in"])

    def test_a_login_from_the_environment_is_not_changed_by_the_page(self):
        self.sign_in()
        env = credentials.Login("env@example.com", "pw", "environment")
        with mock.patch.object(credentials, "from_environment", return_value=env), \
                mock.patch.object(credentials, "forget") as forget:
            refused = self.post("/api/login", {"email": "other@example.com", "password": "pw2"}, expect=409)
            self.assertIn("DMM_USERNAME and DMM_PASSWORD", refused["error"])
            self.assertIn("DMM_USERNAME", self.post("/api/logout", expect=409)["error"])
        forget.assert_not_called()

    def test_not_logged_in_refuses_to_start(self):
        self.sign_in()
        with mock.patch.object(DmmSession, "is_logged_in", return_value=False):
            self.server.app.open_session()
        state = self.state()
        self.assertFalse(state["dmm"]["logged_in"])
        self.post("/api/add", {"text": "abc00123"})
        self.wait_for(lambda s: s["jobs"][0]["state"] == "bad")
        self.post("/api/start", expect=409)


class FetchHelperTest(unittest.TestCase):
    def test_where_the_download_program_comes_from(self):
        folder = Path(tempfile.mkdtemp(prefix="dmm helper "))
        self.addCleanup(shutil.rmtree, folder, True)
        with mock.patch.object(web.tools, "program_dir", return_value=folder):
            self.assertIsNone(web.fetch_helper_command(config.Settings()))  # not next to the program: in-process
            (folder / web.FETCH_PROGRAM).write_bytes(b"")
            self.assertEqual(web.fetch_helper_command(config.Settings()), [str(folder / web.FETCH_PROGRAM)])
            self.assertIsNone(web.fetch_helper_command(config.Settings(fetch_helper="off")))
        self.assertEqual(web.fetch_helper_command(config.Settings(fetch_helper="module"))[1:], ["-m", "dmm_downloader.fetch"])


if __name__ == "__main__":
    unittest.main()
