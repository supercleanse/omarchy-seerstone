"""Notes on media files, kept in a sidecar Markdown file next to each one.

    intro.mp4  ->  intro.mp4.notes.md

One note per line, as a checklist, so a person or an AI can read it, act on
it, and tick items off:

    - [ ] 00:01:23.400 (frame 2502): Logo comes in too early.   (video)
    - [ ] 00:00:12.500: Breath noise here.                       (audio)
    - [ ] page 3: Typo in the second paragraph.                  (PDF)
    - [ ] Make the whole thing warmer.                           (the whole file)

Times are HH:MM:SS.mmm. Seerstone rewrites the file in time/page order when
you change notes in the app, and re-reads it when something else edits it.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

NOTES_SUFFIX = ".notes.md"

FORMAT_COMMENT = (
    "<!-- Seerstone notes. One note per line. \"- [ ] TIME (frame N): text\" for video, "
    "\"- [ ] TIME: text\" for audio, \"- [ ] page N: text\" for PDFs, \"- [ ] text\" for the whole file. "
    "TIME is HH:MM:SS.mmm from the start. \"- [x]\" marks a note as done. -->"
)

_NOTE = re.compile(
    r"^\s*[-*+]\s+(?:\[(?P<done>[ xX])\]\s+)?"
    r"(?:(?P<time>\d{1,3}:\d{2}:\d{2}(?:\.\d{1,6})?|\d{1,3}:\d{2}(?:\.\d{1,6})?)"
    r"(?:\s*\(frame\s+(?P<frame>\d+)\))?\s*:\s*"
    r"|page\s+(?P<page>\d+)\s*:\s*)?"
    r"(?P<text>.*?)\s*$",
    re.IGNORECASE,
)


@dataclass
class Note:
    text: str
    time_us: int | None = None  # audio/video position
    frame: int | None = None    # video frame at time_us, when the frame rate is known
    page: int | None = None     # 1-based PDF page
    done: bool = False
    _order: int = field(default=0, compare=False)

    @property
    def anchored(self) -> bool:
        return self.time_us is not None or self.page is not None

    def anchor_text(self, short: bool = False) -> str:
        if self.time_us is not None:
            return fmt_time(self.time_us, short=short)
        if self.page is not None:
            return f"p. {self.page}" if short else f"page {self.page}"
        return ""

    def sort_key(self):
        if self.time_us is not None:
            return (1, self.time_us, self._order)
        if self.page is not None:
            return (1, self.page, self._order)
        return (0, 0, self._order)


def notes_path(media_path: str) -> str:
    return media_path + NOTES_SUFFIX


def is_notes_file(name: str) -> bool:
    return name.endswith(NOTES_SUFFIX)


def fmt_time(us: int, short: bool = False) -> str:
    ms_total = max(0, int(round(us / 1000)))
    s, ms = divmod(ms_total, 1000)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if short:
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}.{ms // 100}"
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def parse_time(text: str) -> int:
    parts = text.split(":")
    secs = float(parts[-1])
    mins = int(parts[-2]) if len(parts) >= 2 else 0
    hours = int(parts[-3]) if len(parts) >= 3 else 0
    return int(round((hours * 3600 + mins * 60 + secs) * 1_000_000))


def parse(content: str) -> list[Note]:
    notes: list[Note] = []
    in_comment = False
    for raw in content.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue
        if stripped.startswith("<!--"):
            in_comment = "-->" not in stripped
            continue
        if not stripped or stripped.startswith("#"):
            continue
        m = _NOTE.match(line) if re.match(r"^\s*[-*+]\s+", line) else None
        if m is None:
            # Free text: continues the note above, or stands as a note on the whole file.
            if notes and raw[:1].isspace():
                notes[-1].text = (notes[-1].text + " " + stripped).strip()
            else:
                notes.append(Note(text=stripped, _order=len(notes)))
            continue
        note = Note(text=m.group("text") or "", done=(m.group("done") or " ").lower() == "x", _order=len(notes))
        if m.group("time"):
            note.time_us = parse_time(m.group("time"))
            if m.group("frame"):
                note.frame = int(m.group("frame"))
        elif m.group("page"):
            note.page = int(m.group("page"))
        notes.append(note)
    return notes


def render(media_name: str, notes: list[Note]) -> str:
    lines = [f"# Notes on {media_name}", "", FORMAT_COMMENT, ""]
    for n in sorted(notes, key=Note.sort_key):
        box = "[x]" if n.done else "[ ]"
        text = " ".join(n.text.split())
        if n.time_us is not None:
            frame = f" (frame {n.frame})" if n.frame is not None else ""
            lines.append(f"- {box} {fmt_time(n.time_us)}{frame}: {text}")
        elif n.page is not None:
            lines.append(f"- {box} page {n.page}: {text}")
        else:
            lines.append(f"- {box} {text}")
    return "\n".join(lines) + "\n"


def load(media_path: str) -> list[Note]:
    try:
        with open(notes_path(media_path), encoding="utf-8") as fh:
            notes = parse(fh.read())
    except (OSError, UnicodeDecodeError):
        return []
    notes.sort(key=Note.sort_key)
    for i, n in enumerate(notes):
        n._order = i
    return notes


def save(media_path: str, notes: list[Note]) -> None:
    """Write the sidecar (atomically), or remove it when there are no notes left."""
    path = notes_path(media_path)
    if not notes:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    d, name = os.path.split(path)
    tmp = os.path.join(d, f".{name}.seerstone-tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(render(os.path.basename(media_path), notes))
    os.replace(tmp, path)
