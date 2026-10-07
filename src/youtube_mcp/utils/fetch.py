"""Helpers to fetch remote files (Google Drive share links or plain URLs)."""

import re
import tempfile
from pathlib import Path

import httpx

_DRIVE_ID = re.compile(
    r"(?:drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:[^#]*&)?id=)"
    r"|drive\.usercontent\.google\.com/download\?(?:[^#]*&)?id=)([\w-]{10,})"
)

USER_AGENT = "youtube-studio-mcp/0.4"


def normalize_url(url: str) -> str:
    """Turn a Google Drive share link into a direct-download URL.

    The Drive file must be shared as "Anyone with the link". `confirm=t` skips the
    virus-scan interstitial that Drive shows for files over ~100 MB.
    """
    m = _DRIVE_ID.search(url)
    if m:
        return (
            "https://drive.usercontent.google.com/download"
            f"?id={m.group(1)}&export=download&confirm=t"
        )
    return url


def check_not_html(response: httpx.Response, url: str) -> None:
    """Drive returns an HTML page instead of the file when it isn't publicly shared."""
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("text/html"):
        raise ValueError(
            f"{url} returned an HTML page, not a file. If it is a Google Drive link, "
            "share it as 'Anyone with the link' and try again."
        )


def download_to_temp(url: str, suffix: str = "", max_bytes: int = 50 * 1024 * 1024) -> Path:
    """Download a (small) remote file to a temp path and return it."""
    url = normalize_url(url)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    size = 0
    with httpx.stream(
        "GET", url, follow_redirects=True, timeout=120, headers={"User-Agent": USER_AGENT}
    ) as r:
        r.raise_for_status()
        check_not_html(r, url)
        with tmp:
            for chunk in r.iter_bytes(1 << 20):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"{url} is larger than {max_bytes} bytes")
                tmp.write(chunk)
    return Path(tmp.name)


def fetch_text(url: str, max_bytes: int = 5 * 1024 * 1024) -> str:
    """Download a small text file (e.g. an .srt) and return it decoded as UTF-8."""
    path = download_to_temp(url, max_bytes=max_bytes)
    try:
        return path.read_bytes().decode("utf-8-sig")
    finally:
        path.unlink(missing_ok=True)
