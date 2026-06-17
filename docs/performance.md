# Performance

Designed for large libraries (tens of thousands of images):

- **Constant memory / constant speed.** Each image is processed inside its own
  Objective-C autorelease pool, so the CGImages, bitmap contexts and Vision
  result objects are freed immediately instead of piling up. Memory stays flat
  for the whole run — without this, pyobjc leaks a few MB per image and the
  scan slows down badly over time.
- **Parallel by default.** Work is spread across worker *processes* (`-j`) to
  use all your CPU cores and sidestep the GIL. On a 10-core machine this is
  roughly 3-4x faster than serial. Use `-j1` for deterministic serial behaviour
  or to minimize load.
- **`--no-vision`** skips the Vision passes entirely for a fast, purely
  classical first pass.
- The **[cache](cache.md)** makes re-runs near-instant.

The progress line on stderr shows throughput and an ETA:

```
  1500/34723  (95.4/s, eta 5.8 min)
```

## How many workers?

By default the worker count is **auto-detected from your host**:

- On Apple Silicon it uses your **performance-core count** (queried via
  `sysctl hw.perflevel0.logicalcpu`), ignoring the slower efficiency cores. On
  other Macs it falls back to logical cores minus two.
- With Vision enabled the value is **capped at 8**, because the aesthetics and
  feature-print passes share the Neural Engine / GPU and stop scaling past
  roughly 6-8 workers no matter how many cores you have. A pure-classical
  (`--no-vision`) run may use all performance cores.

RAM is not the limiter — each worker holds flat at a few hundred MB. The run's
first log line shows the chosen count, e.g. `... with 8 worker(s) (auto)`.

Override it with `-j N` when you want to: drop to `-j6` to keep the machine
responsive while it runs, or `-j1` for a single-threaded, fully deterministic
pass. A live benchmark to find the exact optimum isn't worth it — the optimum
also depends on your storage speed and image sizes, and the heuristic captures
nearly all of the available speedup.
