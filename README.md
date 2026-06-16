# photocull

**Find and cull blurry, dark, low-quality and duplicate photos in a macOS
library — to reclaim disk space, safely.**

`photocull` scans a photo archive and flags **blurry, dark, dull, grainy or
otherwise low-quality** images, plus **near-duplicate/burst** frames, so you
can delete the junk and keep the keepers. It pairs fast classical image metrics
with Apple's on-device **Vision** framework — no model downloads, no network,
nothing leaves your Mac. HEIC/HEIF from an Apple photo library works out of the
box because decoding goes through ImageIO/Quartz.

> **Safety first:** `photocull` **never deletes anything.** It writes a CSV
> report and — only if you explicitly ask — *moves* flagged files into a review
> folder. You stay in control of the actual deletion.

## Features

- 🔍 **Blur / focus detection** — variance of the Laplacian, resolution-normalized.
- 🌑 **Exposure & tone** — darkness, low contrast, and black/white clipping.
- 🌫️ **Grain / noise estimate** — Immerkær σ measured on a native-resolution crop.
- 🍎 **Apple Vision quality signals** — aesthetics score, a utility flag for
  screenshots/receipts/documents, and per-face capture quality (best-shot for
  portraits).
- 👯 **Near-duplicate & burst clustering** (`--dedupe`) — Vision feature-print
  embeddings group near-identical shots; the best frame is kept, the rest flagged.
- 🛟 **Non-destructive** — CSV report + optional *move-to-quarantine*; never deletes.
- 🖥️ **Review gallery** (`./review`) — a local web app to browse findings,
  view full-size photos, select in bulk, and move rejects to the Trash.
- 🖼️ **Broad format support** — HEIC/HEIF, JPEG, PNG, TIFF, WebP, GIF, DNG.
- ⚡ **Built for big libraries** — multi-core, flat memory, and an on-disk cache
  that makes re-runs near-instant and lets you sweep thresholds with no rescan.
- 🔒 **100% on-device** — uses Apple Vision; no cloud, no downloads.

## Requirements

- macOS with Apple Vision (the aesthetics API needs macOS 15+).
- Python 3.9+.

Python dependencies (installed automatically by the `scan` launcher, or via
`requirements.txt`): `pyobjc-framework-Vision`, `pyobjc-framework-Quartz`, `numpy`.

## Quickstart

```bash
git clone https://github.com/maciejjedrzejczyk/photocull.git
cd photocull
./scan ~/Pictures -r
```

The `scan` launcher creates a local virtualenv and installs dependencies on the
first run, then writes `photo_quality_report.csv`. Open it in Numbers/Excel,
sort/filter, and decide what to delete.

Prefer to manage the environment yourself?

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python photo_quality.py ~/Pictures -r
```

## What it measures

Classical metrics (computed straight from pixels, fast and deterministic):

| Metric       | Meaning                                   | Flags when        |
|--------------|-------------------------------------------|-------------------|
| `sharpness`  | Variance of the Laplacian                 | low → **blurry**  |
| `brightness` | Mean luminance (0–255)                    | low → **dark**    |
| `contrast`   | Luminance standard deviation              | low → **dull**    |
| `noise`      | Immerkær σ estimate (native-res crop)     | high → **grainy** |
| `black/white_frac` | Fraction of clipped pixels          | high → **bad exposure** |

Apple Vision signals (on-device ML):

| Signal         | Source                                          |
|----------------|-------------------------------------------------|
| `aesthetic`    | `VNCalculateImageAestheticsScoresRequest.overallScore` (≈ −1…1) |
| `is_utility`   | Vision's flag for screenshots/receipts/documents vs. real photos |
| `face_quality` | `VNDetectFaceCaptureQualityRequest` — worst face capture quality in frame |
| feature print  | `VNGenerateImageFeaturePrintRequest` — a 768-dim embedding used for near-duplicate clustering (`--dedupe`) |

Each image gets a **recommendation**:

- **delete** — a strong, objective defect: clearly blurry, clearly dark, a
  washed-out/blurry combination, or a portrait Vision rates as a poor capture.
- **duplicate** — a near-duplicate/burst frame that isn't the best in its group
  (only with `--dedupe`). The best frame is kept; the rest are flagged.
- **review** — milder or subjective issues worth a human glance (noisy, low
  contrast, low aesthetic score, utility shot).
- **keep** — no flags.

## Usage

```bash
./scan ~/Pictures -r                         # scan a library, recursively
./scan ~/Pictures -r -o report.csv           # choose the report path
./scan photo.heic                            # inspect a single file
./scan ~/Pictures -r --no-vision             # classical metrics only (faster)
./scan ~/Pictures -r --dedupe                # also cluster near-duplicates/bursts

