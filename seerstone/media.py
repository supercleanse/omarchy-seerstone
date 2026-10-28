"""Audio and video: letterboxed video, an audio card with cover art and a
waveform you can click to seek, and one transport bar for both."""
from __future__ import annotations

import json
import os

import gi

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import proc  # noqa: E402
from .files import human_size, human_time  # noqa: E402
from .notes_ui import MarkerLane  # noqa: E402
from .player import Player  # noqa: E402

# Only start a player once a file has stayed selected this long, so arrowing
# quickly through a folder doesn't spin up (and tear down) a pipeline per file.
START_DELAY_MS = 150
# Mid-drag on slow-to-decode media, holding the knob still this long shows the exact frame.
DRAG_REST_MS = 120


class Waveform(Gtk.Widget):
    """A pre-rendered waveform picture with the played part lit and a playhead."""
    __gtype_name__ = "SeerstoneWaveform"
    __gsignals__ = {"seek": (GObject.SignalFlags.RUN_FIRST, None, (float,))}

    def __init__(self):
        super().__init__()
        self.texture: Gdk.Texture | None = None
        self.fraction = 0.0
        self.markers: list[tuple[float, str, bool]] = []
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)
        self.accent = Gdk.RGBA()
        self.accent.parse("#e68e0d")
        self.set_size_request(-1, 110)
        self.set_hexpand(True)
        click = Gtk.GestureClick.new()
        click.connect("pressed", lambda _g, _n, x, _y: self._seek(x))
        self.add_controller(click)
        drag = Gtk.GestureDrag.new()
        drag.connect("drag-update", self._dragged)
        self.add_controller(drag)
        self._drag_x = 0.0
        drag.connect("drag-begin", lambda _g, x, _y: setattr(self, "_drag_x", x))
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))

    def _dragged(self, _g, dx, _dy):
        self._seek(self._drag_x + dx)

    def _marker_at(self, x):
        w = self.get_width()
        best, best_d = -1, 8
        for i, (f, _t, _d) in enumerate(self.markers):
            d = abs(f * w - x)
            if d < best_d:
                best, best_d = i, d
        return best

    def _tooltip(self, _w, x, _y, _kb, tooltip):
        i = self._marker_at(x)
        if i < 0:
            return False
        tooltip.set_text(self.markers[i][1])
        return True

    def _seek(self, x):
        w = self.get_width()
        if w > 0:
            i = self._marker_at(x)
            f = self.markers[i][0] if i >= 0 else x / w  # snap onto a note marker
            self.emit("seek", max(0.0, min(1.0, f)))

    def set_fraction(self, f: float):
        self.fraction = max(0.0, min(1.0, f))
        self.queue_draw()

    def do_snapshot(self, snapshot):
        from gi.repository import Graphene
        w, h = self.get_width(), self.get_height()
        if self.texture is not None:
            rect = Graphene.Rect().init(0, 0, w, h)
            snapshot.push_opacity(0.35)
            snapshot.append_texture(self.texture, rect)
            snapshot.pop()
            played = Graphene.Rect().init(0, 0, w * self.fraction, h)
            snapshot.push_clip(played)
            snapshot.append_texture(self.texture, rect)
            snapshot.pop()
        else:
            mid = Gdk.RGBA()
            mid.parse("rgba(128,128,128,0.35)")
            snapshot.append_color(mid, Graphene.Rect().init(0, h / 2 - 1, w, 2))
        dim = self.accent.copy()
        dim.alpha = 0.4
        for f, _t, done in self.markers:
            mx = w * f
            c = dim if done else self.accent
            snapshot.append_color(c, Graphene.Rect().init(mx - 0.5, 0, 1, h))
            snapshot.append_color(c, Graphene.Rect().init(mx - 4, 0, 8, 8))
        x = w * self.fraction
        snapshot.append_color(self.accent, Graphene.Rect().init(x - 1, 0, 2, h))


