"""Seerstone: a simple media browser for Omarchy.

Open a folder and every image, PDF, audio, and video file in it is listed in a
compact sidebar. Images and PDFs fit the window (letterboxed) and can be zoomed
and rotated in 90-degree steps, and a rotation can be saved back to the file.
Audio and video play with a scrubber. Anything can be shared (copied) or
exported to another format.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango  # noqa: E402

from . import convert, notes, theme  # noqa: E402
from .canvas import ImageView  # noqa: E402
from .files import KIND_ICONS, NO_INPLACE_SAVE, Item, human_size, kind_of, scan  # noqa: E402
from .media import MediaView  # noqa: E402
from .notes_ui import NotesPanel, NoteText  # noqa: E402
from .proc import Progress, run_steps  # noqa: E402

APP_ID = "com.blairwilliams.Seerstone"
DOCKS = ("left", "top", "right", "bottom")
DOCK_DEFAULT_SIZE = {"left": 240, "right": 240, "top": 34, "bottom": 34}
DOCK_SIZE_RANGE = {"left": (150, 900), "right": (150, 900), "top": (30, 400), "bottom": (30, 400)}
# Holding an arrow on audio/video: one frame per press at first, then after
# HOLD_DELAY seconds each key repeat jumps further, growing with the square of
# the time held, up to HOLD_MAX_UNITS frames per repeat.
HOLD_DELAY = 0.35
HOLD_RAMP = 4.0
HOLD_MAX_UNITS = 240
RECENT_MAX = 10
COMPACT_WIDTH = 700  # below this the control bar shows icons only
HANDLE = 6  # the paned handle's thickness (5px + its 1px border line)
STATE_PATH = os.path.join(GLib.get_user_config_dir(), "seerstone", "state.json")

SHORTCUTS = [
    ("← →", "Images and PDFs: previous / next file"),
    ("← →", "Video: step one frame (hold to scrub faster); audio: nudge"),
    ("↑ ↓", "Previous / next file"),
    (", .", "Step one frame back / forward"),
    ("N", "New note here (pauses playback)"),
    ("Shift+N", "Show / hide notes"),
    ("Home / End", "First / last file"),
    ("+  −", "Zoom in / out (or Ctrl+scroll)"),
    ("0", "Fit to window"),
    ("1", "Actual size (100%)"),
    ("R / Shift+R", "Rotate clockwise / counterclockwise"),
    ("Ctrl+S", "Save rotation to the file"),
    ("PgUp / PgDn", "Previous / next PDF page"),
    ("Space", "Play / pause"),
    ("Shift+← →", "Back / forward 5 seconds"),
    ("Esc", "Leave the note box"),
    ("M", "Mute"),
    ("Ctrl+C", "Copy image (or file)"),
    ("Ctrl+E", "Export"),
    ("Ctrl+O", "Open folder"),
    ("F or F11", "Fullscreen"),
    ("F9 or Ctrl+B", "Show / hide the sidebar"),
]


def load_state() -> dict:
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def icon_button(icon: str, tooltip: str, cb=None, *css) -> Gtk.Button:
    b = Gtk.Button.new_from_icon_name(icon)
    b.add_css_class("ss-btn")
    for c in css:
        b.add_css_class(c)
    b.set_tooltip_text(tooltip)
    b.set_focusable(False)
    if cb:
        b.connect("clicked", lambda *_a: cb())
    return b


def separator() -> Gtk.Widget:
    s = Gtk.Box()
    s.add_css_class("ss-sep")
    return s


class Toast(Gtk.Revealer):
    """One message at a time along the bottom of the stage."""

    def __init__(self):
        super().__init__()
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.END)
        box = Gtk.Box(spacing=8)
        box.add_css_class("ss-toast")
        self.box = box
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.label = Gtk.Label(xalign=0)
        self.label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.label.set_max_width_chars(60)
        inner.append(self.label)
        self.bar = Gtk.ProgressBar()
        self.bar.set_size_request(260, -1)
        inner.append(self.bar)
        box.append(inner)
        self.action = Gtk.Button()
        self.action.set_focusable(False)
        self.action.connect("clicked", self._act)
        box.append(self.action)
        self.set_child(box)
        self._cb = None
        self._timer = 0

    def notify_text(self, text: str, action: str | None = None, cb=None, progress: bool = False,
             bad: bool = False, seconds: int = 5):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self.label.set_text(text)
        self.bar.set_visible(progress)
        self.bar.set_fraction(0)
        self.action.set_visible(bool(action))
        self.action.set_label(action or "")
        self._cb = cb
        if bad:
            self.box.add_css_class("bad")
        else:
            self.box.remove_css_class("bad")
        self.set_reveal_child(True)
        if not progress and seconds:
            self._timer = GLib.timeout_add_seconds(seconds, self.dismiss)

    def progress(self, fraction: float):
        self.bar.set_fraction(fraction)

    def dismiss(self):
        self._timer = 0
        self.set_reveal_child(False)
        return False

    def _act(self, *_a):
        cb, self._cb = self._cb, None
        self.dismiss()
        if cb:
            cb()


class WidthProbe(Gtk.Widget):
    """A zero-height strip across the right-hand pane that reports its width,
    so the control bar can drop to icons when space is tight. (A Box subclass
    can't do this: GTK lays boxes out through a layout manager and never calls
    their size_allocate.)"""
    __gtype_name__ = "SeerstoneWidthProbe"

    def __init__(self, on_width):
        super().__init__()
        self._on_width = on_width
        self._width = -1
        self.set_hexpand(True)

    def do_measure(self, orientation, for_size):
        return 0, 0, -1, -1

    def do_size_allocate(self, width, height, baseline):
        if width != self._width:
            self._width = width
            GLib.idle_add(lambda: self._on_width(width) and False)


class Window(Gtk.ApplicationWindow):
    def __init__(self, app, opts, state: dict):
        super().__init__(application=app, title="Seerstone")
        self.add_css_class("seerstone")
        self.opts = opts
        self.state = state
        self.render_mode = bool(opts.render_png)
        self.set_default_size(1280, 820)
        self.set_icon_name(APP_ID)
        self.set_titlebar(Gtk.Box())  # no title bar: Omarchy tiles windows, so it only wastes room
        self.folder: str | None = None
        self.rotations: dict[str, int] = {}
        self.autoplay = bool(state.get("autoplay", True)) and not self.render_mode
        self.job_cancel: Gio.Cancellable | None = None
        self._monitor = None
        self._rescan_source = 0
        self._current: Item | None = None
        self.notes: list[notes.Note] = []
        self._notes_written: str | None = None  # what we last wrote, to tell our saves from outside edits
        self._row_boxes: dict[str, Gtk.Widget] = {}
        self._hold_dir = 0
        self._hold_start = 0
        self._hold_last = 0

        paned = Gtk.Paned()
        paned.set_wide_handle(True)  # an easy-to-grab handle for dragging the sidebar bigger or smaller
        paned.add_css_class("ss-paned")
        paned.connect("notify::position", self._sidebar_resized)
        self.paned = paned
        self.set_child(paned)
        self._applying_size = False
        self.sidebar_sizes = dict(state.get("sidebar_sizes", {}))

        dock = opts.dock or state.get("dock", "left")
        dock = dock if dock in DOCKS else "left"
        self.recent: list[str] = [p for p in state.get("recent", []) if isinstance(p, str)]
        self._install_actions(dock)
        self._build_sidebar()
        self.main = self._build_main()
        self.apply_dock(dock)

        keys = Gtk.EventControllerKey.new()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        keys.connect("key-released", lambda *_a: setattr(self, "_hold_dir", 0))
        self.add_controller(keys)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._dropped)
        self.add_controller(drop)

        self.connect("close-request", self._closing)
        if state.get("sidebar") is False:
            self.set_sidebar(False)
        if state.get("notes_open") or opts.notes_open:
            self.set_notes_open(True)
        self._update_controls()

    # ---------- layout ----------

    def _build_sidebar(self) -> Gtk.Widget:
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side.add_css_class("ss-sidebar")
        self.sidebar = side

        bar = Gtk.Box(spacing=4)
        bar.add_css_class("ss-folderbar")
        self.folder_label = Gtk.Label(label="No folder", xalign=0)
        self.folder_label.add_css_class("ss-folder")
        self.folder_label.set_hexpand(True)
        self.folder_label.set_ellipsize(Pango.EllipsizeMode.START)
        bar.append(self.folder_label)
        bar.append(icon_button("folder-open-symbolic", "Open folder (Ctrl+O)", self.choose_folder, "flat"))
        menu = Gtk.MenuButton()
        menu.set_menu_model(self._build_app_menu())
        menu.set_icon_name("open-menu-symbolic")
        menu.add_css_class("ss-btn")
        menu.add_css_class("flat")
        menu.set_focusable(False)
        menu.set_tooltip_text("Menu")
        self.app_menu = menu
        bar.append(menu)
        self.folderbar = bar
        side.append(bar)

        self.store = Gio.ListStore(item_type=Item)
        self.selection = Gtk.SingleSelection(model=self.store)
        self.selection.set_autoselect(False)
        self.selection.set_can_unselect(True)
        self.selection.connect("notify::selected", self._selected)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._row_setup)
        factory.connect("bind", self._row_bind)
        self.row_factory = factory
        self.listview = Gtk.ListView(model=self.selection, factory=factory)
        self.listview.add_css_class("ss-list")
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self.listview)
        wheel = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        wheel.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        wheel.connect("scroll", self._strip_wheel)
        scroll.add_controller(wheel)
        self.list_scroll = scroll
        side.append(scroll)

        self.count_label = Gtk.Label(xalign=0)
        self.count_label.set_valign(Gtk.Align.CENTER)
        self.count_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.count_label.add_css_class("ss-count")
        side.append(self.count_label)
        return side

    def _row_setup(self, _f, li):
        box = Gtk.Box(spacing=7)
        icon = Gtk.Image()
        icon.set_pixel_size(14)
        icon.add_css_class("ss-row-icon")
        label = Gtk.Label(xalign=0)
        label.add_css_class("ss-row-name")
        label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        label.set_max_width_chars(28)
        label.set_hexpand(True)
        noted = Gtk.Image.new_from_icon_name("document-edit-symbolic")
        noted.set_pixel_size(12)
        noted.add_css_class("ss-row-noted")
        noted.set_tooltip_text("Has notes")
        box.append(icon)
        box.append(label)
        box.append(noted)
        li.set_child(box)

    def _row_bind(self, _f, li):
        item = li.get_item()
        box = li.get_child()
        icon = box.get_first_child()
        label = icon.get_next_sibling()
        box.get_last_child().set_visible(os.path.exists(notes.notes_path(item.path)))
        self._row_boxes[item.path] = box
        icon.set_from_icon_name(KIND_ICONS[item.kind])
        label.set_text(item.name)
        # A sideways strip sizes items at their minimum width, so give names real room there.
        label.set_width_chars(-1 if getattr(self, "dock", "left") in ("left", "right") else min(len(item.name), 28))
        box.set_tooltip_text(item.name)

    def _install_actions(self, dock: str):
        def simple(name, cb, ptype=None):
            act = Gio.SimpleAction.new(name, GLib.VariantType.new(ptype) if ptype else None)
            act.connect("activate", cb)
            self.add_action(act)
            return act
        simple("open-folder", lambda *_a: self.choose_folder())
        simple("open-recent", lambda _a, v: self.open_path(v.get_string()), "s")
        simple("clear-recent", lambda *_a: self.clear_recent())
        simple("shortcuts", lambda *_a: self.show_shortcuts())
        self.autoplay_action = Gio.SimpleAction.new_stateful("autoplay", None, GLib.Variant.new_boolean(self.autoplay))
        self.autoplay_action.connect("change-state", self._autoplay_changed)
        self.add_action(self.autoplay_action)
        self.dock_action = Gio.SimpleAction.new_stateful(
            "dock", GLib.VariantType.new("s"), GLib.Variant.new_string(dock))
        self.dock_action.connect("change-state", self._dock_changed)
        self.add_action(self.dock_action)

    def _build_app_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        files = Gio.Menu()
        files.append("Open Folder…", "win.open-folder")
        self.recent_menu = Gio.Menu()
        files.append_submenu("Open Recent", self.recent_menu)
        menu.append_section(None, files)
        play = Gio.Menu()
        play.append("Autoplay audio and video", "win.autoplay")
        menu.append_section(None, play)
        side = Gio.Menu()
        for d in DOCKS:
            side.append(f"Sidebar on the {d.capitalize()}", f"win.dock::{d}")
        menu.append_section(None, side)
        help_ = Gio.Menu()
        help_.append("Keyboard Shortcuts", "win.shortcuts")
        menu.append_section(None, help_)
        self._rebuild_recent()
        return menu

    # ---------- recent folders ----------

    def _remember_folder(self, folder: str):
        recent = [p for p in self.recent if p != folder]
        self.recent = [folder] + recent[: RECENT_MAX - 1]
        if not self.render_mode:
            self.state["recent"] = self.recent
        self._rebuild_recent()

    def _rebuild_recent(self):
        menu = self.recent_menu
        menu.remove_all()
        self.recent = [p for p in self.recent if os.path.isdir(p)]
        folders = Gio.Menu()
        home = os.path.expanduser("~")
        for path in self.recent:
            name = os.path.basename(path.rstrip("/")) or path
            parent = os.path.dirname(path.rstrip("/"))
            if parent.startswith(home):
                parent = "~" + parent[len(home):]
            label = f"{name}    {parent}".replace("_", "__")  # "_" would turn into a mnemonic
            folders.append_item(Gio.MenuItem.new(label, f"win.open-recent({GLib.Variant.new_string(path).print_(False)})"))
        if not self.recent:
            folders.append("No recent folders", "win.none")
        menu.append_section(None, folders)
        clear = Gio.Menu()
        clear.append("Clear Recent", "win.clear-recent")
        menu.append_section(None, clear)

    def clear_recent(self):
        self.recent = []
        if not self.render_mode:
            self.state["recent"] = []
            save_state(self.state)
        self._rebuild_recent()

    # ---------- docking ----------

    def apply_dock(self, dock: str):
        """Put the sidebar on the left or right (a list), or the top or bottom
        (a strip that scrolls sideways)."""
        self.dock = dock
        p = self.paned
        p.set_start_child(None)
        p.set_end_child(None)
        side_view = dock in ("left", "right")
        H, V = Gtk.Orientation.HORIZONTAL, Gtk.Orientation.VERTICAL
        p.set_orientation(H if side_view else V)
        self.sidebar.set_orientation(V if side_view else H)
        self.listview.set_orientation(V if side_view else H)
        self.list_scroll.set_policy(*((Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC) if side_view
                                      else (Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)))
        self.list_scroll.set_vexpand(side_view)
        self.list_scroll.set_hexpand(not side_view)
        self.folder_label.set_hexpand(side_view)
        self.folder_label.set_max_width_chars(-1 if side_view else 18)
        for w in (self.sidebar, self.listview):
            if side_view:
                w.remove_css_class("strip")
            else:
                w.add_css_class("strip")
        self.sidebar.set_size_request(150 if side_view else -1, -1 if side_view else 30)
        self.listview.set_factory(None)  # rebind rows for the new orientation
        self.listview.set_factory(self.row_factory)
        first = dock in ("left", "top")
        if first:
            p.set_start_child(self.sidebar)
            p.set_end_child(self.main)
        else:
            p.set_start_child(self.main)
            p.set_end_child(self.sidebar)
        p.set_resize_start_child(not first)
        p.set_shrink_start_child(not first)
        p.set_resize_end_child(first)
        p.set_shrink_end_child(first)
        if self.dock_action.get_state().get_string() != dock:
            self.dock_action.set_state(GLib.Variant.new_string(dock))
        self._size_tries = 0
        self._applying_size = True
        GLib.idle_add(self._apply_sidebar_size)
        i = self.selection.get_selected()
        if i != Gtk.INVALID_LIST_POSITION:
            GLib.idle_add(lambda: self.listview.scroll_to(i, Gtk.ListScrollFlags.NONE, None) and False)

    def _paned_extent(self) -> int:
        p = self.paned
        return p.get_width() if p.get_orientation() == Gtk.Orientation.HORIZONTAL else p.get_height()

    def _apply_sidebar_size(self):
        extent = self._paned_extent()
        if extent <= 1 and self._size_tries < 100:
            self._size_tries += 1
            GLib.timeout_add(20, self._apply_sidebar_size)
            return False
        self._set_sidebar_size(self._wanted_size())
        # GTK can re-clamp the splitter for a moment after the paned flips
        # orientation; keep putting it back until it holds, and only then
        # start remembering drags as the user's chosen size.
        self._settle = [0, 0]  # ticks, consecutive ticks at the right size
        GLib.timeout_add(100, self._settle_tick)
        return False

    def _settle_tick(self):
        ticks, steady = self._settle
        want = self._wanted_size()
        pos = self.paned.get_position()
        have = pos if self.dock in ("left", "top") else self._paned_extent() - pos - HANDLE
        if abs(have - want) > 1:
            self._set_sidebar_size(want)
            steady = 0
        else:
            steady += 1
        ticks += 1
        self._settle = [ticks, steady]
        if steady >= 4 or ticks >= 20:
            self._applying_size = False
            return False
        return True

    def _wanted_size(self) -> int:
        lo, hi = DOCK_SIZE_RANGE[self.dock]
        return max(lo, min(hi, int(self.sidebar_sizes.get(self.dock, DOCK_DEFAULT_SIZE[self.dock]))))

    def _set_sidebar_size(self, size: int):
        extent = self._paned_extent()
        pos = size if self.dock in ("left", "top") else extent - size - HANDLE
        self.paned.set_position(max(0, pos))


    def _dock_changed(self, action, value):
        dock = value.get_string()
        action.set_state(value)
        if dock in DOCKS and dock != self.dock:
            self.apply_dock(dock)
            if not self.render_mode:
                self.state["dock"] = dock
                save_state(self.state)

    def _strip_wheel(self, controller, _dx, dy):
        """In the top/bottom strip, the mouse wheel scrolls sideways."""
        if self.dock in ("left", "right"):
            return False
        adj = self.list_scroll.get_hadjustment()
        step = 60 if controller.get_unit() == Gdk.ScrollUnit.WHEEL else 1
        adj.set_value(adj.get_value() + dy * step)
        return True

    @staticmethod
    def _menu_item(box, label, cb, pop=None, sensitive=True):
        b = Gtk.Button(label=label)
        b.add_css_class("ss-menu-item")
        b.get_child().set_xalign(0)
        b.set_sensitive(sensitive)

        def clicked(*_a):
            if pop:
                pop.popdown()
            cb()
        b.connect("clicked", clicked)
        box.append(b)
        return b

    def _build_main(self) -> Gtk.Widget:
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main.set_size_request(240, 160)
        main.append(WidthProbe(self._main_width))

        info = Gtk.Box(spacing=12)
        info.add_css_class("ss-info")
        self.name_label = Gtk.Label(xalign=0)
        self.name_label.add_css_class("ss-name")
        self.name_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.name_label.set_hexpand(True)
        self.meta_label = Gtk.Label(xalign=1)
        self.meta_label.add_css_class("ss-meta")
        self.meta_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.pos_label = Gtk.Label(xalign=1)
        self.pos_label.add_css_class("ss-meta")
        info.append(self.name_label)
        info.append(self.meta_label)
        info.append(self.pos_label)
        self.info_bar = info
        main.append(info)

        overlay = Gtk.Overlay()
        overlay.add_css_class("ss-stage")
        overlay.set_vexpand(True)
        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.set_hexpand(True)
        self.stack.set_hhomogeneous(False)  # size to the visible page, not the biggest one
        self.stack.set_vhomogeneous(False)
        overlay.set_child(self.stack)

        self.stack.add_named(self._build_empty(), "empty")
        self.image_view = ImageView()
        self.image_view.connect("changed", lambda *_a: self._update_controls())
        self.image_view.connect("loaded", self._image_loaded)
        self.stack.add_named(self.image_view, "image")
        self.media_view = MediaView()
        self.media_view.connect("loaded", self._media_loaded)
        self.media_view.connect("toggle-fullscreen", lambda *_a: self.toggle_fullscreen())
        self.media_view.connect("info", lambda _v, text: self.meta_label.set_text(text))
        self.stack.add_named(self.media_view, "media")
        err = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        err.set_valign(Gtk.Align.CENTER)
        err.set_halign(Gtk.Align.CENTER)
        err_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        err_icon.set_pixel_size(48)
        err_icon.add_css_class("ss-art-glyph")
        err.append(err_icon)
        self.error_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.error_label.set_max_width_chars(70)
        self.error_label.add_css_class("ss-error")
        self.error_label.set_selectable(True)
        err.append(self.error_label)
        self.stack.add_named(err, "error")

        self.prev_btn = icon_button("go-previous-symbolic", "Previous file (↑, or ← on images)", self.prev)
        self.prev_btn.add_css_class("ss-nav")
        self.prev_btn.remove_css_class("ss-btn")
        self.prev_btn.set_halign(Gtk.Align.START)
        self.prev_btn.set_valign(Gtk.Align.CENTER)
        self.prev_btn.set_margin_start(10)
        overlay.add_overlay(self.prev_btn)
        self.next_btn = icon_button("go-next-symbolic", "Next file (↓, or → on images)", self.next)
        self.next_btn.add_css_class("ss-nav")
        self.next_btn.remove_css_class("ss-btn")
        self.next_btn.set_halign(Gtk.Align.END)
        self.next_btn.set_valign(Gtk.Align.CENTER)
        self.next_btn.set_margin_end(10)
        overlay.add_overlay(self.next_btn)
        self.toast = Toast()
        overlay.add_overlay(self.toast)
        overlay.set_hexpand(True)

        self.notes_panel = NotesPanel(
            anchor=self._note_anchor, on_changed=self._notes_changed, on_jump=self._jump_to_note,
            on_copy=self.copy_notes, on_open_file=self.open_notes_file, on_close=lambda: self.set_notes_open(False),
            height=self.state.get("notes_height", 220), on_height=self._notes_resized,
            max_height=self._notes_max_height)
        # A drawer that slides up from the bottom of the media pane, just above the toolbar.
        self.notes_revealer = Gtk.Revealer()
        self.notes_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.notes_revealer.set_child(self.notes_panel)
        # Don't inherit "expand" from widgets inside the drawer; otherwise the pane
        # splits spare height with it even while it's hidden.
        self.notes_revealer.set_vexpand(False)
        self.notes_revealer.set_hexpand(False)
        self.media_view.connect("marker-activated", self._marker_activated)
        self.media_view.connect("position", self._media_position)
        overlay.set_vexpand(True)
        main.append(overlay)
        main.append(self.notes_revealer)

        # The bar never forces the window wider; when it's cramped it goes to
        # icons only, and anything still left over can be scrolled sideways.
        bar_scroll = Gtk.ScrolledWindow()
        bar_scroll.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        bar_scroll.set_propagate_natural_height(True)
        bar_scroll.set_child(self._build_controls())
        main.append(bar_scroll)
        return main

    def _main_width(self, width: int):
        compact = width < COMPACT_WIDTH
        if compact == getattr(self, "_compact", None):
            return
        self._compact = compact
        for btn in (self.notes_btn, self.share_btn, self.export_btn):
            btn.get_child().get_last_child().set_visible(not compact)
        self.fit_btn.set_visible(not compact)
        if compact:
            self.controls.add_css_class("compact")
        else:
            self.controls.remove_css_class("compact")
        self._update_controls()

    def _build_empty(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_icon_name(APP_ID)
        icon.set_pixel_size(96)
        box.append(icon)
        self.empty_title = Gtk.Label(label="Open a folder")
        self.empty_title.add_css_class("ss-empty-title")
        box.append(self.empty_title)
        self.empty_sub = Gtk.Label(label="Images, PDFs, audio, and video. Or drop a folder here.")
        self.empty_sub.add_css_class("ss-empty-sub")
        box.append(self.empty_sub)
        btn = Gtk.Button(label="Open Folder…")
        btn.add_css_class("ss-btn")
        btn.add_css_class("accent")
        btn.set_halign(Gtk.Align.CENTER)
        btn.set_margin_top(8)
        btn.connect("clicked", lambda *_a: self.choose_folder())
        box.append(btn)
        return box

    def _build_controls(self) -> Gtk.Widget:
        bar = Gtk.Box(spacing=4)
        bar.add_css_class("ss-controls")

        self.sidebar_btn = Gtk.ToggleButton()
        self.sidebar_btn.set_icon_name("sidebar-show-symbolic")
        self.sidebar_btn.add_css_class("ss-btn")
        self.sidebar_btn.add_css_class("flat")
        self.sidebar_btn.set_focusable(False)
        self.sidebar_btn.set_tooltip_text("Show / hide the sidebar (F9)")
        self.sidebar_btn.set_active(True)
        self.sidebar_btn.connect("toggled", lambda b: self.set_sidebar(b.get_active()))
        bar.append(self.sidebar_btn)
        bar.append(separator())

        self.image_controls = Gtk.Box(spacing=4)
        iv = self.image_view
        self.zoom_out_btn = icon_button("zoom-out-symbolic", "Zoom out (−)", iv.zoom_out)
        self.zoom_label = Gtk.Button(label="Fit")
        self.zoom_label.add_css_class("ss-btn")
        self.zoom_label.add_css_class("ss-zoom")
        self.zoom_label.set_focusable(False)
        self.zoom_label.set_tooltip_text("Fit to window (0) / actual size (1)")
        self.zoom_label.connect("clicked", lambda *_a: iv.zoom_actual() if iv.is_fit() else iv.zoom_fit())
        self.zoom_in_btn = icon_button("zoom-in-symbolic", "Zoom in (+)", iv.zoom_in)
        self.fit_btn = icon_button("zoom-fit-best-symbolic", "Fit to window (0)", iv.zoom_fit)
        for w in (self.zoom_out_btn, self.zoom_label, self.zoom_in_btn, self.fit_btn):
            self.image_controls.append(w)
        self.image_controls.append(separator())
        self.rot_ccw_btn = icon_button("object-rotate-left-symbolic", "Rotate counterclockwise (Shift+R)",
                                       lambda: self.rotate(-1))
        self.rot_cw_btn = icon_button("object-rotate-right-symbolic", "Rotate clockwise (R)",
                                      lambda: self.rotate(1))
        self.image_controls.append(self.rot_ccw_btn)
        self.image_controls.append(self.rot_cw_btn)
        self.save_btn = Gtk.Button(label="Save Rotation")
        self.save_btn.add_css_class("ss-btn")
        self.save_btn.add_css_class("accent")
        self.save_btn.set_focusable(False)
        self.save_btn.set_tooltip_text("Write the rotated picture back to the file (Ctrl+S)")
        self.save_btn.connect("clicked", lambda *_a: self.save_rotation())
        self.image_controls.append(self.save_btn)
        bar.append(self.image_controls)

        self.pdf_controls = Gtk.Box(spacing=4)
        self.pdf_controls.append(separator())
        self.pdf_controls.append(icon_button("go-up-symbolic", "Previous page (PgUp)", lambda: self.page(-1)))
        self.page_label = Gtk.Label()
        self.page_label.add_css_class("ss-page")
        self.pdf_controls.append(self.page_label)
        self.pdf_controls.append(icon_button("go-down-symbolic", "Next page (PgDn)", lambda: self.page(1)))
        bar.append(self.pdf_controls)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)

        self.notes_btn = Gtk.ToggleButton()
        self.notes_btn.set_child(self._icon_label("document-edit-symbolic", "Notes"))
        self.notes_btn.add_css_class("ss-btn")
        self.notes_btn.set_focusable(False)
        self.notes_btn.set_tooltip_text("Notes on this file (Shift+N; N adds one)")
        self.notes_btn.connect("toggled", lambda b: self.set_notes_open(b.get_active()))
        bar.append(self.notes_btn)

        self.share_btn = Gtk.MenuButton()
        self.share_btn.set_child(self._icon_label("send-to-symbolic", "Share"))
        self.share_btn.add_css_class("ss-btn")
        self.share_btn.set_focusable(False)
        self.share_btn.set_tooltip_text("Share")
        self.share_btn.set_create_popup_func(self._fill_share)
        self.share_btn.set_popover(Gtk.Popover())
        bar.append(self.share_btn)
        self.export_btn = Gtk.MenuButton()
        self.export_btn.set_child(self._icon_label("document-save-as-symbolic", "Export"))
        self.export_btn.add_css_class("ss-btn")
        self.export_btn.set_focusable(False)
        self.export_btn.set_tooltip_text("Export / convert (Ctrl+E)")
        self.export_btn.set_create_popup_func(self._fill_export)
        self.export_btn.set_popover(Gtk.Popover())
        bar.append(self.export_btn)
        self.controls = bar
        return bar

    @staticmethod
    def _icon_label(icon: str, text: str) -> Gtk.Widget:
        box = Gtk.Box(spacing=6)
        box.append(Gtk.Image.new_from_icon_name(icon))
        box.append(Gtk.Label(label=text))
        return box

    # ---------- folder ----------

    def open_path(self, path: str):
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(path):
            self.open_folder(path)
        elif os.path.isfile(path):
            self.open_folder(os.path.dirname(path), select=path)
        else:
            self.toast.notify_text(f"Not found: {path}", bad=True)

    def open_folder(self, folder: str, select: str | None = None):
        self.folder = folder
        self.folder_label.set_text(os.path.basename(folder.rstrip("/")) or folder)
        self.folder_label.set_tooltip_text(folder)
        self.set_title(f"Seerstone — {os.path.basename(folder.rstrip('/')) or folder}")
        self._watch(folder)
        self._fill(scan(folder), select=select, initial=True)
        self._remember_folder(folder)
        if not self.render_mode:
            self.state["folder"] = folder
            save_state(self.state)

    def _fill(self, items, select=None, initial=False):
        keep = select or (self._current.path if self._current else None)
        old_index = self.selection.get_selected()
        current = self._current
        self.selection.handler_block_by_func(self._selected)
        self.store.splice(0, self.store.get_n_items(), items)
        self.selection.handler_unblock_by_func(self._selected)
        self._current = current
        n = len(items)
        counts = {}
        for it in items:
            counts[it.kind] = counts.get(it.kind, 0) + 1
        parts = [f"{counts[k]} {label}" for k, label in
                 (("image", "img"), ("pdf", "pdf"), ("video", "vid"), ("audio", "aud")) if counts.get(k)]
        self.count_label.set_text(f"{n} files" + (f"  ·  {' '.join(parts)}" if len(parts) > 1 else ""))
        if n == 0:
            self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
            self._show(None)
            self.empty_title.set_text("Nothing to show here")
            self.empty_sub.set_text("This folder has no images, PDFs, audio, or video.")
            return
        idx = next((i for i, it in enumerate(items) if it.path == keep), None)
        if idx is None:
            idx = 0 if initial or old_index == Gtk.INVALID_LIST_POSITION else min(old_index, n - 1)
        same = self._current is not None and items[idx].path == self._current.path
        if same:
            self._current = items[idx]
            self.selection.handler_block_by_func(self._selected)
            self.selection.set_selected(idx)
            self.selection.handler_unblock_by_func(self._selected)
            self._update_position()
        else:
            self.select(idx)

    def _watch(self, folder):
        if self._monitor:
            self._monitor.cancel()
        try:
            self._monitor = Gio.File.new_for_path(folder).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            self._monitor.connect("changed", self._folder_changed)
        except GLib.Error:
            self._monitor = None

    def _folder_changed(self, _m, f, _other, event):
        name = f.get_basename() or ""
        if ".seerstone-tmp" in name:
            return
        if notes.is_notes_file(name):
            GLib.timeout_add(250, self._notes_file_changed, name)
            return
        if self._rescan_source:
            GLib.source_remove(self._rescan_source)
        self._rescan_source = GLib.timeout_add(400, self._rescan)

    def _rescan(self):
        self._rescan_source = 0
        if self.folder:
            self._fill(scan(self.folder))
        return False

    def choose_folder(self):
        dlg = Gtk.FileDialog(title="Open Folder")
        if self.folder:
            dlg.set_initial_folder(Gio.File.new_for_path(self.folder))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.open_folder(f.get_path())
        dlg.select_folder(self, None, done)

    def _dropped(self, _t, value, _x, _y):
        files = value.get_files()
        if files and files[0].get_path():
            self.open_path(files[0].get_path())
            return True
        return False

    # ---------- selection ----------

    def select(self, index: int):
        n = self.store.get_n_items()
        if n == 0:
            return
        index = max(0, min(index, n - 1))
        if self.selection.get_selected() == index and self._current is not None:
            return
        self.selection.set_selected(index)
        self.listview.scroll_to(index, Gtk.ListScrollFlags.NONE, None)

    def prev(self):
        i = self.selection.get_selected()
        if i != Gtk.INVALID_LIST_POSITION and i > 0:
            self.select(i - 1)

    def next(self):
        i = self.selection.get_selected()
        if i == Gtk.INVALID_LIST_POSITION:
            self.select(0)
        elif i + 1 < self.store.get_n_items():
            self.select(i + 1)

    def _selected(self, *_a):
        item = self.selection.get_selected_item()
        self._show(item)

    def _show(self, item: Item | None):
        self.media_view.clear()
        self.image_view.clear()
        self._current = item
        if item is None:
            self._load_notes()
            self.stack.set_visible_child_name("empty")
            self.name_label.set_text("")
            self.meta_label.set_text("")
            self.pos_label.set_text("")
            self._update_controls()
            return
        self.name_label.set_text(item.name)
        self.name_label.set_tooltip_text(item.path)
        self.meta_label.set_text(self._size_text(item))
        self._update_position()
        if item.kind in ("image", "pdf"):
            self.stack.set_visible_child_name("image")
            self.image_view.load(item.path, item.kind, self.rotations.get(item.path, 0))
        else:
            self.stack.set_visible_child_name("media")
            self.media_view.load(item.path, item.kind, autoplay=self.autoplay)
        self._load_notes()  # after load(): loading resets the scrub-bar markers
        self._update_controls()

    def _update_position(self):
        i = self.selection.get_selected()
        n = self.store.get_n_items()
        self.pos_label.set_text(f"{i + 1} / {n}" if i != Gtk.INVALID_LIST_POSITION else "")

    @staticmethod
    def _size_text(item: Item) -> str:
        try:
            return human_size(os.path.getsize(item.path))
        except OSError:
            return ""

    def _image_loaded(self, _v, error: str):
        item = self._current
        if item is None:
            return
        if error:
            self._show_error(error)
            return
        w, h = self.image_view.image_size
        ext = os.path.splitext(item.name)[1].lstrip(".").upper()
        bits = []
        if item.kind == "pdf":
            n = self.image_view.n_pages
            bits.append(f"{n} page{'s' if n != 1 else ''}")
        elif w and h:
            bits.append(f"{w} × {h}")
        bits += [self._size_text(item), ext]
        self.meta_label.set_text("  ·  ".join(b for b in bits if b))
        self._update_controls()

    def _media_loaded(self, _v, error: str):
        item = self._current
        if item is None:
            return
        if error:
            self._show_error(error)
            return
        if item.kind == "video":
            w, h = self.media_view.video_size()
            bits = [f"{w} × {h}" if w and h else "", self._size_text(item),
                    os.path.splitext(item.name)[1].lstrip(".").upper()]
            self.meta_label.set_text("  ·  ".join(b for b in bits if b))

    def _show_error(self, text: str):
        self.error_label.set_text(text)
        self.stack.set_visible_child_name("error")

    # ---------- image actions ----------

    def _can_save_rotation(self) -> bool:
        item = self._current
        if item is None or item.kind not in ("image", "pdf"):
            return False
        return os.path.splitext(item.path)[1].lower() not in NO_INPLACE_SAVE

    def rotate(self, quarter_turns: int):
        item = self._current
        if item is None or item.kind not in ("image", "pdf") or self.image_view.canvas.texture is None:
            return
        self.image_view.rotate(quarter_turns)
        self.rotations[item.path] = self.image_view.rotation
        self._update_controls()

    def page(self, delta: int):
        self.image_view.set_page(self.image_view.page + delta)
        self._update_controls()
        self._highlight_note()
        if not self.notes_panel.typing:
            self.notes_panel.refresh_anchor()

    def save_rotation(self):
        item = self._current
        rot = self.image_view.rotation
        if item is None or not rot:
            return
        if not self._can_save_rotation():
            self.toast.notify_text("This format can't be rotated in place. Use Export instead.", bad=True)
            return
        if self.job_cancel is not None:
            self.toast.notify_text("Another save or export is still running.", bad=True)
            return
        try:
            backup = convert.backup(item.path)
        except OSError as e:
            self.toast.notify_text(f"Couldn't back up the original: {e}", bad=True)
            return
        path = item.path
        self.job_cancel = Gio.Cancellable()
        self.toast.notify_text(f"Saving {item.name}…", progress=True)
        self.toast.bar.pulse()

        def done(ok, err):
            self.job_cancel = None
            if not ok:
                convert.discard(path)
                self.toast.notify_text(f"Save failed: {err.strip() or 'unknown error'}", bad=True, seconds=8)
                return
            try:
                convert.finish(path)
            except OSError as e:
                self.toast.notify_text(f"Save failed: {e}", bad=True)
                return
            self.rotations.pop(path, None)
            if self._current and self._current.path == path:
                self._show(self._current)
            self.toast.notify_text(f"Saved {os.path.basename(path)} rotated", "Undo",
                            lambda: self._undo_save(path, backup), seconds=8)

        run_steps(convert.save_rotated_job(path, item.kind, rot), done, self.job_cancel)

    def _undo_save(self, path: str, backup: str):
        try:
            tmp = convert.temp_path(path)
            shutil.copyfile(backup, tmp)
            os.replace(tmp, path)
        except OSError as e:
            self.toast.notify_text(f"Undo failed: {e}", bad=True)
            return
        if self._current and self._current.path == path:
            self._show(self._current)
        self.toast.notify_text(f"Restored the original {os.path.basename(path)}")

    # ---------- notes ----------

    def _load_notes(self):
        item = self._current
        self.notes = notes.load(item.path) if item else []
        self._notes_written = None
        self.notes_panel.set_notes(self.notes, item.kind if item else None)
        self.media_view.set_note_markers(self.notes)
        self._update_notes_button()
        self._highlight_note()

    def _note_anchor(self) -> dict:
        item = self._current
        if item is None:
            return {}
        if item.kind in ("video", "audio") and self.media_view.media is not None:
            us = self.media_view.position_us()
            fps = self.media_view.fps if item.kind == "video" else 0
            if fps:
                frame = self.media_view.frame_at(us)
                return {"time_us": int(round(frame * 1_000_000 / fps)), "frame": frame}  # the frame's own start
            return {"time_us": us, "frame": None}
        if item.kind == "pdf" and self.image_view.n_pages:
            return {"page": self.image_view.page + 1}
        return {}

    def _notes_changed(self, new_notes):
        item = self._current
        if item is None:
            return
        self.notes = new_notes
        try:
            notes.save(item.path, new_notes)
            self._notes_written = notes.render(item.name, new_notes) if new_notes else ""
        except OSError as e:
            self.toast.notify_text(f"Couldn't save notes: {e}", bad=True)
        self.media_view.set_note_markers(new_notes)
        self._update_notes_button()
        box = self._row_boxes.get(item.path)
        if box is not None and box.get_last_child() is not None:
            box.get_last_child().set_visible(bool(new_notes))

    def _notes_file_changed(self, name):
        """Something wrote a .notes.md: an AI, an editor, or us."""
        if self.folder:
            media = os.path.join(self.folder, name[: -len(notes.NOTES_SUFFIX)])
            box = self._row_boxes.get(media)
            if box is not None:
                box.get_last_child().set_visible(os.path.exists(notes.notes_path(media)))
        item = self._current
        if item is None or name != os.path.basename(notes.notes_path(item.path)):
            return False
        try:
            with open(notes.notes_path(item.path), encoding="utf-8") as fh:
                on_disk = fh.read()
        except OSError:
            on_disk = ""
        if on_disk == (self._notes_written or ""):
            return False  # our own save
        focus = self.get_focus()
        if isinstance(focus, Gtk.Text) and focus.get_ancestor(NoteText) is not None:
            return False  # don't yank a note out from under an edit; the next change will catch up
        self._load_notes()
        self.toast.notify_text(f"Notes for {item.name} were updated outside Seerstone")
        return False

    def _update_notes_button(self):
        n = sum(1 for x in self.notes if not x.done)
        label = self.notes_btn.get_child().get_last_child()
        label.set_text(f"Notes {n}" if n else "Notes")

    def _notes_max_height(self) -> int:
        main = getattr(self, "main", None)  # not built yet while the drawer is being made
        h = main.get_height() if main is not None else 0
        return int(h * 0.75) if h > 0 else 2000

    def _notes_resized(self, height: int):
        if not self.render_mode:
            self.state["notes_height"] = height
            save_state(self.state)

    def set_notes_open(self, shown: bool):
        self.notes_revealer.set_reveal_child(shown)
        if self.notes_btn.get_active() != shown:
            self.notes_btn.set_active(shown)
        if not shown and self.get_focus() is not None and self.get_focus().get_ancestor(NotesPanel):
            self.set_focus(None)
        if not self.render_mode:
            self.state["notes_open"] = shown
            save_state(self.state)

    def new_note(self):
        if self._current is None:
            return
        if self._current.kind in ("video", "audio"):
            self.media_view.pause()
        self.set_notes_open(True)
        self.notes_panel.start_note()

    def _jump_to_note(self, note):
        if note.time_us is not None:
            self.media_view.seek_to(note.time_us)
            self.media_view.pause()
        elif note.page is not None and self._current and self._current.kind == "pdf":
            self.image_view.set_page(note.page - 1)
            self._update_controls()
        self.notes_panel.select(note)
        self._highlight_note()

    def _marker_activated(self, _v, index):
        note = self.media_view.marker_note(index)
        if note is not None:
            self.set_notes_open(True)
            self._jump_to_note(note)

    def _media_position(self, *_a):
        if not self.notes_panel.typing:
            self.notes_panel.refresh_anchor()  # the stamp follows the playhead until you type
        self._highlight_note()

    def _highlight_note(self):
        item = self._current
        current = None
        if item is not None and item.kind in ("video", "audio") and self.media_view.media is not None:
            pos = self.media_view.position_us() + 20_000
            for n in self.notes:
                if n.time_us is not None and n.time_us <= pos:
                    current = n
        elif item is not None and item.kind == "pdf":
            page = self.image_view.page + 1
            current = next((n for n in self.notes if n.page == page), None)
        self.notes_panel.highlight(current)

    def notes_for_ai(self) -> str:
        item = self._current
        if item is None:
            return ""
        body = notes.render(item.name, self.notes)
        return body.replace(f"# Notes on {item.name}", f"# Notes on {item.path}", 1)

    def copy_notes(self):
        if not self.notes:
            self.toast.notify_text("No notes to copy yet")
            return
        self.get_clipboard().set(self.notes_for_ai())
        self.toast.notify_text("Notes copied, with the file's full path")

    def open_notes_file(self):
        item = self._current
        if item is None:
            return
        path = notes.notes_path(item.path)
        if not os.path.exists(path):
            self.toast.notify_text("No notes file yet. Add a note first.")
            return
        Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(self, None, None)

    # ---------- share ----------

    def _fill_share(self, menubutton):
        pop = menubutton.get_popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        item = self._current
        has = item is not None
        visual = has and item.kind in ("image", "pdf") and self.image_view.canvas.texture is not None
        self._menu_item(box, "Copy Image", self.copy_image, pop, visual)
        self._menu_item(box, "Copy File", self.copy_file, pop, has)
        self._menu_item(box, "Copy Path", self.copy_path, pop, has)
        self._menu_item(box, "Copy Notes", self.copy_notes, pop, has and bool(self.notes))
        self._menu_item(box, "Open With…", self.open_with, pop, has)
        self._menu_item(box, "Show in Folder", self.show_in_folder, pop, has)
        pop.set_child(box)

    def copy_image(self):
        tex = self.image_view.rotated_texture(self.get_renderer())
        if tex is None:
            return
        val = GObject.Value(Gdk.Texture, tex)
        self.get_clipboard().set_content(Gdk.ContentProvider.new_union([
            Gdk.ContentProvider.new_for_value(val),
            Gdk.ContentProvider.new_for_bytes("image/png", tex.save_to_png_bytes()),
        ]))
        self.toast.notify_text("Image copied")

    def copy_file(self):
        item = self._current
        if item is None:
            return
        gfile = Gio.File.new_for_path(item.path)
        uri = gfile.get_uri()
        provider = Gdk.ContentProvider.new_union([
            Gdk.ContentProvider.new_for_value(Gdk.FileList.new_from_list([gfile])),
            Gdk.ContentProvider.new_for_bytes("text/uri-list", GLib.Bytes.new((uri + "\r\n").encode())),
            Gdk.ContentProvider.new_for_bytes("text/plain;charset=utf-8", GLib.Bytes.new(item.path.encode())),
        ])
        self.get_clipboard().set_content(provider)
        self.toast.notify_text(f"Copied {item.name} — paste it into a folder, chat, or LocalSend")

    def copy_path(self):
        if self._current:
            self.get_clipboard().set(self._current.path)
            self.toast.notify_text("Path copied")

    def open_with(self):
        if not self._current:
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(self._current.path))
        launcher.set_always_ask(True)

        def done(lch, res):
            try:
                lch.launch_finish(res)
            except GLib.Error as e:
                if not e.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                    self.toast.notify_text(f"Couldn't open: {e.message}", bad=True)
        launcher.launch(self, None, done)

    def show_in_folder(self):
        if not self._current:
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(self._current.path))

        def done(lch, res):
            try:
                lch.open_containing_folder_finish(res)
            except GLib.Error as e:
                self.toast.notify_text(f"Couldn't open the folder: {e.message}", bad=True)
        launcher.open_containing_folder(self, None, done)

    # ---------- export ----------

    def _fill_export(self, menubutton):
        pop = menubutton.get_popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        item = self._current
        title = Gtk.Label(label="Export as", xalign=0)
        title.add_css_class("ss-menu-title")
        box.append(title)
        if item is None:
            self._menu_item(box, "Nothing selected", lambda: None, pop, False)
        else:
            for fid, label, ext in convert.EXPORTS[item.kind]:
                self._menu_item(box, label, lambda f=fid, e=ext: self.export(f, e), pop)
            if item.kind in ("image", "pdf") and self.image_view.rotation:
                note = Gtk.Label(label=f"Rotation ({self.image_view.rotation}°) is applied", xalign=0)
                note.add_css_class("ss-menu-title")
                box.append(note)
        pop.set_child(box)

    def export(self, fmt: str, ext: str):
        item = self._current
        if item is None:
            return
        if self.job_cancel is not None:
            self.toast.notify_text("Another save or export is still running.", bad=True)
            return
        stem = os.path.splitext(item.name)[0]
        name = f"{stem}.{ext}"
        if os.path.join(os.path.dirname(item.path), name) == item.path:
            name = f"{stem}-copy.{ext}"
        dlg = Gtk.FileDialog(title=f"Export {item.name}")
        dlg.set_initial_folder(Gio.File.new_for_path(os.path.dirname(item.path)))
        dlg.set_initial_name(name)
        kind, rotation, page = item.kind, self.image_view.rotation, self.image_view.page

        def chosen(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self._run_export(item, kind, fmt, f.get_path(), rotation, page)
        dlg.save(self, None, chosen)

    def _run_export(self, item, kind, fmt, target, rotation, page):
        mode, job = convert.export_job(item.path, kind, fmt, target, rotation, page)
        self.job_cancel = cancel = Gio.Cancellable()
        out_name = os.path.basename(target)
        self.toast.notify_text(f"Exporting {out_name}…", "Cancel", cancel.cancel, progress=True)

        def done(ok, err):
            self.job_cancel = None
            if cancel.is_cancelled():
                convert.discard(target)
                self.toast.notify_text("Export canceled")
                return
            if not ok:
                convert.discard(target)
                self.toast.notify_text(f"Export failed: {err.strip() or 'unknown error'}", bad=True, seconds=10)
                return
            try:
                convert.finish(target)
            except OSError as e:
                self.toast.notify_text(f"Export failed: {e}", bad=True)
                return
            self.toast.notify_text(f"Exported {out_name}", "Show", lambda: self._reveal(target), seconds=8)

        if mode == "ffmpeg":
            Progress(job, self.media_view.duration_us or self._probe_duration(item.path),
                     self.toast.progress, done, cancel)
        else:
            self.toast.bar.pulse()
            run_steps(job, done, cancel)

    @staticmethod
    def _probe_duration(path) -> int:
        try:
            out = GLib.spawn_sync(None, ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                                         "-of", "csv=p=0", path], None, GLib.SpawnFlags.SEARCH_PATH, None)
            return int(float(out[1].decode().strip()) * 1_000_000)
        except (GLib.Error, ValueError, IndexError):
            return 0

    def _reveal(self, path):
        if self.folder and os.path.dirname(path) == self.folder:
            for i in range(self.store.get_n_items()):
                if self.store.get_item(i).path == path:
                    self.select(i)
                    return
        Gtk.FileLauncher.new(Gio.File.new_for_path(path)).open_containing_folder(self, None, None)

    # ---------- misc ----------

    def _update_controls(self):
        item = self._current
        visual = item is not None and item.kind in ("image", "pdf")
        loaded = visual and self.image_view.canvas.texture is not None
        self.image_controls.set_visible(visual)
        for w in (self.zoom_out_btn, self.zoom_label, self.zoom_in_btn, self.fit_btn,
                  self.rot_ccw_btn, self.rot_cw_btn):
            w.set_sensitive(loaded)
        self.zoom_label.set_label("Fit" if self.image_view.is_fit() else f"{self.image_view.zoom_percent()}%")
        if loaded and self.image_view.is_fit():
            self.zoom_label.set_label(f"Fit {self.image_view.zoom_percent()}%")
        rot = self.image_view.rotation if loaded else 0
        self.save_btn.set_visible(bool(rot) and self._can_save_rotation())
        if getattr(self, "_compact", False):
            self.save_btn.set_label(f"Save {rot}°" if rot else "Save")
        else:
            self.save_btn.set_label(f"Save Rotation ({rot}°)" if rot else "Save Rotation")
        is_pdf = item is not None and item.kind == "pdf" and self.image_view.n_pages > 1
        self.pdf_controls.set_visible(is_pdf)
        if is_pdf:
            self.page_label.set_text(f"Page {self.image_view.page + 1} / {self.image_view.n_pages}")
        self.share_btn.set_sensitive(item is not None)
        self.export_btn.set_sensitive(item is not None)
        i = self.selection.get_selected()
        n = self.store.get_n_items()
        has = i != Gtk.INVALID_LIST_POSITION
        self.prev_btn.set_sensitive(has and i > 0)
        self.next_btn.set_sensitive(n > 0 and (not has or i + 1 < n))

    def toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def toggle_sidebar(self):
        self.set_sidebar(not self.sidebar.get_visible())

    def set_sidebar(self, shown: bool):
        self.sidebar.set_visible(shown)
        if self.sidebar_btn.get_active() != shown:
            self.sidebar_btn.set_active(shown)
        if not self.render_mode:
            self.state["sidebar"] = shown
            save_state(self.state)

    def show_shortcuts(self):
        pop = Gtk.Popover()
        grid = Gtk.Grid(column_spacing=18, row_spacing=4)
        grid.add_css_class("ss-help")
        for r, (k, desc) in enumerate(SHORTCUTS):
            kl = Gtk.Label(label=k, xalign=1)
            kl.add_css_class("key")
            grid.attach(kl, 0, r, 1, 1)
            grid.attach(Gtk.Label(label=desc, xalign=0), 1, r, 1, 1)
        pop.set_child(grid)
        pop.set_parent(self.app_menu)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def _autoplay_changed(self, action, value):
        action.set_state(value)
        self.autoplay = value.get_boolean()
        if not self.render_mode:
            self.state["autoplay"] = self.autoplay
            save_state(self.state)

    def _sidebar_resized(self, paned, _p):
        if self._applying_size or not self.sidebar.get_visible():
            return
        pos = paned.get_position()
        size = pos if self.dock in ("left", "top") else self._paned_extent() - pos - HANDLE
        lo, hi = DOCK_SIZE_RANGE[self.dock]
        if lo <= size <= hi:
            self.sidebar_sizes[self.dock] = size
            if not self.render_mode:
                self.state["sidebar_sizes"] = self.sidebar_sizes

    def _closing(self, *_a):
        self.media_view.clear()
        if not self.render_mode:
            self.state["volume"] = self.media_view.volume
            save_state(self.state)
        return False

    def _in_popover(self) -> bool:
        w = self.get_focus()
        while w is not None:
            if isinstance(w, Gtk.Popover):
                return True
            w = w.get_parent()
        return False

    def _typing(self) -> bool:
        focus = self.get_focus()
        return isinstance(focus, (Gtk.Text, Gtk.TextView))

    def _scrub(self, direction: int):
        """Arrow-key frame stepping. A tap moves one frame; holding the key
        repeats, and each repeat moves further the longer it's held."""
        now = GLib.get_monotonic_time()
        if self._hold_dir == direction and now - self._hold_last < 1_500_000:  # key release resets
            held = (now - self._hold_start) / 1_000_000
        else:
            self._hold_start = now
            held = 0.0
        self._hold_dir = direction
        self._hold_last = now
        units = 1 if held < HOLD_DELAY else min(HOLD_MAX_UNITS, int(1 + ((held - HOLD_DELAY) * HOLD_RAMP) ** 2))
        self.media_view.step(direction * units)

    def _key(self, _c, keyval, _code, state):
        if self._in_popover():
            return False
        if self._typing():
            if keyval == Gdk.KEY_Escape:
                self.set_focus(None)  # back to browsing; arrows navigate again
                return True
            return False
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & Gdk.ModifierType.ALT_MASK)
        if alt:
            return False
        item = self._current
        media = item is not None and item.kind in ("audio", "video")
        visual = item is not None and item.kind in ("image", "pdf")
        K = Gdk
        if ctrl:
            if keyval in (K.KEY_o, K.KEY_O):
                self.choose_folder()
            elif keyval in (K.KEY_s, K.KEY_S):
                self.save_rotation()
            elif keyval in (K.KEY_e, K.KEY_E):
                if item:
                    self.export_btn.popup()
            elif keyval in (K.KEY_c, K.KEY_C):
                if visual and self.image_view.canvas.texture is not None:
                    self.copy_image()
                else:
                    self.copy_file()
            elif keyval in (K.KEY_b, K.KEY_B):
                self.toggle_sidebar()
            elif keyval in (K.KEY_q, K.KEY_Q, K.KEY_w, K.KEY_W):
                self.close()
            elif keyval in (K.KEY_plus, K.KEY_equal, K.KEY_KP_Add) and visual:
                self.image_view.zoom_in()
            elif keyval in (K.KEY_minus, K.KEY_KP_Subtract) and visual:
                self.image_view.zoom_out()
            elif keyval in (K.KEY_0, K.KEY_KP_0) and visual:
                self.image_view.zoom_fit()
            else:
                return False
            return True
        left = keyval in (K.KEY_Left, K.KEY_KP_Left)
        right = keyval in (K.KEY_Right, K.KEY_KP_Right)
        if (left or right) and shift and media:
            self.media_view.seek_relative(-5 if left else 5)
        elif (left or right) and media:
            self._scrub(-1 if left else 1)
        elif keyval in (K.KEY_comma, K.KEY_period) and media:
            self.media_view.step(-1 if keyval == K.KEY_comma else 1)
        elif left or keyval in (K.KEY_Up, K.KEY_KP_Up):
            self.prev()
        elif right or keyval in (K.KEY_Down, K.KEY_KP_Down):
            self.next()
        elif keyval == K.KEY_n and item is not None:
            self.new_note()
        elif keyval == K.KEY_N:
            self.set_notes_open(not self.notes_revealer.get_reveal_child())
        elif keyval in (K.KEY_Home, K.KEY_KP_Home):
            self.select(0)
        elif keyval in (K.KEY_End, K.KEY_KP_End):
            self.select(self.store.get_n_items() - 1)
        elif keyval in (K.KEY_Page_Up, K.KEY_KP_Page_Up) and visual:
            self.page(-1)
        elif keyval in (K.KEY_Page_Down, K.KEY_KP_Page_Down) and visual:
            self.page(1)
        elif keyval in (K.KEY_plus, K.KEY_equal, K.KEY_KP_Add) and visual:
            self.image_view.zoom_in()
        elif keyval in (K.KEY_minus, K.KEY_underscore, K.KEY_KP_Subtract) and visual:
            self.image_view.zoom_out()
        elif keyval in (K.KEY_0, K.KEY_KP_0) and visual:
            self.image_view.zoom_fit()
        elif keyval in (K.KEY_1, K.KEY_KP_1) and visual:
            self.image_view.zoom_actual()
        elif keyval == K.KEY_r and visual:
            self.rotate(1)
        elif keyval == K.KEY_R and visual:
            self.rotate(-1)
        elif keyval == K.KEY_bracketright and visual:
            self.rotate(1)
        elif keyval == K.KEY_bracketleft and visual:
            self.rotate(-1)
        elif keyval == K.KEY_space and media:
            self.media_view.toggle_play()
        elif keyval in (K.KEY_m, K.KEY_M) and media:
            self.media_view.toggle_mute()
        elif keyval in (K.KEY_f, K.KEY_F, K.KEY_F11):
            self.toggle_fullscreen()
        elif keyval == K.KEY_Escape and self.is_fullscreen():
            self.unfullscreen()
        elif keyval == K.KEY_F9:
            self.toggle_sidebar()
        elif keyval in (K.KEY_question, K.KEY_F1):
            self.show_shortcuts()
        else:
            return False
        return True


