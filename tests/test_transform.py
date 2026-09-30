import shutil
from pathlib import Path

import pytest

from autotik import transform

RUNNERS = {}


def capture_runner(returncode=0, stderr=""):
    def runner(cmd, **kwargs):
        RUNNERS["cmd"] = cmd
        class R:
            pass
        r = R()
        r.returncode = returncode
        r.stderr = stderr
        out = Path(cmd[-1])
        if returncode == 0:
            out.write_bytes(b"v" * 2_000_000)
        return r
    return runner


def test_portrait_9x16_gets_overlay_only():
    vf, w, h = transform.build_filters(1080, 1920, "@demo", "bar")
    assert (w, h) == (1080, 1920)
    assert "split" not in vf
    assert "drawbox" in vf
    assert "drawtext" in vf
    assert "@demo" in vf
    assert "DejaVuSans-Bold.ttf" in vf
    assert "h=124" in vf
    assert "fontsize=57" in vf


def test_smaller_portrait_keeps_own_resolution():
    vf, w, h = transform.build_filters(720, 1280, "@demo", "bar")
    assert (w, h) == (720, 1280)
    assert "h=1268" in vf
    assert "boxblur" not in vf


def test_landscape_is_letterboxed_with_blur_padding():
    vf, w, h = transform.build_filters(1920, 1080, "@demo", "bar")
    assert (w, h) == (1080, 1920)
    assert "boxblur" in vf
    assert "overlay=(W-w)/2:(H-h)/2" in vf
    assert "force_original_aspect_ratio=increase" in vf
    assert "force_original_aspect_ratio=decrease" in vf


def test_square_input_letterboxes_to_9x16():
    vf, w, h = transform.build_filters(1000, 1000, "@demo", "bar")
    assert (w, h) == (1080, 1920)
    assert "boxblur" in vf


def fake_probe(path):
    return {"width": 1080, "height": 1920, "duration": 2.0}


def test_transform_builds_expected_ffmpeg_command(tmp_path, monkeypatch):
    monkeypatch.setattr(transform, "probe", fake_probe)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"in")
    dst = tmp_path / "out.mp4"
    runner = capture_runner()
    path = transform.transform(src, dst, "@demo", runner=runner)
    cmd = RUNNERS["cmd"]
    assert cmd[0] == "ffmpeg"
    assert "-vf" in cmd
    assert "-c:a" in cmd and "copy" in cmd
    assert "+faststart" in cmd
    assert cmd[-1] == str(dst)
    assert path == dst
    assert dst.stat().st_size == 2_000_000


def test_transform_raises_on_ffmpeg_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(transform, "probe", fake_probe)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"in")
    with pytest.raises(transform.TransformError) as exc:
        transform.transform(src, tmp_path / "o.mp4", "@a", runner=returncode_runner(1, "boom"))
    assert "boom" in str(exc.value)


def returncode_runner(code, stderr):
    def runner(cmd, **kwargs):
        class R:
            pass
        r = R()
        r.returncode = code
        r.stderr = stderr
        return r
    return runner


def test_disabled_transform_returns_source_path(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"in")
    out = transform.transform(src, tmp_path / "o.mp4", "@a", enabled=False)
    assert out == src


def test_nonexistent_source_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(transform, "probe", fake_probe)
    with pytest.raises(transform.TransformError, match="source missing"):
        transform.transform(tmp_path / "missing.mp4", tmp_path / "o.mp4", "@a")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_probe_and_transform(tmp_path, monkeypatch):
    monkeypatch.setattr(transform, "TRANSFORM_MIN_BYTES", 10_000)
    src = tmp_path / "src.mp4"
    import subprocess
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=15",
         "-t", "2", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    info = transform.probe(src)
    assert info["width"] == 1080
    assert info["height"] == 1920
    out = transform.transform(src, tmp_path / "dst.mp4", "@demo")
    assert out.exists() and out.stat().st_size > 0
    assert transform.probe(out)["height"] == 1920

    wide_src = tmp_path / "wide.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=15",
         "-t", "2", "-pix_fmt", "yuv420p", str(wide_src)],
        check=True, capture_output=True,
    )
    out_w, out_h = transform.build_filters(1920, 1080, "@demo", "bar")[1:]
    wide_out = transform.transform(wide_src, tmp_path / "wide_dst.mp4", "@demo")
    assert wide_out.exists() and wide_out.stat().st_size > 0
    wide_info = transform.probe(wide_out)
    assert (wide_info["width"], wide_info["height"]) == (out_w, out_h)
    assert wide_info["height"] > wide_info["width"]
