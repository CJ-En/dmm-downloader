"""Refuse a build that names the computer it was made on: searches every file of the folder for the given
words (user name, folders, accounts), and for the words in refused-words.txt next to this file and in the
environment variable DMM_DOWNLOADER_REFUSE, as UTF-8 and UTF-16 and ignoring case. Words shorter than 4 characters
count only as whole words (not inside a longer word). Exit code 1 when one is found.

refused-words.txt (one word per line, "#" starts a comment) isn't in git: it holds names that must never be in a
build and that the source shouldn't spell either. DMM_DOWNLOADER_REFUSE: words separated by ";"."""

import os
import re
import sys
from pathlib import Path

REFUSED_FILE = Path(__file__).resolve().with_name("refused-words.txt")
REFUSED_ENV = "DMM_DOWNLOADER_REFUSE"
SHORT = 4  # shorter words count only as whole words


def refused_words() -> list[str]:
    """The words of refused-words.txt and of DMM_DOWNLOADER_REFUSE."""
    words = []
    try:
        for line in REFUSED_FILE.read_text(encoding="utf-8-sig").splitlines():
            words.append(line.split("#", 1)[0])
    except FileNotFoundError:
        pass
    words += os.environ.get(REFUSED_ENV, "").split(";")
    return [w.strip() for w in words if w.strip()]


def needles(word: str) -> list[tuple[bytes, re.Pattern | None]]:
    """How `word` may appear in a file (UTF-8 and UTF-16, / for \\, lower and upper case), each with the whole-word
    pattern it must match too when the word is short."""
    found = []
    for variant in dict.fromkeys([word, word.replace("\\", "/"), word.lower(), word.upper()]):
        for encoding, unit in (("utf-8", b""), ("utf-16-le", b"\x00")):
            needle = variant.encode(encoding)
            whole = None
            if len(word) < SHORT:  # no letter, digit or "_" right before or after it (UTF-16: two bytes each)
                edge = rb"[A-Za-z0-9_]" + unit
                whole = re.compile(rb"(?<!" + edge + rb")" + re.escape(needle) + rb"(?!" + edge + rb")", re.IGNORECASE)
            found.append((needle, whole))
    return found


def main() -> int:
    folder = Path(sys.argv[1])
    words = list(dict.fromkeys(w for w in [*sys.argv[2:], *refused_words()] if w))
    patterns = [(word, needle, whole) for word in words for needle, whole in needles(word)]
    found = []
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    for path in files:
        data = path.read_bytes()
        lowered = data.lower()
        for word, needle, whole in patterns:
            if (needle in data or needle.lower() in lowered) and (whole is None or whole.search(data)):
                found.append(f"{path.relative_to(folder)}: contains {word!r}")
                break
    for line in found:
        print(line)
    print(f"checked {len(files)} files for {len(words)} words: " + ("NOT CLEAN" if found else "clean"))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
