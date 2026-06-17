# Options reference

Full command-line reference for the scanner (`./scan` / `photo_quality.py`).

| Option              | Description                                                  |
|---------------------|--------------------------------------------------------------|
| `-r, --recursive`   | Descend into subfolders.                                     |
| `-o, --output`      | CSV report path. Default `photo_quality_report.csv`.         |
| `--no-vision`       | Skip Apple Vision; classical metrics only (faster).          |
| `--no-faces`        | Skip per-face capture-quality (slightly faster).             |
| `-j, --workers`     | Parallel worker processes (default: cores-2, capped at 8). `1` = serial. |
| `--quarantine DIR`  | Move flagged files into `DIR` (preserving structure). Moves `delete` **and** `duplicate` tiers. |
| `--include-review`  | Quarantine the `review` tier too.                            |
| `--dry-run`         | With `--quarantine`, only print what would move.             |
| **Near-duplicates** |                                                              |
| `--dedupe`          | Cluster near-duplicate/burst photos and flag all but the best in each group. See [dedupe.md](dedupe.md). |
| `--dedupe-threshold`| Feature-print L2 distance cutoff (default `0.3`; lower = stricter/safer). |
| **Cache**           |                                                              |
| `--cache PATH`      | Result cache DB (default `.photo_quality_cache.sqlite`). See [cache.md](cache.md). |
| `--no-cache`        | Disable the on-disk cache.                                   |
| **Thresholds**      | See [metrics.md](metrics.md) for how to tune these.          |
| `--blur`            | Sharpness below this is *blurry* (default `100`).            |
| `--blur-hard`       | Sharpness below this is *very blurry* (default `35`).        |
| `--dark`            | Mean luminance below this is *dark* (default `50`).          |
| `--dark-hard`       | Mean luminance below this is *very dark* (default `25`).     |
| `--contrast`        | Luminance std below this is *low contrast* (default `18`).   |
| `--noise`           | Noise sigma above this is *noisy* (default `7`).             |
| `--aesthetic`       | Vision score below this is *low aesthetic* (default `-0.10`).|

The review server (`./review` / `review.py`) has its own options — see
[review.md](review.md).
