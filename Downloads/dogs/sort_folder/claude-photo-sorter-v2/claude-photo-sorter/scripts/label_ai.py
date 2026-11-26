"""Label every file with one AI, or a pool of free AIs that back each other up.

  python scripts/label_ai.py                       # auto: every ready free engine, shared work
  python scripts/label_ai.py --ai gemini,groq      # these engines, in this order
  python scripts/label_ai.py --ai claude           # just Claude Code (your plan)
  python scripts/label_ai.py --consensus 2         # two engines must agree, a third breaks ties
  python scripts/label_ai.py --only 1              # first batch only, to test
  python scripts/label_ai.py --list                # which engines are ready

How the pool works: contact sheets (SHEET_SIZE previews each) are grouped 3 per batch. Every batch goes
to the least busy healthy engine. If that engine is rate-limited, out of quota, down, or gives a bad
answer, the batch moves to the next one, so free tiers add up instead of getting in the way. Bad answers
are repaired by asking again for only the missing files. Finished batches are kept: run again to resume.

Env / .env:  GEMINI_API_KEY GROQ_API_KEY MISTRAL_API_KEY OPENROUTER_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY
             AI_PROVIDERS (same as --ai)  AI_MODEL  AI_WORKERS  AI_CONSENSUS  AI_ALLOW_PAID=1
Old single-engine form still works:  AI_PROVIDER=gemini AI_KEY=... python scripts/label_ai.py
"""
import argparse
import collections
import glob
import os
import random
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))
from common import AI_STATS, DATA, LABELS, SHEET_SIZE, WORK, categories, load, load_env, save
import providers as P

SHEETS_PER_BATCH = 3
MAX_ROUNDS = 4          # passes over the whole pool for one batch before giving up
COOLDOWN_DEAD = 120     # seconds an engine rests after repeated failures

_print = threading.Lock()


def log(msg):
    with _print:
        print(msg, flush=True)


# ---------------------------------------------------------------- engine health

class Engine:
    """One provider plus its live state: pacing, cooldown, model choice and counters."""

    def __init__(self, provider, key):
        self.p, self.key = provider, key
        self.name = provider.name
        self.model_i = 0
        self.lock = threading.Lock()
        self.next_slot = 0.0          # earliest time the next request may start (rate pacing)
        self.cool_until = 0.0
        self.disabled = ""            # reason, when out for the whole run
        self.inflight = 0
        self.streak = 0
        self.stats = collections.Counter()
        self.slots = threading.Semaphore(provider.workers)

    @property
    def model(self):
        return self.p.models[min(self.model_i, len(self.p.models) - 1)]

    def usable(self, now):
        return not self.disabled and now >= self.cool_until

    def pace(self):
        """Block until this engine's requests-per-minute budget has room."""
        if not self.p.rpm:
            return
        gap = 60.0 / self.p.rpm
        with self.lock:
            now = time.time()
            at = max(now, self.next_slot)
            self.next_slot = at + gap
        if at > now:
            time.sleep(at - now)

    def fail(self, err):
        k = err.kind
        with self.lock:
            self.stats["errors"] += 1
            if k == "auth":
                self.disabled = "key rejected" if self.p.api != "claude" else "claude not available"
            elif k == "quota":
                self.disabled = "out of quota for today"
            elif k == "rate":
                self.cool_until = time.time() + (err.retry_after or 20) + random.random() * 2
                self.next_slot = max(self.next_slot, self.cool_until)
            elif k == "model":
                if self.model_i + 1 < len(self.p.models):
                    self.model_i += 1
                    log(f"  {self.name}: model not usable, switching to {self.model}")
                else:
                    self.disabled = "no usable model"
            else:
                self.streak += 1
                if self.streak >= 3:
                    self.cool_until = time.time() + COOLDOWN_DEAD
                    self.streak = 0

    def ok(self):
        self.streak = 0


class Pool:
    def __init__(self, engines):
        self.engines = engines
        self.lock = threading.Lock()
        self.disagreed = 0

    def alive(self):
        return [e for e in self.engines if not e.disabled]

    def acquire(self, exclude=(), strict=False):
        """Least-loaded usable engine, preferring ones not in `exclude`; waits out cooldowns.
        None if no engine can ever work (or, when strict, none outside `exclude`)."""
        while True:
            with self.lock:
                now = time.time()
                alive = self.alive()
                pool = [e for e in alive if e.name not in exclude]
                if not pool and not strict:
                    pool = alive
                if not pool:
                    return None
                ready = [e for e in pool if e.usable(now)]
                if ready:
                    e = min(ready, key=lambda e: (e.inflight / e.p.workers, e.stats["batches"], self.engines.index(e)))
                    e.inflight += 1
                    return e
                wake = min(e.cool_until for e in pool)
            time.sleep(min(max(wake - time.time(), 0.2), 5))

    def release(self, e):
        with self.lock:
            e.inflight -= 1


# ---------------------------------------------------------------- prompts and answers

