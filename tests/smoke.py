"""Offscreen smoke test: drives a real Seerstone window on a Broadway display.

    gtk4-broadwayd :7 &
    GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 python3 tests/smoke.py

Builds its own fixture folder with ffmpeg/magick in a temp dir, so it never
touches real files or Seerstone's saved state.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi  # noqa: E402

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib  # noqa: E402

from seerstone import app as A  # noqa: E402


def make_fixtures(d):
    def sh(*a):
        subprocess.run(a, check=True, capture_output=True)
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x180:rate=15", "-f", "lavfi",
       "-i", "sine=frequency=440:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
       "-shortest", f"{d}/a-clip.mp4")
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=2", f"{d}/b-tone.mp3")
    sh("magick", "-size", "400x200", "gradient:navy-orange", f"{d}/c-wide.jpg")
    sh("magick", "-size", "300x100", "xc:teal", f"{d}/d-flat.png")
    sh("magick", "-size", "300x400", "xc:white", "-size", "300x400", "xc:gray", f"{d}/e-doc.pdf")
    with open(f"{d}/notes.txt", "w") as fh:
        fh.write("not media")


def dims(path):
    out = subprocess.run(["magick", "identify", "-format", "%w %h\n", path], capture_output=True, text=True)
    w, h = out.stdout.split("\n")[0].split()
    return int(w), int(h)


def pdf_rotation(path):
    out = subprocess.run(["qpdf", "--json", "--json-key=pages", path], capture_output=True, text=True)
    pages = json.loads(out.stdout)["pages"]
    return [p.get("rotate", 0) for p in pages] if pages and "rotate" in pages[0] else out.stdout


def main():
    tmp = tempfile.mkdtemp(prefix="seerstone-smoke-")
    make_fixtures(tmp)
    opts = argparse.Namespace(path=tmp, render_png=os.path.join(tmp, ".unused.png"), size="1000x700",
                              select=None, rotate=0, zoom=None, page=0, toast=None, render_delay="999999",
                              omarchy_theme=None, dock="left", notes_open=False)
    app = A.App(opts)
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)

    steps = []

    def step(fn, wait=700):
        steps.append((fn, wait))

    def settle(cond, timeout_ms=4000):
        """Wait until cond() holds (Broadway lays out lazily offscreen) before the next step."""
        def waiter():
            pass
        waiter.cond, waiter.timeout = cond, timeout_ms
        steps.append((waiter, 0))

    def run_next():
        if not steps:
            app.quit()
            return False
        fn, wait = steps.pop(0)
        if hasattr(fn, "cond"):
            def poll(left=[fn.timeout]):
                try:
                    done = fn.cond()
                except Exception:  # noqa: BLE001
                    done = False
                left[0] -= 100
                if done or left[0] <= 0:
                    GLib.idle_add(run_next)
                    return False
                return True
            GLib.timeout_add(100, poll)
            return False
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            check(f"{fn.__name__} raised", False, repr(e))
        GLib.timeout_add(wait, run_next)
        return False

    def win():
        return app.win

    def names():
        return [win().store.get_item(i).name for i in range(win().store.get_n_items())]

    def t_listing():
        check("lists only media, naturally sorted", names() == ["a-clip.mp4", "b-tone.mp3", "c-wide.jpg",
                                                              "d-flat.png", "e-doc.pdf"], names())
        check("first file selected", win().selection.get_selected() == 0)

    def t_keys():
        w = win()
        w._key(None, Gdk.KEY_Down, 0, 0)  # from a video: Down goes to the next file
        w._key(None, Gdk.KEY_Down, 0, 0)
        check("Down goes next", w.selection.get_selected() == 2, w.selection.get_selected())
        w._key(None, Gdk.KEY_Right, 0, 0)  # on an image, Right is next file too
        check("Right goes next on images", w.selection.get_selected() == 3, w.selection.get_selected())
        w._key(None, Gdk.KEY_Left, 0, 0)
        w._key(None, Gdk.KEY_Up, 0, 0)
        check("Left/Up go back", w.selection.get_selected() == 1)
        w._key(None, Gdk.KEY_End, 0, 0)
        check("End jumps to last", w.selection.get_selected() == 4)
        w._key(None, Gdk.KEY_Right, 0, 0)
        check("Right at the end stays put", w.selection.get_selected() == 4)

    def t_audio_plays():
        w = win()
        w.select(1)

    def t_audio_check():
        m = win().media_view.media
        check("mp3 prepared without crashing", m is not None and m.is_prepared(),
              m.get_duration() if m else None)
        win().media_view.toggle_play()

    def t_audio_playing():
        m = win().media_view.media
        check("mp3 timestamp advances", m is not None and m.get_timestamp() > 0, m.get_timestamp() if m else None)
        win().media_view.toggle_play()

    def t_video():
        win().select(0)

    def t_video_check():
        w, h = win().media_view.video_size()
        check("video prepared with its size", (w, h) == (320, 180), (w, h))

    def t_rotate():
        w = win()
        w.select(2)

    def t_rotate2():
        w = win()
        w._key(None, Gdk.KEY_r, 0, 0)
        check("R rotates 90 clockwise", w.image_view.rotation == 90)
        check("Save button shows", w.save_btn.get_visible())
        w._key(None, Gdk.KEY_R, 0, Gdk.ModifierType.SHIFT_MASK)
        w._key(None, Gdk.KEY_R, 0, Gdk.ModifierType.SHIFT_MASK)
        check("Shift+R rotates counterclockwise", w.image_view.rotation == 270)
        check("rotation snaps to 90s", w.image_view.rotation % 90 == 0)
        w.save_rotation()

    def t_saved():
        p = os.path.join(tmp, "c-wide.jpg")
        check("saved rotation swaps dimensions", dims(p) == (200, 400), dims(p))
        check("rotation resets after save", win().image_view.rotation == 0)
        check("no temp file left", not any(".seerstone-tmp" in n for n in os.listdir(tmp)))
        win().toast._act()  # Undo

    def t_undone():
        p = os.path.join(tmp, "c-wide.jpg")
        check("undo restores the original", dims(p) == (400, 200), dims(p))

    def t_nav_keeps_rotation():
        w = win()
        w.select(3)

    def t_nav2():
        w = win()
        w.rotate(1)
        w.select(2)

    def t_nav3():
        w = win()
        w.select(3)

    def t_nav4():
        check("unsaved rotation kept when coming back", win().image_view.rotation == 90)

    def t_zoom():
        iv = win().image_view
        iv.zoom_fit()
        fit = iv.zoom_percent()
        iv.zoom_in()
        check("zoom in grows", iv.zoom_percent() > fit, (fit, iv.zoom_percent()))
        iv.zoom_out()
        check("zooming back out lands on fit", iv.is_fit(), iv.zoom_percent())
        iv.zoom_out()
        check("zoom out below fit is allowed", not iv.is_fit() and iv.zoom_percent() < fit)
        iv.zoom_actual()
        check("1 = 100%", iv.zoom_percent() == 100)
        iv.zoom_fit()

    def t_resize_a():
        w = win()
        w.select(2)  # c-wide.jpg, 400x200, fit by default

    def t_resize_b():
        w = win()
        iv = w.image_view
        t_resize_b.before = (iv.is_fit(), iv.zoom_percent(), iv.get_width())
        w.paned.set_position(w.paned.get_position() + 250)  # same as dragging the sidebar wider

    def t_resize_c():
        w = win()
        iv = w.image_view
        fit0, pct0, width0 = t_resize_b.before
        check("images open fit-to-frame", fit0)
        check("dragging the sidebar narrows the stage", iv.get_width() < width0, (width0, iv.get_width()))
        check("image re-fits when the frame shrinks", iv.is_fit() and iv.zoom_percent() < pct0,
              (pct0, iv.zoom_percent()))
        check("zoom label follows the resize", w.zoom_label.get_label() == f"Fit {iv.zoom_percent()}%",
              w.zoom_label.get_label())
        w.paned.set_position(w.paned.get_position() - 250)

    def t_resize_d():
        iv = win().image_view
        fit0, pct0, _w = t_resize_b.before
        check("image re-fits when the frame grows back", iv.zoom_percent() == pct0, (pct0, iv.zoom_percent()))

    def t_video_resize_a():
        win().select(0)

    def t_video_resize_b():
        pic = win().media_view.picture
        t_video_resize_b.before = pic.get_width()
        check("video fills the frame letterboxed",
              pic.get_content_fit() == A.Gtk.ContentFit.CONTAIN and pic.get_can_shrink())
        win().paned.set_position(win().paned.get_position() + 250)

    def t_video_resize_c():
        pic = win().media_view.picture
        check("video area shrinks with the frame", pic.get_width() < t_video_resize_b.before,
              (t_video_resize_b.before, pic.get_width()))
        win().paned.set_position(win().paned.get_position() - 250)

    def t_sidebar_hide():
        w = win()
        t_sidebar_hide.before = w.image_view.get_width()
        w._key(None, Gdk.KEY_F9, 0, 0)

    def t_sidebar_hidden():
        w = win()
        check("F9 hides the sidebar", not w.sidebar.get_visible() and not w.sidebar_btn.get_active())
        check("stage takes the freed room", w.image_view.get_width() > t_sidebar_hide.before,
              (t_sidebar_hide.before, w.image_view.get_width()))
        w._key(None, Gdk.KEY_Right, 0, 0)
        check("arrows still navigate with the sidebar hidden", w.selection.get_selected() == 3)
        w._key(None, Gdk.KEY_Left, 0, 0)
        w.sidebar_btn.set_active(True)

    def t_sidebar_shown():
        check("toggle button brings the sidebar back", win().sidebar.get_visible())

    def t_narrow_ok():
        w = win()
        mn = w.main.measure(A.Gtk.Orientation.HORIZONTAL, -1)[0]
        check("stage can get narrow (controls never force the window wider)", mn <= 260, mn)

    def side_size(w):
        """The sidebar size the splitter has been set to. (Offscreen Broadway
        reallocates widgets lazily, so get_width() can lag behind it.)"""
        pos = w.paned.get_position()
        return pos if w.dock in ("left", "top") else w._paned_extent() - pos - A.HANDLE

    def make_dock_tests(dock):
        def switch():
            win().activate_action("win.dock", GLib.Variant.new_string(dock))

        def verify():
            w = win()
            H = A.Gtk.Orientation.HORIZONTAL
            side_view = dock in ("left", "right")
            first = dock in ("left", "top")
            p = w.paned
            ok_layout = ((p.get_start_child() is w.sidebar) == first
                         and (p.get_orientation() == H) == side_view
                         and (w.listview.get_orientation() == H) != side_view)
            check(f"dock {dock}: layout", ok_layout)
            size = side_size(w)
            want = A.DOCK_DEFAULT_SIZE[dock] if dock not in w.sidebar_sizes else w.sidebar_sizes[dock]
            check(f"dock {dock}: sidebar at its size", abs(size - want) <= 2, (size, want))
            make_dock_tests.size = size
            delta = 60 if side_view else 30
            p.set_position(p.get_position() + (delta if first else -delta))  # drag it bigger

        def dragged():
            w = win()
            size = side_size(w)
            check(f"dock {dock}: dragging resizes the sidebar", size > make_dock_tests.size,
                  (make_dock_tests.size, size))
            check(f"dock {dock}: image still fits the frame", w.image_view.is_fit())
            w._key(None, Gdk.KEY_Up, 0, 0)
            w._key(None, Gdk.KEY_Down, 0, 0)
            check(f"dock {dock}: arrows navigate", w.selection.get_selected() == 2, w.selection.get_selected())
        return switch, verify, dragged

    def t_copy():
        w = win()
        w.copy_image()
        fmts = w.get_clipboard().get_formats().to_string()
        check("copy image puts a texture on the clipboard", "GdkTexture" in fmts or "image/png" in fmts, fmts)
        w.copy_file()
        fmts = w.get_clipboard().get_formats().to_string()
        check("copy file offers a uri list", "text/uri-list" in fmts, fmts)

    # ---------- notes + frame stepping ----------
    from seerstone import notes as N

    def t_notes_format():
        text = """# Notes on x.mp4

