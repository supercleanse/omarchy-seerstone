"""Scrubbing torture test: drives Seerstone's real MediaView and checks, by
reading the frame number burned into each test frame, that the frame on screen
is exactly the one the scrubber asked for, and how fast it got there.

    gtk4-broadwayd :7 &
    GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 python3 tests/scrub.py [--quick] [--only NAME]

Media comes from tests/make_media.py (built on first run).
"""
from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import make_media  # noqa: E402
from seerstone.media import MediaView  # noqa: E402

FRAME_DEADLINE_MS = 2500   # the exact frame must be on screen within this (worst case: 1080p, 10 s keyframes, software decode)
SNAPPY_MS = 400            # and we report how many seeks beat this
PREVIEW_MS = 300           # something new must show within this after a click


class Runner:
    """Runs generator-based test steps on the GLib main loop."""

    def __init__(self):
        self.loop = GLib.MainLoop()
        self.results = []      # (test, ok, detail)
        self.latencies = {}    # file -> [ms]

    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok), detail))
        print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)

    def run(self, gen):
        def step(value=None):
            try:
                cmd = gen.send(value)
            except StopIteration:
                self.loop.quit()
                return False
            kind = cmd[0]
            if kind == "wait":
                GLib.timeout_add(cmd[1], step)
            elif kind == "until":
                cond, timeout_ms = cmd[1], cmd[2]
                start = time.monotonic()

                def poll():
                    try:
                        done = cond()
                    except Exception:  # noqa: BLE001
                        done = False
                    elapsed = (time.monotonic() - start) * 1000
                    if done or elapsed >= timeout_ms:
                        GLib.idle_add(step, (bool(done), elapsed))
                        return False
                    return True
                GLib.timeout_add(5, poll)
            return False
        GLib.idle_add(step)
        self.loop.run()


def fast_frame(view: MediaView):
    """Which frame is on screen, from the player's record of the frame's
    timestamp (cheap; polled). shown_frame() reads the pixels to confirm."""
    m = view.media
    if m is None or getattr(m, "frame_time", -1) < 0 or not view.fps:
        return None
    return int(m.frame_time * view.fps / 1_000_000 + 1e-3)  # a mid-frame seek stamps the frame with the asked-for time


def shown_frame(view: MediaView):
    m = view.media
    img = m.current_texture() if m is not None and hasattr(m, "current_texture") else None
    if img is None:
        return None
    d = Gdk.TextureDownloader.new(img)
    d.set_format(Gdk.MemoryFormat.R8G8B8A8)
    data, stride = d.download_bytes()
    return make_media.frame_number(data.get_data(), stride)


LAST_FRAME = {}


def player_state(view) -> str:
    m = view.media
    if m is None or not hasattr(m, "pipe"):
        return "no player"
    return (f"ts {m.timestamp} dur {m.duration} seeking {m._seeking} pending {m._pending} want {m._want_playing} "
            f"playing {m.playing} ended {m.ended} state {m.pipe.get_state(0)[1].value_nick} drained {m._drained} "
            f"recovering {m._recovering} decoder {m.decoder}")


def expected_frame(view: MediaView, us: int) -> int:
    fps = view.fps or 30.0
    last = LAST_FRAME.get(os.path.basename(view.path or ""), 10 ** 9)
    return min(last, int(us * fps / 1_000_000 + 1e-6))


def click(view: MediaView, fraction: float):
    """Exactly what a click on the scrubber does: Gtk.Scale emits change-value."""
    view.scale.emit("change-value", Gtk.ScrollType.JUMP, fraction)


