"""The settings file: saves from several requests at once, and a file Windows keeps busy for a moment."""

import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from dmm_downloader import config


class SaveTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dmm config "))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "config.ini"
        self.path.write_text(config.TEMPLATE, encoding="utf-8")

    def leftovers(self):
        return sorted(p.name for p in self.dir.iterdir() if p.name != "config.ini")

    def test_saves_at_the_same_time_all_succeed(self):
        # The page's server answers requests in parallel: two settings changed quickly were saved at the same time,
        # both through "config.tmp", and one failed with "[WinError 5] Access is denied" (its value not saved).
        settings = config.Settings()
        errors, start = [], threading.Barrier(8)

        def change(number):
            try:
                start.wait()
                for turn in range(25):
                    settings.parallel = 1 + (number + turn) % 8
                    config.save(settings, self.path)
            except Exception as exc:  # noqa: BLE001 (any failure counts)
                errors.append(exc)

        threads = [threading.Thread(target=change, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(config.load(self.path).parallel, settings.parallel)  # the last values, whoever saved last
        self.assertEqual(self.leftovers(), [])
        self.assertIn("# Folder for finished videos", self.path.read_text(encoding="utf-8"))  # comments kept

    def test_a_busy_file_is_tried_again(self):
        # Windows: an antivirus or the search indexer may hold config.ini a moment ("Access is denied").
        real, calls = os.replace, []

        def busy_twice(source, target):
            calls.append(target)
            if len(calls) <= 2:
                raise PermissionError(5, "Access is denied")
            real(source, target)

        with mock.patch.object(config.os, "replace", side_effect=busy_twice), mock.patch.object(config.time, "sleep"):
            config.save(config.Settings(retries=9), self.path)
        self.assertEqual(len(calls), 3)
        self.assertEqual(config.load(self.path).retries, 9)
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_save_leaves_no_temporary_file(self):
        with mock.patch.object(config.os, "replace", side_effect=PermissionError(5, "Access is denied")) as replace, \
                mock.patch.object(config.time, "sleep"):
            with self.assertRaises(PermissionError):
                config.save(config.Settings(retries=9), self.path)
        self.assertEqual(replace.call_count, config.REPLACE_TRIES)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(config.load(self.path).retries, 5)  # the file as it was

    def test_each_save_writes_its_own_temporary_file(self):
        names = []
        real = os.replace

        def remember(source, target):
            names.append(Path(source))
            real(source, target)

        with mock.patch.object(config.os, "replace", side_effect=remember):
            config.save(config.Settings(), self.path)
            config.save(config.Settings(), self.path)
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(all(name.parent == self.dir and name.name != "config.tmp" for name in names), names)


if __name__ == "__main__":
    unittest.main()
