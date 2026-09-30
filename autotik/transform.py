import json
import shutil
import subprocess
from pathlib import Path

TRANSFORM_MIN_BYTES = 1_000_000
FONT_FILE = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
TARGET_W, TARGET_H = 1080, 1920
ASPECT_TOLERANCE = 0.02


class TransformError(RuntimeError):
    pass


def probe(path):
    if shutil.which("ffprobe") is None:
        raise TransformError("ffprobe not found on PATH")
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        raise TransformError(f"ffprobe failed: {exc.stderr[-500:]}") from exc
    data = json.loads(out.stdout)
    stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if stream is None:
        raise TransformError(f"no video stream in {path}")
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise TransformError(f"bad dimensions for {path}: {width}x{height}")
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or stream.get("duration") or 0.0)
    return {"width": width, "height": height, "duration": duration}


def _needs_letterbox(width, height):
    return abs((width / height) - (TARGET_W / TARGET_H)) > ASPECT_TOLERANCE


def build_filters(width, height, account, style="bar"):
    if style == "none":
        return "null", width, height

    if _needs_letterbox(width, height):
        out_w, out_h = TARGET_W, TARGET_H
        core = (
            f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},boxblur=22:4[bg];"
            f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1[v0];"
            "[v0]"
        )
    else:
        out_w, out_h = width, height
        core = "[0:v]"

    bar_h = max(40, int(out_h * 0.065))
    font = max(18, int(out_h * 0.03))
    text_y = out_h - int(bar_h * 0.55) - font // 2

    vf = (
        f"{core}"
        f"drawbox=x=0:y={out_h - bar_h}:w={out_w}:h={bar_h}:color=black@0.45:t=fill,"
        f"drawtext=text='{account}':fontfile={FONT_FILE}:fontcolor=white:"
        f"fontsize={font}:x=(w-text_w)/2:y={text_y}:"
        f"shadowx=2:shadowy=2:shadowcolor=black@0.6,"
        f"drawbox=x=6:y=6:w={out_w - 12}:h={out_h - 12}:color=white@0.18:t=4[vout]"
    )
    return vf, out_w, out_h


def transform(src, dst, account, enabled=True, style="bar", runner=subprocess.run):
    src, dst = Path(src), Path(dst)
    if not enabled or style == "none":
        return src
    if not src.exists():
        raise TransformError(f"source missing: {src}")
    if dst.exists() and dst.stat().st_size >= TRANSFORM_MIN_BYTES:
        return dst

    try:
        info = probe(src)
    except TransformError:
        raise
    except Exception as exc:
        raise TransformError(f"probe failed for {src}: {exc}") from exc

    vf, _, _ = build_filters(info["width"], info["height"], account, style)
    cmd = [
        "ffmpeg", "-y", "-i", str(src), "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "copy", "-movflags", "+faststart", str(dst),
    ]
    try:
        result = runner(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise TransformError("ffmpeg not found on PATH") from exc

    if result.returncode != 0:
        if dst.exists():
            dst.unlink()
        tail = (result.stderr or "")[-800:]
        raise TransformError(f"ffmpeg exited {result.returncode}: {tail}")
    if not dst.exists() or dst.stat().st_size < TRANSFORM_MIN_BYTES:
        size = dst.stat().st_size if dst.exists() else 0
        if dst.exists():
            dst.unlink()
        raise TransformError(
            f"transformed output too small ({size} < {TRANSFORM_MIN_BYTES})"
        )
    return dst
