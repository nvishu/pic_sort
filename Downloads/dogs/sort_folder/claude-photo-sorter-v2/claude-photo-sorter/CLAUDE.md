# Photo sorter: instructions for the agent

This repo sorts a camera roll into folders, finds duplicates, and shows it all on a local dashboard. The person you're helping has probably never used Claude Code or a terminal. Do the technical work yourself, keep messages short, and use plain words ("folder", not "directory").

## Ground rules (never break these)

1. **Never move, rename or delete the user's original photos.** The tool only reads them and makes sorted copies. If they want duplicates deleted, show them `duplicates.csv` and let them do it.
2. **Ask before anything that costs money.** Jev (TypeSafe), OpenAI and Anthropic are billed. Auto mode never uses paid engines unless `AI_ALLOW_PAID=1`. For Jev run `label_jev.py --estimate`, show the files and tokens, and wait for a yes.
3. **Keep their photos private.** Never commit anything in `dashboard/data/`, `dashboard/peek/`, `work/` or `.env`.
4. One question at a time, with numbered options they can answer with a number.

## When someone gives you this repo's link (the usual case)

They pasted something like "set up https://github.com/kairothq/claude-photo-sorter". Do this without asking:

1. **Download it.** If you're not already inside this repo: `git clone https://github.com/kairothq/claude-photo-sorter.git ~/claude-photo-sorter` (if that folder exists, `git -C ~/claude-photo-sorter pull` instead), then work from that folder.
2. **Set up.** Run `python3 start.py --setup` (or `./setup.sh`). If Python is missing, ask them to run `xcode-select --install` and wait. If ffmpeg is missing and `brew` exists, offer `brew install ffmpeg` (videos just get grey previews without it).
3. **Open the dashboard.** Run `python3 start.py` in the background (it keeps running and opens the browser at http://127.0.0.1:8765).
4. **Tell them, in about three lines:**
   - The dashboard is open in their browser.
   - Click **Choose folder** and pick their photos (a Google Takeout or Apple Photos export, or any folder), then pick where the sorted copies go, then **Start sorting**.
   - It runs by itself and shows progress. Folders can be changed under **Advanced**. To open it again later: double-click `Open Photo Sorter.command` in `~/claude-photo-sorter`, or ask you to "open my photo sorter".
5. Stay available. If they ask questions, answer from what you see in `dashboard/data/` and the server's `/api/state`.

If they don't have an export yet, explain:
- **Google Photos:** takeout.google.com, select only Google Photos, download, unzip into one folder.
- **iPhone / Apple Photos:** in Photos on the Mac, select all, File > Export > Export Unmodified Originals, into a new folder.
Never read `Photos Library.photoslibrary` directly.

## What the dashboard does by itself

**Start sorting** runs everything in order and shows live progress, a timer and a time estimate:

1. Scan: inventory, sha256, 256px previews plus 72px copies for the animation (skipped if the same folder was already scanned).
2. Duplicates: identical files and look-alikes (bursts, re-saved screenshots).
3. Labels: `scripts/label_ai.py` pools every ready AI (free ones: Gemini, Groq, Mistral, OpenRouter, Ollama, Claude Code). Batches of ~90 files go to the least busy engine, fail over on rate limits, and bad answers are repaired. Under **Advanced** they can pick one engine, paste more free keys (saved to `.env`), or pick Jev.
4. Build folders: copies into `<their folder> (sorted)` or wherever they chose.

Photos fly into their folders as labels come in. After sorting: **Sort with Claude** replays the animation (good for screen recording, `?d=20` makes it slower), **New sort** picks new folders, clicking a folder browses it, and a photo can be moved to another folder from the full-size view.

## If they'd rather do it in this chat

| Step | Command |
|---|---|
| Scan | `.venv/bin/python scripts/scan.py "<photos folder>" [--limit 200]` |
| Duplicates | `.venv/bin/python scripts/dupes.py` (check `work/duplicates.jpg`) |
| Sample sheets | `.venv/bin/python scripts/sheets.py --sample` (look at `work/sample/` to suggest folders) |
| Labels (all ready AIs) | `.venv/bin/python scripts/label_ai.py` (`--list` shows what is ready, `--ai gemini,groq` sets order, `--consensus 2` for accuracy) |
| Labels (Claude only) | `.venv/bin/python scripts/label_claude.py` |
| Labels (Jev, paid) | `.venv/bin/python scripts/label_jev.py --estimate`, then without `--estimate` |
| Build folders | `.venv/bin/python scripts/apply.py "<output folder>"` |

The open dashboard refreshes itself when these run.

**Choosing folders.** Folders live in `categories.json` (`key`, `kind` photo or video, `label`, `emoji`, `rule`). The `rule` is what the model reads. Offer: 1) the standard folders, 2) folders you suggest after looking at `work/sample/` (a creator gets "Content analytics" and "Talking heads", a parent gets "Kids" and "School"), or 3) their own list. Keep at least one photo and one video folder, and make the last folder of each kind a catch-all.

**Labelling yourself instead of `label_claude.py`.** Run `sheets.py`; each sheet in `work/sheets/` shows 30 previews tagged with ids. `VIDEO` tags need a video folder. Write `work/labels/<name>.json` as `{"id": "key"}` for every id. Use subagents for big libraries.

## If something breaks

- Port 8765 busy: `PORT=8766 python3 start.py`.
- After changing scripts, run `python3 -m unittest discover -s tests`.
- "No AI is ready": add a free key (Gemini or Groq) under Advanced > Add another AI, or start Ollama, or make `claude` available on PATH and restart.
- An AI says "out of quota" or keeps rate-limiting: Auto moves on to the others by itself; add another free key to go faster.
- A batch failed: press Start sorting again. Finished batches are kept.
- HEIC photos fail to open: `.venv/bin/pip install -U pillow-heif`.
- Jev 401: the key in `.env` is wrong. Jev 400 "Invalid image dimensions": leave `JEV_MODEL` unset.
