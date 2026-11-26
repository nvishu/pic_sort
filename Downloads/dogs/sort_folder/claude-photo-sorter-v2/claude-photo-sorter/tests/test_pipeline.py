"""End-to-end tests with fake AI servers (no keys, no internet).   python -m unittest discover -s tests -v

Covers: scan, duplicates, the multi-AI pool (rate limits, bad keys, junk answers, consensus, resume),
building folders, and the dashboard server's safety checks.
"""
import csv
import http.client
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

from PIL import Image, ImageDraw  # noqa: E402


# ------------------------------------------------------------ fake AI server

class Fake(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, home):
        super().__init__(("127.0.0.1", 0), Handler)
        self.home, self.calls, self.lock = home, {}, threading.Lock()
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj, headers=None):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        mode = self.path.split("/")[1]
        with self.server.lock:
            n = self.server.calls[mode] = self.server.calls.get(mode, 0) + 1
        if mode == "bad":
            return self.reply(401, {"error": "invalid key"})
        if mode == "flaky" and n <= 2:
            return self.reply(429, {"error": "slow down"}, {"Retry-After": "1"})
        gemini = ":generateContent" in self.path
        prompt = body["contents"][0]["parts"][0]["text"] if gemini else body["messages"][0]["content"][0]["text"]
        inv = jread(os.path.join(self.server.home, "dashboard", "data", "inventory.json"))
        only = re.search(r"Only label these ids \(ignore the others\): ([\d, ]+)\.", prompt)
        ids = [r for r in inv if not only or str(r["id"]) in re.split(r",\s*", only.group(1))]
        pick = {"odd": lambda r: "city", "junk": lambda r: "not-a-folder"}.get(mode, lambda r: ["food", "pets", "travel"][r["id"] % 3])
        labels = {str(r["id"]): ("vclips" if r["kind"] == "video" else pick(r)) for r in ids}
        text = json.dumps(labels)
        if mode == "partial" and not only:  # first answer covers every other file, wrapped in a code fence
            text = "Sure!\n```json\n" + json.dumps(dict(list(labels.items())[::2])) + "\n```"
        if gemini:
            return self.reply(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})
        return self.reply(200, {"choices": [{"message": {"content": text}}]})


# ------------------------------------------------------------ helpers

def jread(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def tread(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def make_home():
    home = tempfile.mkdtemp(prefix="sorter-test-")
    shutil.copy(os.path.join(ROOT, "categories.json"), home)
    return home


def make_photos(folder, n=100):
    os.makedirs(folder)
    rnd = random.Random(5)
    for i in range(n):
        im = Image.new("RGB", (320, 240), tuple(rnd.randint(0, 255) for _ in range(3)))
        d = ImageDraw.Draw(im)
        for _ in range(12):
            x, y = rnd.randint(0, 300), rnd.randint(0, 220)
            d.rectangle((x, y, x + rnd.randint(10, 120), y + rnd.randint(10, 90)), fill=tuple(rnd.randint(0, 255) for _ in range(3)))
        im.save(os.path.join(folder, f"IMG_{i:03d}.jpg"), quality=90)
    shutil.copy(os.path.join(folder, "IMG_000.jpg"), os.path.join(folder, "copy_of_0.jpg"))      # exact duplicates
    shutil.copy(os.path.join(folder, "IMG_001.jpg"), os.path.join(folder, "copy_of_1.jpg"))
    Image.open(os.path.join(folder, "IMG_002.jpg")).save(os.path.join(folder, "resaved_2.jpg"), quality=40)  # near duplicate


def run(home, script, *args, env=None, check=True):
    e = {**os.environ, "PHOTO_SORTER_HOME": home, "PYTHONIOENCODING": "utf-8"}
    for k in list(e):  # tests must not pick up the developer's real keys
        if k.endswith("_API_KEY") or k.startswith(("AI_", "CUSTOM_")):
            del e[k]
    e.update(env or {})
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args], env=e, capture_output=True,
                       text=True, encoding="utf-8", timeout=180)
    if check and r.returncode:
        raise AssertionError(f"{script} failed:\n{r.stdout}\n{r.stderr}")
    return r


def labels_of(home):
    out = {}
    d = os.path.join(home, "work", "labels")
    for f in sorted(os.listdir(d)):
        if f.endswith(".json"):
            out.update(jread(os.path.join(d, f)))
    return out


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = make_home()
        cls.photos = os.path.join(cls.home, "photos")
        make_photos(cls.photos)
        cls.fake = Fake(cls.home)
        run(cls.home, "scan.py", cls.photos)
        run(cls.home, "dupes.py")
        cls.inv = jread(os.path.join(cls.home, "dashboard", "data", "inventory.json"))

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()
        shutil.rmtree(cls.home, ignore_errors=True)

    def url(self, mode):
        return f"http://127.0.0.1:{self.fake.port}/{mode}"

    def reset_labels(self):
        shutil.rmtree(os.path.join(self.home, "work", "labels"), ignore_errors=True)
        self.fake.calls.clear()


