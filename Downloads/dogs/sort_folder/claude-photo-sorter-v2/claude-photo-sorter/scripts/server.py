"""The dashboard app.  python scripts/server.py   ->  http://127.0.0.1:8765

Serves dashboard/ plus a small local API so the page can do everything itself:
pick folders, peek at a folder, run the whole sort (scan, duplicates, AI labels,
build folders) with live progress, edit folders, browse photos at full size, move a
photo to another folder, and open folders in Finder / Explorer.
Listens on 127.0.0.1 only: nothing is reachable from other computers, and requests
whose Host or Origin is not this page are refused (blocks DNS-rebinding and other websites).
"""
import glob
import hashlib
import importlib.util
import io
import json
import mimetypes
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))
from common import (CATEGORIES, DATA, ENV_FILE, IMG, JEV_OUT, LABELS, ROOT, VID, WORK, clean_path, folder_name,
                    load, load_cached, load_env, read_env_file, save)
import providers as P

load_env()
PORT = int(os.environ.get("PORT", 8765))
DASH = os.path.join(ROOT, "dashboard")
PEEK = os.path.join(DASH, "peek")
PREVIEWS = os.path.join(DATA, "previews")  # cached JPEGs of HEIC/TIFF/BMP conversions for the lightbox
OVERRIDES = os.path.join(WORK, "overrides.json")
SCRIPTS = os.path.join(ROOT, "scripts")
PY = sys.executable
MAX_BODY = 1 << 20
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
# The browser hangs up all the time (poll cancelled, tab closed, <video> seek). Those are not bugs.
GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

job = {"step": None, "running": False, "log": [], "code": None, "phase": None, "phases": [], "done": 0, "total": 0,
       "started": None, "phase_started": None, "timings": {}}
job_lock = threading.Lock()
AI_PROVIDERS = list(P.REGISTRY)


# ---------------------------------------------------------------- what is ready

_ready_cache = (0.0, {})


def ready_engines():
    """Engines that can run right now (cached a few seconds: the page polls state often)."""
    global _ready_cache
    if time.time() - _ready_cache[0] > 5:
        _ready_cache = (time.time(), P.ready())
    return _ready_cache[1]


_jev_cache = (0.0, False)


def has_jev_key():
    global _jev_cache
    if time.time() - _jev_cache[0] > 5:
        env = read_env_file()
        _jev_cache = (time.time(), any(os.environ.get(n) or env.get(n) for n in ("JEV_KEY", "TYPESAFE_API_KEY")))
    return _jev_cache[1]


def engines_info():
    avail = ready_engines()
    return [{"name": n, "label": p.label, "ready": n in avail, "paid": p.paid, "needs_key": bool(p.key_envs),
             "signup": p.signup, "hint": p.hint, "key_env": p.key_envs[0] if p.key_envs else ""}
            for n, p in P.REGISTRY.items()]


def can_pick_folder():
    if sys.platform == "darwin":
        return True
    return bool(importlib.util.find_spec("tkinter")) and (sys.platform.startswith("win") or bool(os.environ.get("DISPLAY")))


def state():
    inv = load_cached(os.path.join(DATA, "inventory.json"), [])
    src = load_cached(os.path.join(DATA, "source.json"), {})
    avail = ready_engines()
    with job_lock:
        j = {k: job[k] for k in ("step", "running", "code", "phase", "phases", "done", "total", "started",
                                 "phase_started", "timings")} | {"log": job["log"][-40:], "now": time.time()}
    return {
        "categories": load_cached(CATEGORIES, []),
        "source": src.get("source"), "limit": src.get("limit"), "sorted": src.get("sorted"),
        "files": len(inv), "bytes": sum(r["bytes"] for r in inv),
        "steps": {
            "scan": bool(inv),
            "dupes": os.path.exists(os.path.join(DATA, "dupes.json")),
            "label": bool(glob.glob(os.path.join(LABELS, "*.json"))) or os.path.exists(JEV_OUT),
            "jev_labels": os.path.exists(JEV_OUT),
            "apply": bool(src.get("sorted")) and os.path.exists(os.path.join(DATA, "labels.json")),
        },
        "jev_key": has_jev_key(),
        "claude_cli": "claude" in avail,
        "ollama_running": "ollama" in avail,
        "ai_providers": AI_PROVIDERS,
        "engines": engines_info(),
        "ready_free": [n for n in avail if not P.REGISTRY[n].paid and n != "custom"],
        "overrides": len(load(OVERRIDES, {})),
        "job": j,
        "platform": sys.platform,
        "can_pick": can_pick_folder(),
        "scan_pending": os.path.exists(os.path.join(DATA, ".scanning")),
    }


