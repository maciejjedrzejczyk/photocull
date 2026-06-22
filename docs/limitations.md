# Known limitations

photocull is a triage tool, not an infallible judge. Always review before
deleting, and keep a backup.

- **Variance of the Laplacian conflates focus with contrast.** A correctly
  focused but very flat/dark/foggy image scores low and may be labelled
  "blurry". Read the `contrast`/`brightness` columns before trusting the label.
- **Noise can inflate sharpness**, so a grainy image won't be flagged as blurry
  — that's why noise is measured separately on a native-resolution crop.
- **The aesthetic score is subjective**; it is used as a *review* signal, not a
  hard delete trigger.
- **Near-duplicate clustering uses single-link grouping**, so too high a
  `--dedupe-threshold` causes chaining (distinct shots merged into one cluster).
  Keep it low and review large clusters. See [dedupe.md](dedupe.md).
- **Apple Photos specifics.** Photos doesn't expose per-asset file sizes through
  the public API, so the "reclaimable space" figure reads 0 there; and PhotoKit
  is single-process, so the Photos scan doesn't use the filesystem scanner's
  multi-process parallelism. See [photos.md](photos.md).
