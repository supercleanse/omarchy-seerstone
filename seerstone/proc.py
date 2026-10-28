"""Small async subprocess helpers (ffmpeg, ffprobe, magick, qpdf, pdftoppm)."""
from __future__ import annotations

import hashlib
import os

from gi.repository import Gio, GLib

CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "seerstone")


def cache_path(source: str, suffix: str) -> str:
    try:
        st = os.stat(source)
        stamp = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        stamp = ""
    key = hashlib.sha1(f"{source}\0{stamp}\0{suffix}".encode()).hexdigest()[:20]
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{key}-{suffix}")


def run(argv: list[str], on_done, cancellable: Gio.Cancellable | None = None):
    """Run argv; call on_done(ok, stdout, stderr) on the main loop."""
    try:
        proc = Gio.Subprocess.new(
            argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
    except GLib.Error as e:
        GLib.idle_add(lambda: on_done(False, "", e.message) and False)
        return None

    def finished(p, res):
        try:
            _ok, out, err = p.communicate_utf8_finish(res)
        except GLib.Error as e:
            on_done(False, "", e.message)
            return
        on_done(p.get_successful(), out or "", err or "")

    proc.communicate_utf8_async(None, cancellable, finished)
    return proc


def run_steps(steps: list[list[str]], on_done, cancellable: Gio.Cancellable | None = None):
    """Run commands one after another; stop at the first failure."""
    steps = list(steps)

    def next_step(ok=True, _out="", err=""):
        if not ok:
            on_done(False, err)
            return
        if not steps:
            on_done(True, "")
            return
        run(steps.pop(0), next_step, cancellable)

    next_step()


class Progress:
    """Run ffmpeg with `-progress pipe:1`, reporting a 0..1 fraction."""

    def __init__(self, argv, duration_us: int, on_progress, on_done, cancellable):
        self.duration_us = max(1, duration_us)
        self.on_progress = on_progress
        self.on_done = on_done
        self.cancellable = cancellable
        self.err = []
        try:
            self.proc = Gio.Subprocess.new(
                argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as e:
            GLib.idle_add(lambda: on_done(False, e.message) and False)
            return
        self.out = Gio.DataInputStream.new(self.proc.get_stdout_pipe())
        self.errs = Gio.DataInputStream.new(self.proc.get_stderr_pipe())
        self.out.read_line_async(GLib.PRIORITY_DEFAULT, cancellable, self._line)
        self.errs.read_line_async(GLib.PRIORITY_DEFAULT, cancellable, self._err_line)
        self.proc.wait_async(None, self._exited)
        if cancellable:
            cancellable.connect(lambda *_a: self.proc.force_exit())

    def _line(self, stream, res):
        try:
            line, _n = stream.read_line_finish_utf8(res)
        except GLib.Error:
            return
        if line is None:
            return
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                us = int(line.split("=", 1)[1])
                self.on_progress(min(1.0, us / self.duration_us))
            except ValueError:
                pass
        stream.read_line_async(GLib.PRIORITY_DEFAULT, self.cancellable, self._line)

    def _err_line(self, stream, res):
        try:
            line, _n = stream.read_line_finish_utf8(res)
        except GLib.Error:
            return
        if line is None:
            return
        self.err.append(line)
        self.err = self.err[-20:]
        stream.read_line_async(GLib.PRIORITY_DEFAULT, self.cancellable, self._err_line)

    def _exited(self, proc, res):
        try:
            proc.wait_finish(res)
        except GLib.Error:
            pass
        ok = proc.get_if_exited() and proc.get_exit_status() == 0
        self.on_done(ok, "\n".join(self.err[-4:]))
