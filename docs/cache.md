# Cache

Every run stores its expensive results (decode + Vision metrics + feature
prints) in a small SQLite file, keyed on `(path, size, mtime)`. Re-runs reuse
unchanged files, so the second pass over a library is near-instant and an
interrupted scan effectively resumes.

Crucially, **thresholds and the dedupe distance are applied on top of the
cache**, not baked into it — so you can sweep `--blur`, `--dedupe-threshold`
etc. across many runs with no rescanning. Only files whose size or mtime
changed are recomputed.

- `--cache PATH` — place the DB elsewhere (it's keyed by absolute path, so its
  location doesn't matter). Default: `.photo_quality_cache.sqlite`.
- `--no-cache` — disable the cache entirely.
