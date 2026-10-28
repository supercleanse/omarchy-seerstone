"""Omarchy look: colors from the active theme's colors.toml, the system monospace
font (what `omarchy font set` changes), and a watcher so `omarchy theme set`
restyles the open window live."""
from __future__ import annotations

import os
import tomllib

STATE_DIR = os.path.expanduser("~/.local/state/omarchy/current")
CURRENT_COLORS = os.path.join(STATE_DIR, "theme", "colors.toml")
THEME_DIRS = (os.path.expanduser("~/.config/omarchy/themes"), "/usr/share/omarchy/themes")

# Matte Black, used when no Omarchy theme can be read.
FALLBACK = {
    "accent": "#e68e0d", "background": "#121212", "lighter_background": "#1e1e1e",
    "dark_background": "#0d0d0d", "selection": "#2a2a2a", "muted": "#333333",
    "foreground": "#bebebe", "light_foreground": "#8a8a8d", "red": "#D35F5F",
}


def theme_colors_path(name: str | None) -> str:
    if name:
        slug = name.lower().replace(" ", "-")
        for base in THEME_DIRS:
            path = os.path.join(base, slug, "colors.toml")
            if os.path.exists(path):
                return path
    return CURRENT_COLORS


def load_palette(path: str = CURRENT_COLORS) -> dict:
    pal = dict(FALLBACK)
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        pal.update({k: v for k, v in data.items() if isinstance(v, str)})
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return pal


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")[:6]
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return 0.0

    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def on_color(bg: str, pal: dict) -> str:
    """Readable text on top of bg: whichever of the theme's background or
    foreground contrasts more with it."""
    return max((pal["background"], pal["foreground"]), key=lambda c: _contrast(bg, c))


def is_dark(pal: dict) -> bool:
    return _luminance(pal["background"]) < 0.4


def build_css(pal: dict) -> str:
    p = pal
    defs = {
        "ss_bg": p["background"],
        "ss_stage": p.get("dark_background", p["background"]),
        "ss_panel": p.get("lighter_background", p["background"]),
        "ss_fg": p["foreground"],
        "ss_dim": p.get("light_foreground", p["foreground"]),
        "ss_hover": p.get("selection", p.get("lighter_background", p["background"])),
        "ss_border": p.get("muted", p.get("lighter_background", p["background"])),
        "ss_accent": p["accent"],
        "ss_accent_fg": on_color(p["accent"], p),
        "ss_red": p.get("red", FALLBACK["red"]),
    }
    lines = [f"@define-color {k} {v};" for k, v in defs.items()]
    return "\n".join(lines) + "\n" + STYLE