# Move flagged files out for review (NEVER deletes):
./scan ~/Pictures -r --quarantine ~/Pictures/_rejects --dry-run   # preview
./scan ~/Pictures -r --quarantine ~/Pictures/_rejects             # do it
./scan ~/Pictures -r --quarantine ~/Pictures/_rejects --include-review
```

The report (`photo_quality_report.csv` by default) has one row per image with
every metric, the recommendation, and a `;`-separated `reasons` list, sorted
worst-first. The console prints a summary including **how much disk space** the
`delete` (and `duplicate`) candidates would free.

### Suggested workflow for a big archive

1. `./scan ~/Pictures -r` and read the summary + CSV.
2. Spot-check a few `delete` rows (the CSV `path` column) to sanity-check the
   thresholds against *your* photos.
3. Tune thresholds if needed (see below), re-run (the cache makes this fast).
4. `--quarantine ... --dry-run` to preview the move, then run it for real.
5. Browse the quarantine folder in Finder and delete what you're happy to lose.

## Review gallery (`./review`)

Reading a CSV is fine for triage, but eyeballing the actual photos is faster.
`review` starts a small local web app that turns the report into an interactive
gallery:

```bash
./review photo_quality_report.csv                       # opens your browser
./review report.csv --port 8765 --quarantine ~/_rejects --root ~/Pictures
```

In the browser you can:

- **Filter by tier** (delete / duplicate / review / keep) and sort by any
  metric — or by `cluster`, which groups near-duplicates with the keeper marked.
- **Click any thumbnail** for a full-size view with all the metrics, and arrow
  through the set.
- **Select** photos individually or a whole page, see the total size selected,
  and **bulk-move them to the macOS Trash** (recoverable) or to a quarantine
  folder.

HEIC/HEIF are transcoded to JPEG on the fly so they display in any browser.
Photos are read straight from their original locations — nothing is copied.

**Safety & security.** The server binds to `127.0.0.1` only; it will *only*
serve or delete files that appear in the CSV (path whitelist); destructive
actions require a per-session token and a localhost `Host` header, so other
browser tabs or websites can't drive deletions; and "delete" moves files to the
**Trash** (recoverable) — there is no permanent-delete path.

## Options

| Option              | Description                                                  |
|---------------------|--------------------------------------------------------------|
| `-r, --recursive`   | Descend into subfolders.                                     |
| `-o, --output`      | CSV report path. Default `photo_quality_report.csv`.         |
| `--no-vision`       | Skip Apple Vision; classical metrics only (faster).          |
| `--no-faces`        | Skip per-face capture-quality (slightly faster).             |
| `-j, --workers`     | Parallel worker processes (default: cores−2, capped at 8). `1` = serial. |
| `--quarantine DIR`  | Move flagged files into `DIR` (preserving structure). Moves `delete` **and** `duplicate` tiers. |
| `--include-review`  | Quarantine the `review` tier too.                            |
| `--dry-run`         | With `--quarantine`, only print what would move.             |
| **Near-duplicates** |                                                              |
| `--dedupe`          | Cluster near-duplicate/burst photos and flag all but the best in each group. |
| `--dedupe-threshold`| Feature-print L2 distance cutoff (default `0.3`; lower = stricter/safer). |
| **Cache**           |                                                              |
| `--cache PATH`      | Result cache DB (default `.photo_quality_cache.sqlite`).     |
| `--no-cache`        | Disable the on-disk cache.                                   |
| **Thresholds**      |                                                              |
| `--blur`            | Sharpness below this is *blurry* (default `100`).            |
| `--blur-hard`       | Sharpness below this is *very blurry* (default `35`).        |
| `--dark`            | Mean luminance below this is *dark* (default `50`).          |
| `--dark-hard`       | Mean luminance below this is *very dark* (default `25`).     |
| `--contrast`        | Luminance std below this is *low contrast* (default `18`).   |
| `--noise`           | Noise σ above this is *noisy* (default `7`).                 |
| `--aesthetic`       | Vision score below this is *low aesthetic* (default `-0.10`).|

## Near-duplicate detection (`--dedupe`)

Bursts and near-identical re-shoots are usually the **biggest** reclaimable
category in a real library. With `--dedupe`, the tool computes an Apple Vision
**feature print** (a 768-dim embedding) for every image, clusters images whose
embeddings are within `--dedupe-threshold` (L2 distance), and within each
cluster keeps the single best frame — flagging the rest as `duplicate`.

The keeper is chosen by reusing the quality signals already computed:
**face capture quality** first (Apple's own best-shot metric — favours open
eyes / sharp faces), then **sharpness**, then aesthetics and resolution.

Clustering of tens of thousands of images takes only seconds: distances are
computed with blocked BLAS matrix products, not a Python loop.

### Choosing the threshold

This matters, and the safe direction is **lower**:

| Threshold | Behaviour                                                        |
|-----------|------------------------------------------------------------------|
| `0.2`     | Only near-identical frames (very strict).                        |
| `0.3`     | **Default.** Genuine bursts/re-shoots of the same moment.        |
| `0.4–0.5` | Starts merging *distinct* photos of the same scene/outing — risky for deletion. |

On a 1,293-photo test set, `0.5` chained an entire hiking outing (dozens of
different moments) into one 57-image "cluster", whereas `0.3` produced clean
bursts (e.g. nine consecutive frames at one signpost). Because a missed
duplicate is harmless but a false one deletes a unique photo, the default errs
strict. Verify visually before trusting large clusters — sort the CSV by
`cluster_id` and eyeball each group, or make a quick contact sheet.

The report adds `cluster_id`, `cluster_size` and `is_keeper` columns so you can
audit every group. `--quarantine` moves `duplicate` files along with `delete`.

## Cache

Every run stores its expensive results (decode + Vision metrics + feature
prints) in a small SQLite file, keyed on `(path, size, mtime)`. Re-runs reuse
unchanged files, so the second pass over a library is near-instant and an
interrupted scan effectively resumes.

Crucially, **thresholds and the dedupe distance are applied on top of the
cache**, not baked into it — so you can sweep `--blur`, `--dedupe-threshold`
etc. across many runs with no rescanning. Only files whose size or mtime
changed are recomputed. Use `--no-cache` to disable, or `--cache PATH` to place
the DB elsewhere (it's keyed by absolute path, so its location doesn't matter).

## Performance

Designed for large libraries (tens of thousands of images):

- **Constant memory / constant speed.** Each image is processed inside its own
  Objective-C autorelease pool, so the CGImages, bitmap contexts and Vision
  result objects are freed immediately instead of piling up. Memory stays flat
  for the whole run — without this, pyobjc leaks a few MB per image and the
  scan slows down badly over time.
- **Parallel by default.** Work is spread across worker *processes* (`-j`,
  default cores−2 capped at 8) to use all your CPU cores and sidestep the GIL.
  On a 10-core machine this is roughly 3–4× faster than serial. Use `-j1` for
  deterministic serial behaviour or to minimize load.
- `--no-vision` skips the Vision passes entirely for a fast, purely classical
  first pass.

The progress line on stderr shows throughput and an ETA:

```
  1500/34723  (95.4/s, eta 5.8 min)
