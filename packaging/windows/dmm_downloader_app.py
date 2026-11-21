"""Start of the Windows program (DMM-Downloader.exe): without arguments the browser page, with arguments the
terminal commands. Not named dmm_downloader.py, which would hide the package of the same name."""

import sys

from dmm_downloader.cli import main

if __name__ == "__main__":
    sys.exit(main())
