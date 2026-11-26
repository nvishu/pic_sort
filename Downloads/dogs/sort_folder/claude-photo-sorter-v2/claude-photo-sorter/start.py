#!/usr/bin/env python3
"""One launcher for Mac, Windows and Linux.   python start.py [--demo] [--setup]

First run: makes .venv/ and installs the packages (again only when requirements.txt changes).
Then starts the dashboard at http://127.0.0.1:8765 and opens it. Safe to run twice: if it is
already running it just opens the page.   PORT=8766 python start.py   to use another port.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import venv
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
PORT = int(os.environ.get("PORT", 8765))
BASE = f"http://127.0.0.1:{PORT}/"
WIN = os.name == "nt"
VPY = os.path.join(ROOT, ".venv", "Scripts" if WIN else "bin", "python.exe" if WIN else "python")
STAMP = os.path.join(ROOT, ".venv", ".requirements.sha1")


def running():
    try:
        urllib.request.urlopen(BASE + "api/state", timeout=1.5).read(1)
        return True
    except Exception:
        return False


def setup():
    if sys.version_info < (3, 9):
        sys.exit("Python 3.9 or newer is needed. Get it from python.org (tick 'Add to PATH' on Windows).")
    with open("requirements.txt", "rb") as f:
        want = hashlib.sha1(f.read()).hexdigest()
    have = open(STAMP).read().strip() if os.path.exists(STAMP) else ""
    if os.path.exists(VPY) and have == want:
        return
    if not os.path.exists(VPY):
        print("Setting up (one time, about a minute)...", flush=True)
        shutil.rmtree(".venv", ignore_errors=True)
        venv.create(".venv", with_pip=True)
    subprocess.check_call([VPY, "-m", "pip", "install", "-q", "--upgrade", "pip"])
    subprocess.check_call([VPY, "-m", "pip", "install", "-q", "-r", "requirements.txt"])
    with open(STAMP, "w") as f:
        f.write(want)
    if not shutil.which("ffmpeg"):
        print("Note: ffmpeg is not installed, so videos get grey previews. "
              "Mac: brew install ffmpeg   Windows: winget install ffmpeg")
    print("Setup done.", flush=True)


def main():
    demo = "--demo" in sys.argv
    url = BASE + ("?demo" if demo else "")
    if running():
        print("Photo sorter is already running.")
        webbrowser.open(url)
        return
    setup()
    if "--setup" in sys.argv:
        return
    if demo and not os.path.exists(os.path.join("dashboard", "demo", "mini", "0.jpg")):
        subprocess.check_call([VPY, os.path.join("scripts", "demo.py")])
    threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()
    try:
        sys.exit(subprocess.call([VPY, os.path.join("scripts", "server.py")], env={**os.environ, "PORT": str(PORT)}))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
