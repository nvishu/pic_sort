#!/bin/bash
# One-time setup: makes .venv/ and installs the Python packages. (./start.sh does this by itself too.)
cd "$(dirname "$0")" || exit 1
PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || { echo "Python 3 is missing. On a Mac run: xcode-select --install   (or install from python.org)"; exit 1; }
"$PY" start.py --setup && echo "Next: ./start.sh   (or ./start.sh --demo to see the demo)"