class App(Gtk.Application):
    def __init__(self, opts):
        render = bool(opts.render_png)
        super().__init__(
            application_id=APP_ID + (".Render" if render else ""),
            flags=Gio.ApplicationFlags.HANDLES_OPEN | (Gio.ApplicationFlags.NON_UNIQUE if render else 0))
        self.opts = opts
        self.css = Gtk.CssProvider()
        self.win: Window | None = None
        self.state = {} if render else load_state()
        GLib.set_application_name("Seerstone")

    def do_startup(self):
        Gtk.Application.do_startup(self)
        Gtk.Window.set_default_icon_name(APP_ID)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._load_theme()
        if self.opts.render_png:
            # Offscreen Broadway has no frame clock ticking, so animations never finish.
            Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
        else:
            self._watcher = theme.ThemeWatcher(self._load_theme)

    def _load_theme(self):
        pal = theme.load_palette(theme.theme_colors_path(self.opts.omarchy_theme))
        self.css.load_from_string(theme.build_css(pal))
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-application-prefer-dark-theme", theme.is_dark(pal))
        if self.win is not None:
            self.win.media_view.set_accent(pal["accent"])
        self._accent = pal["accent"]

    def _window(self) -> Window:
        if self.win is None:
            self.win = Window(self, self.opts, self.state)
            self.win.media_view.set_accent(self._accent)
            if "volume" in self.state:
                self.win.media_view.vol.set_value(float(self.state["volume"]))
            if self.opts.render_png:
                w, h = (int(x) for x in self.opts.size.lower().split("x"))
                self.win.set_default_size(w, h)
        return self.win

    def do_activate(self):
        win = self._window()
        win.present()
        if self.opts.path:
            win.open_path(self.opts.path)
        elif self.state.get("folder") and os.path.isdir(self.state["folder"]) and win.folder is None:
            win.open_folder(self.state["folder"])
        if self.opts.render_png:
            GLib.timeout_add(int(self.opts.render_delay), self._render_prep)

    def do_open(self, files, _n, _hint):
        win = self._window()
        win.present()
        if files and files[0].get_path():
            win.open_path(files[0].get_path())

    # ---------- dev rendering ----------

    def _render_prep(self):
        win = self.win
        o = self.opts
        if o.select:
            for i in range(win.store.get_n_items()):
                if win.store.get_item(i).name == o.select:
                    win.select(i)
        GLib.timeout_add(900, self._render_actions)
        return False

    def _render_actions(self):
        win = self.win
        o = self.opts
        for _ in range(o.rotate % 4):
            win.rotate(1)
        if o.zoom:
            win.image_view.set_zoom(float(o.zoom))
        if o.page:
            win.image_view.set_page(o.page - 1)
            win._update_controls()
        if o.toast:
            win.toast.notify_text(o.toast, "Undo", lambda: None)
        GLib.timeout_add(900, self._render)
        return False

    def _render(self):
        win = self.win
        w, h = win.get_width(), win.get_height()
        paintable = Gtk.WidgetPaintable.new(win)
        snap = Gtk.Snapshot()
        paintable.snapshot(snap, w, h)
        node = snap.to_node()
        tex = win.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
        tex.save_to_png(self.opts.render_png)
        print(f"rendered {self.opts.render_png} ({w}x{h})")
        win.media_view.clear()
        self.quit()
        return False


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="seerstone", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", nargs="?", help="a folder to open, or a file to open its folder at")
    p.add_argument("--render-png", metavar="PATH", help="dev: render the window to a PNG and quit")
    p.add_argument("--size", default="1280x820", help="dev: window size for --render-png")
    p.add_argument("--select", help="dev: file name to select before rendering")
    p.add_argument("--rotate", type=int, default=0, help="dev: quarter turns clockwise before rendering")
    p.add_argument("--zoom", help="dev: zoom factor before rendering (1 = 100%%)")
    p.add_argument("--page", type=int, default=0, help="dev: PDF page (1-based) before rendering")
    p.add_argument("--toast", help="dev: show this toast before rendering")
    p.add_argument("--render-delay", default="900", help="dev: ms to wait before acting")
    p.add_argument("--omarchy-theme", help="dev: style with this Omarchy theme")
    p.add_argument("--notes-open", action="store_true", help="dev: open the notes panel")
    p.add_argument("--dock", choices=DOCKS, help="put the sidebar on this side (remembered otherwise)")
    opts = p.parse_args(argv)
    if opts.path and not os.path.exists(os.path.expanduser(opts.path)):
        print(f"seerstone: not found: {opts.path}", file=sys.stderr)
        return 1
    app = App(opts)
    if opts.path and not opts.render_png:
        # Hand the path to an already-running Seerstone instead of a second window.
        app.register(None)
        if app.get_is_remote():
            app.open([Gio.File.new_for_path(os.path.abspath(os.path.expanduser(opts.path)))], "")
            return 0
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
