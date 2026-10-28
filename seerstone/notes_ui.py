"""The notes panel beside the stage, and the note markers on the scrub bar."""
from __future__ import annotations

from gi.repository import Gdk, GLib, GObject, Graphene, Gtk, Pango

from .notes import Note, fmt_time

PANEL_HEIGHT = 220          # default drawer height
PANEL_MIN_HEIGHT = 110
MARKER_HIT = 7  # px either side of a marker that counts as hovering/clicking it
SCALE_INSET = 6  # the scrub bar's slider travels this far in from each end


class MarkerLane(Gtk.Widget):
    """A thin lane above the scrub bar with one marker per timed note.
    Hover shows the note; click jumps to it."""
    __gtype_name__ = "SeerstoneMarkerLane"
    __gsignals__ = {"activated": (GObject.SignalFlags.RUN_FIRST, None, (int,))}

    def __init__(self, inset: float = SCALE_INSET, height: int = 14):
        super().__init__()
        self.inset = inset
        self.markers: list[tuple[float, str, bool]] = []  # (fraction, text, done)
        self.accent = Gdk.RGBA()
        self.accent.parse("#e68e0d")
        self.set_size_request(-1, height)
        self.set_hexpand(True)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)
        click = Gtk.GestureClick.new()
        click.connect("released", self._clicked)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", self._motion)
        self.add_controller(motion)

    def set_markers(self, markers):
        self.markers = markers
        self.queue_draw()

    def x_for(self, fraction: float) -> float:
        w = self.get_width()
        return self.inset + fraction * max(0.0, w - 2 * self.inset)

    def hit(self, x: float) -> int:
        best, best_d = -1, MARKER_HIT + 1
        for i, (f, _t, _d) in enumerate(self.markers):
            d = abs(self.x_for(f) - x)
            if d < best_d:
                best, best_d = i, d
        return best

    def _tooltip(self, _w, x, _y, _kb, tooltip):
        i = self.hit(x)
        if i < 0:
            return False
        tooltip.set_text(self.markers[i][1])
        return True

    def _clicked(self, _g, _n, x, _y):
        i = self.hit(x)
        if i >= 0:
            self.emit("activated", i)

    def _motion(self, _c, x, _y):
        self.set_cursor(Gdk.Cursor.new_from_name("pointer") if self.hit(x) >= 0 else None)

    def do_snapshot(self, snapshot):
        h = self.get_height()
        dim = self.accent.copy()
        dim.alpha = 0.35
        for f, _t, done in self.markers:
            x = self.x_for(f)
            color = dim if done else self.accent
            # A pin: a square head over a stem that reaches down to the scrub bar.
            snapshot.append_color(color, Graphene.Rect().init(x - 4.5, 0, 9, 8))
            snapshot.append_color(color, Graphene.Rect().init(x - 1, 0, 2, h))


class NoteText(Gtk.Stack):
    """Wrapping note text; double-click to edit, Enter or click away to keep."""
    __gtype_name__ = "SeerstoneNoteText"

    def __init__(self, text: str, on_commit):
        super().__init__()
        self._on_commit = on_commit
        self.label = Gtk.Label(label=text, xalign=0, wrap=True)
        self.label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.label.set_max_width_chars(140)
        self.label.add_css_class("ss-note-text")
        self.label.set_tooltip_text("Double-click to edit")
        click = Gtk.GestureClick.new()
        click.connect("pressed", lambda _g, n, _x, _y: self.edit() if n == 2 else None)
        self.label.add_controller(click)
        self.entry = Gtk.Entry(text=text)
        self.entry.connect("activate", lambda *_a: self._commit())
        focus = Gtk.EventControllerFocus.new()
        focus.connect("leave", lambda *_a: self._commit())
        self.entry.add_controller(focus)
        self.add_named(self.label, "show")
        self.add_named(self.entry, "edit")
        self.set_hhomogeneous(False)
        self.set_vhomogeneous(False)

    def edit(self):
        self.entry.set_text(self.label.get_text())
        self.set_visible_child_name("edit")
        self.entry.grab_focus()

    def _commit(self):
        if self.get_visible_child_name() != "edit":
            return
        self.set_visible_child_name("show")
        GLib.idle_add(lambda: self._on_commit(self.entry.get_text()) and False)