def build_prompt(cats, n_sheets, only_ids=None):
    folders = "\n".join(f"- {c['key']} ({c['kind']}): {c['rule']}" for c in cats)
    extra = f"\nOnly label these ids (ignore the others): {', '.join(only_ids)}.\n" if only_ids else ""
    return (
        "You sort a camera roll into folders. These are the folders, as key (kind): what goes in it\n"
        f"{folders}\n\n"
        f"Look at these {n_sheets} contact sheet image(s). Each sheet shows up to {SHEET_SIZE} square previews, "
        "each tagged in yellow with a number (its id). A tag with VIDEO is a video and must get a (video) "
        "folder. Every other tile is a photo and must get a (photo) folder. PNG usually means a screenshot. "
        "Give every id you see exactly one folder key from the list." + extra + "\n\n"
        'Reply with only a JSON object mapping id to folder key, like {"12": "food", "13": "selfies"}. '
        "No other text.")


def make_normaliser(cats, kinds):
    """raw model answer -> {id: key}, keeping only wanted ids whose folder exists and fits (photo vs video)."""
    by_key = {c["key"]: c for c in cats}
    squash = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())
    alias = {}
    for c in cats:  # models sometimes answer with the folder's display name instead of its key
        for a in (c["key"], c["label"], c["label"].replace("&", "and"), c["label"].replace("&", "")):
            alias[squash(a)] = c["key"]

    def norm(raw, want):
        out = {}
        for k, v in raw.items():
            k = str(k).strip()
            if k not in want or not isinstance(v, str):
                continue
            key = alias.get(squash(v))
            if key and by_key[key]["kind"] == kinds[k]:
                out[k] = key
        return out
    return norm


# ---------------------------------------------------------------- one batch

def ask_engine(eng, prompt, images):
    """Call one engine, retrying short-lived trouble. Returns text, or raises P.ProviderError."""
    last = None
    for attempt in range(3):
        eng.pace()
        with eng.slots:
            t0 = time.time()
            try:
                text = P.ask(eng.p, eng.model, eng.key, prompt, images)
                eng.ok()
                eng.stats["seconds"] += time.time() - t0
                return text
            except P.ProviderError as err:
                last = err
                eng.fail(err)
        if last.kind in ("auth", "quota", "model", "rate") or eng.disabled or time.time() < eng.cool_until:
            break  # let another engine (or the next model) take it
        time.sleep(2 * (attempt + 1) + random.random())
    raise last


def label_batch(pool, n, batch, want, kinds, cats, out_path, prior, consensus):
    norm = make_normaliser(cats, kinds)
    wantset = set(want)
    got = norm(prior, wantset)            # resume: keep what an earlier run (or engine) already got
    votes = collections.defaultdict(list)
    used, tried, why = [], set(), "no engine could answer"
    for _ in range(MAX_ROUNDS * max(1, len(pool.engines))):
        missing = [i for i in want if i not in got]
        if consensus < 2 and not missing:
            break
        if consensus >= 2 and not missing and len(used) >= consensus:
            break
        extra_vote = consensus >= 2 and not missing
        eng = pool.acquire(exclude=tried | (set(used) if consensus >= 2 else set()), strict=extra_vote)
        if eng is None:
            break
        try:
            only = missing if missing and len(missing) < len(want) else None
            text = ask_engine(eng, build_prompt(cats, len(batch), only), batch)
            labels = norm(P.parse_json(text), set(only or want))
            if not labels:
                eng.stats["bad"] += 1
                eng.fail(P.ProviderError("no usable labels", "bad_output"))
                with open(os.path.join(WORK, f"ai_failed_{eng.name}_{n:02d}.txt"), "w", encoding="utf-8") as f:
                    f.write(text or "")
                raise P.ProviderError("no usable labels: " + (" ".join((text or "").split())[:90] or "empty answer"),
                                      "bad_output")
            for i, k in labels.items():
                votes[i].append((eng.name, k))
                got.setdefault(i, k)
            eng.stats["batches"] += 1
            eng.stats["files"] += len(labels)
            used.append(eng.name)
            tried.add(eng.name)
            save(out_path, got)  # partial progress: the dashboard animates it and a rerun resumes from it
        except P.ProviderError as err:
            why = f"{eng.name}: {err}"
            log(f"  batch {n + 1}: {eng.name} failed ({str(err)[:110]}); trying another")
            tried.add(eng.name)
            if tried >= {x.name for x in pool.alive()}:
                tried.clear()
                time.sleep(2 + random.random() * 2)
        finally:
            pool.release(eng)
    if consensus >= 2:  # majority vote; a tie goes to the engine that answered first
        for i, vs in votes.items():
            got[i] = collections.Counter(k for _, k in vs).most_common(1)[0][0]
            if len({k for _, k in vs}) > 1:
                pool.disagreed += 1
    if got:
        save(out_path, got)
    if len(got) < 0.9 * len(want):
        raise RuntimeError(f"batch {n + 1} only got {len(got)}/{len(want)} labels ({why})")
    return n, len(got), "+".join(dict.fromkeys(used)) or "cached"


