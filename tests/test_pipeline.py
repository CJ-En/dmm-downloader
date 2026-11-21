import errno
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from dmm_downloader import download, fetch, links, pipeline
from dmm_downloader.credentials import Login

from .fakes import MEDIA_TOOLS, MEDIAINFO, copy_to, make_clip, make_fake_javit


class Quiet(pipeline.Report):
    def __init__(self):
        self.steps = []

    def step(self, text):
        self.steps.append(text)


class PlaceTest(unittest.TestCase):
    def test_an_existing_file_is_only_replaced_when_asked(self):
        folder = Path(tempfile.mkdtemp(prefix="dmm place "))
        self.addCleanup(shutil.rmtree, folder, True)
        new, target = folder / "part1.mkv", folder / "abc00123.mkv"
        new.write_bytes(b"new")
        target.write_bytes(b"old")
        with self.assertRaises(FileExistsError) as caught:
            pipeline._place(new, target)
        self.assertEqual(caught.exception.filename, str(target))
        self.assertEqual((new.read_bytes(), target.read_bytes()), (b"new", b"old"))
        pipeline._place(new, target, replace=True)
        self.assertEqual(target.read_bytes(), b"new")
        self.assertFalse(new.exists())


class DmmIdsTest(unittest.TestCase):
    def test_dmms_ids_are_kept_exactly_and_a_failed_write_leaves_nothing(self):
        out = Path(tempfile.mkdtemp(prefix="dmm ids "))
        self.addCleanup(shutil.rmtree, out, True)
        pipeline._remember_dmm_id(out, "abc123", "abc00123")
        self.assertEqual(pipeline.known_dmm_ids(out), {"abc123": "abc00123"})
        self.assertIsNone(pipeline.known_dmm_id(out, "abc0123"))  # only the id DMM's file was read for
        with mock.patch.object(pipeline.os, "replace", side_effect=PermissionError(13, "in use")):
            pipeline._remember_dmm_id(out, "xyz456", "xyz00456")  # e.g. read by another copy at that moment
        self.assertEqual(pipeline.known_dmm_ids(out), {"abc123": "abc00123"})
        self.assertEqual([p.name for p in (out / pipeline.WORK_FOLDER).iterdir()], [pipeline.IDS_FILE])
        pipeline._remember_dmm_id(out, "abc123", "abc123")  # DMM's file says the id itself now
        self.assertEqual(pipeline.known_dmm_ids(out), {})
        self.assertFalse((out / pipeline.WORK_FOLDER / pipeline.IDS_FILE).exists())


