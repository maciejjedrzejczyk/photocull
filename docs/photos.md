# Apple Photos source

photocull can analyse your **Apple Photos library** directly (via PhotoKit)
instead of a folder of files. It scans a chosen subset, runs the same quality +
near-duplicate analysis, writes the same CSV report, and then gathers the
candidates into **review albums inside Photos** — so you browse and delete them
in Photos.app yourself.

> **Safety.** This never deletes or moves photos. It only *adds* flagged photos
> to `photocull` review albums. Removing a photo from an album, or deleting the
> album, never deletes the photo. **Favourites are excluded by default.**

## Why it needs an app bundle

macOS only grants Photos access to a **code-signed `.app` bundle** that
declares an `NSPhotoLibraryUsageDescription`. A plain script run from Terminal
is denied instantly and never even appears in System Settings → Privacy →
Photos. So the Photos source ships as a small app you build locally.

## Build and run

```bash
./build_photos_app.sh                 # creates dist/PhotoCull.app
```

Then:

1. **Move `PhotoCull.app` out of Downloads/Desktop/Documents** (e.g. to
   `/Applications`). Those folders are themselves protected, and an app can't
   even read its own bundle from inside them.
2. Double-click it. On first launch, click **Allow** on the Photos prompt.
3. It scans, writes `~/.photocull/report.csv`, opens a run log, and creates the
   review albums in Photos.app.

The app is self-contained (it embeds its own Python and dependencies) and reads
nothing from your protected folders — it only needs the Photos permission.

## Choosing what to scan

Edit `~/.photocull/photos.args` (created on first run) — one option per line:

```
--smart-album recently-added
--limit 1000
--dedupe
```

Subset options:

| Option | Scans |
|---|---|
| `--album "NAME"` | a single user album by name |
| `--smart-album NAME` | a built-in smart album: `recently-added`, `screenshots`, `selfies`, `bursts`, `favorites`, `panoramas` |
| `--since YYYY-MM-DD` / `--until YYYY-MM-DD` | a creation-date range |
| `--include-favorites` | include favourites (excluded by default) |
| `--limit N` | cap the number of photos |

Analysis options mirror the filesystem scanner: `--dedupe`,
`--dedupe-threshold`, `--no-faces`, and the thresholds (`--blur`, `--dark`, …).
See [options.md](options.md) and [metrics.md](metrics.md).

iCloud: cloud-only originals are downloaded on demand for analysis. Use
`--no-download` to skip (and just flag) assets that aren't stored locally — and
scope the scan (album / date / `--limit`) to keep downloads in check.

## The review albums

After a scan, photocull creates up to three albums for that **session**. The
title is built from a template you control; by default it's timestamped so
every run is distinct:

```
photocull 2026-06-17-15-00 Delete candidates
photocull 2026-06-17-15-00 Duplicates
photocull 2026-06-17-15-00 Review
```

(Empty tiers are skipped.) Open them in Photos.app, browse full-screen (they
sync to your other devices too), and delete what you agree with. Each run
produces a **new** session's albums; previous sessions are left in place.

You fully control the naming — only the tier label is fixed:

- `--album-template` — title pattern with placeholders `{prefix}`, `{date}`,
  `{tier}`. `{tier}` is required and is always one of *Delete candidates /
  Duplicates / Review*. Default: `{prefix} {date} {tier}`.
  e.g. `--album-template "{prefix}-{date}-{tier}"` →
  `photocull-2026-06-17-15-00-Delete candidates`.
- `--album-date-format` — strftime format for `{date}` (default
  `%Y-%m-%d-%H-%M`).
- `--album-prefix NAME` — value for `{prefix}` (default `photocull`).
- `--replace-albums` — delete earlier albums whose title starts with the prefix
  first (removes albums only, never photos).
- `--no-albums` — produce just the CSV. `--dry-run` — report without touching
  Photos.

## Limitations specific to the Photos source

- **No byte sizes.** Photos doesn't expose per-asset file sizes through the
  public API, so the "reclaimable space" figure isn't available here (it reads
  0 B). The recommendations and albums are unaffected.
- **Single process.** PhotoKit is tied to one process, so the Photos scan
  doesn't use the multi-process parallelism of the filesystem scanner; Vision
  is the bottleneck either way.
- Distribution-grade signing/notarization isn't included — the build uses an
  ad-hoc signature, which is fine for running it on your own Mac.