_live_cache = (0.0, {})


def live_labels():
    """Labels written so far (all AI batches + confident Jev answers), for the live animation.
    Cached for half a second: the dashboard polls often and the merged dict can be large."""
    global _live_cache
    now = time.time()
    if now - _live_cache[0] <= 0.5:
        return _live_cache[1]
    out = {}
    for f in sorted(glob.glob(os.path.join(LABELS, "*.json"))):
        out.update({str(k): v for k, v in load_cached(f, {}).items()})
    for k, v in load_cached(JEV_OUT, {}).items():
        if "key" in v and (v.get("confidence", 0) >= 0.7 or k not in out):
            out[k] = v["key"]
    _live_cache = (now, out)
    return out


def clear_library():
    """A new folder (or sample size) means the old labels no longer match."""
    for f in (os.path.join(DATA, "labels.json"), os.path.join(DATA, "dupes.json"), JEV_OUT, OVERRIDES):
        if os.path.exists(f):
            os.remove(f)
    for d in (LABELS, os.path.join(WORK, "sheets"), PREVIEWS):
        shutil.rmtree(d, ignore_errors=True)


def categories_fingerprint():
    with open(CATEGORIES, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()[:12]


# ---------------------------------------------------------------- jobs

def say(line):
    with job_lock:
        job["log"].append(line)
        if len(job["log"]) > 500:
            del job["log"][:-300]


def stream(cmd, env=None):
    """Run one script, keep its output for the log, and read PROGRESS lines."""
    full_env = {**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})}
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace", cwd=ROOT, env=full_env)
    for line in p.stdout:
        line = line.rstrip()
        m = re.match(r"PROGRESS (\d+) (\d+)", line)
        if m:
            with job_lock:
                job["done"], job["total"] = int(m.group(1)), int(m.group(2))
        else:
            say(line)
    return p.wait()


def start_job(step, phases, body):
    with job_lock:
        if job["running"]:
            raise RuntimeError("Something is already running. Wait for it to finish.")
        now = time.time()
        job.update(step=step, running=True, log=[f"$ {step}"], code=None, phase=None, phases=phases, done=0, total=0,
                   started=now, phase_started=now, timings={})

    def guarded():
        try:
            body()
        except Exception as e:  # noqa: BLE001 - never leave the dashboard stuck on "running"
            say(f"Error: {e}")
            with job_lock:
                job.update(running=False, code=1)
    threading.Thread(target=guarded, daemon=True).start()


def run_phase(name, cmd, env=None):
    with job_lock:
        job.update(phase=name, done=0, total=0, phase_started=time.time())
    code = stream([PY, "-u"] + cmd, env=env)
    with job_lock:
        job["timings"][name] = round(time.time() - job["phase_started"], 1)
    return code


def label_env(labeler, api_key, model):
    """Environment for label_ai.py: which engines, and a key typed into the dashboard (if any)."""
    env = {"AI_PROVIDERS": labeler}
    if model:
        env["AI_MODEL"] = model
    if api_key and "," not in labeler and labeler != "auto":
        env["AI_PROVIDER"], env["AI_KEY"] = labeler, api_key
    return env