@unittest.skipUnless(MEDIA_TOOLS and MEDIAINFO, "ffmpeg/mediainfo not installed")
class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dmm pipeline "))
        self.src = self.dir / "source"
        self.src.mkdir()
        self.out = self.dir / "out"
        javit = make_fake_javit(self.dir / "tools")
        self.tools = pipeline.Tools(javit, MEDIAINFO, MEDIA_TOOLS)
        self.login = Login("fan@example.com", "secret", "test")
        patcher = mock.patch.dict(os.environ, {"FAKE_JAVIT_MODE": "ok"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_video(self, cid, sources, label="4k", vr=False, **options):
        """Process `cid` as if DMM offered one link per file in `sources`."""
        choice = links.Choice("premium", label, [f"https://www.dmm.co.jp/x/part={n}/" for n in range(1, len(sources) + 1)], vr=vr)
        by_url = dict(zip(choice.urls, sources))

        def fake_download(session, url, target, label, attempts=5, progress=None, retry_delay=10.0, **kwargs):
            return copy_to(by_url[url])(session, url, target, label, attempts, progress, retry_delay)

        opts = pipeline.Options(out_dir=self.out, retry_delay=0, **options)
        with mock.patch.object(links, "find", return_value=choice), \
                mock.patch.object(download, "download_part", side_effect=fake_download) as dl:
            result = pipeline.process(cid, mock.Mock(), self.login, self.tools, opts, Quiet())
        return result, dl

    def chapters(self, path):
        out = subprocess.run([str(MEDIA_TOOLS.ffprobe), "-v", "error", "-show_chapters", "-show_format", "-of", "json", str(path)],
                             capture_output=True, text=True).stdout
        return json.loads(out)

    def test_h264_parts_are_joined_and_named_by_content_id(self):
        parts = [make_clip(self.src / f"1abc000102d_v1_drm_a_4k{n}.dcv", "1abc00010", n) for n in (1, 2, 3)]
        result, _ = self.run_video("1abc00010", parts)
        self.assertTrue(result.ok, result.message)
        self.assertEqual([p.name for p in self.out.iterdir()], ["1abc00010.mkv"])
        info = self.chapters(self.out / "1abc00010.mkv")
        self.assertEqual(len(info["chapters"]), 3)
        self.assertAlmostEqual(float(info["format"]["duration"]), 6.0, delta=0.5)

    def test_single_file_takes_dmms_id(self):
        clip = make_clip(self.src / "abc00123hhb.dcv", "abc00123", None)
        result, _ = self.run_video("abc123", [clip])  # asked with another id, DMM's file says abc00123
        self.assertTrue(result.ok, result.message)
        self.assertEqual([p.name for p in self.out.iterdir() if p.name != pipeline.WORK_FOLDER], ["abc00123.mkv"])
        self.assertTrue(any("abc00123" in w for w in result.warnings))

        # The work folder is gone, DMM's id for abc123 stays: the next run knows the video is saved.
        self.assertEqual([p.name for p in (self.out / pipeline.WORK_FOLDER).iterdir()], [pipeline.IDS_FILE])
        self.assertEqual(pipeline.known_dmm_id(self.out, "abc123"), "abc00123")
        again, dl = self.run_video("abc123", [clip])
        self.assertTrue(again.skipped, again.message)
        self.assertEqual(again.files, [self.out / "abc00123.mkv"])
        self.assertEqual(dl.call_count, 0)
        self.assertIsNone(pipeline.known_dmm_id(self.out, "abc0123"))  # exactly the id typed: no padding guessed

    def test_without_dmms_id_in_the_file_nothing_is_remembered(self):
        clip = make_clip(self.src / "abc123.dcv", None, None)
        result, _ = self.run_video("abc123", [clip])
        self.assertTrue(result.ok, result.message)
        self.assertEqual([p.name for p in self.out.iterdir()], ["abc123.mkv"])  # no work folder left
        self.assertEqual(pipeline.known_dmm_ids(self.out), {})

    def test_parts_dont_count_as_saved_while_a_run_of_the_video_is_unfinished(self):
        # A run stopped while saving the parts (here a file took part 2's name meanwhile): part 1 alone is there.
        parts = [make_clip(self.src / f"p{n}.dcv", "abc00123", n) for n in (1, 2)]
        place = pipeline._place

        def second_taken(source, target, replace=False):
            if target.name == "abc00123-pt2.mkv":
                raise FileExistsError(errno.EEXIST, "already exists", str(target))
            return place(source, target, replace)

        with mock.patch.object(pipeline, "_place", side_effect=second_taken):
            first, _ = self.run_video("abc00123", parts, merge=False)
        self.assertFalse(first.ok)
        self.assertTrue((self.out / "abc00123-pt1.mkv").exists())
        self.assertEqual(pipeline.saved_files(self.out, ["abc00123"]), [])  # the run isn't finished
        self.assertEqual(pipeline.saved_files(self.out, ["abc00123"], asked="xyz00456"),
                         [self.out / "abc00123-pt1.mkv"])  # another video's run says nothing about these files

        again, _ = self.run_video("abc00123", parts, merge=False)
        self.assertFalse(again.skipped)  # not "already downloaded" with one part of two
        self.assertFalse(again.ok)
        self.assertIn("abc00123-pt1.mkv is already there", again.message)
        replaced, _ = self.run_video("abc00123", parts, merge=False, overwrite=True)
        self.assertTrue(replaced.ok, replaced.message)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["abc00123-pt1.mkv", "abc00123-pt2.mkv"])
        self.assertEqual(pipeline.saved_files(self.out, ["abc00123"]),
                         [self.out / "abc00123-pt1.mkv", self.out / "abc00123-pt2.mkv"])

        # A single file is the whole video, whatever a run left behind.
        (self.out / "xyz00456.mkv").write_bytes(b"x")
        (self.out / pipeline.WORK_FOLDER / "xyz00456").mkdir(parents=True)
        (self.out / pipeline.WORK_FOLDER / "xyz00456" / "state.json").write_text("{}")
        self.assertEqual(pipeline.saved_files(self.out, ["xyz00456"]), [self.out / "xyz00456.mkv"])

    def test_joining_never_replaces_a_file_without_overwrite(self):
        # The output was checked before the parts were joined; a file that appeared meanwhile isn't replaced.
        work = self.out / pipeline.WORK_FOLDER / "abc00123"
        work.mkdir(parents=True)
        parts = [make_clip(work / f"part{n}.mkv", "abc00123", n) for n in (1, 2)]
        (self.out / "abc00123.mkv").write_bytes(b"a file that appeared after the check")
        tracks = [pipeline.mediainfo.read(p, MEDIAINFO) for p in parts]
        targets = []
        merge = pipeline.merging.merge

        def watched(item, *args, **kwargs):
            targets.append(item.target)
            return merge(item, *args, **kwargs)

        with mock.patch.object(pipeline.merging, "merge", side_effect=watched):
            with self.assertRaises(FileExistsError):
                pipeline._multi_part("abc00123", parts, tracks, self.tools, pipeline.Options(out_dir=self.out), Quiet(),
                                     [])
        self.assertEqual(targets, [work / "joined" / "abc00123.mkv"])  # FFmpeg writes in the video's own work folder
        self.assertEqual((self.out / "abc00123.mkv").read_bytes(), b"a file that appeared after the check")
        self.assertTrue((work / "joined" / "abc00123.mkv").exists())  # kept for a look

        files = pipeline._multi_part("abc00123", parts, tracks, self.tools,
                                     pipeline.Options(out_dir=self.out, overwrite=True), Quiet(), [])
        self.assertEqual(files, [self.out / "abc00123.mkv"])
        self.assertAlmostEqual(float(self.chapters(files[0])["format"]["duration"]), 4.0, delta=0.5)

    def test_2d_videos_fail_without_their_programs(self):
        missing = "jav-it was not found. Put jav-it (and DECRYPT.MOD, for VR also VR.MOD) next to dmm-downloader."
        tools = pipeline.Tools(Path("jav-it"), Path("mediainfo"), no_vr="jav-it was not found", no_2d=missing)
        choice = links.Choice("premium", "4k", ["https://www.dmm.co.jp/x/"])
        with mock.patch.object(download, "download_part") as dl:
            result = pipeline.process("abc00123", mock.Mock(), self.login, tools, pipeline.Options(out_dir=self.out),
                                      Quiet(), choice=choice)
        self.assertFalse(result.ok)
        self.assertEqual(result.message, missing)
        dl.assert_not_called()
        self.assertFalse(self.out.exists())  # nothing started

        part = self.src / "p1.wsdcf"
        part.write_bytes(os.urandom(1000))
        vr = links.Choice("library", "8k", ["https://www.dmm.co.jp/x/part=1/"], vr=True)
        with mock.patch.object(download, "download_part", side_effect=copy_to(part)):
            result = pipeline.process("sivr00123", mock.Mock(), self.login, tools, pipeline.Options(out_dir=self.out),
                                      Quiet(), choice=vr)
        self.assertTrue(result.ok, result.message)  # VR is saved encrypted: no program needed
        self.assertTrue(result.encrypted)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["sivr00123.wsdcf"])

    def test_a_file_with_dmms_id_is_never_replaced(self):
        # Asked as abc123, DMM's file says abc00123, and abc00123.mkv is already there (e.g. another quality).
        self.out.mkdir()
        (self.out / "abc00123.mkv").write_bytes(b"the earlier file")
        clip = make_clip(self.src / "abc00123hhb.dcv", "abc00123", None)
        result, _ = self.run_video("abc123", [clip])
        self.assertFalse(result.ok)
        work = self.out / pipeline.WORK_FOLDER / "abc123"
        self.assertIn(str(self.out / "abc00123.mkv"), result.message)
        self.assertIn(str(work), result.message)
        self.assertEqual((self.out / "abc00123.mkv").read_bytes(), b"the earlier file")
        self.assertTrue((work / "part1.mkv").exists())  # the new file stays in the work folder

        # DMM's id is remembered: the next run finds the video already saved, and says where the new one is.
        again, dl = self.run_video("abc123", [clip])
        self.assertTrue(again.skipped)
        self.assertEqual(dl.call_count, 0)
        self.assertIn(str(work), " ".join(again.warnings))
        self.assertEqual(pipeline.known_dmm_id(self.out, "abc123"), "abc00123")

        # "Download again videos already saved" replaces it, with the part already decrypted.
        replaced, dl = self.run_video("abc123", [clip], overwrite=True)
        self.assertTrue(replaced.ok, replaced.message)
        self.assertEqual(dl.call_count, 0)
        self.assertNotEqual((self.out / "abc00123.mkv").read_bytes(), b"the earlier file")

    def test_already_saved_under_the_premium_pages_id(self):
        self.out.mkdir()
        (self.out / "abc00123.mkv").write_bytes(b"x")
        choice = links.Choice("premium", "4k", ["https://www.dmm.co.jp/x/"], page_cid="abc00123")
        with mock.patch.object(download, "download_part") as dl:
            result = pipeline.process("abc123", mock.Mock(), self.login, self.tools, pipeline.Options(out_dir=self.out),
                                      Quiet(), choice=choice)
        self.assertTrue(result.skipped)
        dl.assert_not_called()

    def test_a_vr_file_is_never_replaced(self):
        self.out.mkdir()
        (self.out / "sivr00123.wsdcf").write_bytes(b"earlier")
        part = self.src / "p1.wsdcf"
        part.write_bytes(os.urandom(1000))
        choice = links.Choice("library", "8k", ["https://www.dmm.co.jp/x/part=1/"], vr=True)
        work = self.out / pipeline.WORK_FOLDER / "sivr00123"
        with mock.patch.object(download, "download_part", side_effect=copy_to(part)):
            # As if the file appeared while downloading (the check at the start didn't see it).
            with mock.patch.object(pipeline, "saved_files", return_value=[]):
                result = pipeline.process("sivr00123", mock.Mock(), self.login, self.tools,
                                          pipeline.Options(out_dir=self.out), Quiet(), choice=choice)
        self.assertFalse(result.ok)
        self.assertIn("already there", result.message)
        self.assertEqual((self.out / "sivr00123.wsdcf").read_bytes(), b"earlier")
        self.assertTrue((work / "part1.wsdcf").exists())

    def test_ffprobe_failing_keeps_the_parts(self):
        parts = [make_clip(self.src / f"p{n}.dcv", "abc00123", n) for n in (1, 2)]
        with mock.patch.object(pipeline.probing, "probe", side_effect=pipeline.probing.ProbeError("unreadable")):
            result, _ = self.run_video("abc00123", parts)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["abc00123-pt1.mkv", "abc00123-pt2.mkv"])
        self.assertIn("ffprobe could not read them", " ".join(result.warnings))

    def test_a_dmm_error_fails_the_video_only(self):
        with mock.patch.object(links, "find", side_effect=pipeline.DmmError("Premium page of abc00123: DMM answered HTTP 500")):
            result = pipeline.process("abc00123", mock.Mock(), self.login, self.tools,
                                      pipeline.Options(out_dir=self.out, source="premium"), Quiet())
        self.assertFalse(result.ok)
        self.assertIn("HTTP 500", result.message)

    def test_kept_encrypted_downloads_go_next_to_the_result(self):
        parts = [make_clip(self.src / f"p{n}.dcv", "abc00123", n) for n in (1, 2)]
        result, _ = self.run_video("abc00123", parts, keep_dcv=True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["abc00123-pt1.dcv", "abc00123-pt2.dcv", "abc00123.mkv"])  # no hidden work folder left
        self.assertEqual((self.out / "abc00123-pt2.dcv").read_bytes(), parts[1].read_bytes())

        single = self.dir / "single"
        clip = make_clip(self.src / "s.dcv", "xyz00456", None)
        opts = pipeline.Options(out_dir=single, retry_delay=0, keep_dcv=True)
        choice = links.Choice("premium", "4k", ["https://www.dmm.co.jp/x/"])
        with mock.patch.object(download, "download_part", side_effect=copy_to(clip)):
            result = pipeline.process("xyz00456", mock.Mock(), self.login, self.tools, opts, Quiet(), choice=choice)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(sorted(p.name for p in single.iterdir()), ["xyz00456.dcv", "xyz00456.mkv"])

    def test_a_kept_encrypted_download_is_never_replaced(self):
        self.out.mkdir()
        (self.out / "abc00123.dcv").write_bytes(b"earlier")
        clip = make_clip(self.src / "p1.dcv", "abc00123", None)
        result, _ = self.run_video("abc00123", [clip], keep_dcv=True)
        self.assertFalse(result.ok)
        self.assertIn("abc00123.dcv is already there", result.message)
        self.assertEqual((self.out / "abc00123.dcv").read_bytes(), b"earlier")
        self.assertFalse((self.out / "abc00123.mkv").exists())

    def test_decrypting_counts_the_downloads_running(self):
        clip = make_clip(self.src / "p1.dcv", "abc00123", None)
        # The disk's free space is faked before the other download claims its share: the test machine may have less.
        with mock.patch.object(fetch.shutil, "disk_usage", return_value=mock.Mock(free=50e9 + fetch.SPACE_MARGIN + 1000)), \
                fetch.SPACE.claim(self.dir, lambda: int(50e9)):
            result, _ = self.run_video("abc00123", [clip])
        self.assertFalse(result.ok)
        self.assertIn("not enough free space to decrypt", result.message)
        self.assertIn("others running 50.0 GB more", result.message)

    def test_no_merge_keeps_parts(self):
        parts = [make_clip(self.src / f"p{n}.dcv", "abc00123", n) for n in (1, 2)]
        result, _ = self.run_video("abc00123", parts, merge=False)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["abc00123-pt1.mkv", "abc00123-pt2.mkv"])

    def test_vp9_parts_stay_separate(self):
        encoders = subprocess.run([str(MEDIA_TOOLS.ffmpeg), "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        if "libvpx-vp9" not in encoders:
            self.skipTest("no VP9 encoder")
        parts = [make_clip(self.src / f"p{n}.dcv", "abc00123", n, seconds=1, codec="libvpx-vp9") for n in (1, 2)]
        result, _ = self.run_video("abc00123", parts)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["abc00123-pt1.mkv", "abc00123-pt2.mkv"])
        self.assertIn("VP9", " ".join(result.warnings))

    def test_wrong_part_numbers_are_refused(self):
        parts = [make_clip(self.src / f"p{n}.dcv", "abc00123", n) for n in (1, 1)]
        result, _ = self.run_video("abc00123", parts)
        self.assertFalse(result.ok)
        self.assertIn("part numbers", result.message)

    def test_parts_of_different_videos_are_refused(self):
        parts = [make_clip(self.src / "a.dcv", "abc00123", 1), make_clip(self.src / "b.dcv", "abc00124", 2)]
        result, _ = self.run_video("abc00123", parts)
        self.assertFalse(result.ok)
        self.assertIn("different videos", result.message)

    def test_a_second_run_continues(self):
        parts = [make_clip(self.src / f"p{n}.dcv", "abc00123", n) for n in (1, 2)]
        original = copy_to(parts[0])

        def fail_part_two(session, url, target, label, attempts=5, progress=None, retry_delay=10.0, **kwargs):
            if "part=2" in url:
                raise download.DownloadError("connection lost")
            return original(session, url, target, label)

        choice = links.Choice("premium", "4k", ["https://www.dmm.co.jp/x/part=1/", "https://www.dmm.co.jp/x/part=2/"])
        opts = pipeline.Options(out_dir=self.out, retry_delay=0)
        with mock.patch.object(links, "find", return_value=choice), mock.patch.object(download, "download_part", side_effect=fail_part_two):
            first = pipeline.process("abc00123", mock.Mock(), self.login, self.tools, opts, Quiet())
        self.assertFalse(first.ok)
        self.assertIn("connection lost", first.message)

        result, dl = self.run_video("abc00123", parts)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(dl.call_count, 1)  # only part 2; part 1 was already decrypted
        self.assertEqual([p.name for p in self.out.iterdir()], ["abc00123.mkv"])
        calls = (self.tools.javit.parent / "calls.log").read_text().splitlines()
        self.assertEqual(len(calls), 2)  # each part decrypted once

    def test_a_bad_decrypted_file_is_downloaded_again_once(self):
        # jav-it's output fails the checks: the encrypted part is downloaded again (README: "fetched again").
        clip = make_clip(self.src / "p1.dcv", "abc00123", None)
        with mock.patch.dict(os.environ, {"FAKE_JAVIT_MODE": "small-once"}):
            result, dl = self.run_video("abc00123", [clip])
        self.assertTrue(result.ok, result.message)
        self.assertEqual(dl.call_count, 2)
        self.assertEqual([p.name for p in self.out.iterdir()], ["abc00123.mkv"])

    def test_a_decrypted_file_that_stays_bad_fails_after_one_new_download(self):
        clip = make_clip(self.src / "p1.dcv", "abc00123", None)
        steps = Quiet()
        choice = links.Choice("premium", "4k", ["https://www.dmm.co.jp/x/part=1/"])
        opts = pipeline.Options(out_dir=self.out, retry_delay=0)
        with mock.patch.dict(os.environ, {"FAKE_JAVIT_MODE": "small"}), \
                mock.patch.object(links, "find", return_value=choice), \
                mock.patch.object(download, "download_part", side_effect=copy_to(clip)) as dl:
            result = pipeline.process("abc00123", mock.Mock(), self.login, self.tools, opts, steps)
        self.assertFalse(result.ok)
        self.assertEqual(dl.call_count, 2)
        self.assertIn("too small", result.message)
        self.assertIn("also after downloading it again", result.message)
        self.assertTrue(any("downloading it again" in s for s in steps.steps))

    def test_premium_refused_falls_back_to_the_library(self):
        # "auto": Premium lists the video, but its link isn't for this account.
        clip = make_clip(self.src / "p1.dcv", "abc00123", None)
        premium = links.Choice("premium", "4k", ["https://www.dmm.co.jp/monthly/premium/x/"])
        library = links.Choice("library", "4k", ["https://www.dmm.co.jp/digital/x/"])
        copy = copy_to(clip)

        def fake_download(session, url, target, label, attempts=5, progress=None, retry_delay=10.0, **kwargs):
            if "premium" in url:
                raise download.NotAllowed(f"{label}: DMM didn't give the download (HTTP 403)")
            return copy(session, url, target, label)

        for source, ok in (("auto", True), ("premium", False)):
            steps = Quiet()
            opts = pipeline.Options(out_dir=self.dir / source, retry_delay=0, source=source)
            with mock.patch.object(links, "find", return_value=library) as find, \
                    mock.patch.object(download, "download_part", side_effect=fake_download):
                result = pipeline.process("abc00123", mock.Mock(), self.login, self.tools, opts, steps, choice=premium)
            self.assertEqual(result.ok, ok, result.message)
            if ok:
                find.assert_called_once()
                self.assertEqual(find.call_args.args[2], "auto")  # the sources after Premium: VR Channel, library
                self.assertEqual(find.call_args.kwargs["skip"], ("premium",))
                self.assertTrue(result.message.startswith("library"), result.message)
                self.assertTrue(any("trying your library" in s for s in steps.steps))
                self.assertTrue((self.dir / source / "abc00123.mkv").exists())
            else:
                find.assert_not_called()
                self.assertIn("HTTP 403", result.message)

    def test_vr_without_vr_mod_is_saved_encrypted(self):
        parts = [self.src / f"p{n}.wsdcf" for n in (1, 2)]
        for n, part in enumerate(parts, 1):
            part.write_bytes(os.urandom(1000 + n))  # encrypted: nothing MediaInfo or jav-it gets to see
        self.assertIn("VR.MOD is not next to jav-it", self.tools.no_vr)  # the fake jav-it here has DECRYPT.MOD only
        result, dl = self.run_video("sivr00123", parts, label="8k", vr=True)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.encrypted)
        self.assertEqual(dl.call_count, 2)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["sivr00123-pt1.wsdcf", "sivr00123-pt2.wsdcf"])
        self.assertEqual((self.out / "sivr00123-pt2.wsdcf").read_bytes(), parts[1].read_bytes())
        self.assertFalse((self.tools.javit.parent / "calls.log").exists())  # jav-it never ran
        self.assertIn("saved encrypted (.wsdcf), not decrypted: VR.MOD is not next to jav-it", " ".join(result.warnings))

        again, dl = self.run_video("sivr00123", parts, label="8k", vr=True)
        self.assertTrue(again.skipped)  # without VR.MOD, the encrypted files are the result
        self.assertEqual(dl.call_count, 0)

    def test_vr_parts_take_dmms_part_numbers(self):
        parts = [self.src / f"p{n}.wsdcf" for n in (1, 3)]
        for part in parts:
            part.write_bytes(os.urandom(1000))
        choice = links.Choice("library", "8k", ["https://www.dmm.co.jp/x/part=1/", "https://www.dmm.co.jp/x/part=3/"], vr=True)
        by_url = dict(zip(choice.urls, parts))

        def fake_download(session, url, target, label, attempts=5, progress=None, retry_delay=10.0, **kwargs):
            self.assertTrue(kwargs.get("binary_only"))  # VR parts: a page sent instead of the file is refused
            return copy_to(by_url[url])(session, url, target, label)

        with mock.patch.object(download, "download_part", side_effect=fake_download):
            result = pipeline.process("sivr00123", mock.Mock(), self.login, self.tools, pipeline.Options(out_dir=self.out),
                                      Quiet(), choice=choice)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["sivr00123-pt1.wsdcf", "sivr00123-pt3.wsdcf"])
        self.assertEqual((self.out / "sivr00123-pt3.wsdcf").read_bytes(), parts[1].read_bytes())
        self.assertIn("parts 1, 3", " ".join(result.warnings))

    def test_already_downloaded_is_skipped(self):
        self.out.mkdir()
        (self.out / "abc00123.mkv").write_bytes(b"x")
        result, dl = self.run_video("abc00123", [])
        self.assertTrue(result.skipped)
        self.assertEqual(dl.call_count, 0)


