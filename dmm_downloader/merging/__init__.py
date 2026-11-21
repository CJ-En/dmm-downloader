"""Merging multi-part downloads: a copy of the code of an earlier version of VideoMerger, a sister tool.

probe.py, discover.py, plan.py, merge.py and tools.py are those files; only this note and the install hint in
tools.py differ. It doesn't follow VideoMerger's later changes (looser part names, WMV parts with AsfBin): the
downloader joins only its own DMM downloads, whose names and format are fixed. tools.Popen (how ffmpeg, ffprobe and
MediaInfo are started) is set below, so they end when the downloader ends (dmm_downloader/children.py).
"""

from .. import children as _children
from . import tools as _tools

_tools.Popen = _children.Popen
