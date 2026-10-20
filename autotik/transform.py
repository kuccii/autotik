import hashlib
import json
import shutil
import subprocess
from pathlib import Path

TRANSFORM_MIN_BYTES = 20_000
FONT_FILE = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
TARGET_W, TARGET_H = 1080, 1920
ASPECT_TOLERANCE = 0.02
VARIANTS = ("control", "heavy")
SPEED = 1.05

# Edits that change the picture beyond a border overlay: a 1.1x punch-in (which
# also crops the source's own watermark), a mirror, a slight speed-up with
# pitch shift, and a mild grade. Repost detection keys on frame similarity, so
# a border alone still reads as an unoriginal upload.
def variant_chain(out_w, out_h):
    even_w = out_w - out_w % 2
    even_h = out_h - out_h % 2
    up_w = int(round(even_w * 1.1 / 2)) * 2
    up_h = int(round(even_h * 1.1 / 2)) * 2
    return (
        f"scale={up_w}:{up_h}:flags=bicubic,"
        f"crop={even_w}:{even_h},"
        f"hflip,setpts=PTS/{SPEED},"
        "eq=contrast=1.04:saturation=1.1,"
    )


def pick_variant(video_id, ratio):
    """Deterministically assign the heavy edit to a share of the videos.

    Stable per video id so a re-run of discover cannot flip the arm.
    """
    if ratio <= 0:
        return "control"
    if ratio >= 1:
        return "heavy"
    digest = hashlib.sha1(str(video_id).encode()).hexdigest()
    return "heavy" if (int(digest[:8], 16) % 1000) < ratio * 1000 else "control"


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
    has_audio = any(
        s.get("codec_type") == "audio" for s in data.get("streams", [])
    )
    return {
        "width": width, "height": height,
        "duration": duration, "has_audio": has_audio,
    }


def _needs_letterbox(width, height):
    return abs((width / height) - (TARGET_W / TARGET_H)) > ASPECT_TOLERANCE


def build_filters(width, height, account, style="bar", variant="control"):
    if style == "none":
        return "null", width, height

    if _needs_letterbox(width, height):
        out_w, out_h = TARGET_W, TARGET_H
        edit = variant_chain(out_w, out_h) if variant == "heavy" else ""
        core = (
            f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},boxblur=22:4[bg];"
            f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1[v0];"
            f"[v0]{edit}"
        )
    else:
        out_w, out_h = width, height
        edit = variant_chain(out_w, out_h) if variant == "heavy" else ""
        core = f"[0:v]{edit}"

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


def transform(
    src, dst, account, enabled=True, style="bar", variant="control",
    runner=subprocess.run,
):
    src, dst = Path(src), Path(dst)
    if not enabled or style == "none":
        return src
    if not src.exists():
        raise TransformError(f"source missing: {src}")
    floor = TRANSFORM_MIN_BYTES
    if dst.exists() and dst.stat().st_size >= floor:
        return dst

    try:
        info = probe(src)
    except TransformError:
        raise
    except Exception as exc:
        raise TransformError(f"probe failed for {src}: {exc}") from exc

    if variant not in VARIANTS:
        variant = "control"
    vf, _, _ = build_filters(
        info["width"], info["height"], account, style, variant
    )
    has_audio = bool(info.get("has_audio"))
    cmd = ["ffmpeg", "-y", "-i", str(src)]
    if ";" in vf:
        cmd += ["-filter_complex", vf, "-map", "[vout]"]
    else:
        # any -map disables ffmpeg's automatic stream selection, so the video
        # has to be mapped explicitly or the output ends up audio-only
        cmd += ["-vf", vf, "-map", "0:v:0"]
    if has_audio:
        cmd += ["-map", "0:a?"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
    ]
    if not has_audio:
        cmd += ["-an"]
    elif variant == "heavy":
        # keep the audio aligned with the sped-up video and shift its pitch
        cmd += ["-af", f"atempo={SPEED}", "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-c:a", "copy"]
    cmd += ["-movflags", "+faststart", str(dst)]
    try:
        result = runner(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise TransformError("ffmpeg not found on PATH") from exc

    if result.returncode != 0:
        if dst.exists():
            dst.unlink()
        tail = (result.stderr or "")[-800:]
        raise TransformError(f"ffmpeg exited {result.returncode}: {tail}")
    if not dst.exists() or dst.stat().st_size < floor:
        size = dst.stat().st_size if dst.exists() else 0
        if dst.exists():
            dst.unlink()
        raise TransformError(
            f"transformed output too small ({size} < {floor})"
        )
    out_info = probe(dst)
    if info["duration"] > 0 and out_info["duration"] < info["duration"] * 0.6:
        dst.unlink()
        raise TransformError(
            "transformed output truncated "
            f"({out_info['duration']:.2f}s of {info['duration']:.2f}s)"
        )
    return dst
