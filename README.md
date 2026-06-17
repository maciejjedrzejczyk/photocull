# photocull

**Find and cull blurry, dark, low-quality and duplicate photos in a macOS
library — to reclaim disk space, safely.**

## Overview

`photocull` scans a photo archive and flags **blurry, dark, dull, grainy or
otherwise low-quality** images, plus **near-duplicate/burst** frames, then
writes a CSV report sorted worst-first. It pairs fast classical image metrics
(sharpness, exposure, contrast, noise) with Apple's on-device **Vision**
framework (aesthetics, screenshot/document detection, face capture quality, and
feature-print embeddings for duplicate detection). Everything runs locally — no
model downloads, no network, nothing leaves your Mac. HEIC/HEIF from an Apple
photo library works out of the box.

A companion **review gallery** (`./review`) turns the report into a local web
app so you can eyeball the findings and bulk-cull from the browser.

> **Safety first.** photocull **never deletes anything.** It writes a CSV report
> and — only if you ask — *moves* flagged files to a quarantine folder or the
> macOS Trash (recoverable). You stay in control of the actual deletion.

## Requirements

- macOS with Apple Vision (the aesthetics API needs macOS 15+).
- Python 3.9+.

Dependencies (`pyobjc-framework-Vision`, `pyobjc-framework-Quartz`, `numpy`) are
installed automatically by the `scan`/`review` launchers on first run, or via
`pip install -r requirements.txt`.

## Quickstart

```bash
git clone https://github.com/maciejjedrzejczyk/photocull.git
cd photocull
```

The `scan` launcher creates a local virtualenv and installs dependencies on the
first run.

### (a) Scan a library for low-quality photos

```bash
./scan ~/Pictures -r                                  # recursive scan -> photo_quality_report.csv
./scan ~/Pictures -r -o report.csv                    # choose the report path
./scan ~/Pictures -r --no-vision                      # classical metrics only (faster)
./scan ~/Pictures -r --quarantine ~/_rejects --dry-run  # preview moving flagged files
./scan ~/Pictures -r --quarantine ~/_rejects          # move them (never deletes)
```

Common options: `-j N` (workers), `--blur`, `--dark`, `--noise`, etc.
Full list in [docs/options.md](docs/options.md).

### (b) Scan and also detect near-duplicates / bursts

```bash
./scan ~/Pictures -r --dedupe                         # cluster bursts, keep the best of each
./scan ~/Pictures -r --dedupe --dedupe-threshold 0.25 # stricter (safer) clustering
```

`--dedupe` adds a `duplicate` tier and `cluster_id`/`is_keeper` columns. The
default threshold is conservative on purpose. See [docs/dedupe.md](docs/dedupe.md).

### (c) Review the results in a browser

```bash
./review photo_quality_report.csv                     # opens a local gallery
./review report.csv --quarantine ~/_rejects --root ~/Pictures
```

Browse thumbnails, filter by tier, sort by metric or `cluster`, click for a
full-size view, then bulk-move rejects to the Trash or a quarantine folder.
Details and the security model in [docs/review.md](docs/review.md).

## Known limitations

- **Variance of the Laplacian conflates focus with contrast.** A correctly
  focused but very flat/dark/foggy image scores low and may be labelled
  "blurry". Read the `contrast`/`brightness` columns before trusting the label.
- **Noise can inflate sharpness**, so a grainy image won't be flagged as blurry
  — that's why noise is measured separately on a native-resolution crop.
- **The aesthetic score is subjective**; it is used as a *review* signal, not a
  hard delete trigger.
- **Near-duplicate clustering uses single-link grouping**, so too high a
  `--dedupe-threshold` causes chaining (distinct shots merged into one cluster).
  Keep it low and review large clusters.
- This is a triage tool, not an infallible judge. Always review before deleting,
  and keep a backup.

## Documentation

- [docs/metrics.md](docs/metrics.md) — what photocull measures and how to tune thresholds.
- [docs/options.md](docs/options.md) — full command-line reference.
- [docs/dedupe.md](docs/dedupe.md) — near-duplicate detection in depth.
- [docs/review.md](docs/review.md) — the review gallery and its security model.
- [docs/cache.md](docs/cache.md) — the on-disk result cache.
- [docs/performance.md](docs/performance.md) — performance and worker tuning.

## License

Released under the [MIT License](LICENSE). (c) 2026 maciejjedrzejczyk.
