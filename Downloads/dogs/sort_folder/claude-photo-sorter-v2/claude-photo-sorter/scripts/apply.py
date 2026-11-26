"""Step 4: build the folders.  python scripts/apply.py "/where/to/put/Sorted"

Reads labels from work/labels/*.json (any AI) and/or work/jev.json (Jev).
If both exist: Jev's answer wins when it is >= 70% confident, otherwise the AI's.
COPIES files into <output>/<Folder>/ and duplicates into <output>/_Review/duplicates/.
On a Mac (same disk) copies are APFS clones: instant and they take no extra space.
Originals are never moved, renamed or deleted. Also writes sorted.csv and duplicates.csv.
"""
import csv
import glob
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from common import CATEGORIES, DATA, JEV_OUT, LABELS, WORK, catch_all, categories, clean_path, folder_name, load, save

OVERRIDES = os.path.join(WORK, "overrides.json")
src_info = load(os.path.join(DATA, "source.json"), {})
src_dir = src_info.get("source", "")
default = (src_dir.rstrip("/\\") + " (sorted)") if src_dir else ""
arg = sys.argv[1] if len(sys.argv) > 1 else ""
dest = clean_path(arg or default) if (arg or default) else ""
if not dest:
    sys.exit('Usage: python scripts/apply.py "/where/to/put/Sorted"')
norm = lambda p: os.path.normcase(os.path.abspath(p))
if src_dir and (norm(dest) + os.sep).startswith(norm(src_dir) + os.sep):
    sys.exit("Put the sorted folders outside the photos folder, or the next scan would count them twice.")

# The dashboard normally suggests "<source> (sorted)". A typed path is on its own.
if os.path.exists(dest) and not os.path.isdir(dest):
    sys.exit(f"{dest} is not a folder.")
if os.path.isdir(dest) and not os.path.exists(os.path.join(dest, "sorted.csv")):
    existing = [n for n in os.listdir(dest) if not n.startswith(".")]
    if existing:
        sys.exit(f"{dest} already has {len(existing)} item(s) and no sorted.csv from a previous run. "
                 "Pick an empty folder, or use the path this tool suggested. Nothing was moved.")

recs = load(os.path.join(DATA, "inventory.json"), [])
cats_list = categories()
cats = {c["key"]: c for c in cats_list}
dupes = load(os.path.join(DATA, "dupes.json"), [])

ai = {}
for f in sorted(glob.glob(os.path.join(LABELS, "*.json"))):
    ai.update({str(k): v for k, v in load(f, {}).items()})
jev = load(JEV_OUT, {})
if not ai and not jev:
    sys.exit("No labels yet. Label the files first (scripts/label_ai.py, or Jev: scripts/label_jev.py).")

labels, fixed, missing = {}, 0, 0
for r in recs:
    k = str(r["id"])
    j = jev.get(k, {})
    pick = j.get("key") if j.get("confidence", 0) >= 0.7 or k not in ai else ai.get(k)
    pick = pick or ai.get(k)
    if pick is None:
        missing += 1
    if pick not in cats or cats[pick]["kind"] != r["kind"]:  # photos only in photo folders, videos in video folders
        fixed += pick is not None
        pick = catch_all(cats_list, r["kind"])
    labels[k] = pick

for k, v in load(OVERRIDES, {}).items():  # moves made by hand in the dashboard win
    if k in labels and v in cats and cats[v]["kind"] == next((r["kind"] for r in recs if str(r["id"]) == k), None):
        labels[k] = v

save(os.path.join(DATA, "labels.json"), labels)
src_info["sorted"] = dest
save(os.path.join(DATA, "source.json"), src_info)
shutil.copy(CATEGORIES, os.path.join(DATA, "categories.json"))

dupe_of = {d: g for g in dupes for d in g["dupes"]}
byid = {r["id"]: r for r in recs}


def copy(src, dst):
    """Cheapest copy the disk allows: APFS/btrfs/xfs clone first, then a normal copy."""
    if sys.platform == "darwin":
        cmd = ["cp", "-c", "-p", src, dst]
    elif sys.platform.startswith("linux"):
        cmd = ["cp", "--reflink=auto", "-p", src, dst]
    else:
        cmd = None
    if cmd and subprocess.run(cmd, capture_output=True).returncode == 0:
        return
    shutil.copy2(src, dst)


os.makedirs(dest, exist_ok=True)
old = os.path.join(dest, "sorted.csv")
if os.path.exists(old):  # rebuilding: remove only the copies this tool made last time
    with open(old, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            p = os.path.join(dest, row["file"])
            if os.path.isfile(p):
                os.remove(p)
    for d, _, _ in os.walk(dest, topdown=False):
        if d != dest and not os.listdir(d):
            os.rmdir(d)

made, errors = set(), []
with open(os.path.join(dest, "sorted.csv"), "w", newline="", encoding="utf-8-sig") as fc, \
     open(os.path.join(dest, "duplicates.csv"), "w", newline="", encoding="utf-8-sig") as fd:
    wc, wd = csv.writer(fc), csv.writer(fd)
    wc.writerow(["file", "folder", "original_path"])
    wd.writerow(["duplicate", "kept_copy", "type", "duplicate_path", "kept_path"])
    for n, r in enumerate(recs, 1):
        is_dupe = r["id"] in dupe_of
        folder = os.path.join("_Review", "duplicates") if is_dupe else folder_name(cats[labels[str(r["id"])]]["label"])
        out_dir = os.path.join(dest, folder)
        if out_dir not in made:
            os.makedirs(out_dir, exist_ok=True)
            made.add(out_dir)
        target = os.path.join(out_dir, r["name"])
        if os.path.exists(target):  # same name from another folder/year
            b, e = os.path.splitext(r["name"])
            target = os.path.join(out_dir, f"{b}__{r['id']}{e}")
        try:
            if not os.path.exists(target):
                copy(r["path"], target)
        except OSError as e:  # one unreadable file must not stop the rest
            errors.append(f"{r['name']}: {e}")
            continue
        if is_dupe:
            g = dupe_of[r["id"]]
            k = byid[g["keep"]]
            wd.writerow([r["name"], k["name"], g["type"], r["path"], k["path"]])
        wc.writerow([os.path.relpath(target, dest), folder, r["path"]])
        if n % 25 == 0 or n == len(recs):
            print(f"PROGRESS {n} {len(recs)}", flush=True)
        if n % 500 == 0:
            print(f"  copied {n}/{len(recs)}", flush=True)

counts = {}
for r in recs:
    if r["id"] not in dupe_of:
        counts[labels[str(r["id"])]] = counts.get(labels[str(r["id"])], 0) + 1
print(f"Sorted into {dest}")
for k, c in cats.items():
    print(f"  {c['label']:24} {counts.get(k, 0)}")
print(f"  {'_Review/duplicates':24} {len(dupe_of)}")
if missing or fixed:
    print(f"  ({missing} files had no label and {fixed} had a wrong-type label; they went to the last folder of their kind)")
if errors:
    print(f"  warning: {len(errors)} files could not be copied (first: {errors[0]})")