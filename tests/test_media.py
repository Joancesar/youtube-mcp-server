"""Tests for the streaming resumable upload and URL helpers."""

import re
from unittest.mock import patch

import httpx

from youtube_mcp.utils.fetch import normalize_url


def test_normalize_drive_links():
    fid = "1oN5SUrXbBmVX2wNeyO65WxuwLtXnovxo"
    direct = f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"
    assert normalize_url(f"https://drive.google.com/file/d/{fid}/view?usp=sharing") == direct
    assert normalize_url(f"https://drive.google.com/open?id={fid}") == direct
    assert normalize_url(f"https://drive.google.com/uc?export=download&id={fid}") == direct
    assert normalize_url("https://example.com/video.mp4") == "https://example.com/video.mp4"


class FakeResumableServer:
    """Mimics YouTube's resumable endpoint; commits at most `cap` bytes per PUT."""

    def __init__(self, cap=None, fail_first=False):
        self.data = bytearray()
        self.cap = cap
        self.fail_first = fail_first
        self.puts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.puts += 1
        if self.fail_first and self.puts == 1:
            return httpx.Response(503)
        cr = request.headers["content-range"]
        m = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)", cr)
        if m:
            start = int(m.group(1))
            assert start == len(self.data), (start, len(self.data))
            body = request.content
            if self.cap:
                body = body[: self.cap]
            self.data += body
            total = m.group(3)
            if total != "*" and len(self.data) == int(total):
                return httpx.Response(200, json={"id": "vid123", "status": {}})
        else:  # status query "bytes */N"
            total = cr.split("/")[1]
            if total != "*" and len(self.data) == int(total):
                return httpx.Response(200, json={"id": "vid123", "status": {}})
        headers = {"range": f"bytes=0-{len(self.data) - 1}"} if self.data else {}
        return httpx.Response(308, headers=headers)


def _run(server, payload, total, piece=1 << 20):
    from youtube_mcp.tools import media

    media._jobs["j"] = {"bytes_sent": 0}
    source = (payload[i : i + piece] for i in range(0, len(payload), piece))
    with patch.object(media, "_token", return_value="tok"), patch.object(media.time, "sleep"):
        with httpx.Client(transport=httpx.MockTransport(server)) as http:
            return media._stream_upload(http, "https://upload/session", source, total, "j")


def test_stream_upload_known_length():
    payload = bytes(range(256)) * (20 * 4096 + 7)  # ~21 MB, not chunk aligned
    server = FakeResumableServer()
    assert _run(server, payload, len(payload))["id"] == "vid123"
    assert bytes(server.data) == payload


def test_stream_upload_unknown_length_and_partial_commits():
    payload = b"x" * (19 * 1024 * 1024 + 123)
    server = FakeResumableServer(cap=3 * 1024 * 1024)
    assert _run(server, payload, None)["id"] == "vid123"
    assert bytes(server.data) == payload


def test_stream_upload_retries_after_server_error():
    payload = b"y" * (9 * 1024 * 1024)
    server = FakeResumableServer(fail_first=True)
    assert _run(server, payload, len(payload))["id"] == "vid123"
    assert bytes(server.data) == payload