def video_tests(r: Runner, view: MediaView, path: str, quick: bool):
    name = os.path.basename(path)
    LAST_FRAME[name] = make_media.frame_count(name) - 1
    lat = r.latencies.setdefault(name, [])
    rng = random.Random(hash(name) & 0xFFFF)

    # Open, and click the scrubber immediately (before the player is even up).
    t0 = time.monotonic()
    view.load(path, "video", autoplay=False)
    click(view, 0.5)
    done, _ = yield ("until", lambda: view.media is not None and view.media.is_prepared() and view.fps > 0, 8000)
    r.check(f"{name}: opens", done, f"{(time.monotonic() - t0) * 1000:.0f} ms")
    if not done:
        return
    dur = view.media.get_duration()
    exp = expected_frame(view, int(0.5 * dur))
    done, ms = yield ("until", lambda: fast_frame(view) == exp, 4000)
    r.check(f"{name}: a click made while it was still opening lands", done, f"want {exp}, showing {shown_frame(view)}")

    prev = r.latencies.setdefault(name + " (first picture)", [])

    def seek_and_check(label, target_us, use_click=True):
        before = shown_frame(view)
        if use_click:
            frac = target_us / dur
            click(view, frac)
            target_us = int(frac * dur)
        exp = expected_frame(view, min(target_us, dur - 1))
        if before != exp:
            seen, pms = yield ("until", lambda: fast_frame(view) != before, PREVIEW_MS * 4)
            prev.append(pms)
        done, ms = yield ("until", lambda: fast_frame(view) == exp, FRAME_DEADLINE_MS)
        lat.append(ms if done else FRAME_DEADLINE_MS)
        if done and shown_frame(view) != exp:  # the pixels must agree with the timestamp
            r.check(f"{name}: {label}: picture matches its timestamp", False,
                    f"timestamp says {exp}, pixels say {shown_frame(view)}")
            done = False
        if not done:
            r.check(f"{name}: {label}", False, f"want frame {exp}, showing {shown_frame(view)} after {ms:.0f} ms")
        return done

    # Random clicks while paused.
    n = 10 if quick else 40
    bad = 0
    for i in range(n):
        f = rng.uniform(0.0, 1.0)
        target = int(f * dur)
        # stay clear of an exact frame boundary, where "which frame" is a coin flip
        fps = view.fps
        if abs((target * fps / 1e6) % 1) < 0.02 or abs((target * fps / 1e6) % 1) > 0.98:
            target += int(0.3 * 1e6 / fps)
        ok = yield from seek_and_check(f"paused click #{i} at {target / 1e6:.3f}s", target)
        bad += 0 if ok else 1
    r.check(f"{name}: {n} paused clicks all land on the exact frame", bad == 0, f"{n - bad}/{n}")

    # The frames just before a keyframe. With open-GOP streams (HEVC as
    # phones record it) a plain exact seek there lands on the keyframe; the
    # player has to repair that. Click each of them, and step across one.
    g = make_media.gop(name)
    fps = view.fps
    misses = []
    for k in [g * i for i in range(1, 6) if g * i < LAST_FRAME[name]]:
        for want in (k - 2, k - 1):
            target = int((want + 0.4) * 1e6 / fps)
            click(view, target / dur)
            exp = expected_frame(view, int((target / dur) * dur))
            done, ms = yield ("until", lambda: fast_frame(view) == exp, FRAME_DEADLINE_MS)
            lat.append(ms if done else FRAME_DEADLINE_MS)
            if not (done and shown_frame(view) == exp):
                misses.append((exp, shown_frame(view)))
    r.check(f"{name}: clicks on the frames just before keyframes land exactly", not misses,
            f"missed {misses[:4]}" if misses else "")
    if g * 2 < LAST_FRAME[name]:
        k = g * 2
        click(view, int((k - 3 + 0.4) * 1e6 / fps) / dur)
        yield ("until", lambda: fast_frame(view) == k - 3, FRAME_DEADLINE_MS)
        seen = []
        for _ in range(5):
            view.step(1)
            yield ("until", lambda: fast_frame(view) == (seen[-1] if seen else k - 3) + 1, FRAME_DEADLINE_MS)
            seen.append(shown_frame(view))
        r.check(f"{name}: stepping forward across a keyframe hits every frame", seen == list(range(k - 2, k + 3)),
                f"from {k - 3}: {seen}")

    # Ends of the range.
    ok = yield from seek_and_check("click at the very start", 0)
    r.check(f"{name}: click at the very start shows frame 0", ok)
    ok = yield from seek_and_check("click at the very end", dur)
    r.check(f"{name}: click at the very end shows the last frame", ok, f"showing {shown_frame(view)}")

    # Drag storm: press, scrub fast back and forth, let go somewhere.
    for storm in range(2 if quick else 4):
        f = rng.uniform(0.1, 0.9)
        view.begin_scrub()
        for _ in range(60):
            f = min(0.98, max(0.0, f + rng.uniform(-0.05, 0.05)))
            click(view, f)
            yield ("wait", 16)
        # Finger still on the knob, holding still. GTK keeps re-reporting the
        # same position while the button is held, so do that too. (Room for
        # one seek in flight plus the last.)
        for _ in range(20):
            click(view, f)
            yield ("wait", 30)
        knob = expected_frame(view, int(f * dur))
        shown = shown_frame(view)
        g = make_media.gop(name)
        r.check(f"{name}: mid-drag, the picture tracks the knob (storm #{storm})",
                shown is not None and abs(shown - knob) <= g,
                f"knob at frame {knob}, showing {shown} (keyframes every {g})")
        final = rng.uniform(0.05, 0.95)
        click(view, final)
        view.end_scrub()
        exp = expected_frame(view, int(final * dur))
        done, ms = yield ("until", lambda: fast_frame(view) == exp, 2500)
        lat.append(ms if done else 2500)
        r.check(f"{name}: drag storm #{storm} settles on the frame where you let go", done,
                f"want {exp}, showing {shown_frame(view)}, {ms:.0f} ms" + ("" if done else "; " + player_state(view)))
        sv = view.scale.get_value()
        r.check(f"{name}: scrubber knob stays where you let go (storm #{storm})", abs(sv - final) < 0.01,
                f"knob {sv:.3f} vs {final:.3f}")

    # Click storm: many separate clicks (no drag) in quick succession; the last one wins.
    for storm in range(2 if quick else 3):
        for _ in range(25):
            click(view, rng.uniform(0, 1))
            yield ("wait", rng.choice((5, 20, 60)))
        final = rng.uniform(0.05, 0.95)
        click(view, final)
        exp = expected_frame(view, int(final * dur))
        done, ms = yield ("until", lambda: fast_frame(view) == exp, 2500)
        lat.append(ms if done else 2500)
        r.check(f"{name}: rapid clicks (#{storm}) end on the last one", done,
                f"want {exp}, showing {shown_frame(view)}, {ms:.0f} ms")

    # The nastiest case for openh264: decode to the very end (it then stops
    # producing frames for good), then hammer clicks. Each burst must still
    # end on the last click's exact frame.
    for burst in range(2 if quick else 4):
        click(view, 1.0)
        yield ("wait", rng.choice((50, 300)))
        for _ in range(15):
            click(view, rng.uniform(0, 1))
            yield ("wait", rng.choice((5, 20, 60)))
        final = rng.uniform(0.05, 0.9)
        click(view, final)
        exp = expected_frame(view, int(final * dur))
        done, ms = yield ("until", lambda: fast_frame(view) == exp, 3000)
        lat.append(ms if done else 3000)
        r.check(f"{name}: end, then a burst of clicks (#{burst}) lands on the last", done and shown_frame(view) == exp,
                f"want {exp}, showing {shown_frame(view)}, {ms:.0f} ms" + ("" if done else "; " + player_state(view)))

    # Arrow-key stepping mixed with clicks.
    click(view, 0.4)
    yield ("until", lambda: fast_frame(view) == expected_frame(view, int(0.4 * dur)), 2000)
    start = shown_frame(view)
    for _ in range(5):
        view.step(1)
        yield ("wait", 40)
    done, _ = yield ("until", lambda: fast_frame(view) == (start or 0) + 5, 2000)
    r.check(f"{name}: 5 single-frame steps after a click land 5 frames on", done,
            f"from {start} to {shown_frame(view)}")
    view.step(-3)
    done, _ = yield ("until", lambda: fast_frame(view) == (start or 0) + 2, 2000)
    r.check(f"{name}: stepping back 3 frames", done, f"showing {shown_frame(view)}")

    # Click while playing: keeps playing from the new spot.
    view.media.play()
    yield ("wait", 500)
    click(view, 0.25)
    exp = expected_frame(view, int(0.25 * dur))
    # (the clicked spot is behind where it was playing, so wait for a frame
    # just after it, not merely "at or past" it)
    done, _ = yield ("until", lambda: exp <= (fast_frame(view) or -1) < exp + view.fps, FRAME_DEADLINE_MS)
    yield ("wait", 600)
    fr = shown_frame(view)
    r.check(f"{name}: click while playing jumps there and keeps playing",
            done and view.media.get_playing() and fr is not None and exp < fr < exp + view.fps * 2,
            f"jumped to {exp}, now {fr}, playing {view.media.get_playing()}; {player_state(view)}")
    if not (done and view.media.get_playing()) and os.environ.get("SCRUB_DIAG"):
        t_fail = time.monotonic()
        landed, _ = yield ("until", lambda: not view.media._seeking, 8000)
        print(f"   DIAG: seek landed={landed} after {time.monotonic() - t_fail:.2f}s more; {player_state(view)}; "
              f"fed {view.media.decoder_input}", flush=True)
    view.media.pause()

    # Play to the end, then scrub: the player must not be stuck at end-of-stream.
    click(view, 0.995)
    yield ("wait", 300)
    view.media.play()
    done, _ = yield ("until", lambda: view.media.get_ended(), 5000)
    r.check(f"{name}: plays to the end", done, "" if done else player_state(view))
    ok = yield from seek_and_check("click after the video ended", int(0.3 * dur))
    r.check(f"{name}: click after the end shows the right frame", ok, f"showing {shown_frame(view)}")
    view.toggle_play()
    yield ("wait", 700)
    fr = shown_frame(view)
    exp = expected_frame(view, int(0.3 * dur))
    r.check(f"{name}: play after the end resumes from the clicked spot",
            view.media.get_playing() and fr is not None and exp < fr < exp + view.fps * 2,
            f"from {exp} to {fr}")
    view.media.pause()


