import hashlib
from pathlib import Path

import httpx
import pytest

from autotik import download


def item():
    return {
        "video_id": "v1",
        "author": "someone",
        "source_url": "https://www.tiktok.com/@someone/video/v1",
        "play_url": "https://cdn.test/video.mp4",
    }


def test_direct_download_writes_file_and_hash(tmp_path):
    payload = b"x" * 60_000

    def handler(request):
        return httpx.Response(200, content=payload, headers={"content-type": "video/mp4"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    path, digest = download.download(item(), tmp_path, client)
    assert path.exists()
    assert path.read_bytes() == payload
    assert digest == hashlib.sha256(payload).hexdigest()


def test_small_file_rejected_then_falls_back_to_ytdlp(tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"tiny")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    seen = {}

    def fake_ytdlp(url, dest):
        seen["url"] = url
        dest.write_bytes(b"y" * 60_000)

    path, _ = download.download(item(), tmp_path, client, ytdlp=fake_ytdlp)
    assert seen["url"] == item()["source_url"]
    assert path.stat().st_size == 60_000


def test_fallback_failure_raises_download_error(tmp_path):
    def handler(request):
        return httpx.Response(404, content=b"gone")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    def boom(url, dest):
        raise RuntimeError("yt-dlp exploded")

    with pytest.raises(download.DownloadError):
        download.download(item(), tmp_path, client, ytdlp=boom)


def test_output_path_is_named_after_video_id(tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"z" * 60_000)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    path, _ = download.download(item(), tmp_path, client)
    assert path.name == "v1.mp4"


def test_non_http_exception_in_direct_leg_falls_back(tmp_path):
    def handler(request):
        raise ValueError("parser blew up")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    seen = {}

    def fake_ytdlp(url, dest):
        seen["url"] = url
        dest.write_bytes(b"y" * 60_000)

    path, digest = download.download(item(), tmp_path, client, ytdlp=fake_ytdlp)
    assert seen["url"] == item()["source_url"]
    assert path.stat().st_size == 60_000
    assert digest == hashlib.sha256(b"y" * 60_000).hexdigest()


def test_ytdlp_output_below_minimum_raises_download_error(tmp_path):
    def handler(request):
        return httpx.Response(404, content=b"gone")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    def undersized(url, dest):
        dest.write_bytes(b"y" * 1_000)

    with pytest.raises(download.DownloadError):
        download.download(item(), tmp_path, client, ytdlp=undersized)
    assert not (tmp_path / "v1.mp4").exists()


def test_video_id_path_traversal_stays_in_dest_dir(tmp_path):
    payload = b"x" * 60_000

    def handler(request):
        return httpx.Response(200, content=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    bad = item()
    bad["video_id"] = "../evil"
    path, _ = download.download(bad, tmp_path, client)
    assert path.name == "evil.mp4"
    assert path.resolve().parent == tmp_path.resolve()
    assert path.read_bytes() == payload
