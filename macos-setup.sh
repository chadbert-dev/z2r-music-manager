#!/usr/bin/env bash
# Installs what Z2R Music Manager needs on macOS, using Homebrew.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This script is for macOS." >&2
    exit 1
fi

if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew is not installed. Install it from https://brew.sh, then run this script again." >&2
    exit 1
fi

echo "Installing Python, GTK 4, PyGObject and the icon theme with Homebrew..."
brew install python pygobject3 gtk4 adwaita-icon-theme

PY="$(brew --prefix)/bin/python3"
echo "Checking that GTK 4.10+ works from $PY..."
"$PY" - <<'PYEOF'
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk
v = (Gtk.get_major_version(), Gtk.get_minor_version())
if v < (4, 10):
    raise SystemExit(f"GTK {v[0]}.{v[1]} is too old; Z2R Music Manager needs GTK 4.10 or newer.")
print(f"OK: GTK {v[0]}.{v[1]}")
PYEOF

echo "Setup complete. Start the app with ./launch-macos.sh [music_folder]"