def audio_tests(r: Runner, view: MediaView, path: str, quick: bool):
    name = os.path.basename(path)
    rng = random.Random(hash(name) & 0xFFFF)
    view.load(path, "audio", autoplay=False)
    click(view, 0.5)
    done, _ = yield ("until", lambda: view.media is not None and view.media.is_prepared()
                     and view.media.get_duration() > 0, 8000)
    r.check(f"{name}: opens", done)
    if not done:
        return
    dur = view.media.get_duration()
    done, _ = yield ("until", lambda: abs(view.media.get_timestamp() - dur / 2) < 100_000, 3000)
    r.check(f"{name}: a click made while it was still opening lands", done,
            f"{view.media.get_timestamp() / 1e6:.2f}s")

    def at(us, tol=100_000):
        return abs(view.media.get_timestamp() - us) < tol

    n = 6 if quick else 20
    good = 0
    for _ in range(n):
        f = rng.uniform(0, 0.98)
        click(view, f)
        done, _ = yield ("until", lambda: at(int(f * dur)), 1500)
        good += done
    r.check(f"{name}: {n} paused clicks report the clicked time", good == n, f"{good}/{n}")

    view.media.play()
    yield ("wait", 300)
    click(view, 0.6)
    yield ("wait", 800)
    ts = view.media.get_timestamp()
    r.check(f"{name}: click while playing keeps playing from there",
            view.media.get_playing() and 0.6 * dur + 300_000 < ts < 0.6 * dur + 2_000_000, f"{ts / 1e6:.2f}s")
    view.media.pause()

    for _ in range(60):
        click(view, rng.uniform(0, 0.98))
        yield ("wait", 16)
    click(view, 0.42)
    done, _ = yield ("until", lambda: at(int(0.42 * dur)), 2000)
    r.check(f"{name}: drag storm settles where you let go", done, f"{view.media.get_timestamp() / 1e6:.2f}s")

    click(view, 0.995)
    yield ("wait", 200)
    view.media.play()
    done, _ = yield ("until", lambda: view.media.get_ended(), 5000)
    r.check(f"{name}: plays to the end", done)
    click(view, 0.2)
    done, _ = yield ("until", lambda: at(int(0.2 * dur)), 1500)
    r.check(f"{name}: click after the end lands", done, f"{view.media.get_timestamp() / 1e6:.2f}s")
    view.toggle_play()
    yield ("wait", 700)
    ts = view.media.get_timestamp()
    r.check(f"{name}: play after the end resumes from the clicked spot",
            view.media.get_playing() and 0.2 * dur + 200_000 < ts < 0.2 * dur + 2_000_000, f"{ts / 1e6:.2f}s")
    view.media.pause()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", help="run only files whose name contains this")
    opts = ap.parse_args()
    media_dir = make_media.build()
    Gtk.init()
    win = Gtk.Window()
    win.set_default_size(1000, 700)
    view = MediaView()
    win.set_child(view)
    win.present()
    r = Runner()

    def all_tests():
        yield ("wait", 300)
        for name in sorted(make_media.VIDEOS):
            if opts.only and opts.only not in name:
                continue
            yield from video_tests(r, view, os.path.join(media_dir, name), opts.quick)
        for name in sorted(make_media.AUDIO):
            if opts.only and opts.only not in name:
                continue
            yield from audio_tests(r, view, os.path.join(media_dir, name), opts.quick)
        view.clear()

    r.run(all_tests())
    print()
    for name, lats in r.latencies.items():
        if lats:
            fast = sum(1 for x in lats if x <= SNAPPY_MS)
            print(f"{name:28s} seeks {len(lats):3d}  median {statistics.median(lats):6.0f} ms  "
                  f"worst {max(lats):6.0f} ms  under {SNAPPY_MS} ms: {fast}/{len(lats)}")
    failed = [x for x in r.results if not x[1]]
    print(f"\n{len(r.results) - len(failed)}/{len(r.results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
