"""Step 2: find duplicates.  python scripts/dupes.py

Exact = identical bytes (sha256). Near = looks the same (perceptual hash distance <= 4):
burst shots, re-saved screenshots, resized copies. The biggest file in each group is kept,
the rest are listed in dashboard/data/dupes.json and later copied to _Review/duplicates/.
Also writes work/duplicates.jpg so you can eyeball the groups before trusting them.

Speed: hashes are packed into uint64 and compared a whole row at a time with numpy
(100,000 photos take seconds, not hours).
"""
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(__file__))
from common import DATA, THUMBS, WORK, load, save

NEAR = 4  # max differing bits out of 64 to call two pictures the same

recs = load(os.path.join(DATA, "inventory.json"), [])
parent = {r["id"]: r["id"] for r in recs}
byid = {r["id"]: r for r in recs}


def find(x):
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        parent[rb] = ra


by_sha = {}
for r in recs:
    by_sha.setdefault(r["sha256"], []).append(r["id"])
for ids in by_sha.values():
    for x in ids[1:]:
        union(ids[0], x)

_gray = {}


def gray(i):
    if i not in _gray:
        with Image.open(os.path.join(THUMBS, f"{i}.jpg")) as im:
            _gray[i] = np.asarray(im.convert("L"), dtype=np.int16)
    return _gray[i]


def screenshots_differ(a, b):
    """Screenshots of the same app look identical to a perceptual hash (dark UI, same layout),
    so for PNGs also require almost no changed pixels: <0.4% of the preview."""
    return (np.abs(gray(a) - gray(b)) > 40).mean() > 0.004


POP = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)  # bits set in a byte

for kind in ("photo", "video"):  # a photo is never a duplicate of a video frame
    rows = [r for r in recs if r["kind"] == kind and r.get("phash") and "error" not in r]
    if len(rows) < 2:
        continue
    ids = [r["id"] for r in rows]
    h = np.array([int(r["phash"], 16) for r in rows], dtype=np.uint64)
    for i in range(len(rows) - 1):
        if i % 200 == 0:
            print(f"PROGRESS {i} {len(rows)}", flush=True)
        x = np.bitwise_xor(h[i + 1:], h[i])                       # differing bits with every later file
        dist = POP[x.view(np.uint8).reshape(-1, 8)].sum(axis=1)   # popcount per row
        for j in np.nonzero(dist <= NEAR)[0] + i + 1:
            a, b = ids[i], ids[j]
            if find(a) == find(b):
                continue
            ra, rb = byid[a], byid[b]
            if ra["ext"] == ".png" and rb["ext"] == ".png" and ra["sha256"] != rb["sha256"] and screenshots_differ(a, b):
                continue
            union(a, b)

groups = {}
for r in recs:
    groups.setdefault(find(r["id"]), []).append(r)
out = []
for g in groups.values():
    if len(g) < 2:
        continue
    g.sort(key=lambda r: (-r["bytes"], len(r["name"]), r["id"]))
    out.append({"keep": g[0]["id"], "dupes": [r["id"] for r in g[1:]],
                "type": "exact" if len({r["sha256"] for r in g}) == 1 else "near",
                "freed": sum(r["bytes"] for r in g[1:])})
out.sort(key=lambda g: g["keep"])
save(os.path.join(DATA, "dupes.json"), out)

# contact sheet: one row per group, keeper first
if out:
    rows = [[g["keep"]] + g["dupes"] for g in out][:60]
    W = min(max(map(len, rows)), 8)
    sheet = Image.new("RGB", (W * 170, len(rows) * 170), "white")
    d = ImageDraw.Draw(sheet)
    for y, row in enumerate(rows):
        for x, i in enumerate(row[:W]):
            with Image.open(os.path.join(THUMBS, f"{i}.jpg")) as t:
                sheet.paste(t.resize((160, 160)), (x * 170, y * 170))
            d.rectangle((x * 170, y * 170, x * 170 + 60, y * 170 + 14), fill="black")
            d.text((x * 170 + 3, y * 170 + 2), ("KEEP " if x == 0 else "") + str(i), fill="yellow")
    os.makedirs(WORK, exist_ok=True)
    sheet.save(os.path.join(WORK, "duplicates.jpg"), quality=80)

n = sum(len(g["dupes"]) for g in out)
print(f"{len(out)} duplicate groups, {n} extra copies, {sum(g['freed'] for g in out) / 1e9:.2f} GB could be freed"
      + ("  (preview: work/duplicates.jpg)" if out else ""))