class NotesPanel(Gtk.Box):
    """Lists the current file's notes and adds new ones.

    The window supplies anchor() -> dict(time_us=..., frame=..., page=...) for
    "where am I right now", and gets on_changed(notes) / on_jump(note) back.
    """
    __gtype_name__ = "SeerstoneNotesPanel"

    def __init__(self, anchor, on_changed, on_jump, on_copy, on_open_file, on_close,
                 height=PANEL_HEIGHT, on_height=None, max_height=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("ss-notes")
        self._on_height = on_height
        self._max_height = max_height or (lambda: 2000)
        self.set_height(height)

        # Drag the top edge to make the drawer taller or shorter.
        grip = Gtk.Box()
        grip.add_css_class("ss-notes-grip")
        grip.set_cursor(Gdk.Cursor.new_from_name("ns-resize"))
        grip.set_tooltip_text("Drag to resize")
        drag = Gtk.GestureDrag.new()
        drag.connect("drag-begin", lambda *_a: setattr(self, "_drag_from", self.height))
        drag.connect("drag-update", lambda _g, _dx, dy: self.set_height(self._drag_from - dy))
        drag.connect("drag-end", lambda *_a: self._on_height and self._on_height(self.height))
        grip.add_controller(drag)
        self.append(grip)
        self._anchor = anchor
        self._on_changed = on_changed
        self._on_jump = on_jump
        self.notes: list[Note] = []
        self.kind: str | None = None
        self._pending_anchor: dict | None = None
        self._rows: list[Gtk.ListBoxRow] = []

        head = Gtk.Box(spacing=4)
        head.add_css_class("ss-notes-head")
        self.title = Gtk.Label(label="Notes", xalign=0)
        self.title.add_css_class("ss-notes-title")
        self.title.set_hexpand(True)
        head.append(self.title)
        for icon, tip, cb in (("edit-copy-symbolic", "Copy these notes (for pasting into an AI chat)", on_copy),
                              ("document-open-symbolic", "Open the notes file in a text editor", on_open_file),
                              ("window-close-symbolic", "Hide notes (Shift+N)", on_close)):
            b = Gtk.Button.new_from_icon_name(icon)
            b.add_css_class("ss-btn")
            b.add_css_class("flat")
            b.set_focusable(False)
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda *_a, c=cb: c())
            head.append(b)
        self.append(head)

        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("ss-notes-list")
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_activate_on_single_click(False)
        # Like a chat log: when the notes don't fill the drawer, they stack up
        # from the bottom, against the note box, not from the top.
        self.listbox.set_valign(Gtk.Align.END)
        self.empty = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.empty.add_css_class("ss-notes-empty")
        self.listbox.set_placeholder(self.empty)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(self.listbox)
        self.list_scroll = scroll
        self._reveal_row = None
        self._reveal_timer = 0
        self._apply_source = 0
        # GTK updates the scroll range before it moves the rows, so measure
        # only after layout has finished (an idle at lower priority than redraw).
        scroll.get_vadjustment().connect("changed", lambda *_a: self._queue_apply())
        # Scrolling the list yourself ends any automatic positioning right away.
        wheel = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        wheel.connect("scroll-begin", lambda *_a: setattr(self, "_reveal_row", None))
        wheel.connect("scroll", lambda *_a: setattr(self, "_reveal_row", None) or False)
        scroll.add_controller(wheel)
        self.append(scroll)

        add = Gtk.Box(spacing=8)  # one line: [pin] where it goes  [ entry ............ ]
        add.add_css_class("ss-notes-add")
        anchor_row = Gtk.Box(spacing=6)
        anchor_row.set_valign(Gtk.Align.CENTER)
        self.pin = Gtk.CheckButton()
        self.pin.set_active(True)
        self.pin.set_focusable(False)
        self.pin.set_tooltip_text("Attach the note to this moment / page (off: a note on the whole file)")
        self.pin.connect("toggled", lambda *_a: self.refresh_anchor())
        anchor_row.append(self.pin)
        self.anchor_label = Gtk.Label(xalign=0)
        self.anchor_label.add_css_class("ss-notes-anchor")
        anchor_row.append(self.anchor_label)
        add.append(anchor_row)
        self.entry = Gtk.Entry()
        self.entry.set_hexpand(True)
        self.entry.set_placeholder_text("Add a note, then Enter")
        self.entry.connect("activate", self._add)
        self.entry.connect("changed", self._typed)
        add.append(self.entry)
        self.append(add)

    def set_height(self, h: float):
        self.height = int(max(PANEL_MIN_HEIGHT, min(h, max(PANEL_MIN_HEIGHT, self._max_height()))))
        self.set_size_request(-1, self.height)

    # ---------- content ----------

    def set_notes(self, notes: list[Note], kind: str | None):
        self.notes = notes
        self.kind = kind
        self.pin.set_visible(kind in ("video", "audio", "pdf"))
        self.empty.set_text({
            "video": "No notes yet. Pause on a frame and type one below, or press N.",
            "audio": "No notes yet. Pause at a moment and type one below, or press N.",
            "pdf": "No notes yet. Type one below to note this page, or press N.",
            "image": "No notes yet. Type one below, or press N.",
        }.get(kind, "Open a file to take notes."))
        self.entry.set_sensitive(kind is not None)
        self._rebuild()
        self.refresh_anchor()

    def _rebuild(self):
        self.listbox.remove_all()
        self._rows = []
        for note in self.notes:
            row = self._row(note)
            self.listbox.append(row)
            self._rows.append(row)
        n = len(self.notes)
        open_n = sum(1 for x in self.notes if not x.done)
        self.title.set_text(f"Notes  {open_n} open · {n - open_n} done" if n else "Notes")

    def _row(self, note: Note) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.add_css_class("ss-note")
        if note.done:
            row.add_css_class("done")
        box = Gtk.Box(spacing=6)
        check = Gtk.CheckButton()
        check.set_active(note.done)
        check.set_valign(Gtk.Align.START)
        check.set_tooltip_text("Done")
        check.connect("toggled", lambda c: self._set_done(note, c.get_active()))
        box.append(check)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        body.set_hexpand(True)
        if note.anchored:
            jump = Gtk.Button(label=note.anchor_text() + (f"  ·  frame {note.frame}" if note.frame is not None else ""))
            jump.add_css_class("ss-note-anchor")
            jump.set_halign(Gtk.Align.START)
            jump.set_focusable(False)
            jump.set_tooltip_text("Jump here")
            jump.connect("clicked", lambda *_a: self._on_jump(note))
            body.append(jump)
        body.append(NoteText(note.text, lambda t: self._edited(note, t)))
        box.append(body)
        delete = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        delete.add_css_class("ss-btn")
        delete.add_css_class("flat")
        delete.add_css_class("ss-note-delete")
        delete.set_valign(Gtk.Align.START)
        delete.set_focusable(False)
        delete.set_tooltip_text("Delete note")
        delete.connect("clicked", lambda *_a: self._delete(note))
        box.append(delete)
        row.set_child(box)
        return row

    def _changed(self):
        self.notes.sort(key=Note.sort_key)
        for i, n in enumerate(self.notes):
            n._order = i
        self._on_changed(self.notes)
        self._rebuild()

    def _set_done(self, note, done):
        if note.done != done:
            note.done = done
            GLib.idle_add(lambda: self._changed() and False)

    def _edited(self, note, raw):
        text = " ".join(raw.split())
        if not text:
            self._delete(note)
        elif text != note.text:
            note.text = text
            self._changed()

    def _delete(self, note):
        if note in self.notes:
            self.notes.remove(note)
            self._changed()

    # ---------- adding ----------

    def refresh_anchor(self):
        a = self._pending_anchor or self._anchor() or {}
        if not self.pin.get_visible() or not self.pin.get_active() or not a:
            what = {"image": "this image", "pdf": "this PDF", "video": "this video", "audio": "this audio"}
            self.anchor_label.set_text(f"New note on {what.get(self.kind, 'this file')}")
        elif a.get("time_us") is not None:
            frame = f", frame {a['frame']}" if a.get("frame") is not None else ""
            self.anchor_label.set_text(f"New note at {fmt_time(a['time_us'])}{frame}")
        elif a.get("page") is not None:
            self.anchor_label.set_text(f"New note on page {a['page']}")

    def _typed(self, entry):
        """A note is stamped where you were when you started typing it. Until
        then (an empty box), the stamp follows the playhead / page."""
        if entry.get_text().strip():
            if self._pending_anchor is None:
                self._pending_anchor = self._anchor()
                self.refresh_anchor()
        elif self._pending_anchor is not None:
            self._pending_anchor = None
            self.refresh_anchor()

    @property
    def typing(self) -> bool:
        return self._pending_anchor is not None

    def start_note(self):
        self.entry.grab_focus()

    def _add(self, entry):
        text = " ".join(entry.get_text().split())
        if not text or self.kind is None:
            return
        a = (self._pending_anchor or self._anchor() or {}) if self.pin.get_visible() and self.pin.get_active() else {}
        note = Note(text=text, time_us=a.get("time_us"), frame=a.get("frame"), page=a.get("page"),
                    _order=len(self.notes))
        self.notes.append(note)
        entry.set_text("")  # clears the stamp: the next note gets wherever you are when you type it
        self._changed()
        self.select(note)
        self.refresh_anchor()

    def select(self, note: Note | None):
        if note is None or note not in self.notes:
            self.listbox.unselect_all()
            return
        row = self._rows[self.notes.index(note)]
        self.listbox.select_row(row)
        self._reveal(row)

    def _reveal(self, row):
        """Scroll so this note sits at the bottom of the list, right above the
        note box, without taking focus. (Clamped: a note near the top of a long
        list can't go lower than the list allows.) The list was rebuilt just
        before this, so keep re-applying as the layout settles, then let go so
        manual scrolling isn't fought."""
        self._reveal_row = row
        if self._reveal_timer:
            GLib.source_remove(self._reveal_timer)
        self._reveal_timer = GLib.timeout_add(1000, self._end_reveal)
        self._queue_apply()

    def _queue_apply(self):
        if self._reveal_row is not None and not self._apply_source:
            self._apply_source = GLib.idle_add(self._apply_queued, priority=GLib.PRIORITY_DEFAULT_IDLE)

    def _apply_queued(self):
        self._apply_source = 0
        return self._apply_reveal()

    def _reveal_tick(self, _widget, _clock):
        if self._reveal_row is None:
            self._ticking = False
            return False
        self._queue_apply()
        return True

    def _end_reveal(self):
        self._apply_reveal()
        self._reveal_row = None
        self._reveal_timer = 0
        return False

    def _apply_reveal(self, *_a):
        row = self._reveal_row
        if row is not None and not getattr(self, "_ticking", False):
            self._ticking = True
            self.add_tick_callback(self._reveal_tick)
        if row is None or row.get_parent() is None or row.get_height() <= 0:
            return False
        adj = self.list_scroll.get_vadjustment()
        bottom_of_list = max(0.0, adj.get_upper() - adj.get_page_size())
        if self._rows and row is self._rows[-1]:
            adj.set_value(bottom_of_list)
            return False
        # The row's full box (padding and border included), measured against
        # the list itself, which doesn't move when it scrolls. When the list is
        # shorter than the view it sits at the bottom (chat-style), so shift by
        # the empty space above it.
        ok, box = row.compute_bounds(self.listbox)
        if ok:
            page = adj.get_page_size()
            bottom = box.get_y() + box.get_height() + max(0.0, page - self.listbox.get_height())
            adj.set_value(max(0.0, min(bottom_of_list, bottom - page)))
        return False

    def highlight(self, note: Note | None):
        """Mark the note at (or just before) the play position without moving focus."""
        for row, n in zip(self._rows, self.notes):
            if n is note:
                row.add_css_class("current")
            else:
                row.remove_css_class("current")
