"""A log file per user (rotating), with passwords, cookies and signed download links removed."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config

SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:dl_key|token|access_token|auth|signature|password|pass|session|cookie|key)=)[^&\s]+"
)
SENSITIVE_HEADER_RE = re.compile(r"(?i)((?:authorization|cookie|set-cookie|proxy-authorization)\s*[:=]\s*)([^\r\n]+)")
PROXY_CREDENTIAL_RE = re.compile(r"(https?://)([^:/@\s]+):([^/@\s]+)@")
# DMM's download hosts carry keys and tokens in the path and the query: str/stcN.dmm.com (2D), wsd-r.dmm.co.jp and
# wsd-cN.dmm.com (VR). Their whole address after the host is hidden.
DOWNLOAD_HOST = r"(?:str|stc\d*|wsd-r|wsd-c\d+)\.dmm\.(?:com|co\.jp)"
DMM_SIGNED_URL_RE = re.compile(rf"https?://({DOWNLOAD_HOST})(?::\d+)?/\S+", re.IGNORECASE)
# The same address in connection errors: "HTTPSConnectionPool(host='wsd-r.dmm.co.jp', port=443): ... with url: /..."
DMM_POOL_URL_RE = re.compile(rf"(host='{DOWNLOAD_HOST}'.*?\burl: )\S+", re.IGNORECASE)
# Any other DMM address: the query string is hidden (the path, e.g. the product id, stays readable).
DMM_QUERY_RE = re.compile(r"(https?://(?:[a-z0-9-]+\.)*dmm\.(?:com|co\.jp)(?::\d+)?(?:/[^\s?#]*)?)\?[^\s#]+", re.IGNORECASE)
POOL_QUERY_RE = re.compile(r"(\burl: [^\s?]*)\?[^\s)]+")
CREDENTIAL_ENV_RE = re.compile(r"(?i)\b((?:DMM_)?(?:USERNAME|PASSWORD|login_id)\s*[:=]\s*)[^\s,;]+")


def redact(value) -> str:
    """Remove secrets from a log line while keeping enough to understand it."""
    text = str(value or "")
    text = PROXY_CREDENTIAL_RE.sub(r"\1[hidden]@", text)
    text = DMM_SIGNED_URL_RE.sub(r"https://\1/[signed-link-hidden]", text)
    text = DMM_POOL_URL_RE.sub(r"\1[signed-link-hidden]", text)
    text = DMM_QUERY_RE.sub(r"\1?[hidden]", text)
    text = POOL_QUERY_RE.sub(r"\1?[hidden]", text)
    text = SENSITIVE_QUERY_RE.sub(r"\1[hidden]", text)
    text = SENSITIVE_HEADER_RE.sub(r"\1[hidden]", text)
    text = CREDENTIAL_ENV_RE.sub(r"\1[hidden]", text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup(verbose: bool = False) -> Path | None:
    """Log to <log_dir>/dmm-downloader.log; returns its path, or None if it can't be written."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for old in [h for h in root.handlers if getattr(h, "dmm_downloader", False)]:
        root.removeHandler(old)
        old.close()
    for noisy in ("urllib3", "charset_normalizer", "keyring"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if verbose:
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG)
        console.setFormatter(RedactingFormatter("  [log] %(message)s"))
        console.dmm_downloader = True
        root.addHandler(console)
    path = config.log_dir() / "dmm-downloader.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    handler.dmm_downloader = True
    root.addHandler(handler)
    return path
