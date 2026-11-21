"""Test helpers: a local file server (Range, cut connections), a fake jav-it (it only copies files: the tests'
"encrypted" parts are plain clips), small video clips."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dmm_downloader.merging import tools

try:
    MEDIA_TOOLS = tools.find_tools()
    MEDIAINFO = tools.find_tool("mediainfo")
except tools.ToolError:
    MEDIA_TOOLS = MEDIAINFO = None


class FileServer:
    """Serves `data` at /file. `cut_after` bytes are sent on the first request, then the
    connection is closed; `ranges` turns HTTP Range support on or off. A range from the end on is answered 416 with
    the file's size, like a real server."""

    def __init__(self, data: bytes, cut_after: int | None = None, ranges: bool = True,
                 content_type: str = "application/octet-stream") -> None:
        self.data, self.cut_after, self.ranges, self.requests = data, cut_after, ranges, []
        self.content_type = content_type
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                header = self.headers.get("Range")
                server.requests.append(header)
                start = 0
                if header and server.ranges:
                    start = int(header.split("=")[1].split("-")[0])
                    if start >= len(server.data):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(server.data)}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(server.data) - 1}/{len(server.data)}")
                else:
                    self.send_response(200)
                body = server.data[start:]
                self.send_header("Content-Type", server.content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if server.cut_after is not None:
                    body, server.cut_after = body[: server.cut_after], None
                    self.wfile.write(body)
                    self.wfile.flush()
                    self.close_connection = True
                    return
                try:
                    self.wfile.write(body)
                except (ConnectionResetError, BrokenPipeError):
                    pass  # the downloader stopped reading (e.g. it refused a page): fine

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/file"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


FAKE_JAVIT = r'''
import os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
src, out = Path(args[args.index("-i") + 1]), Path(args[args.index("-o") + 1])
mode = os.environ.get("FAKE_JAVIT_MODE", "ok")
here = Path(__file__).resolve().parent
log = here / "calls.log"
with open(log, "a", encoding="utf-8") as f:
    f.write(f"{mode} cwd={Path.cwd().resolve() == here} user={os.environ.get('DMM_USERNAME')} tmp={os.environ.get('TMP')}\n")
if mode == "license":
    answer = sys.stdin.read() if not sys.stdin.isatty() else ""
    if "fan@example.com" not in answer:
        print("Please validate your identity. Patreon email address: ", flush=True)
        sys.exit(1)
if mode == "hang":
    import time
    time.sleep(120)
if mode == "connection":
    print("[ !! ] No internet connection", flush=True)
    sys.exit(3)
if mode == "vr-licence":  # what older versions said about a VR file DMM VR Player hadn't played
    print("Open Error: the user has not been authenticated", flush=True)
    sys.exit(1)
with open(here / "kinds.log", "a", encoding="utf-8") as f:
    f.write(args[args.index("-t") + 1] + "\n")
if mode == "small-once":  # a cut-short result the first time only
    marker = here / "small-once.done"
    mode = "ok" if marker.exists() else "small"
    marker.touch()
if mode == "small-calls":  # a cut-short result on the decrypts numbered in FAKE_JAVIT_SMALL ("1,3")
    calls = len((here / "kinds.log").read_text(encoding="utf-8").splitlines())
    mode = "small" if str(calls) in os.environ.get("FAKE_JAVIT_SMALL", "").split(",") else "ok"
result = os.environ.get("FAKE_JAVIT_RESULT")  # what the decrypted file holds (default: a copy of the input)
data = Path(result).read_bytes() if result else src.read_bytes()
out.write_bytes(data[: len(data) // 2] if mode == "small" else data)
print("[ OK ] Decryption complete!", flush=True)
'''


def make_fake_javit(folder: Path, vr: bool = False) -> Path:
    """A jav-it stand-in (copies its input) with DECRYPT.MOD next to it, and VR.MOD with `vr`."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "fake_javit.py").write_text(FAKE_JAVIT, encoding="utf-8")
    (folder / "DECRYPT.MOD").write_text("", encoding="utf-8")
    if vr:
        (folder / "VR.MOD").write_text("", encoding="utf-8")
    if os.name == "nt":
        path = folder / "jav-it.bat"
        path.write_text(f'@"{sys.executable}" "%~dp0fake_javit.py" %*\r\n', encoding="utf-8")
    else:
        path = folder / "jav-it"
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/fake_javit.py" "$@"\n', encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def make_clip(path: Path, file_id: str | None, part: int | None, seconds: float = 2, codec: str = "libx264",
              container: str = "matroska") -> Path:
    """A small video like a decrypted DMM file: H.264 (or VP9) + AAC, DMM's comment tag (VR: container "mp4")."""
    encoders = subprocess.run([str(MEDIA_TOOLS.ffmpeg), "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if codec not in encoders:
        codec = "mpeg4"
    cmd = [str(MEDIA_TOOLS.ffmpeg), "-v", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
           "-c:v", codec, "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-f", container]
    if codec == "libvpx-vp9":
        cmd += ["-deadline", "realtime", "-cpu-used", "8"]
    if file_id:
        data = {"file_id": file_id} if part is None else {"part": str(part), "file_id": file_id}
        cmd += ["-metadata", "comment=" + json.dumps(data, separators=(",", ":"))]
    subprocess.run(cmd + [str(path)], check=True)
    return path


def copy_to(source: Path):
    def fake_download(session, url, target, label, attempts=5, progress=None, retry_delay=10.0, **kwargs):
        shutil.copyfile(source, target)
        if progress:
            progress(target.stat().st_size, target.stat().st_size)
        return target

    return fake_download
