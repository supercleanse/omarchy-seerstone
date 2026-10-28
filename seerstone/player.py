"""Seerstone's own audio/video player: GStreamer playbin, with the frames
handed to GTK as a Gdk.Paintable.

Why not Gtk.MediaFile: GTK's built-in GStreamer backend gives no control over
seeking. With it (GStreamer 1.28, openh264), a click near the end of an H.264
video left the player frozen until the file was reloaded, clicks made while a
file was opening were dropped, fast drags queued dozens of full decodes, and
tearing a player down soon after creating it crashed the app.

How this one scrubs:

- Exact or fast. Exact seeks (ACCURATE) land on the very frame; fast ones
  (KEY_UNIT) on the nearest keyframe. On media where exact seeks are slow (or
  not yet timed), a click shows the keyframe at once and then the exact frame.
- One seek in flight. Newer requests replace the queued one, so the last spot
  always wins; a slow exact seek is abandoned (a new flushing seek) rather
  than waited out. Queued seeks are fired after the bus message, never inside.
- Landings are verified. ASYNC_DONE only counts once the pipeline has really
  settled, and each seek lands once.
- Self-healing. openh264 never produces another frame once it has decoded to
  end-of-file, so after that (or whenever a seek lands with no frame, or goes
  quiet with the decoder idle) the pipeline is reset and the seek repeated:
  the reload you'd otherwise do by hand, done in about 100 ms.
- Overshoot repair. With open-GOP streams (HEVC as phones record it) an
  exact seek to the frame or two just before a keyframe lands on the keyframe
  instead. The player notices the late frame, seeks about a second back
  (without showing it) and steps forward to the exact frame. Stepping moves
  only the video, so the next play() re-seeks to bring the audio along.
- Seeks are clamped to the last frame; after a real end-of-stream the player
  parks on the last frame, paused, so the next click or play just works.
- A seek asked for before the file is ready is applied once it is.

Positions are stream time (not raw PTS, which H.264 with B-frames offsets).
It exposes the slice of the Gtk.MediaStream API that MediaView uses
(is_prepared, get_timestamp, seek, play, notify::timestamp, ...).
"""
from __future__ import annotations

import os
import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, GObject, Graphene, Gst, GstVideo  # noqa: E402

Gst.init(None)

DEBUG = bool(os.environ.get("SEERSTONE_DEBUG"))

# Decoders that stop producing frames for good once they've hit end-of-file;
# the player resets the pipeline before the next seek when one of these is in use.
DRAIN_BROKEN_DECODERS = {"openh264dec"}

# An exact seek slower than this gets a keyframe preview first.
SLOW_EXACT_MS = 180
# A newer exact seek cuts in on an exact seek that has run longer than this.
INTERRUPT_AFTER_MS = 80
# A seek still in flight is checked this often; if the decoder took no new data
# since the last check, the seek is stuck (retried once, then the pipeline reset).
STALL_CHECK_MS = 1000

# playbin flags: video, audio, text (subtitles), vis, soft-volume, ...
_FLAG_VIDEO, _FLAG_AUDIO, _FLAG_TEXT, _FLAG_SOFT_VOLUME = 0x1, 0x2, 0x4, 0x10