def check_labeler(labeler, api_key):
    """Friendly error before the sort starts, never a half-run."""
    if labeler == "jev":
        if not has_jev_key():
            raise ValueError("Add JEV_KEY=your-key to a .env file in this folder to use Jev.")
        return
    env = {**os.environ}
    if api_key and "," not in labeler and labeler != "auto":
        env.update(AI_PROVIDER=labeler, AI_KEY=api_key)
    P.choose(labeler, env)  # raises ValueError with what to do


def sort_all(args):
    source = clean_path(args.get("source") or "") if (args.get("source") or "").strip() else ""
    dest = (args.get("dest") or "").strip()
    limit = int(args.get("limit") or 0)
    labeler = (args.get("labeler") or "auto").strip().lower().replace(" ", "")
    api_key = (args.get("api_key") or "").strip()
    model = (args.get("model") or "").strip()
    force_scan = bool(args.get("force_scan"))

    if not source or not os.path.isdir(source):
        raise ValueError("Choose the folder with your photos first.")
    check_labeler(labeler, api_key)

    src = load(os.path.join(DATA, "source.json"), {})
    have = os.path.exists(os.path.join(DATA, "inventory.json"))
    # A previous scan that never finished leaves .scanning behind; treat that as "rescan please".
    pending = os.path.exists(os.path.join(DATA, ".scanning"))
    need_scan = force_scan or pending or not have or src.get("source") != source or int(src.get("limit") or 0) != limit
    phases = (["scan"] if need_scan else []) + ["dupes", "label", "apply"]

    def body():
        def fail(code):
            with job_lock:
                job.update(running=False, code=code)
        if need_scan:
            clear_library()
            if run_phase("scan", [os.path.join(SCRIPTS, "scan.py"), source] + (["--limit", str(limit)] if limit else [])):
                return fail(1)
        if run_phase("dupes", [os.path.join(SCRIPTS, "dupes.py")]):
            return fail(1)
        marker = os.path.join(LABELS, ".categories")
        if load(marker) != categories_fingerprint():  # folders changed since the last labels
            shutil.rmtree(LABELS, ignore_errors=True)
            if os.path.exists(JEV_OUT):
                os.remove(JEV_OUT)
        os.makedirs(LABELS, exist_ok=True)
        save(marker, categories_fingerprint())

        if labeler == "jev":
            code = run_phase("label", [os.path.join(SCRIPTS, "label_jev.py")])
        else:  # claude, one engine, several, or auto: all the same pooled engine
            code = run_phase("label", [os.path.join(SCRIPTS, "label_ai.py")], env=label_env(labeler, api_key, model))
        if code:
            total = len(load(os.path.join(DATA, "inventory.json"), []))
            if not total or len(live_labels()) < 0.95 * total:
                return fail(1)
            say("A few files could not be labelled; they go to the catch-all folders.")
        if run_phase("apply", [os.path.join(SCRIPTS, "apply.py"), dest]):
            return fail(1)
        with job_lock:
            job.update(running=False, code=0, phase="done")
    start_job("sort", phases, body)


def run_single(step, args):
    cmds = {
        "scan": ["scan.py", args.get("source", "")] + (["--limit", str(args["limit"])] if args.get("limit") else []),
        "dupes": ["dupes.py"], "estimate": ["label_jev.py", "--estimate"], "jev": ["label_jev.py"],
        "claude": ["label_claude.py"], "ai": ["label_ai.py"], "apply": ["apply.py", args.get("dest", "")],
    }
    if step not in cmds:
        raise ValueError("unknown step")
    labeler = (args.get("labeler") or "auto").strip().lower()
    if step == "ai":
        check_labeler(labeler, args.get("api_key", ""))

    def body():
        if step == "scan":
            clear_library()
        env = label_env(labeler, args.get("api_key", ""), args.get("model", "")) if step == "ai" else None
        code = run_phase(step, [os.path.join(SCRIPTS, cmds[step][0])] + cmds[step][1:], env=env)
        with job_lock:
            job.update(running=False, code=code, phase="done" if code == 0 else step)
    start_job(step, [step], body)


