"""DMM-Fetch.exe: only the file download from DMM's file servers, started by DMM-Downloader.exe
(its own program so a VPN's split tunnelling can send just this step through the VPN)."""

import sys

from dmm_downloader.fetch import main

if __name__ == "__main__":
    sys.exit(main())
