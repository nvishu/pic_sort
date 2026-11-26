#!/bin/bash
# Opens the photo sorter at http://127.0.0.1:8765. Safe to run again: if it's already running it just opens the page.
#   ./start.sh          your photos
#   ./start.sh --demo   a fake camera roll, to see what it looks like
cd "$(dirname "$0")" || exit 1
PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || { echo "Python 3 is missing. On a Mac run: xcode-select --install   (or install from python.org)"; exit 1; }
exec "$PY" start.py "$@"
