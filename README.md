# photocull

Find and cull blurry, dark, low-quality and duplicate photos to reclaim disk
space on macOS. **photocull never deletes anything** — it reports, and only
*moves* files to the Trash/quarantine or gathers candidates into Photos albums
you delete yourself. Everything runs locally; nothing leaves your Mac.

## Features

- **Quality detection** — flags blurry, dark, dull, grainy and badly exposed
  shots using fast classical metrics plus Apple's on-device **Vision**
  (aesthetics, screenshot/document detection, face capture quality).
- **Near-duplicate / burst grouping** — clusters similar frames and keeps the
  best one (`--dedupe`).
- **Toggleable signals** — pick exactly what to flag with `--signals` /
  `--exclude-signals`: `blur dark contrast noise exposure aesthetic utility face`.
- **One worst-first report** — a CSV with a `delete` / `duplicate` / `review` /
  `keep` tier per photo, plus an on-disk cache so re-runs are near-instant.
- **Fast triage** — a keyboard-friendly local web gallery for files, or native
  review albums for Apple Photos.
- HEIC/HEIF supported out of the box.

**Requirements:** macOS (Vision aesthetics needs macOS 15+) and Python 3.9+.

```bash
git clone https://github.com/maciejjedrzejczyk/photocull.git
cd photocull
```

## Which photos do you want to scan?

The same engine, signals and report work either way — only the source and the
review surface differ.

### → Local files

```bash
./scan ~/Pictures -r                       # scan a folder -> photo_quality_report.csv
./scan ~/Pictures -r --dedupe              # also group near-duplicates
./scan ~/Pictures -r --signals blur,dark   # only flag certain kinds
```

**Review:** open the report in a local web gallery, mark candidates, then commit
the marked set to the Trash or a quarantine folder (recoverable, with undo):

```bash
./review photo_quality_report.csv
```

### → Apple Photos library

Photos access needs a signed app bundle that embeds the same engine:

```bash
./build_photos_app.sh        # build PhotoCull.app (one time)
# move it to /Applications, double-click, click Allow on the Photos prompt
```

On launch it asks for a **working folder**. Edit `photos.args` there to choose
what to scan and how (same detection flags as `./scan`, plus a subset selector):

```
--smart-album recently-added
--signals blur,dark,contrast,noise,exposure,aesthetic,utility,face
--dedupe
```

**Review:** each run gathers candidates into worst-first **albums inside
Photos.app** (`--albums-by signal` for one album per defect), which you browse
full-resolution and delete with native shortcuts.

## Documentation

- [docs/metrics.md](docs/metrics.md) — what photocull measures, the detection signals, and tuning.
- [docs/options.md](docs/options.md) — full command-line reference.
- [docs/dedupe.md](docs/dedupe.md) — near-duplicate detection in depth.
- [docs/review.md](docs/review.md) — the review gallery and its security model.
- [docs/photos.md](docs/photos.md) — analysing the Apple Photos library directly.
- [docs/cache.md](docs/cache.md) — the on-disk result cache.
- [docs/performance.md](docs/performance.md) — performance and worker tuning.
- [docs/limitations.md](docs/limitations.md) — known limitations and caveats.

## License

Released under the [MIT License](LICENSE). (c) 2026 maciejjedrzejczyk.