class MediaView(Gtk.Box):
    __gtype_name__ = "SeerstoneMediaView"
    __gsignals__ = {
        "loaded": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "toggle-fullscreen": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "info": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "marker-activated": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "position": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.media: Player | None = None
        self._start_source = 0
        self.path: str | None = None
        self.kind: str | None = None
        self.volume = 1.0
        self.muted = False
        self.accent = "#e68e0d"
        self.duration_us = 0
        self.fps = 0.0
        self._scrub_target: int | None = None
        self._scrub_time = 0
        self._marker_notes: list = []
        self._seq = 0
        self._cancel: Gio.Cancellable | None = None
        self._handlers = []

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.set_hhomogeneous(False)
        self.stack.set_vhomogeneous(False)
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.append(self.stack)

        # Video: always the aspect ratio it shipped with, letterboxed.
        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.picture.set_can_shrink(True)
        self.picture.set_hexpand(True)
        self.picture.set_vexpand(True)
        vclick = Gtk.GestureClick.new()
        vclick.connect("released", self._video_clicked)
        self.picture.add_controller(vclick)
        self.stack.add_named(self.picture, "video")

        # Audio card.
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        card.set_valign(Gtk.Align.CENTER)
        card.set_halign(Gtk.Align.FILL)
        card.set_margin_start(24)
        card.set_margin_end(24)
        top = Gtk.Box(spacing=24)
        top.set_halign(Gtk.Align.CENTER)
        self.art_frame = Gtk.Overlay()
        self.art_frame.add_css_class("ss-art")
        self.art_frame.set_size_request(160, 160)
        self.art_frame.set_valign(Gtk.Align.CENTER)
        glyph = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        glyph.set_pixel_size(84)
        glyph.add_css_class("ss-art-glyph")
        self.art_frame.set_child(glyph)
        self.art = Gtk.Picture()
        self.art.set_content_fit(Gtk.ContentFit.COVER)
        self.art.set_size_request(160, 160)
        self.art_frame.add_overlay(self.art)
        top.append(self.art_frame)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        text.set_valign(Gtk.Align.CENTER)
        self.a_title = self._label("ss-audio-title")
        self.a_sub = self._label("ss-audio-sub")
        self.a_meta = self._label("ss-audio-meta")
        for lbl in (self.a_title, self.a_sub, self.a_meta):
            text.append(lbl)
        top.append(text)
        card.append(top)
        self.wave = Waveform()
        self.wave.set_size_request(-1, 120)
        self.wave.connect("seek", self._wave_seek)
        card.append(self.wave)
        self.stack.add_named(card, "audio")

        # Transport.
        bar = Gtk.Box(spacing=8)
        bar.add_css_class("ss-transport")
        self.play_btn = Gtk.Button.new_from_icon_name("media-playback-start-symbolic")
        self.play_btn.add_css_class("ss-btn")
        self.play_btn.set_tooltip_text("Play / pause (Space)")
        self.play_btn.connect("clicked", lambda *_a: self.toggle_play())
        bar.append(self.play_btn)
        self.pos_label = Gtk.Label(label="0:00")
        self.pos_label.add_css_class("ss-time")
        self.pos_label.set_xalign(1)
        bar.append(self.pos_label)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.001)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        self.scale.set_focusable(False)
        self.scale.connect("change-value", self._scrubbed)
        self._dragging = False
        self._pending_fraction = None
        self._rest_source = 0
        self._last_scrub_value = -1.0
        scrub = Gtk.EventControllerLegacy.new()  # sees the press/release around a drag
        scrub.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scrub.connect("event", self._scrub_event)
        self.scale.add_controller(scrub)
        self.lane = MarkerLane()
        self.lane.connect("activated", lambda _l, i: self.emit("marker-activated", i))
        track = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        track.set_hexpand(True)
        track.set_valign(Gtk.Align.CENTER)
        track.append(self.lane)
        track.append(self.scale)
        bar.append(track)
        self.dur_label = Gtk.Label(label="0:00")
        self.dur_label.add_css_class("ss-time")
        self.dur_label.set_xalign(0)
        bar.append(self.dur_label)
        self.mute_btn = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic")
        self.mute_btn.add_css_class("ss-btn")
        self.mute_btn.add_css_class("flat")
        self.mute_btn.set_tooltip_text("Mute (M)")
        self.mute_btn.connect("clicked", lambda *_a: self.toggle_mute())
        bar.append(self.mute_btn)
        self.vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.05)
        self.vol.set_draw_value(False)
        self.vol.set_value(self.volume)
        self.vol.set_size_request(56, -1)
        self.vol.set_focusable(False)
        self.vol.connect("value-changed", self._volume_changed)
        bar.append(self.vol)
        self.append(bar)

    @staticmethod
    def _label(css):
        lbl = Gtk.Label(xalign=0)
        lbl.add_css_class(css)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        lbl.set_max_width_chars(48)
        lbl.set_width_chars(8)
        return lbl

    def set_accent(self, color: str):
        self.accent = color
        self.wave.accent.parse(color)
        self.wave.queue_draw()
        self.lane.accent.parse(color)
        self.lane.queue_draw()

    def set_note_markers(self, notes):
        """Show timed notes as markers on the scrub bar (and the waveform)."""
        self._marker_notes = [n for n in notes if n.time_us is not None]
        self._sync_markers()

    def _sync_markers(self):
        dur = self.duration_us
        markers = []
        if dur > 0:
            for n in self._marker_notes:
                label = f"{human_time(n.time_us)}  {n.text}" + ("  (done)" if n.done else "")
                markers.append((max(0.0, min(1.0, n.time_us / dur)), label, n.done))
        self.lane.set_markers(markers)
        self.wave.markers = markers
        self.wave.queue_draw()

    def marker_note(self, index: int):
        return self._marker_notes[index] if 0 <= index < len(self._marker_notes) else None

    def _wave_seek(self, _w, f):
        i = self.wave._marker_at(f * self.wave.get_width())
        if i >= 0:
            self.emit("marker-activated", i)
        else:
            self.seek_fraction(f)

    # ---------- loading ----------

    def clear(self):
        self._seq += 1
        if self._cancel:
            self._cancel.cancel()
            self._cancel = None
        if self._start_source:
            GLib.source_remove(self._start_source)
            self._start_source = 0
        if self.media is not None:
            for h in self._handlers:
                self.media.disconnect(h)
            self._handlers = []
            self.media.clear()
            self.media = None
        self._pending_fraction = None
        self._dragging = False
        self._cancel_rest()
        self.picture.set_paintable(None)
        self.art.set_paintable(None)
        self.wave.texture = None
        self.wave.set_fraction(0)
        self.scale.set_value(0)
        self.pos_label.set_text("0:00")
        self.dur_label.set_text("0:00")
        self.duration_us = 0
        self.fps = 0.0
        self._scrub_target = None
        self._marker_notes = []
        self._sync_markers()
        self.path = self.kind = None
        self._sync_play_icon()

    def load(self, path: str, kind: str, autoplay: bool = False):
        self.clear()
        self.path, self.kind = path, kind
        seq = self._seq
        self._cancel = Gio.Cancellable()
        if kind == "audio":
            name = os.path.splitext(os.path.basename(path))[0]
            self.a_title.set_text(name)
            self.a_sub.set_text("")
            self.a_meta.set_text("")
        self.stack.set_visible_child_name("video" if kind == "video" else "audio")
        self._start_source = GLib.timeout_add(START_DELAY_MS, self._start, path, kind, autoplay, seq)

    def _start(self, path, kind, autoplay, seq):
        self._start_source = 0
        if seq != self._seq:
            return False
        self.media = Player(path, video=(kind == "video"))
        self.media.set_volume(self.volume)
        self.media.set_muted(self.muted)
        m = self.media
        self._handlers = [
            m.connect("notify::prepared", lambda *_a: self._prepared(seq, autoplay)),
            m.connect("notify::error", lambda *_a: self._errored(seq)),
            m.connect("notify::timestamp", lambda *_a: self._tick()),
            m.connect("notify::playing", lambda *_a: self._sync_play_icon()),
            m.connect("notify::ended", lambda *_a: self._sync_play_icon()),
            m.connect("notify::duration", lambda *_a: self._tick()),
        ]
        if kind == "video":
            self.picture.set_paintable(m)
            self._probe_fps(path, seq)
        else:
            self._probe(path, seq)
            self._make_wave(path, seq)
            self._extract_art(path, seq)
        return False

    def _prepared(self, seq, autoplay):
        if seq != self._seq or self.media is None or not self.media.is_prepared():
            return
        if self.media.fps and not self.fps:
            self.fps = self.media.fps
        if self._pending_fraction is not None:  # clicked the scrubber while the file was opening
            f, self._pending_fraction = self._pending_fraction, None
            self.seek_fraction(f)
        self._tick()
        self.emit("loaded", "")
        if autoplay:
            self.media.play()

    def _errored(self, seq):
        if seq != self._seq or self.media is None:
            return
        err = self.media.get_error()
        if err is None:
            return
        msg = err.message
        if "decoder" in msg.lower() or "codec" in msg.lower() or "plugin" in msg.lower():
            msg += ". Installing gst-libav adds the missing codecs: sudo pacman -S gst-libav"
        self.emit("loaded", f"Can't play this file: {msg}")

    # ---------- audio details ----------

    def _probe(self, path, seq):
        def done(ok, out, _err):
            if seq != self._seq or not ok:
                return
            try:
                data = json.loads(out)
            except ValueError:
                return
            fmt = data.get("format", {})
            tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
            for s in data.get("streams", []):
                tags.update({k.lower(): v for k, v in (s.get("tags") or {}).items() if k.lower() not in tags})
            if tags.get("title"):
                self.a_title.set_text(tags["title"])
            sub = " · ".join(t for t in (tags.get("artist") or tags.get("album_artist"), tags.get("album")) if t)
            self.a_sub.set_text(sub)
            audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
            bits = []
            if audio.get("codec_name"):
                bits.append(audio["codec_name"].upper())
            if audio.get("sample_rate"):
                bits.append(f"{int(audio['sample_rate']) / 1000:g} kHz")
            ch = audio.get("channels")
            if ch:
                bits.append({1: "mono", 2: "stereo"}.get(ch, f"{ch} ch"))
            if fmt.get("bit_rate"):
                bits.append(f"{int(fmt['bit_rate']) // 1000} kbps")
            if fmt.get("size"):
                bits.append(human_size(int(fmt["size"])))
            self.a_meta.set_text("   ".join(bits))
            self.emit("info", "  ·  ".join(bits[:3]))

        proc.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
                 done, self._cancel)

    def _probe_fps(self, path, seq):
        def done(ok, out, _err):
            if seq != self._seq or not ok:
                return
            for rate in out.split():
                try:
                    num, _, den = rate.partition("/")
                    fps = float(num) / float(den or 1)
                except (ValueError, ZeroDivisionError):
                    continue
                if 1 <= fps <= 1000:
                    self.fps = fps
                    self.emit("position")
                    return
        proc.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                  "stream=avg_frame_rate,r_frame_rate", "-of", "default=nw=1:nk=1", path], done, self._cancel)

    def _make_wave(self, path, seq):
        color = self.accent.lstrip("#")
        out = proc.cache_path(path, f"wave-{color}.png")

        def show():
            if seq != self._seq:
                return
            try:
                self.wave.texture = Gdk.Texture.new_from_filename(out)
                self.wave.queue_draw()
            except GLib.Error:
                pass

        if os.path.exists(out):
            show()
            return
        tmp = out + ".part.png"
        argv = ["ffmpeg", "-v", "error", "-y", "-i", path, "-filter_complex",
                f"aformat=channel_layouts=mono,showwavespic=s=1600x240:colors=#{color}:scale=sqrt",
                "-frames:v", "1", tmp]
        proc.run(argv, lambda ok, *_a: (ok and os.replace(tmp, out), show()) if ok else None, self._cancel)

    def _extract_art(self, path, seq):
        out = proc.cache_path(path, "cover.png")

        def show():
            if seq != self._seq:
                return
            try:
                self.art.set_paintable(Gdk.Texture.new_from_filename(out))
            except GLib.Error:
                pass

        if os.path.exists(out):
            show()
            return
        tmp = out + ".part.png"
        argv = ["ffmpeg", "-v", "error", "-y", "-i", path, "-an", "-map", "0:v:0", "-frames:v", "1",
                "-vf", "scale='min(600,iw)':-2", tmp]
        proc.run(argv, lambda ok, *_a: (os.replace(tmp, out), show()) if ok and os.path.exists(tmp) else None,
                 self._cancel)

    # ---------- transport ----------

    def _tick(self):
        m = self.media
        if m is None:
            return
        if not self.fps and getattr(m, "fps", 0):
            self.fps = m.fps
        dur = m.get_duration()
        ts = m.get_timestamp()
        if dur > 0:
            if dur != self.duration_us:
                self.duration_us = dur
                self._sync_markers()
            self.dur_label.set_text(human_time(dur))
            if not self._dragging:  # don't fight the knob while it's being dragged
                self.scale.set_value(ts / dur)
            self.wave.set_fraction(ts / dur)
        self.pos_label.set_text(human_time(ts))
        self.emit("position")

    # Scrubbing: while the knob is held, seek to the nearest keyframe (instant
    # preview); on release, and for a plain click, seek to the exact frame.

    def _scrub_event(self, ctrl, _event):
        # PyGObject hands this signal's GdkEvent over as None; ask the controller.
        event = ctrl.get_current_event()
        if event is None:
            return False
        t = event.get_event_type()
        if t == Gdk.EventType.BUTTON_PRESS:
            self.begin_scrub()
        elif t == Gdk.EventType.BUTTON_RELEASE and self._dragging:
            GLib.idle_add(lambda: self.end_scrub() and False)  # after the scale applies its last value
        return False

    def begin_scrub(self):
        self._dragging = True
        self._last_scrub_value = -1.0

    def end_scrub(self):
        if not self._dragging:
            return
        self._dragging = False
        self._cancel_rest()
        self.seek_fraction(self.scale.get_value())  # exact frame where you let go

    def _cancel_rest(self):
        if self._rest_source:
            GLib.source_remove(self._rest_source)
            self._rest_source = 0

    def _rested(self):
        """The knob has held still mid-drag: show the exact frame there."""
        self._rest_source = 0
        if self._dragging:
            self.seek_fraction(self.scale.get_value())
        return False

    def _scrubbed(self, _scale, _scroll, value):
        # GTK keeps reporting the knob while the button is held, even when it
        # hasn't moved. Repeats aren't new requests: they'd re-seek and keep
        # resetting the "knob is resting" timer, so the exact frame never came.
        if self._dragging and abs(value - self._last_scrub_value) < 1e-9:
            return False
        self._last_scrub_value = value
        m = self.media
        if self._dragging and m is not None and getattr(m, "slow_exact", False):
            # Slow to decode exactly: preview keyframes while moving, exact frame when you pause.
            self.seek_fraction(value, exact=False)
            self._cancel_rest()
            self._rest_source = GLib.timeout_add(DRAG_REST_MS, self._rested)
        else:
            self.seek_fraction(value)  # exact, live, as fast as it decodes
        return False

    def seek_fraction(self, f: float, exact: bool = True):
        f = max(0.0, min(1.0, f))
        m = self.media
        if m is None or not m.is_prepared() or m.get_duration() <= 0:
            self._pending_fraction = f  # applied as soon as the file is ready
            return
        self.seek_to(int(f * m.get_duration()), exact=exact)

    def seek_relative(self, seconds: float):
        m = self.media
        if m is None:
            return
        self.seek_to(self.position_us() + int(seconds * 1_000_000))

    def position_us(self) -> int:
        """Where we are, counting a step/seek that's still landing."""
        if self._scrub_target is not None and GLib.get_monotonic_time() - self._scrub_time < 400_000:
            return self._scrub_target
        return self.media.get_timestamp() if self.media is not None else 0

    def frame_at(self, us: int) -> int | None:
        return int(round(us * self.fps / 1_000_000)) if self.fps else None

    def step(self, units: int):
        """Step by whole video frames (audio: 50 ms units), paused, landing exactly."""
        m = self.media
        if m is None or not m.is_seekable() or not units:
            return
        if m.get_playing():
            m.pause()
        dur = max(0, m.get_duration() - 1)
        if self.kind == "video":
            fps = self.fps or 30.0
            frame = int(round(self.position_us() * fps / 1_000_000)) + units
            last = int(dur * fps / 1_000_000)
            frame = max(0, min(frame, last))
            target = int(frame * 1_000_000 / fps)
            seek_to = min(dur, target + 1000)  # 1 ms in, so we land on this frame, not the one before
        else:
            target = max(0, min(dur, self.position_us() + units * 50_000))
            seek_to = target
        self._scrub_target = target
        self._scrub_time = GLib.get_monotonic_time()
        m.seek(seek_to)
        self.pos_label.set_text(human_time(target))
        self.emit("position")

    def seek_to(self, us: int, exact: bool = True):
        m = self.media
        if m is None:
            return
        self._scrub_target = us
        self._scrub_time = GLib.get_monotonic_time()
        m.seek(max(0, us), accurate=exact, preview=exact and not self._dragging, interrupt=not self._dragging)

    def toggle_play(self):
        m = self.media
        if m is None:
            return
        if m.get_playing():
            m.pause()
        else:
            m.play()

    def pause(self):
        if self.media is not None:
            self.media.pause()

    def toggle_mute(self):
        self.muted = not self.muted
        if self.media is not None:
            self.media.set_muted(self.muted)
        self._sync_volume_icon()

    def _volume_changed(self, scale):
        self.volume = scale.get_value()
        if self.media is not None:
            self.media.set_volume(self.volume)
        if self.muted and self.volume > 0:
            self.toggle_mute()
        self._sync_volume_icon()

    def _sync_volume_icon(self):
        if self.muted or self.volume == 0:
            name = "audio-volume-muted-symbolic"
        elif self.volume < 0.4:
            name = "audio-volume-low-symbolic"
        elif self.volume < 0.8:
            name = "audio-volume-medium-symbolic"
        else:
            name = "audio-volume-high-symbolic"
        self.mute_btn.set_icon_name(name)

    def _sync_play_icon(self):
        playing = self.media is not None and self.media.get_playing() and not self.media.get_ended()
        self.play_btn.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")

    def _video_clicked(self, _g, n_press, _x, _y):
        if n_press == 1:
            self.toggle_play()
        elif n_press == 2:
            self.toggle_play()  # undo the first click's toggle
            self.emit("toggle-fullscreen")

    def video_size(self) -> tuple[int, int]:
        if self.media is None:
            return 0, 0
        return self.media.get_intrinsic_width(), self.media.get_intrinsic_height()
