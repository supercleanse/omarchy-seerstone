"""Build the scrubbing test media (cached; only builds what's missing).

Every video frame carries its own frame number as 12 black/white blocks
(binary, lowest bit first) in the top-left corner, drawn over a busy moving
test pattern so decoding costs what real footage costs. Read the blocks back
with frame_number() to know exactly which frame is on screen. Black/white
survives any color conversion GTK applies (gray levels don't).

    python3 tests/make_media.py            # build into ~/.cache/seerstone-test-media
"""
from __future__ import annotations

import os
import subprocess
import sys

DIR = os.path.expanduser("~/.cache/seerstone-test-media")

BITS = 12          # up to 4096 frames
BLOCK = 40         # each bit is a BLOCK x BLOCK square; the strip is 480 x 40 at (0, 0)

# name: (width, height, fps, seconds, codec args, audio args)
H264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
VIDEOS = {
    "h264-high-1080p-60s.mp4": (1920, 1080, "30", 60, H264 + ["-profile:v", "high", "-bf", "3", "-g", "300"],
                                ["-c:a", "aac", "-b:a", "128k"]),
    "h264-60fps-720p.mp4": (1280, 720, "60", 30, H264 + ["-profile:v", "high", "-bf", "2", "-g", "240"],
                            ["-c:a", "aac", "-b:a", "128k"]),
    "h264-29.97fps.mp4": (1280, 720, "30000/1001", 20, H264 + ["-g", "120"], ["-c:a", "aac"]),
    "h264-24fps.mkv": (1280, 720, "24", 20, H264 + ["-g", "96"], ["-c:a", "aac"]),
    "h264-25fps.mov": (1280, 720, "25", 20, H264 + ["-g", "100"], ["-c:a", "aac"]),
    "h264-portrait.mp4": (1080, 1920, "30", 15, H264 + ["-g", "90"], ["-c:a", "aac"]),
    "h264-noaudio.mp4": (1280, 720, "30", 15, H264 + ["-g", "150"], None),
    "vp9.webm": (1280, 720, "30", 20, ["-c:v", "libvpx-vp9", "-crf", "34", "-b:v", "0", "-deadline", "realtime",
                                       "-cpu-used", "8", "-row-mt", "1", "-g", "150", "-pix_fmt", "yuv420p"],
                 ["-c:a", "libopus"]),
    "av1.mkv": (640, 360, "30", 10, ["-c:v", "libsvtav1", "-preset", "10", "-crf", "35", "-g", "60",
                                     "-pix_fmt", "yuv420p"], ["-c:a", "libopus"]),
    "short-3s.mp4": (640, 360, "30", 3, H264 + ["-g", "90"], ["-c:a", "aac"]),
    # What an iPhone records: HEVC in a .mov, tagged hvc1 (needs gst-libav).
    "hevc-iphone-1080p.mov": (1920, 1080, "30", 20, ["-c:v", "libx265", "-preset", "ultrafast", "-crf", "24",
                                                     "-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-g", "60",
                                                     "-x265-params", "log-level=error"],
                              ["-c:a", "aac", "-b:a", "128k"]),
}
AUDIO = {
    "tone-60s.mp3": ["-c:a", "libmp3lame", "-q:a", "4"],
    "tone-60s.m4a": ["-c:a", "aac", "-b:a", "128k"],
    "tone-60s.flac": ["-c:a", "flac"],
    "tone-60s.ogg": ["-c:a", "libvorbis", "-q:a", "4"],
    "tone-60s.opus": ["-c:a", "libopus"],
    "tone-60s.wav": ["-c:a", "pcm_s16le"],
}


def build():
    os.makedirs(DIR, exist_ok=True)
    lum = f"16+219*mod(floor(N/pow(2,floor(X/{BLOCK}))),2)"
    for name, (w, h, fps, secs, vargs, aargs) in VIDEOS.items():
        out = os.path.join(DIR, name)
        if os.path.exists(out):
            continue
        print("building", name, flush=True)
        argv = ["ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={fps}:d={secs}",
                "-f", "lavfi", "-i", f"color=c=gray:s={BITS * BLOCK}x{BLOCK}:r={fps}:d={secs},format=yuv420p,"
                                     f"geq=lum='{lum}':cb=128:cr=128"]
        if aargs:
            argv += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}"]
        argv += ["-filter_complex", "[0][1]overlay=0:0:shortest=1[v]", "-map", "[v]"]
        if aargs:
            argv += ["-map", "2"] + aargs
        argv += vargs + [out + ".part" + os.path.splitext(out)[1]]
        subprocess.run(argv, check=True)
        os.replace(out + ".part" + os.path.splitext(out)[1], out)
    for name, aargs in AUDIO.items():
        out = os.path.join(DIR, name)
        if os.path.exists(out):
            continue
        print("building", name, flush=True)
        part = out + ".part" + os.path.splitext(out)[1]
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=60"]
                       + aargs + [part], check=True)
        os.replace(part, out)
    return DIR


_COUNTS = {}


def frame_count(name: str) -> int:
    """Frames actually in the file (ffprobe counts the video packets)."""
    if name not in _COUNTS:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                              "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0",
                              os.path.join(DIR, name)], capture_output=True, text=True)
        _COUNTS[name] = int(out.stdout.strip())
    return _COUNTS[name]


def gop(name: str) -> int:
    vargs = VIDEOS[name][4]
    return int(vargs[vargs.index("-g") + 1]) if "-g" in vargs else 250


def frame_number(rgba: bytes, stride: int, bpp: int = 4) -> int | None:
    """Read the frame number from the block strip (RGBA, or gray with bpp=1)."""
    n = 0
    y = BLOCK // 2
    for k in range(BITS):
        x = k * BLOCK + BLOCK // 2
        v = rgba[y * stride + x * bpp]
        if 70 < v < 185:
            return None  # not a clean block: not one of our frames
        if v >= 128:
            n |= 1 << k
    return n


if __name__ == "__main__":
    print(build())
    sys.exit(0)
