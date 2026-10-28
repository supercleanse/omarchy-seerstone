"""Every video gets a YUV color state, even one whose file leaves the color
tags unset. Without a color state GTK reads the Y, Cb and Cr planes as if they
were RGB and the picture turns purple and green.

    python3 tests/color.py
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo  # noqa: E402

import make_media  # noqa: E402
from seerstone.player import Player  # noqa: E402

# Made the way ffmpeg turns a JPEG into video: full range and a BT.601 matrix,
# but no transfer or primaries tags. (A file with no tags at all is fine:
# GStreamer fills in every default itself.)
UNTAGGED = {
    "partly-tagged-hd.mp4": ["-vf", "format=yuvj420p", "-colorspace", "bt470bg", "-color_range", "pc",
                             "-c:v", "libx264", "-preset", "veryfast"],
    "partly-tagged-sd.mp4": ["-vf", "scale=640:360,format=yuv420p", "-colorspace", "bt470bg",
                             "-c:v", "libx264", "-preset", "veryfast"],
}


def first_frame_info(path: str):
    pipe = Gst.parse_launch(f'filesrc location="{path}" ! decodebin ! videoconvert ! '
                            "video/x-raw,format=I420 ! appsink name=s sync=false max-buffers=1")
    pipe.set_state(Gst.State.PAUSED)
    pipe.get_state(Gst.CLOCK_TIME_NONE)
    sample = pipe.get_by_name("s").emit("pull-preroll")
    info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
    pipe.set_state(Gst.State.NULL)
    return info


def main():
    Gst.init(None)
    media = make_media.build()
    for name, args in UNTAGGED.items():
        out = os.path.join(media, name)
        if not os.path.exists(out):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=2"]
                           + args + [out], check=True)
    names = sorted(n for n in os.listdir(media) if n.endswith((".mp4", ".mov", ".mkv", ".webm")))
    bad = []
    for name in names:
        info = first_frame_info(os.path.join(media, name))
        c = info.colorimetry
        p = Player.__new__(Player)
        p._color_key = p._color = None
        cs = Player._color_state(p, info)
        tags = f"{c.matrix.value_nick}/{c.transfer.value_nick}/{c.primaries.value_nick}/{c.range.value_nick}"
        print(f"{name:28s} {tags:36s} {'ok' if cs is not None else 'NO COLOR STATE'}")
        if cs is None:
            bad.append(name)
    print(f"\n{len(names) - len(bad)}/{len(names)} videos get a color state")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
