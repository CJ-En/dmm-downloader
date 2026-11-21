"""One run per video: the work folder's lock (dmm_downloader/worklock.py)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from dmm_downloader import download, links, pipeline, worklock
from dmm_downloader.merging import tools


class WorkLockTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="dmm lock "))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.lock_file = self.work / worklock.LOCK_NAME

    def write_lock(self, pid, start):
        self.lock_file.write_text(json.dumps({"pid": pid, "start": start}), encoding="utf-8")

    def ended_process(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=tools.NO_WINDOW)
        process.wait()
        return process.pid

    def test_this_program_can_hold_a_video_once(self):
        with worklock.acquire(self.work, "abc00123"):
            self.assertTrue(self.lock_file.exists())
            with self.assertRaises(worklock.Busy) as caught:
                worklock.acquire(self.work, "abc00123")
            self.assertIn("another row", str(caught.exception))
        self.assertFalse(self.lock_file.exists())
        worklock.acquire(self.work, "abc00123").release()  # free again

    def test_another_running_copy_keeps_its_lock(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], creationflags=tools.NO_WINDOW)
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.assertIsNotNone(worklock.process_start(other.pid))
        self.write_lock(other.pid, worklock.process_start(other.pid))
        with self.assertRaises(worklock.Busy) as caught:
            worklock.acquire(self.work, "abc00123")
        self.assertIn(f"process {other.pid}", str(caught.exception))

    def test_a_lock_left_by_an_ended_program_is_taken_over(self):
        self.write_lock(self.ended_process(), 12345)
        with worklock.acquire(self.work, "abc00123"):
            self.assertEqual(json.loads(self.lock_file.read_text())["pid"], os.getpid())
            self.assertEqual(list(self.work.iterdir()), [self.lock_file])  # the old lock is gone

    def test_two_copies_taking_over_the_same_left_lock_dont_both_get_it(self):
        self.write_lock(self.ended_process(), 12345)  # left by a program that ended: both copies see it
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], creationflags=tools.NO_WINDOW)
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        take_over = worklock._take_over

        def other_copy_first(path, seen):
            # Between this copy's look at the lock and its takeover, the other copy took it over and holds it now.
            path.unlink()
            self.write_lock(other.pid, worklock.process_start(other.pid))
            return take_over(path, seen)

        with mock.patch.object(worklock, "_take_over", side_effect=other_copy_first):
            with self.assertRaises(worklock.Busy) as caught:
                worklock.acquire(self.work, "abc00123")
        self.assertIn("another copy", str(caught.exception))
        self.assertEqual(json.loads(self.lock_file.read_text())["pid"], other.pid)  # its lock is put back
        self.assertEqual(list(self.work.iterdir()), [self.lock_file])  # and nothing is left beside it

    def test_a_reused_process_id_is_not_the_lock_holder(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], creationflags=tools.NO_WINDOW)
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        start = worklock.process_start(other.pid)
        if not start:
            self.skipTest("start times can't be read here")
        self.write_lock(other.pid, start + 1)  # the lock's holder had this id, but started at another time
        worklock.acquire(self.work, "abc00123").release()

    def test_an_unreadable_lock_is_waited_for_only_while_new(self):
        self.lock_file.write_text("", encoding="utf-8")  # another copy is writing it right now
        with self.assertRaises(worklock.Busy):
            worklock.acquire(self.work, "abc00123")
        old = time.time() - 3600
        os.utime(self.lock_file, (old, old))  # left half-written by a program that ended
        worklock.acquire(self.work, "abc00123").release()

    def test_a_video_already_running_fails_without_downloading(self):
        out = self.work / "out"
        work = out / pipeline.WORK_FOLDER / "abc00123"
        work.mkdir(parents=True)
        choice = links.Choice("library", "4k", ["https://www.dmm.co.jp/x/"])
        with worklock.acquire(work, "abc00123"), mock.patch.object(download, "download_part") as dl:
            result = pipeline.process("abc00123", mock.Mock(), None, pipeline.Tools(Path("j"), Path("m")),
                                      pipeline.Options(out_dir=out), pipeline.Report(), choice=choice)
        self.assertFalse(result.ok)
        self.assertIn("already being downloaded", result.message)
        dl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
