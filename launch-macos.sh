#!/usr/bin/env bash
# Launches Z2R Music Manager with Homebrew's python3. Optional argument: the music folder to open.
set -euo pipefail

if ! command -v brew >/dev/null 2>&1; then
    # Homebrew isn't always on PATH in non-login shells (e.g. when started from a launcher).
    for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
        [[ -x "$p" ]] && eval "$("$p" shellenv)" && break
    done
fi
if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew not found. Run ./macos-setup.sh first." >&2
    exit 1
fi

PY="$(brew --prefix)/bin/python3"
if [[ ! -x "$PY" ]] || ! "$PY" -c 'import gi' 2>/dev/null; then
    echo "Homebrew python3 with PyGObject not found. Run ./macos-setup.sh first." >&2
    exit 1
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$PY" "$DIR/z2r_music_manager.py" "$@"
