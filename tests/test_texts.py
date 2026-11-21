"""The tool's own words: US spelling, and what the page, the settings file and the READMEs advise."""

import re
import unittest
from pathlib import Path

from dmm_downloader import __version__, config, download, links, web

ROOT = Path(__file__).resolve().parents[1]
PAGE = web.web_dir() / "index.html"
WINDOWS_README = ROOT / "packaging" / "windows" / "README-windows.txt"
TEXT_FILES = {".py", ".js", ".cjs", ".html", ".css", ".md", ".txt", ".toml", ".bat", ".sh", ".ps1", ".spec", ".yml"}
SKIPPED = {".git", ".venv", "venv", "build", "dist", "__pycache__", "node_modules", "fixtures"}
# British spellings (the tool writes US English). Whole words from their start: "licence", "licences", ...
BRITISH = re.compile(r"\b(licence|tunnell|labell|recognis|normalis|organis|analyse|initialis|customis|utilis|colour|"
                     r"behaviour|favour|honour|cancell|travell|centre|catalogue|whilst)", re.IGNORECASE)
# What the tool only matches or quotes from jav-it's output stays as jav-it writes it.
QUOTED = {"dmm_downloader/decrypt.py": ("licence information",)}


def words(text: str) -> str:
    return " ".join(text.split())  # line breaks and indents as single spaces


class SpellingTest(unittest.TestCase):
    def test_us_spelling_everywhere(self):
        found = []
        for path in sorted(ROOT.rglob("*")):
            relative = path.relative_to(ROOT)
            if path.suffix not in TEXT_FILES or SKIPPED & set(relative.parts) or path == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8").replace("aria-labelledby", "")  # HTML's own name
            for quoted in QUOTED.get(relative.as_posix(), ()):
                text = text.replace(quoted, "")
            for number, line in enumerate(text.splitlines(), 1):
                found += [f"{relative.as_posix()}:{number}: {m.group(0)}" for m in BRITISH.finditer(line)]
        self.assertEqual(found, [])


class AdviceTest(unittest.TestCase):
    def test_the_patreon_email_field_says_when_it_is_needed(self):
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn("<span>Patreon email for jav-it <small>(only if it differs from your DMM email)</small></span>", page)
        self.assertNotIn("Email jav-it asks for", page)
        self.assertIn("# Patreon email for jav-it, only if it differs from your DMM email", config.TEMPLATE)
        self.assertIn("your Patreon email for jav-it, only if it differs from your DMM email (`javit_email`)",
                      words((ROOT / "README.md").read_text(encoding="utf-8")))

    def test_the_japan_route_advice_recommends_the_proxy(self):
        # The Japan-route note suggested putting DMM-Downloader on a VPN app in Japan, while the VPN note and the VR
        # limit box say to keep it off the VPN: DMM refuses its download links from VPN addresses.
        page = PAGE.read_text(encoding="utf-8")
        note = re.search(r'id="jpCheck">Check</button></div>\s*</div>\s*<div class="note">(.*?)</div>', page, re.S)
        self.assertIsNotNone(note)
        self.assertTrue(note.group(1).startswith("Use a proxy in Japan"), note.group(1))
        self.assertIn("putting DMM-Downloader on a VPN app in Japan reaches the VR Channel too, but then Premium and "
                      "library downloads fail, as DMM refuses its download links from VPN addresses.", note.group(1))
        readme = words((ROOT / "README.md").read_text(encoding="utf-8"))
        section = readme.split("## VR Channel: the Japan route", 1)[1].split(" ## ", 1)[0]
        self.assertLess(section.index("**A proxy in Japan** (the way to use)"),
                        section.index("**A VPN app connected to a server in Japan**"))
        self.assertIn("Premium and library downloads fail", section)
        template = words(re.sub(r"^# ?", "", config.TEMPLATE, flags=re.M))  # the comments without their "#"
        self.assertIn("Use an http proxy in Japan", template)
        self.assertIn("on a VPN Premium and library downloads fail", template)
        # The short messages can't explain what a VPN app on this program breaks: they point to the proxy only.
        for message in (links.NEEDS_JAPAN, download.NOT_IN_JAPAN):
            self.assertIn("Japan proxy", message)
        advice = re.compile(r"connect (a|your) VPN app", re.IGNORECASE)
        sources = [*(ROOT / "dmm_downloader").rglob("*.*"), ROOT / "README.md", WINDOWS_README]
        self.assertEqual([p.name for p in sources if p.suffix in TEXT_FILES and advice.search(p.read_text(encoding="utf-8"))],
                         [])

    def test_the_readmes_say_smart_app_control_blocks_the_program(self):
        for path in (ROOT / "README.md", WINDOWS_README):
            text = words(path.read_text(encoding="utf-8"))
            self.assertIn("With Windows' Smart App Control on, the program is blocked completely (there is no "
                          "\"Run anyway\")", text, path.name)
            self.assertIn("run.bat", text.split("Smart App Control on", 1)[1][:200], path.name)

    def test_the_readme_lists_this_versions_changes(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        versions = re.findall(r"^## Changes in (\S+)$", readme, re.MULTILINE)
        self.assertEqual(versions[:1], [__version__])  # the newest list is this version's


if __name__ == "__main__":
    unittest.main()
