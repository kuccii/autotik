import hashlib
from pathlib import Path
from urllib.parse import urlparse

DOWNLOAD_MIN_BYTES = 50_000
DOWNLOAD_MAX_BYTES = 200 * 1024 * 1024
ALLOWED_HOST_SUFFIXES = (
    "tiktok.com",
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "tiktokv.com",
    "tiktokv.us",
    "tikwm.com",
    "cdn.test",
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


class DownloadError(RuntimeError):
    pass


def validate_url(url):
    parsed = urlparse(str(url))
    if parsed.scheme != "https":
        raise DownloadError(f"non-https url rejected: {url!r}")
    host = (parsed.hostname or "").lower()
    if not any(host == s or host.endswith("." + s) for s in ALLOWED_HOST_SUFFIXES):
        raise DownloadError(f"host not allowlisted: {host!r}")
    return url


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ytdlp_fallback(url, dest):
    from yt_dlp import YoutubeDL

    opts = {
        "outtmpl": str(dest.with_suffix("")),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "mp4/bestvideo*+bestaudio/best",
    }
    with YoutubeDL(opts) as ydl:
        ydl.download([url])


def _direct(client, url, dest):
    with client.stream("GET", url, headers={"User-Agent": UA}, timeout=60.0) as resp:
        if resp.status_code != 200:
            raise DownloadError(f"direct fetch HTTP {resp.status_code}")
        written = 0
        with open(dest, "wb") as fh:
            for chunk in resp.iter_bytes(1 << 16):
                fh.write(chunk)
                written += len(chunk)
                if written > DOWNLOAD_MAX_BYTES:
                    raise DownloadError(
                        f"direct fetch exceeded {DOWNLOAD_MAX_BYTES} bytes"
                    )
    if written < DOWNLOAD_MIN_BYTES:
        raise DownloadError(f"direct fetch too small ({written} bytes)")
    return written


def download(item, dest_dir, client, ytdlp=None):
    validate_url(item["play_url"])
    validate_url(item["source_url"])
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (Path(str(item["video_id"])).name + ".mp4")
    if dest.exists() and dest.stat().st_size >= DOWNLOAD_MIN_BYTES:
        return dest, file_hash(dest)

    ytdlp = ytdlp or _ytdlp_fallback
    errors = []
    try:
        _direct(client, item["play_url"], dest)
    except Exception as exc:
        errors.append(f"direct: {exc}")
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        try:
            ytdlp(item["source_url"], dest)
        except Exception as exc2:
            errors.append(f"ytdlp: {exc2}")
        if not dest.exists() or dest.stat().st_size < DOWNLOAD_MIN_BYTES:
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            raise DownloadError(
                f"download failed for {item['video_id']}: " + " | ".join(errors)
            )
    if not dest.exists():
        raise DownloadError(f"no output for {item['video_id']}")
    return dest, file_hash(dest)
