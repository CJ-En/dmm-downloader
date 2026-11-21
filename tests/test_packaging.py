"""The Windows build only replaces its own files in the program folder (packaging/windows/install-build.ps1), and
refuses to finish when it names the computer it was made on (check_dist.py)."""

import importlib.util
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "install-build.ps1"
CHECK_DIST = SCRIPT.with_name("check_dist.py")
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


@unittest.skipUnless(POWERSHELL, "PowerShell not installed")
class InstallBuildTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dmm build "))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.build, self.app = self.dir / "build", self.dir / "dist" / "DMM-Downloader"
        files = {"DMM-Downloader.exe": "new", "DMM-Fetch.exe": "new", "_internal/python.dll": "new",
                 "LICENSE.txt": "license", "README.txt": "readme"}
        self.write(self.build, files)

    def write(self, folder, files):
        for name, text in files.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(text, encoding="utf-8")

    def install(self):
        return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
                               "-From", str(self.build), "-To", str(self.app)], capture_output=True, text=True, timeout=120)

    def test_only_the_builds_files_are_replaced(self):
        mine = {"downloads/sone00876.mkv": "video", "downloads/.dmm-downloader/sone00961/part1.dcv": "part",
                "jav-it.exe": "jav-it", "DECRYPT.MOD": "mod", "VR.MOD": "mod", "bin/ffmpeg.exe": "ffmpeg",
                "notes.txt": "mine"}
        self.write(self.app, {"DMM-Downloader.exe": "old", "_internal/old.dll": "old", "README.txt": "old", **mine})
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for name, text in mine.items():
            self.assertEqual((self.app / name).read_text(encoding="utf-8"), text, name)
        self.assertEqual((self.app / "DMM-Downloader.exe").read_text(encoding="utf-8"), "new")
        self.assertEqual((self.app / "README.txt").read_text(encoding="utf-8"), "readme")
        self.assertTrue((self.app / "_internal" / "python.dll").exists())
        self.assertFalse((self.app / "_internal" / "old.dll").exists())  # the old build's own folder is replaced whole

    def test_a_build_with_someone_elses_files_changes_nothing(self):
        self.write(self.build, {"jav-it.exe": "x"})
        self.write(self.app, {"DMM-Downloader.exe": "old", "jav-it.exe": "jav-it"})
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.app / "DMM-Downloader.exe").read_text(encoding="utf-8"), "old")
        self.assertEqual((self.app / "jav-it.exe").read_text(encoding="utf-8"), "jav-it")

    def test_first_build_makes_the_folder(self):
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(sorted(p.name for p in self.app.iterdir()),
                         ["DMM-Downloader.exe", "DMM-Fetch.exe", "LICENSE.txt", "README.txt", "_internal"])

    def test_copies_of_the_programs_under_other_names_are_removed(self):
        mine = {"jav-it.exe": "jav-it", "VR.MOD": "mod", "downloads/sivr00457-pt1.wsdcf": "vr", "notes.txt": "mine"}
        self.write(self.app, {"Old-Downloader.exe": "old", "Old-Fetch.exe": "old", "_internal/old.dll": "old", **mine})
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse((self.app / "Old-Downloader.exe").exists())
        self.assertFalse((self.app / "Old-Fetch.exe").exists())
        for name, text in mine.items():
            self.assertEqual((self.app / name).read_text(encoding="utf-8"), text, name)

    def test_a_folder_without_a_build_keeps_programs_with_those_names(self):
        self.write(self.app, {"Other-Downloader.exe": "someone else's"})  # no _internal: no build in this folder
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual((self.app / "Other-Downloader.exe").read_text(encoding="utf-8"), "someone else's")