@unittest.skipUnless(MEDIA_TOOLS and MEDIAINFO, "ffmpeg/mediainfo not installed")
class VrDecryptTest(unittest.TestCase):
    """VR videos with jav-it and VR.MOD: decrypted ("dmm-vr") into .mp4 like 2D parts. The fake jav-it copies its
    input, so the "encrypted" parts here are small MP4 clips."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dmm vr "))
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

    def clip(self, name, part=None, seconds=2):
        return make_clip(self.src / name, "sivr00123", part, seconds=seconds, container="mp4")

    def run_vr(self, sources, parts=None, out=None, **options):
        """Process sivr00123 as if DMM offered one VR link per file in `sources` (DMM's part numbers `parts`)."""
        parts = parts or list(range(1, len(sources) + 1))
        choice = links.Choice("library", "8k", [f"https://www.dmm.co.jp/x/part={n}/" for n in parts], vr=True)
        by_url = dict(zip(choice.urls, sources))
        steps = Quiet()

        def fake_download(session, url, target, label, attempts=5, progress=None, retry_delay=10.0, **kwargs):
            self.assertTrue(kwargs.get("binary_only"))
            return copy_to(by_url[url])(session, url, target, label)

        opts = pipeline.Options(out_dir=out or self.out, retry_delay=0, **options)
        with mock.patch.object(download, "download_part", side_effect=fake_download) as dl:
            result = pipeline.process("sivr00123", mock.Mock(), self.login, self.tools, opts, steps, choice=choice)
        return result, dl, steps

    def names(self, folder=None):
        return sorted(p.name for p in (folder or self.out).iterdir())

    def kinds(self):
        return (self.javit.parent / "kinds.log").read_text().split()

    def calls(self):
        path = self.javit.parent / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_vr_mod_next_to_jav_it_turns_decrypting_on(self):
        self.assertEqual(self.tools.no_vr, "")
        self.assertIn("MediaInfo was not found", pipeline.Tools(self.javit, None).no_vr)
        self.assertEqual(pipeline.Tools(self.javit, MEDIAINFO, no_vr="told").no_vr, "told")
        (self.javit.parent / "VR.MOD").unlink()
        self.assertIn("VR.MOD is not next to jav-it", pipeline.Tools(self.javit, MEDIAINFO).no_vr)
        self.assertEqual(pipeline.decrypting.vr_problem(None, MEDIAINFO), "jav-it was not found")

    def test_one_part_is_decrypted_into_an_mp4(self):
        result, dl, _ = self.run_vr([self.clip("p1.wsdcf")])
        self.assertTrue(result.ok, result.message)
        self.assertFalse(result.encrypted)
        self.assertEqual(dl.call_count, 1)
        self.assertEqual(self.names(), ["sivr00123.mp4"])  # no work folder, no encrypted file left
        self.assertEqual(self.kinds(), ["dmm-vr"])
        self.assertTrue(self.calls()[0].endswith(f"user=fan@example.com tmp={self.out / pipeline.WORK_FOLDER / 'sivr00123' / 'tmp'}"))
        self.assertEqual(result.files, [self.out / "sivr00123.mp4"])

    def test_vr_parts_are_never_joined(self):
        parts = [self.clip(f"p{n}.wsdcf", n) for n in (1, 2)]
        self.assertIsNotNone(self.tools.merge)  # FFmpeg found and "Join parts" on: still two files
        result, _, steps = self.run_vr(parts, merge=True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.names(), ["sivr00123-pt1.mp4", "sivr00123-pt2.mp4"])
        self.assertEqual(result.files, [self.out / "sivr00123-pt1.mp4", self.out / "sivr00123-pt2.mp4"])
        self.assertFalse(any("joining" in s for s in steps.steps))
        self.assertEqual((self.out / "sivr00123-pt2.mp4").read_bytes(), parts[1].read_bytes())  # untouched by FFmpeg

    def test_parts_with_a_gap_keep_dmms_numbers(self):
        parts = [self.clip(f"p{n}.wsdcf", n) for n in (1, 3)]
        result, _, _ = self.run_vr(parts, parts=[1, 3])
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.names(), ["sivr00123-pt1.mp4", "sivr00123-pt3.mp4"])  # never joined over a gap
        self.assertIn("parts 1, 3", " ".join(result.warnings))

    def test_only_the_decrypted_file_is_checked_when_mediainfo_cant_read_the_download(self):
        encrypted = self.src / "p1.wsdcf"
        encrypted.write_bytes(os.urandom(4000))
        with mock.patch.dict(os.environ, {"FAKE_JAVIT_RESULT": str(self.clip("result.mp4"))}):
            result, _, _ = self.run_vr([encrypted])
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.names(), ["sivr00123.mp4"])

    def test_license_needed_fails_at_once_and_keeps_the_download(self):
        with mock.patch.dict(os.environ, {"FAKE_JAVIT_MODE": "vr-license"}):
            result, dl, _ = self.run_vr([self.clip("p1.wsdcf")])
        self.assertFalse(result.ok)
        self.assertIn("DMM VR Player's license", result.message)
        self.assertIn("encrypted parts are kept", result.message)
        self.assertEqual((dl.call_count, len(self.calls())), (1, 1))  # not tried again, not downloaded again
        self.assertTrue((self.out / pipeline.WORK_FOLDER / "sivr00123" / "part1.wsdcf").exists())
        self.assertFalse((self.out / "sivr00123.mp4").exists())

        result, dl, _ = self.run_vr([self.clip("p1.wsdcf")])  # jav-it updated: the kept download is decrypted
        self.assertTrue(result.ok, result.message)
        self.assertEqual(dl.call_count, 0)

    def test_a_hanging_jav_it_is_stopped(self):
        self.assertEqual(pipeline.decrypting.vr_timeout(5_000_000 * 60), 360)  # 5 min + 1 s per 5 MB
        start = time.monotonic()
        with mock.patch.dict(os.environ, {"FAKE_JAVIT_MODE": "hang"}), \
                mock.patch.object(pipeline.decrypting, "VR_TIMEOUT_BASE", 2.0):
            result, _, _ = self.run_vr([self.clip("p1.wsdcf")])
        self.assertLess(time.monotonic() - start, 60)
        self.assertFalse(result.ok)
        self.assertIn("took longer than", result.message)
        self.assertEqual(len(self.calls()), 1)

    def test_a_bad_result_is_downloaded_again_once(self):
        with mock.patch.dict(os.environ, {"FAKE_JAVIT_MODE": "small-once"}):
            result, dl, steps = self.run_vr([self.clip("p1.wsdcf")])
        self.assertTrue(result.ok, result.message)
        self.assertEqual(dl.call_count, 2)
        self.assertTrue(any("downloading it again" in s for s in steps.steps))

    def test_only_one_new_download_per_vr_video(self):
        # Part 1 decrypts badly, is downloaded again and passes; part 2 decrypts badly too: no third download.
        parts = [self.clip(f"p{n}.wsdcf", n) for n in (1, 2)]
        with mock.patch.dict(os.environ, {"FAKE_JAVIT_MODE": "small-calls", "FAKE_JAVIT_SMALL": "1,3"}):
            result, dl, _ = self.run_vr(parts)
        self.assertFalse(result.ok)
        self.assertEqual(dl.call_count, 3)
        self.assertIn("Not downloaded again", result.message)
        self.assertIn("too small", result.message)

    def test_kept_encrypted_downloads_go_next_to_the_result(self):
        parts = [self.clip(f"p{n}.wsdcf", n) for n in (1, 2)]
        result, _, _ = self.run_vr(parts, keep_dcv=True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.names(), ["sivr00123-pt1.mp4", "sivr00123-pt1.wsdcf", "sivr00123-pt2.mp4",
                                        "sivr00123-pt2.wsdcf"])
        self.assertEqual((self.out / "sivr00123-pt2.wsdcf").read_bytes(), parts[1].read_bytes())

    def saved(self, *names_and_parts):
        self.out.mkdir(exist_ok=True)
        files = {}
        for name, part in names_and_parts:
            files[name] = make_clip(self.out / name, "sivr00123", part, container="mp4")
        return {name: path.read_bytes() for name, path in files.items()}

    def test_saved_encrypted_files_are_decrypted_instead_of_downloaded(self):
        self.saved(("sivr00123-pt1.wsdcf", 1), ("sivr00123-pt2.wsdcf", 2))
        self.assertEqual(pipeline.saved_files(self.out, ["sivr00123"], wsdcf=False), [])  # not "already saved"
        result, dl, steps = self.run_vr([self.src / "never1", self.src / "never2"])
        self.assertTrue(result.ok, result.message)
        self.assertEqual(dl.call_count, 0)
        self.assertEqual(self.names(), ["sivr00123-pt1.mp4", "sivr00123-pt2.mp4"])  # "Keep the encrypted ..." off: removed
        self.assertIn("saved earlier", steps.steps[1])
        self.assertIn("sivr00123-pt1.wsdcf, sivr00123-pt2.wsdcf", " ".join(result.warnings))

    def test_saved_encrypted_files_stay_when_kept(self):
        before = self.saved(("sivr00123.wsdcf", None))
        result, dl, _ = self.run_vr([self.src / "never"], keep_dcv=True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(dl.call_count, 0)
        self.assertEqual(self.names(), ["sivr00123.mp4", "sivr00123.wsdcf"])
        self.assertEqual((self.out / "sivr00123.wsdcf").read_bytes(), before["sivr00123.wsdcf"])

    def test_saved_files_are_used_only_when_they_are_this_qualitys_parts(self):
        urls = [f"https://www.dmm.co.jp/x/part={n}/" for n in (1, 2)]
        self.saved(("sivr00123-pt1.wsdcf", 1), ("sivr00123-pt2.wsdcf", 2))
        self.assertEqual(pipeline.saved_vr_parts(self.out, "sivr00123", urls),
                         {1: self.out / "sivr00123-pt1.wsdcf", 2: self.out / "sivr00123-pt2.wsdcf"})
        self.assertEqual(pipeline.saved_vr_parts(self.out, "sivr00123", urls[:1]), {})  # a quality in one part
        self.assertEqual(pipeline.saved_vr_parts(self.out, "sivr00123", urls + ["https://www.dmm.co.jp/x/part=3/"]), {})
        (self.out / "sivr00123-pt2.wsdcf").write_bytes(b"")  # an empty file isn't a part
        self.assertEqual(pipeline.saved_vr_parts(self.out, "sivr00123", urls), {})

    def test_saved_parts_of_another_quality_are_never_mixed_in(self):
        # Saved encrypted earlier in a quality with 3 parts; the quality chosen now has 2.
        before = self.saved(("sivr00123-pt1.wsdcf", 1), ("sivr00123-pt2.wsdcf", 2), ("sivr00123-pt3.wsdcf", 3))
        parts = [self.clip(f"p{n}.wsdcf", n, seconds=3) for n in (1, 2)]
        result, dl, _ = self.run_vr(parts)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(dl.call_count, 2)  # both parts of this quality downloaded
        self.assertEqual((self.out / "sivr00123-pt2.mp4").read_bytes(), parts[1].read_bytes())
        for name, data in before.items():  # left as they are, and said so
            self.assertEqual((self.out / name).read_bytes(), data, name)
        self.assertIn("not used, left as they are: sivr00123-pt1.wsdcf, sivr00123-pt2.wsdcf, sivr00123-pt3.wsdcf",
                      " ".join(result.warnings))
        again, dl, _ = self.run_vr(parts)
        self.assertTrue(again.skipped)  # the decrypted video is complete now
        self.assertEqual(dl.call_count, 0)

    def test_a_part_saved_alone_is_not_used(self):
        before = self.saved(("sivr00123-pt2.wsdcf", 2))
        parts = [self.clip(f"p{n}.wsdcf", n, seconds=3) for n in (1, 2)]
        # Keeping this run's encrypted parts would take that file's name: refused before anything is downloaded.
        refused, dl, _ = self.run_vr(parts, keep_dcv=True)
        self.assertFalse(refused.ok)
        self.assertIn("not started", refused.message)
        self.assertIn("sivr00123-pt2.wsdcf is already there", refused.message)
        self.assertEqual(dl.call_count, 0)

        result, dl, _ = self.run_vr(parts)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(dl.call_count, 2)
        self.assertEqual(self.names(), ["sivr00123-pt1.mp4", "sivr00123-pt2.mp4", "sivr00123-pt2.wsdcf"])
        self.assertEqual((self.out / "sivr00123-pt2.wsdcf").read_bytes(), before["sivr00123-pt2.wsdcf"])
        self.assertEqual((self.out / "sivr00123-pt2.mp4").read_bytes(), parts[1].read_bytes())

    def test_a_saved_web_page_means_the_saved_set_isnt_used(self):
        self.saved(("sivr00123-pt1.wsdcf", 1))
        (self.out / "sivr00123-pt2.wsdcf").write_text("<!doctype html><title>expired</title>\n" * 20, encoding="utf-8")
        parts = [self.clip(f"p{n}.wsdcf", n, seconds=3) for n in (1, 2)]
        result, dl, _ = self.run_vr(parts)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(dl.call_count, 2)  # not part 1 from the saved set and part 2 from a new download
        self.assertIn("sivr00123-pt2.wsdcf: a web page or text", " ".join(result.warnings))
        self.assertEqual((self.out / "sivr00123-pt1.mp4").read_bytes(), parts[0].read_bytes())

    def test_a_saved_file_that_decrypts_badly_is_replaced_by_a_new_download(self):
        self.saved(("sivr00123.wsdcf", None))
        fresh = self.clip("p1.wsdcf", seconds=3)
        with mock.patch.dict(os.environ, {"FAKE_JAVIT_MODE": "small-once"}):
            result, dl, _ = self.run_vr([fresh], keep_dcv=True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(dl.call_count, 1)
        self.assertEqual((self.out / "sivr00123.wsdcf").read_bytes(), fresh.read_bytes())

    def test_without_vr_mod_saved_files_are_already_saved(self):
        self.saved(("sivr00123.wsdcf", None))
        (self.javit.parent / "VR.MOD").unlink()
        self.tools = pipeline.Tools(self.javit, MEDIAINFO, MEDIA_TOOLS)
        result, dl, _ = self.run_vr([self.src / "never"])
        self.assertTrue(result.skipped)
        self.assertEqual(dl.call_count, 0)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