# ---------------------------------------------------------------- main

def prior_labels(n):
    """Labels any earlier run wrote for batch n (this tool's old claude_NN / gemini_NN files count too)."""
    out = {}
    for f in sorted(glob.glob(os.path.join(LABELS, f"*_{n:02d}.json"))):
        out.update({str(k): v for k, v in load(f, {}).items()})
    return out


def main():
    load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ai", default=os.environ.get("AI_PROVIDERS") or os.environ.get("AI_PROVIDER") or "auto")
    ap.add_argument("--consensus", type=int, default=int(os.environ.get("AI_CONSENSUS") or 1))
    ap.add_argument("--only", type=int, default=0, help="only the first N batches")
    ap.add_argument("--list", action="store_true", help="show which engines are ready and exit")
    a = ap.parse_args()

    if a.list:
        avail = P.ready()
        for n, p in P.REGISTRY.items():
            print(f"{'ready ' if n in avail else '------'} {n:11} {p.label}" + ("" if n in avail else f"   ({p.hint})"))
        return 0

    try:
        provs = P.choose(a.ai)
    except ValueError as e:
        sys.exit(str(e))
    if os.environ.get("AI_WORKERS"):
        provs = [P.Provider(**{**p.__dict__, "workers": int(os.environ["AI_WORKERS"])}) for p in provs]
    engines = [Engine(p, p.key()) for p in provs]
    pool = Pool(engines)
    consensus = min(a.consensus, len(engines))

    sheet_dir = os.path.join(WORK, "sheets")
    if not glob.glob(os.path.join(sheet_dir, "sheet_*.jpg")):
        subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "sheets.py")], check=True)
    sheets = sorted(glob.glob(os.path.join(sheet_dir, "sheet_*.jpg")))
    inv = load(os.path.join(DATA, "inventory.json"), [])
    if not inv or not sheets:
        sys.exit("Nothing to label yet: run scan.py first.")
    cats = categories()
    os.makedirs(LABELS, exist_ok=True)
    kinds = {str(r["id"]): r["kind"] for r in inv}
    ids = [str(r["id"]) for r in inv]

    batches = [sheets[i:i + SHEETS_PER_BATCH] for i in range(0, len(sheets), SHEETS_PER_BATCH)]
    if a.only:
        batches = batches[:a.only]
    per = SHEET_SIZE * SHEETS_PER_BATCH
    wants = [ids[i * per:(i + 1) * per] for i in range(len(batches))]

    log(f"Labelling {len(ids)} files in {len(batches)} batches with: "
        + ", ".join(f"{e.name} ({e.model})" for e in engines)
        + (f" - {consensus} engines must agree" if consensus > 1 else ""))
    t0, done, failed, todo = time.time(), 0, [], []
    for n, (b, w) in enumerate(zip(batches, wants)):
        prior = prior_labels(n)
        if consensus < 2 and set(w) <= set(make_normaliser(cats, kinds)(prior, set(w))):
            done += 1
        else:
            todo.append((n, b, w, prior))
    if done:
        log(f"PROGRESS {done} {len(batches)}")
        log(f"  {done} batches already done, resuming")
    with ThreadPoolExecutor(max(1, sum(e.p.workers for e in engines))) as ex:
        futs = [ex.submit(label_batch, pool, n, b, w, kinds, cats, os.path.join(LABELS, f"ai_{n:02d}.json"), prior, consensus)
                for n, b, w, prior in todo]
        for f in as_completed(futs):
            try:
                n, k, who = f.result()
                done += 1
                log(f"PROGRESS {done} {len(batches)}")
                log(f"  batch {done}/{len(batches)} done: {k} files ({who})")
            except Exception as e:  # noqa: BLE001 - one bad batch must not stop the others
                failed.append(str(e))
                log(f"  {e}")
    for e in engines:
        if e.disabled:
            log(f"  note: {e.name} stopped: {e.disabled}")

    prev = load(AI_STATS, {})
    for e in engines:
        s = prev.setdefault(e.name, {})
        for k in ("batches", "files", "errors"):
            s[k] = s.get(k, 0) + e.stats[k]
    save(AI_STATS, prev)

    labelled = set()
    for f in glob.glob(os.path.join(LABELS, "*.json")):
        labelled |= set(load(f, {}))
    missing = len(ids) - len(labelled & set(ids))
    split = ", ".join(f"{e.name} {e.stats['batches']}" for e in engines if e.stats["batches"])
    log(f"Done in {time.time() - t0:.0f}s: {len(ids) - missing} labelled, {missing} missing"
        + (f". Batches by engine: {split}" if split else "")
        + (f". {pool.disagreed} files where the engines disagreed" if pool.disagreed else "")
        + (f". {len(failed)} batches failed (run again to retry)" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())