class CheckDistTest(unittest.TestCase):
    """The build refuses files that name the computer it was built on, or a word that must never be in it."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dmm dist "))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.build = self.dir / "build"
        self.build.mkdir()
        # A copy of the check: the words file next to the real one (the builder's own) plays no part here.
        self.script = self.dir / "check" / CHECK_DIST.name
        self.script.parent.mkdir()
        shutil.copyfile(CHECK_DIST, self.script)

    def check(self, *words, env=None):
        environment = {k: v for k, v in os.environ.items() if k != "DMM_DOWNLOADER_REFUSE"}
        return subprocess.run([sys.executable, str(self.script), str(self.build), *words], capture_output=True,
                              text=True, timeout=60, env={**environment, **(env or {})})

    def refused(self, data: bytes, *words, env=None) -> bool:
        (self.build / "a.bin").write_bytes(data)
        result = self.check(*words, env=env)
        self.assertIn("NOT CLEAN" if result.returncode else "clean", result.stdout)
        return result.returncode == 1

    def test_clean_build(self):
        (self.build / "a.txt").write_text("nothing personal", encoding="utf-8")
        self.assertEqual(self.check("someuser").returncode, 0)

    def test_given_words_are_refused(self):
        (self.build / "a.txt").write_text(r"C:\Users\SomeUser\x", encoding="utf-8")
        self.assertEqual(self.check("someuser").returncode, 1)

    def test_words_from_the_file_next_to_it_and_from_the_environment(self):
        # Words the source must not spell either: refused-words.txt next to check_dist.py (not in git), or the
        # environment. "frobnix" and the others are made up.
        self.assertFalse(self.refused(b"made by Frobnix Downloader"))
        (self.script.parent / "refused-words.txt").write_text("# never in a build\nfrobnix\n\n", encoding="utf-8")
        for data in (b"made by Frobnix Downloader", b"xFROBNIXx", "x frobnix x".encode("utf-16-le")):
            self.assertTrue(self.refused(data), data)
        self.assertFalse(self.refused(b"nothing personal"))
        (self.script.parent / "refused-words.txt").unlink()
        for data in (b"made by Quuxly", b"zorbo inside"):
            self.assertTrue(self.refused(data, env={"DMM_DOWNLOADER_REFUSE": "quuxly; zorbo"}), data)

    def test_short_words_count_as_whole_words(self):
        for data, refused in ((b"made by Zqx.", True), (b"zqx", True), ("a ZQX b".encode("utf-16-le"), True),
                              (b"azqxb", False), (b"zqx_1", False), ("azqxb".encode("utf-16-le"), False)):
            self.assertEqual(self.refused(data, "zqx"), refused, data)


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ico_images(data: bytes) -> dict[int, bytes]:
    """The images of an .ico by size, checked to be PNGs of that size (what make_icon.py writes)."""
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1), "not an icon file"
    images = {}
    for n in range(count):
        width, height, colors, _, planes, bits, size, offset = struct.unpack("<BBBBHHII", data[6 + 16 * n:22 + 16 * n])
        image = data[offset:offset + size]
        assert len(image) == size and image[:8] == b"\x89PNG\r\n\x1a\n", f"image {n} is not a whole PNG"
        assert image[12:16] == b"IHDR"
        png_width, png_height, depth, color = struct.unpack(">IIBB", image[16:26])
        assert (png_width, png_height, depth, color) == (width or 256, height or 256, 8, 6), f"image {n}: {png_width}"
        assert (colors, planes, bits) == (0, 1, 32)
        images[png_width] = image
    return images


class IconTest(unittest.TestCase):
    """The program's icon (make_icon.py) and the page's mark are the same original download sign."""

    HERE = SCRIPT.parent
    SIZES = [16, 24, 32, 48, 64, 128, 256]

    def test_the_icon_in_the_repository(self):
        self.assertEqual(sorted(ico_images((self.HERE / "dmm-downloader.ico").read_bytes())), self.SIZES)
        self.assertIn('ICON = str(HERE / "dmm-downloader.ico")', (self.HERE / "dmm-downloader.spec").read_text(encoding="utf-8"))

    def test_make_icon_writes_every_size(self):
        make_icon = load(self.HERE / "make_icon.py")
        folder = Path(tempfile.mkdtemp(prefix="dmm icon "))
        self.addCleanup(shutil.rmtree, folder, True)
        images = ico_images(make_icon.make(folder / "test.ico").read_bytes())
        self.assertEqual(sorted(images), self.SIZES)
        # The 16 px image: the tile's round corner is partly see-through, its left edge is the accent color, the
        # arrow head is ink.
        image = images[16]
        start = image.index(b"IDAT")
        length = struct.unpack(">I", image[start - 4:start])[0]
        pixels = zlib.decompress(image[start + 4:start + 4 + length])

        def pixel(x, y):
            at = y * (16 * 4 + 1) + 1 + x * 4  # each row starts with its filter byte
            return pixels[at:at + 4]

        self.assertTrue(0 < pixel(0, 0)[3] < 255)
        self.assertEqual(pixel(0, 8), bytes([*make_icon.TILE, 255]))
        self.assertEqual(pixel(8, 9), bytes([*make_icon.INK, 255]))

    def test_page_favicon_and_icon_draw_the_same_mark(self):
        make_icon = load(self.HERE / "make_icon.py")
        web = SCRIPT.parents[2] / "dmm_downloader" / "web"
        favicon = (web / "favicon.svg").read_text(encoding="utf-8")
        page = (web / "index.html").read_text(encoding="utf-8")
        ink = "".join("M" + "L".join(f"{x:g} {y:g}" for x, y in shape) + "Z" for shape in make_icon.INK_SHAPES)
        self.assertIn(f'd="{ink}" fill="#04121f"', favicon)
        self.assertIn(f'<path class="ink" d="{ink}"/>', page)
        self.assertIn('fill="#5ec4fa"', favicon)


if __name__ == "__main__":
    unittest.main()
