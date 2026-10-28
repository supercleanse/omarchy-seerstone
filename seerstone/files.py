"""What Seerstone can show, and the folder listing."""
from __future__ import annotations

import os
import re

from gi.repository import GObject

IMAGE_EXT = {
    ".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".avif", ".jxl", ".svg", ".svgz", ".ico", ".tga", ".qoi", ".exr",
    ".dds",
}
PDF_EXT = {".pdf"}
VIDEO_EXT = {
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".mpg", ".mpeg",
    ".ts", ".mts", ".m2ts", ".3gp", ".ogv",
}
AUDIO_EXT = {
    ".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac", ".wma", ".aiff",
    ".aif", ".alac", ".mka", ".ape", ".wv",
}

KIND_ICONS = {
    "image": "image-x-generic-symbolic",
    "pdf": "x-office-document-symbolic",
    "video": "video-x-generic-symbolic",
    "audio": "audio-x-generic-symbolic",
}

# Images we can't write back in place (vector or exotic formats); export still works.
NO_INPLACE_SAVE = {".svg", ".svgz", ".ico", ".exr", ".dds", ".qoi", ".tga"}


def kind_of(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in PDF_EXT:
        return "pdf"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return None


def natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


class Item(GObject.Object):
    __gtype_name__ = "SeerstoneItem"

    def __init__(self, path: str, kind: str):
        super().__init__()
        self.path = path
        self.name = os.path.basename(path)
        self.kind = kind


def scan(folder: str) -> list[Item]:
    items = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if entry.name.startswith(".") or ".seerstone-tmp" in entry.name:
                    continue
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                kind = kind_of(entry.name)
                if kind:
                    items.append(Item(entry.path, kind))
    except OSError:
        return []
    items.sort(key=lambda i: natural_key(i.name))
    return items


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_time(us: int) -> str:
    s = max(0, int(us // 1_000_000))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