class Scan(Base):
    def test_inventory(self):
        self.assertEqual(len(self.inv), 103)
        self.assertTrue(all("phash" in r and "error" not in r for r in self.inv))

    def test_duplicates_found(self):
        d = jread(os.path.join(self.home, "dashboard", "data", "dupes.json"))
        names = {self.inv[i]["name"] for g in d for i in g["dupes"] + [g["keep"]]}
        self.assertTrue({"IMG_000.jpg", "copy_of_0.jpg", "IMG_001.jpg", "copy_of_1.jpg"} <= names)
        near = [g for g in d if g["type"] == "near"]
        self.assertTrue(any({self.inv[i]["name"] for i in [g["keep"]] + g["dupes"]} == {"IMG_002.jpg", "resaved_2.jpg"} for g in near))


class Pool(Base):
    def setUp(self):
        self.reset_labels()

    def env(self, **extra):
        return {"GROQ_API_KEY": "k" * 12, "MISTRAL_API_KEY": "k" * 12, "GEMINI_API_KEY": "k" * 12,
                "OPENROUTER_API_KEY": "k" * 12, "GROQ_BASE_URL": self.url("flaky"), "MISTRAL_BASE_URL": self.url("ok"),
                "GEMINI_BASE_URL": self.url("gem"), "OPENROUTER_BASE_URL": self.url("bad"), **extra}

    def test_failover_rate_limit_and_bad_key(self):
        r = run(self.home, "label_ai.py", "--ai", "openrouter,groq,mistral,gemini", env=self.env())
        lab = labels_of(self.home)
        self.assertEqual(len(lab), 103, r.stdout)
        self.assertIn("0 missing", r.stdout)
        self.assertIn("openrouter stopped: key rejected", r.stdout)
        self.assertEqual(lab[str(next(x["id"] for x in self.inv if x["id"] % 3 == 0))], "food")

    def test_resume_does_not_ask_again(self):
        run(self.home, "label_ai.py", "--ai", "mistral", env=self.env())
        before = dict(self.fake.calls)
        r = run(self.home, "label_ai.py", "--ai", "mistral", env=self.env())
        self.assertEqual(before, self.fake.calls)
        self.assertIn("already done", r.stdout)

    def test_partial_answer_is_repaired(self):
        run(self.home, "label_ai.py", "--ai", "mistral", env=self.env(MISTRAL_BASE_URL=self.url("partial")))
        self.assertEqual(len(labels_of(self.home)), 103)
        self.assertGreater(self.fake.calls["partial"], 2)  # first answers were cut short, so it asked again

    def test_junk_answers_leave_files_unlabelled_and_fail_loudly(self):
        r = run(self.home, "label_ai.py", "--ai", "mistral", env=self.env(MISTRAL_BASE_URL=self.url("junk")), check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("batches failed", r.stdout)

    def test_consensus_majority_wins(self):
        r = run(self.home, "label_ai.py", "--ai", "mistral,groq,gemini", "--consensus", "3",
                env=self.env(GROQ_BASE_URL=self.url("odd")))
        lab = labels_of(self.home)
        for rec in self.inv:  # two sensible engines outvote the odd one
            self.assertEqual(lab[str(rec["id"])], ["food", "pets", "travel"][rec["id"] % 3])
        self.assertIn("disagreed", r.stdout)

    def test_auto_uses_only_free_and_ready(self):
        r = run(self.home, "label_ai.py", "--list", env={"OPENAI_API_KEY": "k" * 12, "MISTRAL_API_KEY": "k" * 12})
        self.assertRegex(r.stdout, r"ready\s+mistral")
        self.assertRegex(r.stdout, r"ready\s+openai")
        sys.path.insert(0, SCRIPTS)
        import providers as P
        names = [p.name for p in P.choose("auto", {"OPENAI_API_KEY": "k" * 12, "MISTRAL_API_KEY": "k" * 12})]
        self.assertEqual(names, ["mistral"])  # paid engines are not used unless asked for

    def test_no_engine_is_a_clear_error(self):
        r = run(self.home, "label_ai.py", env={"PATH": "/nonexistent"}, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("No AI is ready", r.stdout + r.stderr)


class Apply(Base):
    def test_folders_and_csv(self):
        self.reset_labels()
        run(self.home, "label_ai.py", "--ai", "mistral", env={"MISTRAL_API_KEY": "k" * 12, "MISTRAL_BASE_URL": self.url("ok")})
        dest = os.path.join(self.home, "sorted")
        before = sorted(os.listdir(self.photos))
        r = run(self.home, "apply.py", dest)
        self.assertEqual(sorted(os.listdir(self.photos)), before)  # originals untouched
        self.assertTrue(os.path.isdir(os.path.join(dest, "Food and drinks")))
        self.assertTrue(os.path.isdir(os.path.join(dest, "_Review", "duplicates")))
        with open(os.path.join(dest, "sorted.csv"), encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 103)
        run(self.home, "apply.py", dest)  # rebuilding is clean, no leftovers or doubled files
        self.assertEqual(sum(len(f) for _, _, f in os.walk(dest)), 103 + 2)
        self.assertIn("Sorted into", r.stdout)
        inside = run(self.home, "apply.py", os.path.join(self.photos, "out"), check=False)
        self.assertNotEqual(inside.returncode, 0)  # refuses to write inside the photos folder


class Units(unittest.TestCase):
    def test_parse_json(self):
        import providers as P
        self.assertEqual(P.parse_json('x ```json\n{"1": "a"}\n``` y'), {"1": "a"})
        self.assertEqual(P.parse_json('Here: {"1": "a", "2": "b"} done {"z"'), {"1": "a", "2": "b"})
        self.assertEqual(P.parse_json("nothing"), {})
        self.assertEqual(P.parse_json(None), {})

    def test_folder_name_is_safe_everywhere(self):
        from common import folder_name
        self.assertEqual(folder_name("Food & drinks"), "Food and drinks")
        self.assertEqual(folder_name('a/b:c*d?"e'), "a-b-c-d--e")
        self.assertEqual(folder_name("..."), "Folder")

    def test_normaliser_accepts_display_names_and_checks_kind(self):
        import label_ai as L
        from common import load
        cats = load(os.path.join(ROOT, "categories.json"))
        norm = L.make_normaliser(cats, {"1": "photo", "2": "video", "3": "photo"})
        out = norm({"1": "Food & drinks", "2": "photo-folder", "3": "vclips", "9": "food"}, {"1", "2", "3"})
        self.assertEqual(out, {"1": "food"})  # unknown, wrong-kind and unasked ids are dropped


class Server(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        shutil.copytree(os.path.join(ROOT, "dashboard"), os.path.join(cls.home, "dashboard"), dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("data", "peek", "demo"))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        env = {**os.environ, "PHOTO_SORTER_HOME": cls.home, "PORT": str(cls.port)}
        for k in list(env):
            if k.endswith("_API_KEY"):
                del env[k]
        cls.proc = subprocess.Popen([sys.executable, os.path.join(SCRIPTS, "server.py")], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        super().tearDownClass()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json", **(headers or {})}
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        return r.status, (json.loads(data) if r.getheader("Content-Type", "").startswith("application/json") else data)

    def test_state_and_page(self):
        st, j = self.req("GET", "/api/state")
        self.assertEqual(st, 200)
        self.assertEqual(j["files"], 103)
        self.assertIn("gemini", [e["name"] for e in j["engines"]])
        self.assertEqual(self.req("GET", "/")[0], 200)

    def test_refuses_other_hosts_and_origins(self):
        self.assertEqual(self.req("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)   # DNS rebinding
        self.assertEqual(self.req("POST", "/api/move", {"id": 0, "key": "food"}, {"Origin": "http://evil.example"})[0], 403)

    def test_bad_input_is_a_400_not_a_crash(self):
        self.assertEqual(self.req("POST", "/api/key", {"engine": "gemini", "key": "bad key!"})[0], 400)
        self.assertEqual(self.req("POST", "/api/key", {"engine": "ollama", "key": "x" * 12})[0], 400)
        self.assertEqual(self.req("POST", "/api/move", {"id": 0, "key": "vclips"})[0], 400)         # photo into a video folder
        self.assertEqual(self.req("POST", "/api/sort", {"source": "/definitely/not/here"})[0], 400)
        self.assertEqual(self.req("POST", "/api/sort", {"source": self.photos, "labeler": "gemini"})[0], 400)  # no key yet

    def test_key_round_trip(self):
        st, j = self.req("POST", "/api/key", {"engine": "groq", "key": "gsk_" + "a" * 20})
        self.assertEqual(st, 200)
        self.assertTrue(next(e for e in j["engines"] if e["name"] == "groq")["ready"])
        env = tread(os.path.join(self.home, ".env"))
        self.assertIn("GROQ_API_KEY=gsk_", env)
        self.assertNotIn("gsk_", json.dumps(j))  # keys are never sent back to the page
        st, j = self.req("POST", "/api/key", {"engine": "groq", "key": ""})
        self.assertFalse(next(e for e in j["engines"] if e["name"] == "groq")["ready"])


if __name__ == "__main__":
    unittest.main()