```

## Tuning the thresholds

Sharpness is normalized to a 1024px longest edge so values are comparable
across images, but the *right* cutoff still depends on your content (macro and
fine-textured shots score high; smooth skies and portraits score lower even
when perfectly sharp). The defaults are conservative. The reliable way to tune:

1. Run once, open the CSV, sort by `sharpness`.
2. Eyeball where genuinely blurry shots end and good ones begin.
3. Set `--blur` / `--blur-hard` accordingly.

## Limitations

- **Variance of the Laplacian conflates focus with contrast.** A correctly
  focused but very flat/dark/foggy image scores low and may be labelled
  "blurry". In practice such images are usually low quality anyway and land in
  `delete`/`review`, but read the other columns (`contrast`, `brightness`)
  before trusting the blur label.
- **Noise can inflate sharpness.** A grainy image has high Laplacian variance,
  so it won't be flagged as blurry — that's why noise is measured separately on
  a native-resolution crop.
- The Vision aesthetic score is subjective; it's used as a *review* signal, not
  a hard delete trigger.
- **Near-duplicate clustering uses single-link grouping**, so too high a
  `--dedupe-threshold` causes *chaining*: A≈B and B≈C pulls A and C into one
  cluster even if they differ. Keep the threshold low and review large clusters.
- This is a triage tool to surface candidates, not an infallible judge. Always
  review before deleting, and keep a backup.

## License

Released under the [MIT License](LICENSE). © 2026 maciejjedrzejczyk.
