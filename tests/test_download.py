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

def no_net_client():
    def handler(request):
        raise AssertionError("network attempted")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_non_https_rejected_before_network(tmp_path):
    it = item()
    it["play_url"] = "http://cdn.test/video.mp4"
    with pytest.raises(download.DownloadError, match="non-https"):
        download.download(it, tmp_path, no_net_client())


def test_internal_and_metadata_hosts_rejected(tmp_path):
    bad_hosts = (
        "https://localhost/video.mp4",
        "https://169.254.169.254/latest/meta-data/",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://127.0.0.1:8080/admin",
    )
    for url in bad_hosts:
        it = item()
        it["play_url"] = url
        with pytest.raises(download.DownloadError, match="allowlist"):
            download.download(it, tmp_path, no_net_client())


def test_source_url_validated_before_ytdlp(tmp_path):
    it = item()
    it["source_url"] = "https://evil-internal.example/dump"
    seen = []

    def fake_ytdlp(url, dest):
        seen.append(url)

    with pytest.raises(download.DownloadError, match="allowlist"):
        download.download(it, tmp_path, no_net_client(), ytdlp=fake_ytdlp)
    assert seen == []


def test_oversize_stream_aborted(tmp_path, monkeypatch):
    monkeypatch.setattr(download, "DOWNLOAD_MAX_BYTES", 1000)

    def handler(request):
        return httpx.Response(200, content=b"x" * 5000)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dest = tmp_path / "out.mp4"
    with pytest.raises(download.DownloadError, match="exceeded"):
        download._direct(client, "https://cdn.test/video.mp4", dest)


def test_validate_url_allows_allowlisted_hosts_and_blocks_lookalikes():
    allowed = (
        "https://cdn.test/video.mp4",
        "https://www.tiktok.com/@a/video/1",
        "https://v16m-webapp-prime.tiktokcdn.com/x.mp4",
        "https://v3-web.tiktokcdn.com/y.mp4",
        "https://api16-normal-c-useast1a.tiktokv.com/z",
        "https://www.tikwm.com/api/feed/list",
    )
    for url in allowed:
        assert download.validate_url(url) == url
    for url in ("https://eviltiktok.com/x.mp4", "https://tiktok.com.evil.io/x"):
        with pytest.raises(download.DownloadError):
            download.validate_url(url)


def test_ytdlp_fallback_caps_max_filesize(tmp_path, monkeypatch):
    import sys
    import types

    captured = {}

    class FakeYDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def download(self, urls):
            captured["urls"] = urls

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)

    download._ytdlp_fallback("https://www.tiktok.com/@a/video/1", tmp_path / "out.mp4")
    assert captured["max_filesize"] == download.DOWNLOAD_MAX_BYTES
    assert captured["urls"] == ["https://www.tiktok.com/@a/video/1"]