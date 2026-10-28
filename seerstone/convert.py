"""Export (convert to another format) and save-rotated, as command lines.

Every job writes to a hidden temp file next to the target and only replaces
the target once the tool succeeds, so a failed or canceled run never leaves a
half-written file behind.
"""
from __future__ import annotations

import os
import shutil
import time

from .proc import CACHE_DIR

# (id, menu label, extension)
EXPORTS = {
    "image": [
        ("png", "PNG", "png"), ("jpg", "JPEG", "jpg"), ("webp", "WebP", "webp"),
        ("avif", "AVIF", "avif"), ("heic", "HEIC", "heic"), ("tiff", "TIFF", "tiff"),
        ("gif", "GIF", "gif"), ("bmp", "BMP", "bmp"), ("pdf", "PDF", "pdf"),
    ],
    "pdf": [
        ("pdf", "PDF", "pdf"), ("png", "PNG (this page)", "png"), ("jpg", "JPEG (this page)", "jpg"),
    ],
    "video": [
        ("mp4", "MP4 (H.264)", "mp4"), ("webm", "WebM (VP9)", "webm"), ("mkv", "MKV (H.264)", "mkv"),
        ("mov", "MOV (H.264)", "mov"), ("gif", "Animated GIF", "gif"),
        ("mp3", "MP3 (audio only)", "mp3"), ("m4a", "M4A (audio only)", "m4a"),
    ],
    "audio": [
        ("mp3", "MP3", "mp3"), ("m4a", "M4A (AAC)", "m4a"), ("ogg", "OGG (Vorbis)", "ogg"),
        ("opus", "Opus", "opus"), ("flac", "FLAC", "flac"), ("wav", "WAV", "wav"),
    ],
}

MULTI_FRAME_TARGETS = {"gif", "webp", "tiff", "pdf"}
NO_ALPHA_TARGETS = {"jpg", "bmp"}
UNDO_DIR = os.path.join(CACHE_DIR, "undo")


def temp_path(target: str) -> str:
    d, name = os.path.split(target)
    stem, ext = os.path.splitext(name)
    return os.path.join(d, f".{stem}.seerstone-tmp{ext}")


def _magick_image(src: str, out: str, fmt: str, rotation: int) -> list[str]:
    argv = ["magick"]
    if os.path.splitext(src)[1].lower() in (".svg", ".svgz"):
        argv += ["-density", "192", "-background", "none"]
    argv.append(src if fmt in MULTI_FRAME_TARGETS else src + "[0]")
    argv.append("-auto-orient")
    if rotation:
        argv += ["-rotate", str(rotation)]
    if fmt in NO_ALPHA_TARGETS:
        argv += ["-background", "white", "-alpha", "remove", "-alpha", "off"]
    argv.append(out)
    return argv


def _ffmpeg(src: str, out: str, fmt: str) -> list[str]:
    head = ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-progress", "pipe:1", "-nostats", "-i", src]
    h264 = ["-map", "0:v:0?", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"]
    opts = {
        "mp4": h264 + ["-movflags", "+faststart"],
        "mov": h264 + ["-movflags", "+faststart"],
        "mkv": h264,
        "webm": ["-map", "0:v:0?", "-map", "0:a:0?", "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
                 "-row-mt", "1", "-deadline", "good", "-cpu-used", "4", "-c:a", "libopus", "-b:a", "128k"],
        "gif": ["-an", "-vf", "fps=12,scale='min(640,iw)':-2:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse",
                "-loop", "0"],
        "mp3": ["-vn", "-c:a", "libmp3lame", "-q:a", "2"],
        "m4a": ["-vn", "-c:a", "aac", "-b:a", "192k"],
        "ogg": ["-vn", "-c:a", "libvorbis", "-q:a", "6"],
        "opus": ["-vn", "-c:a", "libopus", "-b:a", "128k"],
        "flac": ["-vn", "-c:a", "flac"],
        "wav": ["-vn", "-c:a", "pcm_s16le"],
    }[fmt]
    return head + opts + [out]


def export_job(src: str, kind: str, fmt: str, target: str, rotation: int = 0, page: int = 0):
    """Return ("steps", [argv, ...]) or ("ffmpeg", argv); the job writes temp_path(target)."""
    tmp = temp_path(target)
    if kind == "image":
        return "steps", [_magick_image(src, tmp, fmt, rotation)]
    if kind == "pdf":
        if fmt == "pdf":
            argv = ["qpdf", "--warning-exit-0", src, tmp]
            if rotation:
                argv[2:2] = [f"--rotate=+{rotation}"]
            return "steps", [argv]
        os.makedirs(CACHE_DIR, exist_ok=True)
        prefix = os.path.join(CACHE_DIR, f"page-{os.getpid()}-{int(time.time() * 1000)}")
        page_png = prefix + ".png"
        render = ["pdftoppm", "-f", str(page + 1), "-l", str(page + 1), "-singlefile", "-r", "200",
                  "-png", src, prefix]
        finish = ["magick", page_png]
        if rotation:
            finish += ["-rotate", str(rotation)]
        if fmt in NO_ALPHA_TARGETS:
            finish += ["-background", "white", "-alpha", "remove", "-alpha", "off"]
        finish.append(tmp)
        return "steps", [render, finish, ["rm", "-f", page_png]]
    return "ffmpeg", _ffmpeg(src, tmp, fmt)


def save_rotated_job(path: str, kind: str, rotation: int) -> list[list[str]]:
    tmp = temp_path(path)
    if kind == "pdf":
        return [["qpdf", "--warning-exit-0", f"--rotate=+{rotation}", path, tmp]]
    return [["magick", path, "-auto-orient", "-rotate", str(rotation), tmp]]


def finish(target: str) -> None:
    os.replace(temp_path(target), target)


def discard(target: str) -> None:
    try:
        os.remove(temp_path(target))
    except OSError:
        pass


def backup(path: str) -> str:
    """Copy the original aside so a save can be undone."""
    os.makedirs(UNDO_DIR, exist_ok=True)
    dest = os.path.join(UNDO_DIR, f"{int(time.time() * 1000)}-{os.path.basename(path)}")
    shutil.copyfile(path, dest)  # fresh mtime, so the week-old sweep below spares it
    # Keep the undo folder from growing forever: drop copies older than a week.
    cutoff = time.time() - 7 * 86400
    for name in os.listdir(UNDO_DIR):
        p = os.path.join(UNDO_DIR, name)
        try:
            if os.path.getmtime(p) < cutoff and p != dest:
                os.remove(p)
        except OSError:
            pass
    return dest
