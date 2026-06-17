# Performance

Designed for large libraries (tens of thousands of images):

- **Constant memory / constant speed.** Each image is processed inside its own
  Objective-C autorelease pool, so the CGImages, bitmap contexts and Vision
  result objects are freed immediately instead of piling up. Memory stays flat
  for the whole run — without this, pyobjc leaks a few MB per image and the
  scan slows down badly over time.
- **Parallel by default.** Work is spread across worker *processes* (`-j`,
  default cores-2 capped at 8) to use all your CPU cores and sidestep the GIL.
  On a 10-core machine this is roughly 3-4x faster than serial. Use `-j1` for
  deterministic serial behaviour or to minimize load.
- **`--no-vision`** skips the Vision passes entirely for a fast, purely
  classical first pass.
- The **[cache](cache.md)** makes re-runs near-instant.

The progress line on stderr shows throughput and an ETA:

```
  1500/34723  (95.4/s, eta 5.8 min)
```

## How many workers?

Cap at your machine's performance-core count. Apple Vision runs on a shared
Neural Engine / GPU, so beyond roughly 6-8 workers the Vision passes stop
speeding up even if you have more cores. RAM is not the limiter — each worker
holds flat at a few hundred MB. The default (cores-2, capped at 8) is a good
choice for an unattended sweep; drop to `-j6` if you want the machine
responsive while it runs.
