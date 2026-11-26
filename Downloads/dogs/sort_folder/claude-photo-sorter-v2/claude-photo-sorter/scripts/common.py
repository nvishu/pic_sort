"""Shared paths and helpers. Everything the tool writes lives inside this repo:
  dashboard/data/   inventory, thumbnails, labels (what the dashboard reads)
  work/             contact sheets and raw labels (scratch)
Your original photos are only ever read, never changed.
"""
import json
import os
import re
import tempfile

ROOT = os.environ.get("PHOTO_SORTER_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "dashboard", "data")
THUMBS = os.path.join(DATA, "thumbs")
WORK = os.path.join(ROOT, "work")
LABELS = os.path.join(WORK, "labels")      # one JSON per batch; written by label_ai.py / label_claude.py
JEV_OUT = os.path.join(WORK, "jev.json")   # Jev labels (optional)
AI_STATS = os.path.join(WORK, "ai_stats.json")  # which AI answered how many batches
CATEGORIES = os.path.join(ROOT, "categories.json")
ENV_FILE = os.path.join(ROOT, ".env")

IMG = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
VID = {".mov", ".mp4", ".m4v", ".3gp", ".avi", ".mkv", ".webm"}

SHEET_SIZE = 30  # previews per contact sheet; used by sheets.py and label_ai.py so they never drift


# ---------- JSON helpers (UTF-8 everywhere: Windows defaults to cp1252 and breaks on emoji) ----------

def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, NotADirectoryError):
        return default
    except ValueError:  # half-written or corrupt file: behave as if it was not there
        return default


def save(path, obj):
    """Atomic write: readers (the dashboard polls every second) never see a half-written file."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_cache = {}


def load_cached(path, default=None):
    """load() that re-reads only when the file changed. The server polls often; inventories can be MBs."""
    try:
        st = os.stat(path)
    except OSError:
        _cache.pop(path, None)
        return default
    key = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    val = load(path, default)
    _cache[path] = (key, val)
    return val


def categories():
    cats = load(CATEGORIES)
    if not cats:
        raise SystemExit("categories.json is missing or empty.")
    keys = [c["key"] for c in cats]
    assert len(keys) == len(set(keys)), "categories.json has duplicate keys"
    for kind in ("photo", "video"):
        assert any(c["kind"] == kind for c in cats), f"categories.json needs at least one {kind} folder"
    return cats


def catch_all(cats, kind):
    """The last folder of each kind catches everything that fits nowhere else."""
    return [c for c in cats if c["kind"] == kind][-1]["key"]


# ---------- .env (no python-dotenv needed) ----------

def read_env_file(path=ENV_FILE):
    out = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip().removeprefix("export ").strip()
                v = v.strip()
                if v.startswith("#"):  # "KEY=   # comment" means empty
                    v = ""
                elif len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if v and not v.startswith(("'", '"')) and " #" in v:
                    v = v.split(" #", 1)[0].strip()
                if k and v:
                    out[k] = v
    except OSError:
        pass
    return out


def load_env():
    """Put .env values into os.environ (real environment variables win). Idempotent."""
    for k, v in read_env_file().items():
        os.environ.setdefault(k, v)


# ---------- misc ----------

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def folder_name(label):
    """Folder name for a category label that is legal on Windows, macOS and Linux."""
    name = _BAD.sub("-", label.replace("&", "and")).strip(" .")
    return name or "Folder"


def clean_path(p):
    """User-typed or picked folder -> normalised absolute path (handles ~, trailing slashes, Windows)."""
    return os.path.abspath(os.path.expanduser((p or "").strip().strip('"')))