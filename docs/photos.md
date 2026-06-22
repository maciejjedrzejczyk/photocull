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
3. On each launch it asks you to **choose a working folder** (where it writes
   `photos.args`, `report.csv`, the cache and `run.log`). It scans, opens the
   run log, and creates the review albums in Photos.app.

The app is self-contained (it embeds its own Python and dependencies) and reads
nothing from your protected folders — it only needs the Photos permission.

## Choosing the working folder

On launch the app asks you to pick the folder that holds its config, report and
cache. The folder is resolved in this order:

1. **A launch argument** — `open -a PhotoCull --args /path/to/folder` (no prompt).
2. **The `PHOTOCULL_HOME` environment variable** — e.g.
   `PHOTOCULL_HOME=~/PhotoCull open -a PhotoCull` (no prompt).
3. **An interactive folder picker** shown on launch (the default for a
   double-click). It opens at the folder you chose last time.
4. **`~/.photocull`** — the historical default, used if you cancel the picker or
   run headless.

The last choice is remembered (in `~/Library/Application Support/PhotoCull`) so
the picker reopens there next time. Nothing in the chosen folder is protected by
macOS, so prefer a normal folder (e.g. `~/PhotoCull`) over Documents/Desktop.

## Choosing what to scan

Edit `photos.args` **inside your working folder** (created on first run) — one
option per line:

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
`--dedupe-threshold`, `--no-faces`, the thresholds (`--blur`, `--dark`, …,
`--face`), and the signal selectors `--signals` / `--exclude-signals` (choose
which kinds of photos to flag — see [metrics.md](metrics.md#detection-signals)).
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

> **Deleting inside Photos — mind the keystroke.** With a photo selected in an
> album, **`⌘⌫` (Cmd-Delete)** deletes it to *Recently Deleted* (recoverable
> for 30 days). Plain **`⌫` (Delete)** only *removes it from the album* and
> leaves the photo in your library. Deleting a photocull album itself never
> deletes any photos. photocull prints this reminder in the run log too.

### Granular albums, one per signal

By default albums are grouped by recommendation **tier** (above). Pass
`--albums-by signal` to instead get **one album per detection signal**, so each
kind of defect lands in its own album:

```
photocull 2026-06-17-15-00 Blurry
photocull 2026-06-17-15-00 Too dark
photocull 2026-06-17-15-00 Noisy
photocull 2026-06-17-15-00 Bad exposure
photocull 2026-06-17-15-00 Low aesthetic
photocull 2026-06-17-15-00 Utility/screenshot
photocull 2026-06-17-15-00 Poor face capture
photocull 2026-06-17-15-00 Duplicates
```

- Only the signals you actually enabled (see `--signals` /
  `--exclude-signals`) get an album; empty ones are skipped.
- A photo appears in **every** album whose signal it triggered (a shot that's
  both blurry and dark shows up in *Blurry* and *Too dark*).
- Near-duplicates keep their own *Duplicates* album (they aren't a quality
  signal).

Combine the two for focused cleanup, e.g. only blurry and too-dark albums:

```
--albums-by signal
--signals blur,dark
```

| Option | Albums created |
|---|---|
| `--albums-by tier` (default) | Delete candidates / Duplicates / Review |
| `--albums-by signal` | one per enabled signal (Blurry, Too dark, …) + Duplicates |

The album-naming options below apply to both modes — in signal mode the
`{tier}` placeholder holds the signal label instead of the tier label.

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