class Player(GObject.Object, Gdk.Paintable):
    __gtype_name__ = "SeerstonePlayer"

    prepared = GObject.Property(type=bool, default=False)
    playing = GObject.Property(type=bool, default=False)
    ended = GObject.Property(type=bool, default=False)
    timestamp = GObject.Property(type=GObject.TYPE_INT64, default=0)
    duration = GObject.Property(type=GObject.TYPE_INT64, default=0)
    error = GObject.Property(type=object)

    def __init__(self, path: str, video: bool):
        super().__init__()
        self.video = video
        self.fps = 0.0
        self._texture: Gdk.Texture | None = None
        self.frame_time = -1
        self._size = (0, 0)
        self._closed = False
        self._seeking = False          # a flushing seek is in flight
        self._pending = None           # (us, accurate) to issue once it lands
        self._want_playing = False
        self._position_timer = 0
        self._lock = threading.Lock()
        self._next_frame = None        # (texture, pts_us) handed over from the streaming thread
        self._frame_idle = 0
        self._seek_started = 0.0
        self._seek_exact = False
        self.exact_seek_ms = 0.0       # running estimate of how long an exact seek takes here
        self.frames_delivered = 0      # bumped by the streaming thread for every frame
        self._frames_at_issue = 0
        self._last_seek = None         # (us, accurate) of the seek in flight
        self._recovering = False
        self.recoveries = 0
        self._recover_attempts = 0
        self._drained = False          # the video path has seen end-of-stream
        self._seek_gen = 0
        self._stall_retries = 0
        self._fed_at_issue = 0
        self._landed_gen = -1
        self.decoder_input = 0         # compressed buffers fed to the video decoder (while seeking)
        self._decoder_pad = None
        self._feed_probe = 0
        self._color_key = None
        self._color = None
        # Overshoot repair (see _fix_overshoot).
        self._expect = None            # (gen, target_us) of an exact seek awaiting its frame
        self._fix = None               # (gen, target_us, phase) while repairing
        self._needs_resync = False     # video was stepped alone; re-seek before playing

        pipe = Gst.ElementFactory.make("playbin", None)
        pipe.set_property("uri", Gst.filename_to_uri(path))
        flags = _FLAG_AUDIO | _FLAG_SOFT_VOLUME | (_FLAG_VIDEO if video else 0)
        pipe.set_property("flags", flags)
        if video:
            # Frames go to GTK in their native YUV layout (I420) with their color
            # space attached; the GPU converts them. Most decoders already output
            # I420, so videoconvert just passes through; it only works when a
            # source is in some other format.
            sink = Gst.parse_bin_from_description(
                "videoconvert n-threads=0 ! video/x-raw,format=I420 ! "
                "appsink name=frames sync=true max-buffers=2 drop=true enable-last-sample=false emit-signals=true",
                True)
            appsink = sink.get_by_name("frames")
            appsink.connect("new-sample", self._on_sample, False)
            appsink.connect("new-preroll", self._on_sample, True)
            # Notice when the decoder runs into end-of-file (it then won't
            # produce frames after a seek; see _check_frame_arrived).
            appsink.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_sink_event)
            pipe.set_property("video-sink", sink)
        else:
            pipe.set_property("video-sink", Gst.ElementFactory.make("fakesink", None))
        self.decoder = ""
        pipe.connect("deep-element-added", self._element_added)
        self.pipe = pipe
        bus = pipe.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_message)
        self._seeking = True  # the initial preroll counts as a seek landing
        pipe.set_state(Gst.State.PAUSED)

    # ---------- frames (streaming thread -> main thread) ----------

    def _on_sample(self, sink, preroll):
        sample = sink.emit("pull-preroll" if preroll else "pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = GLib.Bytes.new(mapinfo.data)
        finally:
            buf.unmap(mapinfo)
        tex = self._texture_for(info, data)
        if tex is None:
            return Gst.FlowReturn.OK
        # Stream time, not raw PTS: H.264 with B-frames stamps the first frame a
        # couple of frames after zero, which would shift every position.
        pts = -1
        if buf.pts != Gst.CLOCK_TIME_NONE:
            st = sample.get_segment().to_stream_time(Gst.Format.TIME, buf.pts)
            pts = st // 1000 if st != Gst.CLOCK_TIME_NONE and st >= 0 else buf.pts // 1000
        fps = info.fps_n / info.fps_d if info.fps_d and info.fps_n else 0.0
        with self._lock:
            self.frames_delivered += 1
            self._next_frame = (tex, pts, fps)
            if not self._frame_idle:
                self._frame_idle = GLib.idle_add(self._show_frame, priority=GLib.PRIORITY_HIGH_IDLE)
        return Gst.FlowReturn.OK

    def _texture_for(self, info, data):
        """An I420 frame as a GdkTexture: three planes plus the video's color
        space (matrix, transfer, primaries, range), converted on the GPU."""
        b = Gdk.MemoryTextureBuilder()
        b.set_width(info.width)
        b.set_height(info.height)
        b.set_bytes(data)
        b.set_format(Gdk.MemoryFormat.G8_B8_R8_420)  # Y, then Cb, then Cr: I420
        for plane in range(3):
            b.set_stride_for_plane(plane, info.stride[plane])
            b.set_offset(plane, info.offset[plane])
        cs = self._color_state(info)
        if cs is not None:
            b.set_color_state(cs)
        try:
            return b.build()
        except Exception:  # noqa: BLE001
            return None

    def _color_state(self, info):
        c = info.colorimetry
        key = (int(c.matrix), int(c.transfer), int(c.primaries), int(c.range))
        if key == self._color_key:
            return self._color
        # Files often leave some of these unset (ISO code 0 or 2). GTK refuses a
        # color state with unset fields, and a YUV texture without one is read
        # as RGB planes: purple and green. So fill the gaps the way players do:
        # BT.709 for HD, BT.601 for SD.
        sd = info.height < 720

        def iso(fn, value, default):
            code = fn(value)
            return default if code in (0, 2) else code

        # (the enum member is literally named "0_255", so compare by nick)
        full = c.range.value_nick == "0-255"
        wanted = (iso(GstVideo.video_color_matrix_to_iso, c.matrix, 6 if sd else 1),
                  iso(GstVideo.video_transfer_function_to_iso, c.transfer, 6 if sd else 1),
                  iso(GstVideo.video_color_primaries_to_iso, c.primaries, 6 if sd else 1))
        cs = None
        for matrix, transfer, primaries in (wanted, (wanted[0], 1, 1), (1, 1, 1)):
            try:
                params = Gdk.CicpParams()
                params.set_matrix_coefficients(matrix)
                params.set_transfer_function(transfer)
                params.set_color_primaries(primaries)
                params.set_range(Gdk.CicpRange.FULL if full else Gdk.CicpRange.NARROW)
                cs = params.build_color_state()
                break
            except Exception:  # noqa: BLE001
                continue
        self._color_key, self._color = key, cs
        return cs

    def _element_added(self, _bin, _sub, element):
        factory = element.get_factory()
        name = factory.get_name() if factory else ""
        if factory and "Decoder/Video" in (factory.get_metadata("klass") or ""):
            self.decoder = name
            self._decoder_pad = element.get_static_pad("sink")

    def _decoder_fed(self, _pad, _info):
        self.decoder_input += 1  # progress signal for the stall check
        return Gst.PadProbeReturn.OK

    def _on_sink_event(self, _pad, info):
        ev = info.get_event()
        if ev is not None and ev.type == Gst.EventType.EOS:
            self._drained = True
        return Gst.PadProbeReturn.OK

    def _show_frame(self):
        with self._lock:
            frame, self._next_frame = self._next_frame, None
            self._frame_idle = 0
        if frame is None or self._closed:
            return False
        tex, pts, fps = frame
        self._recover_attempts = 0
        if fps and not self.fps:
            self.fps = fps
        if self._fix is not None and self._fix[2] == "back" and self._fix[0] == self._seek_gen:
            # Mid-repair: this is the earlier frame we seeked back to. Don't
            # show it; step forward from it to the frame that was asked for.
            gen, target, _phase = self._fix
            self._fix = (gen, target, "step")
            GLib.idle_add(self._fix_step, gen, target, pts)
            return False
        if self._expect is not None:
            gen, target = self._expect
            self._expect = None
            if gen == self._seek_gen and self.fps and pts > target + 1000:
                GLib.idle_add(self._fix_overshoot, gen, target)
        size = (tex.get_width(), tex.get_height())
        self._texture = tex
        self.frame_time = pts  # stream time of the frame on screen
        if size != self._size:
            self._size = size
            self.invalidate_size()
        self.invalidate_contents()
        if pts >= 0 and not self._seeking:
            self.timestamp = pts
        return False

    # ---------- Gdk.Paintable ----------

    def do_snapshot(self, snapshot, width, height):
        if self._texture is not None:
            snapshot.append_texture(self._texture, Graphene.Rect().init(0, 0, width, height))

    def do_get_intrinsic_width(self):
        return self._size[0]

    def do_get_intrinsic_height(self):
        return self._size[1]

    def do_get_intrinsic_aspect_ratio(self):
        return self._size[0] / self._size[1] if self._size[1] else 0.0

    def do_get_current_image(self):
        return self._texture or Gdk.Paintable.new_empty(0, 0)

    def get_intrinsic_width(self):
        return self._size[0]

    def get_intrinsic_height(self):
        return self._size[1]

    def current_texture(self) -> Gdk.Texture | None:
        return self._texture

    # ---------- bus ----------

    def _on_message(self, _bus, msg):
        if self._closed:
            return
        t = msg.type
        if t == Gst.MessageType.ASYNC_DONE:
            if DEBUG:
                print(f"[player {GLib.get_monotonic_time() / 1e6:9.3f}] async-done (seeking {self._seeking}, settled "
                      f"{self.pipe.get_state(0)[0].value_nick})", flush=True)
            # Only trust it if the pipeline really has settled: a pause or an
            # interrupted seek can post one while the newest seek is still
            # prerolling, and counting that as "landed" would be wrong.
            if self.pipe.get_state(0)[0] != Gst.StateChangeReturn.ASYNC:
                self._seek_landed()
            elif self._seeking:
                # Still settling. Look again shortly rather than drop it: if
                # this was the real landing, ignoring it would leave the seek
                # "in flight" forever.
                GLib.timeout_add(40, self._recheck_landed, self._seek_gen)
        elif t == Gst.MessageType.EOS:
            self._want_playing = False
            # Park on the last frame with a flushing seek in PAUSED. Just
            # pausing isn't enough: the end-of-stream state lingers in the
            # video path and later seeks never deliver a frame (the "have to
            # reload" bug). The flush clears it, so the next click or play works.
            self.pipe.set_state(Gst.State.PAUSED)
            self._set_playing(False)
            self._pending = None
            self._issue_seek(self._clamp(self.duration or 0), True)
            self.ended = True
            if self.duration:
                self.timestamp = self.duration
        elif t == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            self.error = err
        elif t == Gst.MessageType.DURATION_CHANGED:
            self._query_duration()
        elif t == Gst.MessageType.STATE_CHANGED and msg.src == self.pipe:
            _old, new, _pending = msg.parse_state_changed()
            self._set_playing(new == Gst.State.PLAYING)

    @property
    def slow_exact(self) -> bool:
        """Exact seeks take long enough here that a keyframe preview first helps."""
        # Until an exact seek has been timed on this file, assume it may be
        # slow: a keyframe preview costs a few ms and never leaves the screen stale.
        return self.exact_seek_ms == 0 or self.exact_seek_ms > SLOW_EXACT_MS

    def _check_frame_arrived(self, frames_at_issue, seek, gen):
        """A video seek landed. If no frame came out of it, the decoder is
        wedged (openh264 never recovers after reaching end-of-file). Reset the
        pipeline and repeat the seek: an automatic, invisible reload."""
        if self._closed or self._recovering or self._seeking or self._pending is not None or gen != self._seek_gen:
            return False
        if self.frames_delivered != frames_at_issue:
            return False
        if DEBUG:
            print(f"[player {GLib.get_monotonic_time() / 1e6:9.3f}] seek to {seek[0]} landed with no frame (decoder {self.decoder})", flush=True)
        if self._recover_attempts >= 2:
            return False  # genuinely nothing there; don't loop
        self._recover_attempts += 1
        self._recover(seek)
        return False

    def _recover(self, seek):
        self._drained = False
        self._recovering = True
        self.recoveries += 1
        if DEBUG:
            print(f"[player {GLib.get_monotonic_time() / 1e6:9.3f}] reset {self.decoder or '?'} before seek to {seek[0]} (#{self.recoveries})", flush=True)
        us, accurate = seek
        self.pipe.set_state(Gst.State.READY)
        # The re-preroll lands like a seek of its own (its own generation, so
        # the once-per-seek landing check doesn't mistake it for the last
        # one), and then the seek is repeated.
        self._seek_gen += 1
        self._seeking = True
        self._pending = (us, accurate)
        self._seek_exact = False
        self._seek_started = GLib.get_monotonic_time()
        self._last_seek = (us, accurate)
        self.pipe.set_state(Gst.State.PAUSED)
        GLib.timeout_add(STALL_CHECK_MS, self._seek_stalled, self._seek_gen, self.decoder_input)

    def _seek_landed(self):
        # Each seek lands once. One seek can produce two ASYNC_DONEs (and the
        # recheck timer can report it too); acting on the second would resume
        # playback before a queued follow-up seek has even been issued.
        if self._seek_gen and self._landed_gen == self._seek_gen:
            return
        self._landed_gen = self._seek_gen
        if DEBUG and self._seeking and self._seek_started:
            print(f"[player {GLib.get_monotonic_time() / 1e6:9.3f}] landed #{self._seek_gen} after "
                  f"{(GLib.get_monotonic_time() - self._seek_started) / 1000:.0f} ms, decoder fed "
                  f"{self.decoder_input - self._fed_at_issue} buffers", flush=True)
        first = not self.prepared
        if self.video and self._last_seek is not None and not first and self._pending is None:
            # Give the frame a moment to come through, then make sure it did.
            GLib.timeout_add(150, self._check_frame_arrived, self._frames_at_issue, self._last_seek, self._seek_gen)
        if self._recovering and self._pending is None:
            self._recovering = False
        if self._seeking and self._seek_exact and self._seek_started:
            ms = (GLib.get_monotonic_time() - self._seek_started) / 1000
            # Weighted toward the worst recent seeks: one slow seek is enough to
            # start previewing; it takes a few fast ones to stop.
            self.exact_seek_ms = ms if ms > self.exact_seek_ms else self.exact_seek_ms * 0.7 + ms * 0.3
        self._seek_started = 0.0
        self._seeking = False
        self._stall_retries = 0
        if self._feed_probe and self._decoder_pad is not None:
            self._decoder_pad.remove_probe(self._feed_probe)
            self._feed_probe = 0
        if first:
            self._query_duration()
            self.prepared = True
        if self._pending is not None:
            us, accurate = self._pending
            self._pending = None
            # Fire it after this bus message is done, not inside it: a flushing
            # seek issued while the pipeline is finishing its preroll can be
            # folded into it, and then nothing ever reports it landed.
            self._seeking = True
            GLib.idle_add(self._issue_queued, us, accurate, self._seek_gen)
            return
        if not self.playing:
            pos = self._query_position()
            if pos is not None and (self._texture is None or not self.video):
                self.timestamp = pos
        if self._want_playing and not self.playing:
            self.pipe.set_state(Gst.State.PLAYING)

    def _query_duration(self):
        ok, dur = self.pipe.query_duration(Gst.Format.TIME)
        if ok and dur > 0:
            self.duration = dur // 1000

    def _query_position(self):
        ok, pos = self.pipe.query_position(Gst.Format.TIME)
        return pos // 1000 if ok and pos >= 0 else None

    def _set_playing(self, playing: bool):
        if playing != self.playing:
            self.playing = playing
        if playing and not self._position_timer:
            self._position_timer = GLib.timeout_add(40, self._poll_position)

    def _poll_position(self):
        if self._closed or not self.playing:
            self._position_timer = 0
            return False
        if not self._seeking:
            pos = self._query_position()
            if pos is not None and not (self.video and self._texture is not None):
                self.timestamp = pos  # video takes its time from the frame on screen
        return True

    # ---------- seeking ----------

    def _clamp(self, us: int) -> int:
        """Keep seeks inside the media: never past the last frame (or the last
        50 ms of audio), which would run the pipeline into end-of-stream."""
        us = max(0, int(us))
        if self.duration > 0:
            tail = int(1_000_000 / self.fps) if (self.video and self.fps) else 50_000
            us = min(us, max(0, self.duration - tail))
        return us

    def seek(self, us: int, accurate: bool = True, preview: bool = False, interrupt: bool = True):
        """Seek to us. accurate: land on that exact frame; otherwise the nearest
        keyframe (fast). preview: on media where exact seeks are slow, show the
        nearest keyframe first, then the exact frame. interrupt: a slow exact
        seek in flight may be abandoned for this one (right for separate clicks;
        wrong mid-drag, where abandoning every seek means none ever shows)."""
        if self._closed:
            return
        us = self._clamp(us)
        if DEBUG:
            print(f"[player {GLib.get_monotonic_time() / 1e6:9.3f}] seek() asked {us} accurate {accurate} preview {preview} "
                  f"want {self._want_playing} state {self.pipe.get_state(0)[1].value_nick}/{self.pipe.get_state(0)[0].value_nick} "
                  f"seeking {self._seeking}", flush=True)
        self.ended = False
        self.timestamp = us
        if self._want_playing and self.pipe.get_state(0)[1] == Gst.State.PLAYING:
            # Seek from PAUSED and resume when it lands: chained flushing seeks
            # on a running pipeline can leave it stalled. (Go by what was asked
            # for, not self.playing, which lags behind a pause() just made.)
            self.pipe.set_state(Gst.State.PAUSED)
            self.pipe.get_state(100 * Gst.MSECOND)  # let the pause finish first
        if not self.prepared or self._recovering:
            self._pending = (us, accurate)
            return
        if self._seeking:
            elapsed = (GLib.get_monotonic_time() - self._seek_started) / 1000
            if interrupt and accurate and self._seek_exact and elapsed > INTERRUPT_AFTER_MS:
                # A slow exact seek is still decoding toward an old spot; a new
                # flushing seek abandons it at once instead of waiting it out.
                self._pending = None
                self._issue_seek(us, accurate)
            else:
                self._pending = (us, accurate)  # newest request wins
                if interrupt and accurate and self._seek_exact:
                    # Don't let it wait out a slow seek: cut in once the
                    # in-flight one is old enough to be worth abandoning.
                    GLib.timeout_add(max(1, int(INTERRUPT_AFTER_MS - elapsed) + 1),
                                     self._interrupt_for_pending, self._seek_gen)
            return
        if self.video and self._drained and not self._recovering and self.decoder in DRAIN_BROKEN_DECODERS:
            self._recover((us, accurate))  # reset the decoder first, then seek
            return
        if accurate and preview and self.video and self.slow_exact:
            self._issue_seek(us, False)
            self._pending = (us, True)
            return
        self._issue_seek(us, accurate)

    def _issue_seek(self, us: int, accurate: bool, expect: bool = True):
        us = self._clamp(us)
        self._fix = None
        self._needs_resync = False
        flags = Gst.SeekFlags.FLUSH | (Gst.SeekFlags.ACCURATE if accurate
                                       else Gst.SeekFlags.KEY_UNIT | Gst.SeekFlags.SNAP_NEAREST)
        self._seeking = True
        self._seek_exact = accurate
        self._seek_started = GLib.get_monotonic_time()
        self._last_seek = (us, accurate)
        self._frames_at_issue = self.frames_delivered
        self._seek_gen += 1
        self._expect = (self._seek_gen, us) if (accurate and expect and self.video) else None
        self._fed_at_issue = self.decoder_input
        if DEBUG:
            print(f"[player {GLib.get_monotonic_time() / 1e6:9.3f}] issue #{self._seek_gen} {'exact' if accurate else 'key'} {us} state "
                  f"{self.pipe.get_state(0)[1].value_nick}/{self.pipe.get_state(0)[0].value_nick}", flush=True)
        if not self.pipe.seek_simple(Gst.Format.TIME, flags, us * 1000):
            self._seeking = False
            return
        if self._decoder_pad is not None and not self._feed_probe:
            # Count decoder input only while a seek is in flight (a Python
            # callback per buffer isn't free).
            self._feed_probe = self._decoder_pad.add_probe(Gst.PadProbeType.BUFFER, self._decoder_fed)
        GLib.timeout_add(STALL_CHECK_MS, self._seek_stalled, self._seek_gen, self.decoder_input)

    def _interrupt_for_pending(self, gen):
        if (self._closed or gen != self._seek_gen or not self._seeking or self._pending is None
                or self._recovering or not self._seek_exact):
            return False
        us, accurate = self._pending
        self._pending = None
        self._issue_seek(us, accurate)
        return False

    # Some streams can't be seeked straight to the frames just before a
    # keyframe: HEVC's open GOPs decode those "leading" frames after the
    # keyframe, and an exact seek there lands on the keyframe instead (a frame
    # or two late). When that happens, seek about a second back and step
    # forward frame by frame to the one asked for.

    def _fix_overshoot(self, gen, target):
        if (self._closed or gen != self._seek_gen or self._pending is not None or self._recovering
                or self._want_playing):  # playing on; a frame or two doesn't matter then
            return False
        if self._seeking:
            GLib.timeout_add(15, self._fix_overshoot, gen, target)  # let it land first
            return False
        if DEBUG:
            print(f"[player {GLib.get_monotonic_time() / 1e6:9.3f}] seek to {target} overshot; "
                  f"repairing by seeking back and stepping", flush=True)
        back = max(0, target - 1_000_000)
        self._issue_seek(back, True, expect=False)
        self._fix = (self._seek_gen, target, "back")
        self.timestamp = target
        return False

    def _fix_step(self, gen, target, landed_at):
        if self._closed or self._fix is None or self._fix[0] != gen or gen != self._seek_gen or self._pending is not None:
            self._fix = None
            return False
        if self._seeking:
            GLib.timeout_add(10, self._fix_step, gen, target, landed_at)
            return False
        fps = self.fps or 30.0
        n = int(target * fps / 1_000_000 + 1e-3) - int(landed_at * fps / 1_000_000 + 1e-3)
        self._fix = None
        if n <= 0:
            return False
        self._seek_gen += 1
        self._seeking = True
        self._seek_exact = False  # don't let the repair skew the exact-seek timing
        self._seek_started = GLib.get_monotonic_time()
        self._last_seek = (target, True)
        self._frames_at_issue = self.frames_delivered
        self._needs_resync = True
        self.pipe.send_event(Gst.Event.new_step(Gst.Format.BUFFERS, n, 1.0, True, False))
        GLib.timeout_add(STALL_CHECK_MS, self._seek_stalled, self._seek_gen, self.decoder_input)
        return False

    def _issue_queued(self, us, accurate, gen):
        if not self._closed and gen == self._seek_gen:
            self._issue_seek(us, accurate)
        return False

    def _recheck_landed(self, gen):
        if self._closed or gen != self._seek_gen or not self._seeking:
            return False
        if self.pipe.get_state(0)[0] != Gst.StateChangeReturn.ASYNC:
            self._seek_landed()
            return False
        return True  # keep looking until it settles (the stall timer is the backstop)

    def _seek_stalled(self, gen, fed_before):
        """Checked every STALL_CHECK_MS while a seek is in flight. A slow seek
        keeps feeding the decoder; only one that has gone quiet is stuck, and
        then it's retried once and after that the pipeline is reset."""
        if self._closed or gen != self._seek_gen or not self._seeking:
            return False
        if self.video and self.decoder and self.decoder_input != fed_before:
            GLib.timeout_add(STALL_CHECK_MS, self._seek_stalled, gen, self.decoder_input)
            return False  # still working, just slow
        if DEBUG:
            print(f"[player {GLib.get_monotonic_time() / 1e6:9.3f}] seek to {self._last_seek[0]} stalled (decoder {self.decoder})", flush=True)
        seek = self._pending or self._last_seek  # the newest request, not a stale one
        self._pending = None
        self._seeking = False
        self._recovering = False
        if self._stall_retries < 1:
            self._stall_retries += 1
            self._issue_seek(*seek)
        else:
            self._stall_retries = 0
            self._recover(seek)
        return False

    def is_seekable(self) -> bool:
        return True

    # ---------- transport ----------

    def play(self):
        if self._closed:
            return
        if self.ended:
            self.seek(0)
        elif self._needs_resync:
            # The last repair stepped the video alone; bring audio along by
            # seeking there properly before playing.
            self._needs_resync = False
            self._issue_seek(self.timestamp, True, expect=False)
        self._want_playing = True
        if not self._seeking:
            self.pipe.set_state(Gst.State.PLAYING)

    def pause(self):
        self._want_playing = False
        if not self._closed:
            self.pipe.set_state(Gst.State.PAUSED)

    def set_playing(self, playing: bool):
        self.play() if playing else self.pause()

    def set_volume(self, v: float):
        self.pipe.set_property("volume", max(0.0, v))

    def set_muted(self, muted: bool):
        self.pipe.set_property("mute", muted)

    # Gtk.MediaStream-shaped getters
    def is_prepared(self):
        return self.prepared

    def get_duration(self):
        return self.duration

    def get_timestamp(self):
        return self.timestamp

    def get_playing(self):
        return self.playing

    def get_ended(self):
        return self.ended

    def get_error(self):
        return self.error

    def clear(self):
        """Stop and free the pipeline (synchronous; safe to call any time)."""
        if self._closed:
            return
        self._closed = True
        if self._position_timer:
            GLib.source_remove(self._position_timer)
            self._position_timer = 0
        bus = self.pipe.get_bus()
        bus.disconnect(self._bus_handler)
        bus.remove_signal_watch()
        self.pipe.set_state(Gst.State.NULL)