# ---------------------------------------------------------------- keys

KEY_RE = re.compile(r"^[A-Za-z0-9_\-.:/+=]{8,400}$")


def set_key(engine, value):
    """Save an API key into .env (this folder only, never committed), or remove it when value is empty."""
    p = P.REGISTRY.get(engine)
    if not p or not p.key_envs:
        raise ValueError("That engine does not use a key.")
    name = p.key_envs[0]
    value = (value or "").strip()
    if value and not KEY_RE.match(value):
        raise ValueError("That does not look like an API key (letters, digits and - _ . only).")
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8-sig") as f:
            lines = [ln.rstrip("\n") for ln in f]
    lines = [ln for ln in lines if not re.match(rf"\s*(export\s+)?({'|'.join(map(re.escape, p.key_envs))})\s*=", ln)]
    if value:
        lines.append(f"{name}={value}")
    with open(ENV_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines).strip("\n") + ("\n" if lines else ""))
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass
    for n in p.key_envs:
        os.environ.pop(n, None)
    if value:
        os.environ[name] = value
    global _ready_cache, _jev_cache
    _ready_cache = (0.0, {})
    _jev_cache = (0.0, False)


# ---------------------------------------------------------------- folders

def peek(source):
    """Count what is in a folder and make a few tiny previews, right after it is picked."""
    source = clean_path(source)
    if not os.path.isdir(source):
        raise ValueError("That folder does not exist.")
    photos, videos, size, pics = 0, 0, 0, []
    for dp, dirs, fs in os.walk(source):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.endswith(" (sorted)")]
        for f in fs:
            ext = os.path.splitext(f)[1].lower()
            if f.startswith("._"):
                continue
            if ext in IMG:
                photos += 1
                if ext in (".jpg", ".jpeg", ".png", ".heic", ".webp"):
                    pics.append(os.path.join(dp, f))
            elif ext in VID:
                videos += 1
            else:
                continue
            try:
                size += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    shutil.rmtree(PEEK, ignore_errors=True)
    os.makedirs(PEEK)
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    out, stamp = [], int(time.time())
    for i, p in enumerate(random.Random(3).sample(pics, min(60, len(pics)))):
        try:
            with Image.open(p) as im:
                im.draft("RGB", (160, 160))
                im = ImageOps.exif_transpose(im).convert("RGB")
                s = min(im.size)
                im = im.crop(((im.width - s) // 2, (im.height - s) // 2, (im.width + s) // 2, (im.height + s) // 2)).resize((64, 64))
                im.save(os.path.join(PEEK, f"{i}.jpg"), quality=70)
                out.append(f"peek/{i}.jpg?{stamp}")
        except Exception:  # noqa: BLE001 - a bad file just means one fewer preview
            pass
    return {"source": source, "photos": photos, "videos": videos, "bytes": size, "previews": out,
            "suggested_dest": source + " (sorted)"}


def open_path(path, reveal=False):
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", path] if reveal else ["open", path])
    elif sys.platform.startswith("win"):
        subprocess.run(["explorer", "/select," + path] if reveal else ["explorer", path])
    else:
        subprocess.run(["xdg-open", os.path.dirname(path) if reveal else path])


_TK = ("import tkinter as t, tkinter.filedialog as f, sys\n"
       "r = t.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
       "print(f.askdirectory(title=sys.argv[1]) or '')")


def pick_folder(prompt):
    """Native folder picker in front of the browser: AppleScript on macOS, tkinter elsewhere."""
    prompt = prompt.replace('"', "'")[:120]
    if sys.platform == "darwin":
        script = ('tell application (path to frontmost application as text)\n'
                  f'  POSIX path of (choose folder with prompt "{prompt}")\n'
                  'end tell')
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        return r.stdout.strip().rstrip("/") or None
    if can_pick_folder():
        r = subprocess.run([PY, "-c", _TK, prompt], capture_output=True, text=True, encoding="utf-8", errors="replace")
        return os.path.normpath(r.stdout.strip()) if r.stdout.strip() else None
    return None


# ---------------------------------------------------------------- HTTP

class H(SimpleHTTPRequestHandler):
    server_version = "PhotoSorter"
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript", ".mjs": "text/javascript"}

    def __init__(self, *a, **k):
        super().__init__(*a, directory=DASH, **k)

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        b = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)
        except GONE:
            pass  # browser navigated away / cancelled the fetch mid-response

    def end_headers(self):
        try:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if self.path.startswith(("/data/", "/peek/")) or self.path.endswith((".js", ".css", ".html")) or self.path == "/":
                self.send_header("Cache-Control", "no-store")
        except GONE:
            raise  # let the caller see it: it happens before the body write
        super().end_headers()

    def host_ok(self):
        return (self.headers.get("Host") or "") in ALLOWED_HOSTS

    def do_GET(self):
        try:
            if not self.host_ok():
                return self.send_json({"error": "forbidden"}, 403)
            u = urllib.parse.urlparse(self.path)
            if u.path == "/api/state":
                return self.send_json(state())
            if u.path == "/api/labels":
                return self.send_json(live_labels())
            m = re.match(r"^/api/file/(\d+)$", u.path)
            if m:
                return self.send_original(int(m.group(1)))
            return super().do_GET()
        except GONE:
            pass  # static file copy aborted (reload, devtools cancel): not an error

    def _send_bytes(self, b, ctype, cache="private, max-age=3600"):
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(b)
        except GONE:
            pass

    def _cached_jpeg(self, rec):
        """HEIC/TIFF/BMP converted to JPEG once and reused; invalidated by source mtime."""
        os.makedirs(PREVIEWS, exist_ok=True)
        cached = os.path.join(PREVIEWS, f"{rec['id']}.jpg")
        try:
            if os.path.exists(cached) and os.path.getmtime(cached) >= os.path.getmtime(rec["path"]):
                with open(cached, "rb") as f:
                    return f.read()
        except OSError:
            pass
        from PIL import Image, ImageOps
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError:
            pass
        with Image.open(rec["path"]) as src:
            src.draft("RGB", (2400, 2400))
            im = ImageOps.exif_transpose(src).convert("RGB")
        im.thumbnail((2400, 2400))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        b = buf.getvalue()
        try:
            tmp = cached + ".tmp"
            with open(tmp, "wb") as f:
                f.write(b)
            os.replace(tmp, cached)
        except OSError:
            pass
        return b

    def send_original(self, i):
        try:
            inv = load_cached(os.path.join(DATA, "inventory.json"), [])
            rec = inv[i] if 0 <= i < len(inv) and inv[i]["id"] == i else next((r for r in inv if r["id"] == i), None)
            if not rec or not os.path.exists(rec["path"]):
                return self.send_error(404)
            path = rec["path"]
            if rec["ext"] in (".heic", ".heif", ".tif", ".tiff", ".bmp"):  # browsers can't show these: convert
                try:
                    return self._send_bytes(self._cached_jpeg(rec), "image/jpeg")
                except Exception:  # noqa: BLE001
                    return self.send_error(415)
            size = os.path.getsize(path)
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if rec["ext"] == ".mov":
                ctype = "video/mp4"  # most iPhone .mov files play fine as mp4
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            if rng:
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
                    else:  # suffix range: the last N bytes
                        start = max(0, size - int(m.group(2)))
                else:
                    rng = None
                if rng and start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    return self.end_headers()
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "private, max-age=3600")
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                try:
                    while left > 0:
                        chunk = f.read(min(1 << 20, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except GONE:
                    pass  # <video> seek cancelled the previous range request
        except GONE:
            pass  # client vanished before any bytes went out

    def do_POST(self):
        try:
            if not self.host_ok() or self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
                return self.send_json({"error": "forbidden"}, 403)  # only this page may call the API
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n > MAX_BODY:
                return self.send_json({"error": "request too large"}, 413)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self.send_json({"error": "bad request"}, 400)
            p = urllib.parse.urlparse(self.path).path
            try:
                if p == "/api/sort":
                    sort_all(body)
                elif p == "/api/run":
                    run_single(body.get("step"), body)
                elif p == "/api/peek":
                    return self.send_json(peek(body.get("source", "")))
                elif p == "/api/key":
                    set_key(body.get("engine"), body.get("key"))
                elif p == "/api/categories":
                    cats = body["categories"]
                    keys = [c["key"] for c in cats]
                    if len(keys) != len(set(keys)) or not all(c.get("label") and c.get("kind") in ("photo", "video") for c in cats):
                        raise ValueError("Every folder needs a name.")
                    if not any(c["kind"] == "photo" for c in cats) or not any(c["kind"] == "video" for c in cats):
                        raise ValueError("Keep at least one photo folder and one video folder.")
                    save(CATEGORIES, cats)
                    save(os.path.join(DATA, "categories.json"), cats)
                elif p == "/api/move":
                    inv = load_cached(os.path.join(DATA, "inventory.json"), [])
                    rec = next((r for r in inv if r["id"] == int(body["id"])), None)
                    cat = next((c for c in load(CATEGORIES, []) if c["key"] == body["key"]), None)
                    if not rec or not cat or cat["kind"] != rec["kind"]:
                        raise ValueError("That photo cannot go in that folder.")
                    ov = load(OVERRIDES, {})
                    ov[str(rec["id"])] = cat["key"]
                    save(OVERRIDES, ov)
                    labels = load(os.path.join(DATA, "labels.json"), {})
                    labels[str(rec["id"])] = cat["key"]
                    save(os.path.join(DATA, "labels.json"), labels)
                elif p == "/api/open":
                    src = load(os.path.join(DATA, "source.json"), {})
                    if body.get("id") is not None:
                        rec = next(r for r in load(os.path.join(DATA, "inventory.json"), []) if r["id"] == int(body["id"]))
                        open_path(rec["path"], reveal=True)
                    elif src.get("sorted"):
                        root = os.path.abspath(src["sorted"])
                        path = root
                        if body.get("category"):
                            c = next((c for c in load(CATEGORIES, []) if c["key"] == body["category"]), None)
                            path = os.path.join(root, folder_name(c["label"])) if c else root
                        elif body.get("folder") == "_Review/duplicates":
                            path = os.path.join(root, "_Review", "duplicates")
                        open_path(path if os.path.isdir(path) else root)
                    else:
                        raise ValueError("Sort first, then the folders can be opened.")
                elif p == "/api/pick":
                    return self.send_json({"path": pick_folder(body.get("prompt") or "Choose the folder with your photos")})
                else:
                    return self.send_json({"error": "not found"}, 404)
            except StopIteration:
                return self.send_json({"error": "not found"}, 404)
            except (ValueError, RuntimeError, KeyError, TypeError) as e:
                return self.send_json({"error": str(e) or "bad request"}, 400)
            return self.send_json(state())
        except GONE:
            pass  # the side effects (job started, key saved) already happened; nothing left to report

    def finish(self):
        try:
            super().finish()
        except GONE:
            pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """A browser hanging up mid-response (WinError 10053, ConnectionReset) is normal, not a bug."""
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    try:
        srv = Server(("127.0.0.1", PORT), H)
    except OSError:
        sys.exit(f"Port {PORT} is busy. If the photo sorter is already open, just visit http://127.0.0.1:{PORT}\n"
                 f"or use another port:  PORT={PORT + 1} ./start.sh")
    print(f"Photo sorter running at http://127.0.0.1:{PORT}  (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")