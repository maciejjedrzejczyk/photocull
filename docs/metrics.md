# What photocull measures

For every image, photocull computes a set of quality metrics plus (optionally)
Apple Vision signals, then turns them into a single recommendation.

## Classical metrics

Computed straight from pixels — fast and deterministic.

| Metric       | Meaning                                   | Flags when        |
|--------------|-------------------------------------------|-------------------|
| `sharpness`  | Variance of the Laplacian                 | low -> **blurry**  |
| `brightness` | Mean luminance (0-255)                    | low -> **dark**    |
| `contrast`   | Luminance standard deviation              | low -> **dull**    |
| `noise`      | Immerkaer sigma estimate (native-res crop)| high -> **grainy** |
| `black/white_frac` | Fraction of clipped pixels          | high -> **bad exposure** |

## Detection signals

Every check above is exposed as a named **signal** you can switch on or off, so
you can target just the kinds of photos you care about (e.g. only find blurry
shots, or everything *except* the subjective aesthetic score).

| Signal | Album label | Flags (reasons) | Needs Vision |
|--------|-------------|-----------------|--------------|
| `blur` | Blurry | `blurry`, `very_blurry` | no |
| `dark` | Too dark | `dark`, `very_dark` | no |
| `contrast` | Low contrast | `low_contrast` | no |
| `noise` | Noisy | `noisy` | no |
| `exposure` | Bad exposure | `bad_exposure` | no |
| `aesthetic` | Low aesthetic | `low_aesthetic` | yes |
| `utility` | Utility/screenshot | `utility` | yes |
| `face` | Poor face capture | `low_face_quality` | yes |

Select signals with:

- `--signals LIST` — restrict detection to these (comma-separated). Default: all.
- `--exclude-signals LIST` — disable these (applied after `--signals`).

```bash
./scan ~/Pictures -r --signals blur,dark          # only blurry / dark photos
./scan ~/Pictures -r --exclude-signals aesthetic  # everything but the subjective score
```

A disabled signal never contributes a reason **or** weight to the verdict, so
e.g. `--signals dark` will not let a low sharpness score push a photo into the
`delete` tier. Signals marked *Needs Vision* are inert under `--no-vision`
(you'll get a note). Disabling `face` also skips the per-face Vision pass, like
`--no-faces`.

> Both the filesystem scanner and the Apple Photos source share the same signal
> set. In the Photos source you can additionally split the review albums by
> signal — see [photos.md](photos.md#the-review-albums).

## Apple Vision signals (on-device ML)

| Signal         | Source                                          |
|----------------|-------------------------------------------------|
| `aesthetic`    | `VNCalculateImageAestheticsScoresRequest.overallScore` (about -1..1) |
| `is_utility`   | Vision's flag for screenshots/receipts/documents vs. real photos |
| `face_quality` | `VNDetectFaceCaptureQualityRequest` - worst face capture quality in frame |
| feature print  | `VNGenerateImageFeaturePrintRequest` - a 768-dim embedding used for near-duplicate clustering (`--dedupe`) |

## The recommendation

Each image gets one of:

- **delete** - a strong, objective defect: clearly blurry, clearly dark, a
  washed-out/blurry combination, or a portrait Vision rates as a poor capture.
- **duplicate** - a near-duplicate/burst frame that isn't the best in its group
  (only with `--dedupe`). The best frame is kept; the rest are flagged.
- **review** - milder or subjective issues worth a human glance (noisy, low
  contrast, low aesthetic score, utility shot).
- **keep** - no flags.

The CSV report has one row per image with every metric, the recommendation, and
a `;`-separated `reasons` list, sorted worst-first.

## Tuning the thresholds

Sharpness is normalized to a 1024px longest edge so values are comparable
across images, but the *right* cutoff still depends on your content (macro and
fine-textured shots score high; smooth skies and portraits score lower even
when perfectly sharp). The defaults are conservative. The reliable way to tune:

1. Run once, open the CSV, sort by `sharpness`.
2. Eyeball where genuinely blurry shots end and good ones begin.
3. Set `--blur` / `--blur-hard` accordingly and re-run (the cache makes this
   fast — see [cache.md](cache.md)).

See [options.md](options.md) for every threshold flag and its default.