STYLE = """
window.seerstone { background: @ss_bg; color: @ss_fg; font-family: monospace, "Noto Color Emoji"; }
window.seerstone .titlebar, window.seerstone headerbar { min-height: 0; }

/* Sidebar */
.ss-sidebar { background: @ss_bg; }
paned.ss-paned > separator { min-width: 5px; background: @ss_bg; background-image: none;
  border-left: 1px solid @ss_border; border-right: none; box-shadow: none; }
paned.ss-paned.vertical > separator { min-width: 0; min-height: 5px; border-left: none;
  border-top: 1px solid @ss_border; }
paned.ss-paned.vertical > separator:hover { border-top-color: @ss_accent; }
.ss-sidebar.strip .ss-folderbar { border-bottom: none; border-right: 1px solid @ss_border; padding: 0 6px 0 10px; }
.ss-sidebar.strip .ss-count { border-top: none; border-left: 1px solid @ss_border; padding: 0 10px; }
.ss-list.strip > row { padding: 0 12px; border-right: 1px solid alpha(@ss_border, 0.7); }
paned.ss-paned > separator:hover { background: alpha(@ss_accent, 0.5); border-left-color: @ss_accent; }
.ss-folderbar { padding: 6px 6px 6px 10px; border-bottom: 1px solid @ss_border; }
.ss-folder { font-size: 12px; font-weight: bold; }
.ss-count { font-size: 11px; color: @ss_dim; padding: 4px 10px; border-top: 1px solid @ss_border; }
.ss-list { background: transparent; }
.ss-list > row { padding: 3px 8px; min-height: 0; border-radius: 0; }
.ss-list > row:hover { background: @ss_hover; }
.ss-list > row:selected { background: @ss_accent; color: @ss_accent_fg; }
.ss-list > row:selected .ss-row-icon { color: @ss_accent_fg; }
.ss-row-name { font-size: 12px; }
.ss-row-icon { color: @ss_dim; }

/* Info strip */
.ss-info { padding: 6px 12px; border-bottom: 1px solid @ss_border; }
.ss-name { font-size: 13px; font-weight: bold; }
.ss-meta { font-size: 11px; color: @ss_dim; }

/* Stage */
.ss-stage { background: @ss_stage; }
.ss-stage scrolledwindow, .ss-stage viewport { background: transparent; }
.ss-empty-title { font-size: 15px; font-weight: bold; }
.ss-empty-sub { font-size: 12px; color: @ss_dim; }
.ss-error { font-size: 12px; color: @ss_red; }

/* Prev/next arrows floating on the stage */
.ss-nav { background: alpha(@ss_bg, 0.55); color: @ss_fg; border: 1px solid alpha(@ss_border, 0.8);
  border-radius: 0; min-width: 34px; min-height: 64px; padding: 0; box-shadow: none; }
.ss-nav:hover { background: alpha(@ss_accent, 0.9); color: @ss_accent_fg; }
.ss-nav:disabled { opacity: 0; }

/* Bottom control bar */
.ss-controls { padding: 6px 8px; border-top: 1px solid @ss_border; background: @ss_bg; }
button.ss-btn, menubutton.ss-btn > button {
  background: @ss_panel; color: @ss_fg; border: 1px solid @ss_border; border-radius: 0;
  box-shadow: none; background-image: none; min-height: 30px; min-width: 30px; padding: 0 8px; }
button.ss-btn:hover, menubutton.ss-btn > button:hover { background: @ss_hover; }
button.ss-btn:disabled { opacity: 0.4; }
button.ss-btn.accent { background: @ss_accent; color: @ss_accent_fg; border-color: @ss_accent; }
button.ss-btn.flat { background: transparent; border-color: transparent; }
button.ss-btn.flat:hover { background: @ss_hover; }
button.ss-btn.flat:checked { background: transparent; color: @ss_accent; }
menubutton.ss-btn.flat > button { background: transparent; border-color: transparent; }
menubutton.ss-btn.flat > button:hover { background: @ss_hover; }
.ss-zoom { font-size: 12px; min-width: 5em; }
.ss-controls.compact button.ss-btn, .ss-controls.compact menubutton.ss-btn > button {
  padding: 0 5px; min-width: 24px; }
.ss-controls.compact .ss-zoom { min-width: 0; font-size: 11px; }
.ss-controls.compact .ss-sep { margin: 4px 2px; }
.ss-sep { background: @ss_border; min-width: 1px; margin: 4px 6px; }
.ss-page { font-size: 12px; color: @ss_dim; min-width: 5em; }

popover, popover.menu { font-family: monospace, "Noto Color Emoji"; }
popover contents, popover.menu contents { background: @ss_bg; color: @ss_fg;
  border: 1px solid @ss_border; border-radius: 0; }
popover modelbutton:hover { background: @ss_hover; }
popover .ss-menu-title { font-size: 11px; color: @ss_dim; padding: 6px 10px 2px 10px; }
popover button.ss-menu-item { background: transparent; border: none; border-radius: 0; box-shadow: none;
  padding: 6px 12px; min-height: 0; color: @ss_fg; }
popover button.ss-menu-item:hover { background: @ss_hover; }
popover button.ss-menu-item:disabled { opacity: 0.4; }

/* Media */
.ss-transport { padding: 8px 12px; background: alpha(@ss_bg, 0.85); border-top: 1px solid @ss_border; }
.ss-time { font-size: 12px; color: @ss_dim; min-width: 3.2em; }
scale trough { background: @ss_panel; border: none; outline: none; border-radius: 0; min-height: 4px; }
scale highlight { background: @ss_accent; border-radius: 0; }
scale slider { background: @ss_fg; border-radius: 0; min-width: 12px; min-height: 12px; margin: -4px; }
.ss-audio-title { font-size: 18px; font-weight: bold; }
.ss-audio-sub { font-size: 13px; color: @ss_dim; }
.ss-audio-meta { font-size: 11px; color: @ss_dim; }
.ss-art { background: @ss_panel; border: 1px solid @ss_border; }
.ss-art-glyph { color: @ss_dim; }

/* Toast */
.ss-toast { background: @ss_panel; color: @ss_fg; border: 1px solid @ss_border; padding: 6px 8px 6px 12px;
  margin-bottom: 14px; font-size: 12px; }
.ss-toast.bad { border-color: @ss_red; }
.ss-toast button { background: transparent; color: @ss_accent; border: none; box-shadow: none;
  border-radius: 0; font-weight: bold; padding: 2px 8px; min-height: 0; }
.ss-toast button:hover { background: @ss_hover; }
.ss-toast progressbar trough { min-height: 4px; background: @ss_bg; border-radius: 0; }
.ss-toast progressbar progress { min-height: 4px; background: @ss_accent; border-radius: 0; }

/* Notes */
.ss-row-noted { color: @ss_accent; }
.ss-list > row:selected .ss-row-noted { color: @ss_accent_fg; }
.ss-notes { background: @ss_bg; border-top: 1px solid @ss_border; }
.ss-notes-grip { min-height: 5px; background: transparent; }
.ss-notes-grip:hover { background: alpha(@ss_accent, 0.5); }
.ss-notes-head { padding: 0 6px 2px 12px; border-bottom: 1px solid @ss_border; }
.ss-notes-title { font-size: 12px; font-weight: bold; }
.ss-notes-list { background: transparent; }
.ss-notes-list > row { padding: 6px 8px 6px 12px; border-bottom: 1px solid alpha(@ss_border, 0.6); border-radius: 0; }
.ss-notes-list > row:selected { background: @ss_hover; }
.ss-notes-list > row.current { box-shadow: inset 3px 0 @ss_accent; }
.ss-notes-list > row.done .ss-note-text { color: @ss_dim; }
.ss-note-anchor { background: transparent; border: none; box-shadow: none; padding: 0; min-height: 0;
  color: @ss_accent; font-size: 11px; font-weight: bold; }
.ss-note-anchor:hover { text-decoration: underline; background: transparent; }
.ss-note-text { font-size: 12px; }
.ss-note-delete { opacity: 0.35; }
.ss-notes-list > row:hover .ss-note-delete { opacity: 1; }
.ss-notes-empty { font-size: 12px; color: @ss_dim; padding: 16px; }
.ss-notes-add { padding: 6px 10px; border-top: 1px solid @ss_border; }
.ss-notes-anchor { font-size: 11px; color: @ss_dim; }
.ss-notes entry { background: @ss_panel; color: @ss_fg; border: 1px solid @ss_border; border-radius: 0;
  box-shadow: none; min-height: 30px; font-size: 12px; }
.ss-notes entry:focus-within { border-color: @ss_accent; outline: none; }
.ss-notes editablelabel text { background: @ss_panel; }
.ss-notes checkbutton check { border-radius: 0; background: @ss_panel; border: 1px solid @ss_border;
  box-shadow: none; background-image: none; color: transparent; }
.ss-notes checkbutton check:checked { background: @ss_accent; border-color: @ss_accent; color: @ss_accent_fg;
  -gtk-icon-source: -gtk-icontheme("object-select-symbolic"); }
.ss-notes .ss-note-text entry { min-height: 26px; }
popover checkbutton check:checked, popover checkbutton radio:checked { background: @ss_accent;
  border-color: @ss_accent; color: @ss_accent_fg; }

.ss-help { padding: 10px 14px; font-size: 12px; }
.ss-help .key { color: @ss_accent; font-weight: bold; }
"""


class ThemeWatcher:
    """Calls on_change() ~400ms after `omarchy theme set` rewrites the theme."""

    def __init__(self, on_change):
        from gi.repository import Gio, GLib
        self._glib = GLib
        self._on_change = on_change
        self._source = None
        self._monitors = []
        for path in (os.path.join(STATE_DIR, "theme.name"), CURRENT_COLORS):
            try:
                mon = Gio.File.new_for_path(path).monitor_file(Gio.FileMonitorFlags.NONE, None)
            except Exception:  # noqa: BLE001
                continue
            mon.connect("changed", self._changed)
            self._monitors.append(mon)

    def _changed(self, *_a):
        if self._source is not None:
            self._glib.source_remove(self._source)
        self._source = self._glib.timeout_add(400, self._fire)

    def _fire(self):
        self._source = None
        self._on_change()
        return False
