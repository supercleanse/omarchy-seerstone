"""The image and PDF stage: fit-to-window by default (letterboxed), zoom with
panning, and rotation in 90-degree steps."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gly", "2")
gi.require_version("GlyGtk4", "2")
gi.require_version("Poppler", "0.18")
import cairo  # noqa: E402
from gi.repository import (  # noqa: E402
    Gdk, Gio, GLib, GObject, Gly, GlyGtk4, Graphene, Gsk, Gtk, Poppler,
)

MIN_ZOOM, MAX_ZOOM = 0.02, 32.0
ZOOM_STEP = 1.25
PDF_MAX_SIDE = 8192  # pixels; cap for a re-rendered PDF page


class Canvas(Gtk.Widget):
    """Draws one texture into a logical rectangle, rotated and scaled.

    zoom None means "fit": the picture fills the widget at its widest
    proportions, letterboxed. A number is the scale against the logical size
    (1.0 = 100%), and the widget then asks for that much room so the
    surrounding ScrolledWindow can pan it.
    """
    __gtype_name__ = "SeerstoneCanvas"

    def __init__(self):
        super().__init__()
        self.texture: Gdk.Texture | None = None
        self.logical = (0.0, 0.0)
        self.rotation = 0
        self.zoom: float | None = None
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)

    def do_size_allocate(self, width, height, baseline):
        if (width, height) != getattr(self, "_last_size", None):
            self._last_size = (width, height)
            GLib.idle_add(self._resized)

    def _resized(self):
        parent = self.get_ancestor(ImageView)
        if parent is not None:
            parent.emit("changed")
        return False

    def rotated_logical(self) -> tuple[float, float]:
        w, h = self.logical
        return (h, w) if self.rotation % 180 else (w, h)

    def fit_scale(self, width: float, height: float) -> float:
        rw, rh = self.rotated_logical()
        if rw <= 0 or rh <= 0 or width <= 0 or height <= 0:
            return 1.0
        return min(width / rw, height / rh)

    def do_measure(self, orientation, for_size):
        if self.texture is None or self.zoom is None:
            return 0, 0, -1, -1
        rw, rh = self.rotated_logical()
        v = int((rw if orientation == Gtk.Orientation.HORIZONTAL else rh) * self.zoom)
        return v, v, -1, -1

    def scale_for(self, width: float, height: float) -> float:
        return self.zoom if self.zoom is not None else self.fit_scale(width, height)

    def do_snapshot(self, snapshot):
        if self.texture is None:
            return
        w, h = self.get_width(), self.get_height()
        s = self.scale_for(w, h)
        lw, lh = self.logical
        dw, dh = lw * s, lh * s
        snapshot.save()
        snapshot.translate(Graphene.Point().init(w / 2, h / 2))
        if self.rotation:
            snapshot.rotate(self.rotation)
        rect = Graphene.Rect().init(-dw / 2, -dh / 2, dw, dh)
        on_screen = dw * self.get_scale_factor()
        filt = Gsk.ScalingFilter.TRILINEAR if on_screen < self.texture.get_width() else Gsk.ScalingFilter.LINEAR
        if s * (lw / max(1, self.texture.get_width())) >= 4:
            filt = Gsk.ScalingFilter.NEAREST  # deep zoom: show real pixels
        snapshot.append_scaled_texture(self.texture, filt, rect)
        snapshot.restore()


class ImageView(Gtk.ScrolledWindow):
    """Canvas in a ScrolledWindow, plus loading for images (glycin) and PDFs (Poppler)."""
    __gtype_name__ = "SeerstoneImageView"
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "loaded": (GObject.SignalFlags.RUN_FIRST, None, (str,)),  # error text, "" on success
    }

    def __init__(self):
        super().__init__()
        self.canvas = Canvas()
        self.set_child(self.canvas)
        self.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.path: str | None = None
        self.kind: str | None = None
        self.image_size = (0, 0)
        self._seq = 0
        self._cancel: Gio.Cancellable | None = None
        self._anim_source = 0
        self._gly_image = None
        self._pdf: Poppler.Document | None = None
        self.page = 0
        self.n_pages = 0
        self._pdf_render_scale = 0.0
        self._pdf_rerender = 0
        self._pending_anchor = None

        drag = Gtk.GestureDrag.new()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        self.canvas.add_controller(drag)
        self._drag_origin = (0.0, 0.0)

        click = Gtk.GestureClick.new()
        click.connect("pressed", self._clicked)
        self.canvas.add_controller(click)

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", self._scrolled)
        self.add_controller(scroll)
        self._pointer = None
        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", lambda _c, x, y: setattr(self, "_pointer", (x, y)))
        motion.connect("leave", lambda _c: setattr(self, "_pointer", None))
        self.add_controller(motion)

        pinch = Gtk.GestureZoom.new()
        pinch.connect("begin", self._pinch_begin)
        pinch.connect("scale-changed", self._pinch_changed)
        self.add_controller(pinch)
        self._pinch_start = 1.0

        for adj in (self.get_hadjustment(), self.get_vadjustment()):
            adj.connect("changed", self._apply_pending_anchor)

    # ---------- loading ----------

    def clear(self):
        self._seq += 1
        if self._cancel:
            self._cancel.cancel()
            self._cancel = None
        self._stop_animation()
        self._gly_image = None
        self._pdf = None
        self.path = self.kind = None
        self.page = self.n_pages = 0
        self.canvas.texture = None
        self.canvas.zoom = None
        self.canvas.rotation = 0
        self.canvas.queue_resize()
        self.emit("changed")

    def load(self, path: str, kind: str, rotation: int = 0):
        self.clear()
        self.path, self.kind = path, kind
        self.canvas.rotation = rotation % 360
        if kind == "pdf":
            self._load_pdf(path)
        else:
            self._load_image(path)

    def _load_image(self, path: str):
        seq = self._seq
        self._cancel = Gio.Cancellable()
        loader = Gly.Loader.new(Gio.File.new_for_path(path))

        def loaded(_src, res):
            if seq != self._seq:
                return
            try:
                image = loader.load_finish(res)
            except GLib.Error as e:
                self._fallback_texture(path, seq, e.message)
                return
            self._gly_image = image
            image.next_frame_async(self._cancel, frame_ready)

        def frame_ready(img, res):
            if seq != self._seq:
                return
            try:
                frame = img.next_frame_finish(res)
            except GLib.Error as e:
                self._fallback_texture(path, seq, e.message)
                return
            first = self.canvas.texture is None
            self._show_texture(GlyGtk4.frame_get_texture(frame))
            if first:
                self.emit("loaded", "")
            delay = frame.get_delay()  # microseconds; 0 for a still image
            if delay > 0:
                self._anim_source = GLib.timeout_add(
                    max(20, delay // 1000),
                    lambda: self._next_frame(img, seq, frame_ready))

        loader.load_async(self._cancel, loaded)

    def _next_frame(self, img, seq, cb):
        self._anim_source = 0
        if seq == self._seq:
            img.next_frame_async(self._cancel, cb)
        return False

    def _stop_animation(self):
        if self._anim_source:
            GLib.source_remove(self._anim_source)
            self._anim_source = 0

    def _fallback_texture(self, path, seq, why):
        try:
            tex = Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            if seq == self._seq:
                self.emit("loaded", f"Can't open this image ({why})")
            return
        if seq == self._seq:
            self._show_texture(tex)
            self.emit("loaded", "")

    def _show_texture(self, tex: Gdk.Texture, logical=None):
        self.canvas.texture = tex
        self.canvas.logical = logical or (float(tex.get_width()), float(tex.get_height()))
        if logical is None:
            self.image_size = (tex.get_width(), tex.get_height())
        self.canvas.queue_resize()
        self.canvas.queue_draw()
        self.emit("changed")

    def _load_pdf(self, path: str):
        try:
            self._pdf = Poppler.Document.new_from_file(Gio.File.new_for_path(path).get_uri(), None)
        except GLib.Error as e:
            self.emit("loaded", f"Can't open this PDF ({e.message})")
            return
        self.n_pages = self._pdf.get_n_pages()
        if self.n_pages == 0:
            self.emit("loaded", "This PDF has no pages")
            return
        self.page = 0
        self._render_pdf_page(initial=True)
        self.emit("loaded", "")

    def set_page(self, page: int):
        if not self._pdf or not (0 <= page < self.n_pages) or page == self.page:
            return
        self.page = page
        self.canvas.zoom = None
        self._render_pdf_page(initial=True)
        self.get_vadjustment().set_value(0)

    def _pdf_page_logical(self):
        pw, ph = self._pdf.get_page(self.page).get_size()
        return pw * 96 / 72, ph * 96 / 72  # 100% = the page at 96 dpi

    def _render_pdf_page(self, initial=False):
        page = self._pdf.get_page(self.page)
        pw, ph = page.get_size()
        lw, lh = pw * 96 / 72, ph * 96 / 72
        sf = self.get_scale_factor() or 1
        if initial:
            # Enough for a crisp fit on a large screen.
            want = max(2.0, sf * 2.0) * 96 / 72
        else:
            want = self._display_scale() * sf * 96 / 72 * 1.25
        want = min(want, PDF_MAX_SIDE / max(pw, ph))
        want = max(want, 0.5)
        if not initial and want <= self._pdf_render_scale * 1.05:
            return
        w, h = max(1, int(pw * want)), max(1, int(ph * want))
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        ctx = cairo.Context(surf)
        ctx.set_source_rgb(1, 1, 1)
        ctx.paint()
        ctx.scale(want, want)
        page.render(ctx)
        surf.flush()
        tex = Gdk.MemoryTexture.new(
            w, h, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
            GLib.Bytes.new(bytes(surf.get_data())), surf.get_stride())
        self._pdf_render_scale = want
        self.image_size = (int(pw), int(ph))
        self._show_texture(tex, logical=(lw, lh))

    def _maybe_rerender_pdf(self):
        if self._pdf is None:
            return
        if self._pdf_rerender:
            GLib.source_remove(self._pdf_rerender)

        def go():
            self._pdf_rerender = 0
            if self._pdf is not None:
                self._render_pdf_page()
            return False
        self._pdf_rerender = GLib.timeout_add(150, go)

    # ---------- zoom / rotate ----------

    def _viewport(self):
        return max(1, self.get_width()), max(1, self.get_height())

    def _display_scale(self) -> float:
        return self.canvas.scale_for(*self._viewport())

    def zoom_percent(self) -> int:
        return round(self._display_scale() * 100)

    def is_fit(self) -> bool:
        return self.canvas.zoom is None

    def set_zoom(self, zoom: float | None, anchor=None):
        if self.canvas.texture is None:
            return
        vw, vh = self._viewport()
        fit = self.canvas.fit_scale(vw, vh)
        old = self._display_scale()
        if zoom is not None:
            zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
            # Zooming back out past "fit" lands on fit rather than skipping it.
            if abs(zoom - fit) < 1e-3 * fit or (old > fit + 1e-6 and zoom < fit):
                zoom = None
        # Remember which picture point sits under the anchor so it stays put.
        ax, ay = anchor if anchor else (vw / 2, vh / 2)
        hadj, vadj = self.get_hadjustment(), self.get_vadjustment()
        cw, ch = self.canvas.get_width(), self.canvas.get_height()
        rw, rh = self.canvas.rotated_logical()
        ox, oy = max(0.0, (cw - rw * old) / 2), max(0.0, (ch - rh * old) / 2)
        fx = (hadj.get_value() + ax - ox) / max(1e-6, rw * old)
        fy = (vadj.get_value() + ay - oy) / max(1e-6, rh * old)
        self.canvas.zoom = zoom
        new = zoom if zoom is not None else fit
        self._pending_anchor = (fx, fy, ax, ay, new)
        self.canvas.queue_resize()
        self.canvas.queue_draw()
        self._maybe_rerender_pdf()
        self.emit("changed")
        GLib.idle_add(self._apply_pending_anchor)

    def _apply_pending_anchor(self, *_a):
        if not self._pending_anchor:
            return False
        fx, fy, ax, ay, s = self._pending_anchor
        vw, vh = self._viewport()
        rw, rh = self.canvas.rotated_logical()
        cw, ch = max(vw, rw * s), max(vh, rh * s)
        hadj, vadj = self.get_hadjustment(), self.get_vadjustment()
        if hadj.get_upper() + 1 < cw or vadj.get_upper() + 1 < ch:
            return False  # layout hasn't caught up yet; "changed" will call again
        ox, oy = max(0.0, (cw - rw * s) / 2), max(0.0, (ch - rh * s) / 2)
        hadj.set_value(fx * rw * s + ox - ax)
        vadj.set_value(fy * rh * s + oy - ay)
        self._pending_anchor = None
        return False

    def zoom_by(self, factor: float, anchor=None):
        self.set_zoom(self._display_scale() * factor, anchor)

    def zoom_in(self, anchor=None):
        self.zoom_by(ZOOM_STEP, anchor)

    def zoom_out(self, anchor=None):
        self.zoom_by(1 / ZOOM_STEP, anchor)

    def zoom_fit(self):
        self.set_zoom(None)

    def zoom_actual(self):
        self.set_zoom(1.0)

    def rotate(self, quarter_turns: int):
        if self.canvas.texture is None:
            return
        self.canvas.rotation = (self.canvas.rotation + 90 * quarter_turns) % 360
        self.canvas.zoom = None
        self.canvas.queue_resize()
        self.canvas.queue_draw()
        self.emit("changed")

    @property
    def rotation(self) -> int:
        return self.canvas.rotation

    def rotated_texture(self, renderer: Gsk.Renderer) -> Gdk.Texture | None:
        """The picture as currently rotated, at full resolution (for the clipboard)."""
        tex = self.canvas.texture
        if tex is None:
            return None
        if not self.canvas.rotation:
            return tex
        w, h = tex.get_width(), tex.get_height()
        rw, rh = (h, w) if self.canvas.rotation % 180 else (w, h)
        snap = Gtk.Snapshot()
        snap.translate(Graphene.Point().init(rw / 2, rh / 2))
        snap.rotate(self.canvas.rotation)
        snap.append_texture(tex, Graphene.Rect().init(-w / 2, -h / 2, w, h))
        node = snap.to_node()
        return renderer.render_texture(node, Graphene.Rect().init(0, 0, rw, rh))

    # ---------- input ----------

    def _drag_begin(self, gesture, _x, _y):
        if self.canvas.zoom is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self._drag_origin = (self.get_hadjustment().get_value(), self.get_vadjustment().get_value())
        self.canvas.set_cursor(Gdk.Cursor.new_from_name("grabbing"))

    def _drag_update(self, _gesture, dx, dy):
        self.get_hadjustment().set_value(self._drag_origin[0] - dx)
        self.get_vadjustment().set_value(self._drag_origin[1] - dy)

    def _clicked(self, gesture, n_press, x, y):
        self.canvas.set_cursor(None)
        if n_press == 2:
            if self.canvas.zoom is None:
                hadj, vadj = self.get_hadjustment(), self.get_vadjustment()
                self.set_zoom(1.0, (x - hadj.get_value(), y - vadj.get_value()))
            else:
                self.zoom_fit()

    def _scrolled(self, controller, _dx, dy):
        state = controller.get_current_event_state()
        if not state & Gdk.ModifierType.CONTROL_MASK:
            return False
        anchor = self._pointer
        if dy < 0:
            self.zoom_in(anchor)
        elif dy > 0:
            self.zoom_out(anchor)
        return True

    def _pinch_begin(self, *_a):
        self._pinch_start = self._display_scale()

    def _pinch_changed(self, _g, scale):
        self.set_zoom(self._pinch_start * scale, self._pointer)
