"""Playback smoothness: play each test video for a few seconds and count the
frames the player delivers against the frames that should have arrived.

It measures the player, not the screen: offscreen Broadway renders every
frame on the CPU (a real desktop does it on the GPU), which makes on-screen
counts noisy without saying anything about Seerstone.

    GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 python3 tests/playback.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

import make_media  # noqa: E402
from seerstone.player import Player  # noqa: E402

PLAY_SECONDS = 5
MIN_SHARE = 0.95  # at least this share of frames must be shown

FILES = ["hevc-iphone-1080p.mov", "h264-high-1080p-60s.mp4", "h264-60fps-720p.mp4", "h264-portrait.mp4", "h264-29.97fps.mp4",
         "vp9.webm", "av1.mkv"]


def main():
    media_dir = make_media.build()
    Gtk.init()
    win = Gtk.Window()
    win.set_default_size(1000, 700)
    pic = Gtk.Picture()
    win.set_child(pic)
    win.present()
    loop = GLib.MainLoop()
    results = []

    def run(i=0):
        if i >= len(FILES):
            loop.quit()
            return False
        name = FILES[i]
        p = Player(os.path.join(media_dir, name), True)
        shown = [0]
        p.connect("invalidate-contents", lambda *_a: shown.__setitem__(0, shown[0] + 1))

        def start():
            p.seek(2_000_000)
            GLib.timeout_add(600, go)
            return False

        def go():
            shown[0] = 0
            t0 = time.monotonic()
            p.play()

            def stop():
                secs = time.monotonic() - t0
                fps = p.fps or 30
                share = shown[0] / (fps * secs)
                results.append((name, fps, shown[0], secs, share))
                print(f"{name:26s} {fps:5.2f} fps  delivered {shown[0]:4d} frames in {secs:.1f}s = {share * 100:5.1f}%"
                      f"  {'ok' if share >= MIN_SHARE else 'DROPPING'}", flush=True)
                p.clear()
                GLib.idle_add(run, i + 1)
                return False
            GLib.timeout_add(int(PLAY_SECONDS * 1000), stop)
            return False
        GLib.timeout_add(800, start)
        return False

    GLib.idle_add(run)
    loop.run()
    bad = [r for r in results if r[4] < MIN_SHARE]
    print(f"\n{len(results) - len(bad)}/{len(results)} play smoothly (>= {MIN_SHARE * 100:.0f}% of frames)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