<!-- format comment
spanning lines -->

- [ ] 00:01:23.400 (frame 2502): Logo too early.
- [x] 0:05: Done one
  continued here
- [ ] page 3: Typo
- [ ] Whole file note
Loose line becomes a note
"""
        ns = N.parse(text)
        check("notes parse: count", len(ns) == 5, len(ns))
        check("notes parse: time + frame", ns[0].time_us == 83_400_000 and ns[0].frame == 2502 and not ns[0].done)
        check("notes parse: short time, done, continuation",
              ns[1].time_us == 5_000_000 and ns[1].done and ns[1].text == "Done one continued here", ns[1])
        check("notes parse: page", ns[2].page == 3 and ns[2].text == "Typo")
        check("notes parse: whole-file notes", ns[3].time_us is None and ns[4].text == "Loose line becomes a note")
        again = N.parse(N.render("x.mp4", ns))
        check("notes render/parse round-trips",
              [(n.time_us, n.frame, n.page, n.done, n.text) for n in sorted(ns, key=N.Note.sort_key)]
              == [(n.time_us, n.frame, n.page, n.done, n.text) for n in again])

    def t_video_select():
        win().select(0)  # a-clip.mp4, 15 fps

    def t_frame_step():
        w = win()
        check("fps probed", abs(w.media_view.fps - 15) < 0.01, w.media_view.fps)
        file_before = w.selection.get_selected()
        w._hold_dir = 0
        w._key(None, Gdk.KEY_Right, 0, 0)
        w._hold_dir = 0  # key released
        check("Right on video stays on the file", w.selection.get_selected() == file_before)
        check("Right steps exactly one frame", w.media_view.frame_at(w.media_view.position_us()) == 1,
              w.media_view.position_us())
        w._key(None, Gdk.KEY_Right, 0, 0)
        w._hold_dir = 0
        w._key(None, Gdk.KEY_Left, 0, 0)
        w._hold_dir = 0
        check("Left steps back one frame", w.media_view.frame_at(w.media_view.position_us()) == 1)
        check("stepping pauses playback", not w.media_view.media.get_playing())

    def t_frame_step_landed():
        m = win().media_view.media
        check("the player actually lands on that frame", round(m.get_timestamp() * 15 / 1e6) == 1,
              m.get_timestamp())

    def t_hold_accelerates():
        w = win()
        now = GLib.get_monotonic_time()
        w._hold_dir, w._hold_start, w._hold_last = 1, now - 1_500_000, now  # held for 1.5 s
        before = w.media_view.frame_at(w.media_view.position_us())
        w._scrub(1)
        jumped = w.media_view.frame_at(w.media_view.position_us()) - before
        expect = min(A.HOLD_MAX_UNITS, int(1 + ((1.5 - A.HOLD_DELAY) * A.HOLD_RAMP) ** 2))
        check("holding the arrow jumps further each repeat", jumped == min(expect, 44 - before), (jumped, expect))
        w._hold_dir = 0

    def t_updown_files():
        w = win()
        w._key(None, Gdk.KEY_Down, 0, 0)
        check("Down moves to the next file on video", w.selection.get_selected() == 1)
        w._key(None, Gdk.KEY_Up, 0, 0)

    def t_note_add():
        w = win()
        w.media_view.seek_to(int(2 * 1_000_000 / 15))  # frame 2
        t_note_add.media = w.media_view.media

    def t_note_add2():
        w = win()
        w._key(None, Gdk.KEY_n, 0, 0)
        check("N opens notes and focuses the box", w.notes_revealer.get_reveal_child() and w._typing())
        w._key(None, Gdk.KEY_Right, 0, 0)
        check("arrows type, not scrub, while in the note box",
              w.media_view.frame_at(w.media_view.position_us()) == 2)
        w.notes_panel.entry.set_text("Logo too early")
        w.notes_panel.entry.emit("activate")
        w.notes_panel.entry.set_text("Second note same frame")
        w.notes_panel.entry.emit("activate")
        w._key(None, Gdk.KEY_Escape, 0, 0)
        check("Esc leaves the note box", not w._typing())

    def t_note_refocus():
        w = win()
        w.notes_panel.entry.grab_focus()  # click back into the note box after adding notes

    def t_note_refocus2():
        w = win()
        f = w.get_focus()
        check("the note box keeps focus after adding a note",
              isinstance(f, A.Gtk.Text) and f.get_ancestor(A.Gtk.Entry) is w.notes_panel.entry,
              type(f).__name__ if f else None)
        w.notes_panel.entry.set_text("Third note")
        w.notes_panel.entry.emit("activate")
        w.set_focus(None)
        check("a further note can be added", len(w.notes) == 3, len(w.notes))
        w.notes_panel._delete(next(n for n in w.notes if n.text == "Third note"))

    def t_note_moved():
        w = win()  # the note box still has focus; move the playhead with the mouse (scrub bar)
        w.media_view.seek_to(int(30 * 1_000_000 / 15))  # frame 30

    def t_note_moved2():
        w = win()
        check("the stamp follows the playhead before you type",
              "frame 30" in w.notes_panel.anchor_label.get_text(), w.notes_panel.anchor_label.get_text())
        w.notes_panel.entry.set_text("Somewhere else")
        w.media_view.seek_to(int(40 * 1_000_000 / 15))  # playback moves on while typing
        w.notes_panel.entry.emit("activate")
        n = next(n for n in w.notes if n.text == "Somewhere else")
        check("a note after moving the playhead lands at the new spot", n.frame == 30, n.frame)
        fr = sorted(round(m[0], 3) for m in w.media_view.lane.markers)
        check("its marker shows up right away, apart from the others", len(set(fr)) == 2 and len(fr) == 3, fr)
        w.notes_panel._delete(n)
        w.media_view.seek_to(int(2 * 1_000_000 / 15))

    def t_note_many():
        w = win()
        w.notes_panel.set_height(140)  # a short drawer, so the list has to scroll
        t_note_many.added = []
        for i in range(12):
            w.media_view._scrub_target = int((3 + i) * 1_000_000 / 15)  # as if the playhead moved on
            w.media_view._scrub_time = GLib.get_monotonic_time()
            w.notes_panel.entry.set_text(f"Batch note {i}")
            w.notes_panel.entry.emit("activate")
            t_note_many.added.append(f"Batch note {i}")

    def _at_bottom():
        adj = win().notes_panel.list_scroll.get_vadjustment()
        return adj.get_upper() > adj.get_page_size() and abs(adj.get_value() - (adj.get_upper() - adj.get_page_size())) < 1

    def t_note_many2():
        w = win()
        adj = w.notes_panel.list_scroll.get_vadjustment()
        check("after Enter the list scrolls to the bottom", _at_bottom(),
              (adj.get_value(), adj.get_upper(), adj.get_page_size()))
        check("...where the newest note is", w.notes_panel.notes[-1].text == "Batch note 11")
        adj.set_value(0)  # you can still scroll back up

    def t_note_many3():
        adj = win().notes_panel.list_scroll.get_vadjustment()
        check("manual scrolling isn't fought afterward", adj.get_value() == 0, adj.get_value())
        w = win()
        for n in [n for n in w.notes if n.text.startswith("Batch note")]:
            w.notes.remove(n)
        w.notes_panel._changed()
        w.notes_panel.set_height(220)

    def _row_bottom_gap(note_text):
        """Pixels between a note's bottom edge and the bottom of the visible list (0 = right above the box)."""
        w = win()
        panel = w.notes_panel
        i = next(i for i, n in enumerate(panel.notes) if n.text == note_text)
        row = panel._rows[i]
        ok, box = row.compute_bounds(panel.list_scroll)  # full box, padding and border included
        return panel.list_scroll.get_height() - (box.get_y() + box.get_height()) if ok else 9999

    def t_note_chat_a():
        w = win()
        w.notes_panel.set_height(320)  # tall drawer, few notes

    def t_note_chat_b():
        gap = _row_bottom_gap(win().notes_panel.notes[-1].text)
        check("few notes stack against the note box (no gap)", abs(gap) <= 2, gap)

    def t_note_mid_a():
        w = win()
        w.notes_panel.set_height(140)
        for i in range(10):
            w.media_view._scrub_target = int((20 + i) * 1_000_000 / 15)
            w.media_view._scrub_time = GLib.get_monotonic_time()
            w.notes_panel.entry.set_text(f"Late note {i}")
            w.notes_panel.entry.emit("activate")
        w.media_view._scrub_target = int(10 * 1_000_000 / 15)  # back up the timeline
        w.media_view._scrub_time = GLib.get_monotonic_time()
        w.notes_panel.entry.set_text("Earlier note")
        w.notes_panel.entry.emit("activate")

    def t_note_mid_b():
        gap = _row_bottom_gap("Earlier note")
        check("a note added mid-timeline lands right above the note box", abs(gap) <= 2, gap)
        w = win()
        for n in [n for n in w.notes if n.text.startswith("Late note") or n.text == "Earlier note"]:
            w.notes.remove(n)
        w.notes_panel._changed()
        w.notes_panel.set_height(220)

    def t_note_saved():
        w = win()
        path = os.path.join(tmp, "a-clip.mp4.notes.md")
        body = open(path).read() if os.path.exists(path) else ""
        check("note saved beside the video", "- [ ] 00:00:00.133 (frame 2): Logo too early" in body, body)
        check("second note at the same moment", "(frame 2): Second note same frame" in body)
        check("markers on the scrub bar", len(w.media_view.lane.markers) == 2, len(w.media_view.lane.markers))
        check("saving notes doesn't reload the video", w.media_view.media is t_note_add.media)
        check("notes file isn't listed as media", not any(n.endswith(".notes.md") for n in names()))
        box = w._row_boxes.get(os.path.join(tmp, "a-clip.mp4"))
        check("sidebar marks the file as having notes", box is not None and box.get_last_child().get_visible())
        check("copy for AI includes the full path", f"# Notes on {os.path.join(tmp, 'a-clip.mp4')}" in w.notes_for_ai())

    def t_note_jump():
        w = win()
        w.media_view.seek_to(2_000_000)

    def t_note_jump2():
        w = win()
        w.media_view.emit("marker-activated", 0)
        check("clicking a marker jumps to its frame",
              w.media_view.frame_at(w.media_view.position_us()) == 2, w.media_view.position_us())

    def t_note_external():
        path = os.path.join(tmp, "a-clip.mp4.notes.md")
        with open(path, "a") as fh:
            fh.write("- [x] 00:00:02.000 (frame 30): Fixed the color (AI)\n")

    def t_note_external2():
        w = win()
        check("edits made outside Seerstone show up", any("(AI)" in n.text for n in w.notes), len(w.notes))
        check("...including their markers", len(w.media_view.lane.markers) == 3)

    def t_note_pdf():
        w = win()
        w.select(4)  # e-doc.pdf

    def t_note_pdf2():
        w = win()
        w.page(1)
        w.new_note()
        w.notes_panel.entry.set_text("Typo here")
        w.notes_panel.entry.emit("activate")
        w.set_focus(None)
        body = open(os.path.join(tmp, "e-doc.pdf.notes.md")).read()
        check("PDF note records the page", "- [ ] page 2: Typo here" in body, body)

    def t_note_image():
        w = win()
        w.select(2)

    def t_note_image2():
        w = win()
        w.new_note()
        w.notes_panel.entry.set_text("Make it warmer")
        w.notes_panel.entry.emit("activate")
        w.set_focus(None)
        w._key(None, Gdk.KEY_Right, 0, 0)
        check("arrows browse files again after a note", w.selection.get_selected() == 3)
        body = open(os.path.join(tmp, "c-wide.jpg.notes.md")).read()
        check("image note is a whole-file note", "- [ ] Make it warmer" in body, body)
        w.select(2)

    def t_notes_drawer_open():
        w = win()
        t_notes_drawer_open.stage = w.stack.get_height()
        w.set_notes_open(True)

    def t_notes_drawer_open2():
        w = win()
        r = w.notes_revealer
        check("notes drawer sits between the stage and the toolbar",
              r.get_prev_sibling() is w.stack.get_parent() and r.get_next_sibling().get_child() is not None)
        check("open drawer is its set height", abs(r.get_height() - w.notes_panel.height) <= 2,
              (r.get_height(), w.notes_panel.height))
        check("open drawer spans the media pane", r.get_width() == w.main.get_width(), (r.get_width(), w.main.get_width()))
        w.notes_panel.set_height(w.notes_panel.height + 60)  # what dragging the grip does
        t_notes_drawer_open2.h = w.notes_panel.height

    def t_notes_drawer_resized():
        w = win()
        check("dragging the grip resizes the drawer", abs(w.notes_revealer.get_height() - t_notes_drawer_open2.h) <= 2,
              (w.notes_revealer.get_height(), t_notes_drawer_open2.h))
        w.set_notes_open(False)

    def t_notes_drawer_closed():
        w = win()
        check("closed drawer takes no room", w.notes_revealer.get_height() == 0, w.notes_revealer.get_height())
        check("stage gets the full height back", w.stack.get_height() == t_notes_drawer_open.stage,
              (w.stack.get_height(), t_notes_drawer_open.stage))

    def make_full_width(index, label):
        def go():
            w = win()
            w.set_notes_open(False)
            w.select(index)

        def check_it():
            w = win()
            iv = w.image_view
            check(f"{label} uses the pane's full width", w.stack.get_width() == w.main.get_width()
                  and iv.get_width() == w.main.get_width(), (iv.get_width(), w.main.get_width()))
            rw, rh = iv.canvas.rotated_logical()
            fit = min(iv.get_width() / rw, iv.get_height() / rh)
            check(f"{label} fits the frame", abs(iv._display_scale() - fit) < 1e-6, (iv._display_scale(), fit))
        return go, check_it

    def t_note_delete():
        w = win()
        w.notes_panel._delete(w.notes_panel.notes[0])
        check("deleting the last note removes the file", not os.path.exists(os.path.join(tmp, "c-wide.jpg.notes.md")))
        w.set_notes_open(False)

    def t_recent_open_other():
        other = os.path.join(tmp, "other")
        os.makedirs(other, exist_ok=True)
        subprocess.run(["magick", "-size", "20x20", "xc:blue", os.path.join(other, "blue.png")], check=True)
        win().open_path(other)

    def t_recent_back():
        w = win()
        check("recent lists newest first", w.recent[:2] == [os.path.join(tmp, "other"), tmp], w.recent)
        labels = [w.recent_menu.get_item_link(0, "section").get_item_attribute_value(i, "label", None).get_string()
                  for i in range(w.recent_menu.get_item_link(0, "section").get_n_items())]
        check("Open Recent submenu shows the folders", len(labels) == 2 and labels[0].startswith("other"), labels)
        w.activate_action("win.open-recent", GLib.Variant.new_string(tmp))

    def t_recent_check():
        w = win()
        check("choosing a recent folder opens it", w.folder == tmp and "a-clip.mp4" in names(), w.folder)
        check("...and moves it to the top", w.recent[0] == tmp)
        dup = len(w.recent) == len(set(w.recent))
        check("recent has no duplicates", dup, w.recent)
        w.activate_action("win.clear-recent", None)
        check("Clear Recent empties it", w.recent == [])

    exports = [
        (3, "jpg", "jpg", "out-flat-rot.jpg"),
        (2, "webp", "webp", "out-wide.webp"),
        (4, "pdf", "pdf", "out-doc-rot.pdf"),
        (4, "png", "png", "out-page2.png"),
        (1, "ogg", "ogg", "out-tone.ogg"),
        (0, "webm", "webm", "out-clip.webm"),
        (0, "gif", "gif", "out-clip.gif"),
        (0, "mp3", "mp3", "out-clip.mp3"),
    ]

    def make_export(i, fmt, ext, name):
        def go():
            w = win()
            w.select(i)

        def run():
            w = win()
            item = w.store.get_item(i)
            rot, page = 0, 0
            if name == "out-doc-rot.pdf":
                w.rotate(1)
                rot = 90
            if name == "out-page2.png":
                page = 1
            if name == "out-flat-rot.jpg":
                rot = w.image_view.rotation
            w._run_export(item, item.kind, fmt, os.path.join(tmp, "out", name), rot, page)
        return go, run

    os.makedirs(os.path.join(tmp, "out"), exist_ok=True)

    def t_exports_check():
        out = os.path.join(tmp, "out")
        for _i, _f, _e, name in exports:
            p = os.path.join(out, name)
            check(f"export {name}", os.path.exists(p) and os.path.getsize(p) > 0)
        if os.path.exists(os.path.join(out, "out-flat-rot.jpg")):
            check("image export applies rotation", dims(os.path.join(out, "out-flat-rot.jpg")) == (100, 300),
                  dims(os.path.join(out, "out-flat-rot.jpg")))
        if os.path.exists(os.path.join(out, "out-page2.png")):
            check("pdf page export uses that page", dims(os.path.join(out, "out-page2.png"))[0] > 0)
        check("no temp files in export dir", not any(".seerstone-tmp" in n for n in os.listdir(out)))

    def t_rescan():
        with open(os.path.join(tmp, "f-new.png"), "wb") as fh:
            subprocess.run(["magick", "-size", "10x10", "xc:red", "png:-"], stdout=fh, check=True)

    def t_rescan_check():
        check("new file appears in the list", "f-new.png" in names(), names())

    def start():
        step(t_listing, 300)
        step(t_keys, 500)
        step(t_audio_plays, 1500)
        step(t_audio_check, 1200)
        step(t_audio_playing, 300)
        step(t_video, 1500)
        step(t_video_check, 300)
        step(t_rotate, 1200)
        step(t_rotate2, 2500)
        step(t_saved, 1000)
        step(t_undone, 300)
        step(t_nav_keeps_rotation, 900)
        step(t_nav2, 900)
        step(t_nav3, 900)
        step(t_nav4, 300)
        step(t_zoom, 300)
        step(t_resize_a, 1200)
        step(t_resize_b, 300)
        settle(lambda: win().image_view.get_width() < t_resize_b.before[2]
               and win().zoom_label.get_label() == f"Fit {win().image_view.zoom_percent()}%")
        step(t_resize_c, 300)
        settle(lambda: win().image_view.zoom_percent() == t_resize_b.before[1])
        step(t_resize_d, 300)
        step(t_video_resize_a, 1500)
        step(t_video_resize_b, 300)
        settle(lambda: win().media_view.picture.get_width() < t_video_resize_b.before)
        step(t_video_resize_c, 300)
        step(t_resize_a, 1200)
        step(t_sidebar_hide, 300)
        settle(lambda: win().image_view.get_width() > t_sidebar_hide.before)
        step(t_sidebar_hidden, 800)
        step(t_sidebar_shown, 300)
        step(t_narrow_ok, 300)
        for dock in ("top", "right", "bottom", "left"):
            a, b, c = make_dock_tests(dock)
            step(a, 1500)
            step(b, 700)
            step(c, 500)
        step(t_copy, 300)
        step(t_notes_format, 100)
        step(t_video_select, 2000)
        step(t_frame_step, 900)
        step(t_frame_step_landed, 100)
        step(t_hold_accelerates, 600)
        step(t_updown_files, 1500)
        step(t_video_select, 2000)
        step(t_note_add, 700)
        step(t_note_add2, 900)
        step(t_note_refocus, 800)
        step(t_note_refocus2, 600)
        step(t_note_moved, 800)
        step(t_note_moved2, 800)
        step(t_note_many, 300)
        settle(_at_bottom)
        step(lambda: None, 1100)
        step(t_note_many2, 1300)  # past the 1 s auto-position window before scrolling by hand
        step(t_note_many3, 300)
        step(t_note_chat_a, 300)
        settle(lambda: abs(_row_bottom_gap(win().notes_panel.notes[-1].text)) <= 2)
        step(t_note_chat_b, 100)
        step(t_note_mid_a, 300)
        settle(lambda: abs(_row_bottom_gap("Earlier note")) <= 2)
        step(t_note_mid_b, 300)
        step(t_note_saved, 300)
        step(t_note_jump, 600)
        step(t_note_jump2, 300)
        step(t_note_external, 300)
        settle(lambda: len(win().media_view.lane.markers) == 3)
        step(t_note_external2, 100)
        step(t_note_pdf, 1500)
        step(t_note_pdf2, 500)
        step(t_note_image, 1500)
        step(t_note_image2, 900)
        step(t_note_delete, 900)
        step(t_notes_drawer_open, 300)
        settle(lambda: win().notes_revealer.get_height() > 100)
        step(t_notes_drawer_open2, 300)
        settle(lambda: abs(win().notes_revealer.get_height() - t_notes_drawer_open2.h) <= 2)
        step(t_notes_drawer_resized, 300)
        settle(lambda: win().notes_revealer.get_height() == 0)
        step(t_notes_drawer_closed, 300)
        for idx, label in ((3, "image"), (4, "PDF")):
            go, chk = make_full_width(idx, label)
            step(go, 300)
            settle(lambda: win().image_view.canvas.texture is not None
                   and win().image_view.get_width() == win().main.get_width())
            step(chk, 100)
        step(t_recent_open_other, 1200)
        step(t_recent_back, 1200)
        step(t_recent_check, 100)
        for e in exports:
            go, run = make_export(*e)
            step(go, 1200)
            step(run, 6000)
        step(t_exports_check, 300)
        step(t_rescan, 1500)
        step(t_rescan_check, 300)
        GLib.timeout_add(1500, run_next)

    app.connect("activate", lambda *_a: GLib.idle_add(start))
    app.run(["smoke"])
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed  (fixtures in {tmp})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
