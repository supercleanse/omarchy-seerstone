#!/bin/bash
# Install Seerstone for this user: its prerequisites (asks for your password
# if any are missing), a command on PATH, a launcher entry for Super+Space
# (also listed under "Open With" for images, PDFs, audio, video), and an icon.
# Re-run any time; it only (re)writes its own files. It does not make
# Seerstone the default app for anything.
#
#   ./install.sh               install (and any missing prerequisites)
#   ./install.sh --check       just report missing prerequisites
#   ./install.sh --uninstall   remove the command, launcher entry, and icon
set -euo pipefail
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

if [[ "${1:-}" == "--uninstall" ]]; then
  # Only remove the command if it is ours (a symlink into this checkout).
  if [[ -L ~/.local/bin/seerstone && "$(readlink -f ~/.local/bin/seerstone)" == "$here/bin/seerstone" ]]; then
    rm -f ~/.local/bin/seerstone
  fi
  rm -f ~/.local/share/applications/com.blairwilliams.Seerstone.desktop \
        ~/.local/share/icons/hicolor/scalable/apps/com.blairwilliams.Seerstone.svg
  update-desktop-database ~/.local/share/applications 2>/dev/null || true
  gtk-update-icon-cache -q ~/.local/share/icons/hicolor 2>/dev/null || true
  echo "Removed Seerstone's command, launcher entry, and icon."
  echo "Kept: prerequisite packages, notes files, and settings in ~/.config/seerstone/."
  exit 0
fi

# Prerequisites. gst-libav is required, not optional: without it H.264 falls
# back to openh264, which decodes several times slower and stops producing
# frames for good once it has decoded to the end of a file, and iPhone HEVC
# video doesn't play at all.
need=(gtk4 python-gobject python-cairo glycin-gtk4 poppler-glib
      gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad gst-libav
      ffmpeg imagemagick qpdf poppler wl-clipboard)
missing=()
for pkg in "${need[@]}"; do
  pacman -Q "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done

if [[ "${1:-}" == "--check" ]]; then
  if ((${#missing[@]})); then echo "Missing: ${missing[*]}"; exit 1; fi
  echo "All prerequisites installed."; exit 0
fi

if ((${#missing[@]})); then
  echo "Seerstone needs: ${missing[*]}"
  if ! sudo pacman -S --needed "${missing[@]}"; then
    echo "Couldn't install the prerequisites, so Seerstone wasn't installed." >&2
    exit 1
  fi
fi

# Make sure GStreamer actually picks up FFmpeg's decoders (gst-libav).
if ! gst-inspect-1.0 avdec_h264 >/dev/null 2>&1; then
  rm -rf ~/.cache/gstreamer-1.0  # stale plugin registry; GStreamer rebuilds it
  if ! gst-inspect-1.0 avdec_h264 >/dev/null 2>&1; then
    echo "gst-libav is installed but GStreamer can't load avdec_h264; not installing." >&2
    exit 1
  fi
fi

mkdir -p ~/.local/bin ~/.local/share/applications ~/.local/share/icons/hicolor/scalable/apps
ln -sf "$here/bin/seerstone" ~/.local/bin/seerstone
cp "$here/data/seerstone.svg" ~/.local/share/icons/hicolor/scalable/apps/com.blairwilliams.Seerstone.svg
cat > ~/.local/share/applications/com.blairwilliams.Seerstone.desktop <<DESKTOP
[Desktop Entry]
Type=Application
Name=Seerstone
GenericName=Media Browser
Comment=Browse a folder of images, PDFs, audio, and video
Exec=$HOME/.local/bin/seerstone %f
Icon=com.blairwilliams.Seerstone
Terminal=false
Categories=Graphics;Viewer;AudioVideo;Player;
Keywords=image;photo;picture;pdf;video;audio;music;viewer;browser;rotate;convert;
MimeType=image/png;image/jpeg;image/webp;image/gif;image/bmp;image/tiff;image/heic;image/heif;image/avif;image/jxl;image/svg+xml;application/pdf;video/mp4;video/quicktime;video/x-matroska;video/webm;video/x-msvideo;video/mpeg;video/ogg;audio/mpeg;audio/x-wav;audio/wav;audio/ogg;audio/opus;audio/flac;audio/x-flac;audio/mp4;audio/aac;audio/x-m4a;
StartupWMClass=com.blairwilliams.Seerstone
DESKTOP
update-desktop-database ~/.local/share/applications 2>/dev/null || true
gtk-update-icon-cache -q ~/.local/share/icons/hicolor 2>/dev/null || true
echo "Installed. Open it with Super+Space -> Seerstone, or run: seerstone [folder or file]"
