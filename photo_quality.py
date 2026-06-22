#!/usr/bin/env python3
"""
photo_quality - scan a photo archive and flag low-quality images for cleanup.

For every image it computes a set of quality metrics and an overall
recommendation (keep / review / delete) so you can reclaim disk space by
deleting blurry, dark, dull or junk photos.

Signals
-------
Classical (fast, deterministic, computed from pixels):
  * sharpness  - variance of the Laplacian. Low = blurry / out of focus.
  * brightness - mean luminance (0-255). Low = too dark.
  * contrast   - luminance standard deviation. Low = flat / dull / foggy.
  * noise      - Immerkaer sigma estimate on a native-res crop. High = grainy.
  * clipping   - fraction of pure-black / pure-white pixels (bad exposure).

Apple Vision (on-device ML, no network):
  * aesthetics - VNCalculateImageAestheticsScoresRequest.overallScore (~-1..1).
  * is_utility - Vision's flag for "utility" shots (screenshots, receipts,
                 documents) rather than memorable photos.
  * face_quality - VNDetectFaceCaptureQualityRequest, the worst face capture
                 quality in the frame (low = blurry/badly captured portrait).

Decoding goes through Quartz/ImageIO (CGImageSource), so HEIC/HEIF from an
Apple photo library is supported alongside JPEG/PNG/TIFF/etc.

SAFETY: this tool never deletes anything. It writes a CSV report and, only if
you ask with --quarantine, *moves* flagged files into a folder for you to
review and delete yourself.

Usage
-----
    python photo_quality.py ~/Pictures -r
    python photo_quality.py ~/Pictures -r -o report.csv
    python photo_quality.py ~/Pictures -r --quarantine ~/Pictures/_rejects
    python photo_quality.py photo.heic            # inspect a single file
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, asdict
from functools import partial
from pathlib import Path

import numpy as np

import objc
import Quartz
import Vision
from Foundation import NSURL


# Image formats we scan for inside directories. Explicitly-named files are
# processed regardless of extension.
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff",
    ".webp", ".heic", ".heif", ".dng",
}

# Resolution we normalize to before measuring sharpness/brightness/contrast so
# the numbers are comparable across images of different sizes.
ANALYSIS_LONGEST_EDGE = 1024

# Size of the native-resolution centre crop used for the noise estimate
# (downscaling destroys grain, so noise must be measured at full res).
NOISE_CROP = 768


class DecodeError(RuntimeError):
    pass


# Bump when the meaning of any cached metric/feature-print changes, so stale
# cache rows are recomputed automatically.
METRICS_VERSION = 1


# --------------------------------------------------------------------------
# Default thresholds. All overridable from the CLI; see README for tuning.
# --------------------------------------------------------------------------
@dataclass
class Thresholds:
    blur: float = 100.0          # sharpness below this -> "blurry"
    blur_hard: float = 35.0      # sharpness below this -> "very blurry"
    dark: float = 50.0           # mean luminance below this -> "dark"
    dark_hard: float = 25.0      # mean luminance below this -> "very dark"
    contrast: float = 18.0       # luminance std below this -> "low_contrast"
    noise: float = 7.0           # noise sigma above this -> "noisy"
    aesthetic: float = -0.10     # Vision overallScore below this -> "low_aesthetic"
    clip: float = 0.55           # >55% of pixels black or white -> bad exposure
    face: float = 0.30           # face capture quality below this -> "low_face_quality"


# --------------------------------------------------------------------------
# Detection signals. Each named signal maps to one quality check in decide()
# and the reason string(s) it can emit. Users select which signals to run from
# the CLI (--signals / --exclude-signals) to control *what kinds of photos* get
# flagged. To expand detection, add a new Signal here and a matching check in
# decide(); everything else (CLI, album grouping, docs lookups) derives from
# this registry.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Signal:
    key: str            # CLI token, e.g. "blur"
    label: str          # human label (used for album names), e.g. "Blurry"
    reasons: tuple      # reason string(s) this signal may emit
    needs_vision: bool  # True if it depends on an Apple Vision metric


SIGNALS: "dict[str, Signal]" = {
    "blur":      Signal("blur",      "Blurry",             ("blurry", "very_blurry"), False),
    "dark":      Signal("dark",      "Too dark",           ("dark", "very_dark"),     False),
    "contrast":  Signal("contrast",  "Low contrast",       ("low_contrast",),         False),
    "noise":     Signal("noise",     "Noisy",              ("noisy",),                False),
    "exposure":  Signal("exposure",  "Bad exposure",       ("bad_exposure",),         False),
    "aesthetic": Signal("aesthetic", "Low aesthetic",      ("low_aesthetic",),        True),
    "utility":   Signal("utility",   "Utility/screenshot", ("utility",),              True),
    "face":      Signal("face",      "Poor face capture",  ("low_face_quality",),     True),
}

# Reason string -> signal key (reverse lookup for album grouping etc.).
REASON_TO_SIGNAL: "dict[str, str]" = {
    r: key for key, sig in SIGNALS.items() for r in sig.reasons
}


def resolve_signals(include: "str | None", exclude: "str | None") -> "set[str]":
    """Turn the --signals / --exclude-signals CLI strings into an enabled set.

    `include` (comma-separated) restricts detection to those signals; when it
    is omitted, every known signal starts enabled. `exclude` (comma-separated)
    then removes signals from the set. Unknown names raise ValueError listing
    the valid tokens.
    """
    known = set(SIGNALS)

    def parse(s: "str | None") -> "list[str]":
        return [tok.strip() for tok in (s or "").split(",") if tok.strip()]

    def check(names: "list[str]") -> None:
        bad = [n for n in names if n not in known]
        if bad:
            raise ValueError(
                f"unknown signal(s): {', '.join(bad)}; "
                f"valid signals are: {', '.join(SIGNALS)}")

    inc = parse(include)
    check(inc)
    enabled = set(inc) if inc else set(known)

    exc = parse(exclude)
    check(exc)
    enabled -= set(exc)
    return enabled


def add_signal_cli(group, d: "Thresholds") -> None:
    """Register the shared signal-selection options on an argparse group.

    Used by both the filesystem scanner and the Apple Photos source so the two
    CLIs stay in lockstep.
    """
    group.add_argument(
        "--signals", metavar="LIST",
        help="comma-separated detection signals to USE (default: all). "
             "Available: " + ", ".join(SIGNALS))
    group.add_argument(
        "--exclude-signals", metavar="LIST",
        help="comma-separated detection signals to DISABLE (applied after "
             "--signals)")
    group.add_argument(
        "--face", type=float, default=d.face,
        help=f"face capture quality below this is a poor portrait "
             f"(default {d.face}); part of the 'face' signal")


@dataclass
class Metrics:
    path: str
    asset_id: str = ""
    width: int = 0
    height: int = 0
    megapixels: float = 0.0
    file_bytes: int = 0
    sharpness: float = 0.0
    brightness: float = 0.0
    contrast: float = 0.0
    noise: float = 0.0
    black_frac: float = 0.0
    white_frac: float = 0.0
    aesthetic: float | None = None
    is_utility: bool | None = None
    face_quality: float | None = None
    recommendation: str = "keep"
    reasons: list[str] = field(default_factory=list)
    error: str = ""
    # Near-duplicate clustering (filled in by --dedupe).
    cluster_id: int = -1
    cluster_size: int = 1
    is_keeper: bool = True
    # Raw Vision feature-print as float32 bytes (768 dims). Not written to CSV;
    # cached and used for near-duplicate clustering. None unless --dedupe.
    fprint: bytes | None = None


# --------------------------------------------------------------------------
# Pixel decoding (Quartz / ImageIO -> grayscale numpy)
# --------------------------------------------------------------------------
def _load_cgimage(path: str):
    url = NSURL.fileURLWithPath_(path)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        raise DecodeError("could not open image")
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if cg is None:
        raise DecodeError("could not decode image")
    return cg


def _cg_to_gray(cg, longest_edge: int | None) -> np.ndarray:
    """Render a CGImage into a grayscale float64 numpy array.

    If longest_edge is given the image is scaled so its longer side equals it
    (never upscaled). Returns an (h, w) array of luminance values 0-255.
    """
    W = Quartz.CGImageGetWidth(cg)
    H = Quartz.CGImageGetHeight(cg)
    if longest_edge:
        scale = min(1.0, float(longest_edge) / max(W, H))
    else:
        scale = 1.0
    w = max(1, int(round(W * scale)))
    h = max(1, int(round(H * scale)))

    cs = Quartz.CGColorSpaceCreateDeviceGray()
    ctx = Quartz.CGBitmapContextCreate(
        None, w, h, 8, w, cs, Quartz.kCGImageAlphaNone
    )
    if ctx is None:
        raise DecodeError("could not create bitmap context")
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationHigh)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), cg)

    data = Quartz.CGBitmapContextGetData(ctx)
    if data is None:
        raise DecodeError("could not read bitmap data")
    # CGBitmapContextGetData returns an objc.varlist; .as_buffer gives a
    # readable buffer. astype() copies the data out so it survives ctx release.
    arr = np.frombuffer(data.as_buffer(w * h), dtype=np.uint8)
    return arr.reshape(h, w).astype(np.float64)


def _native_centre_crop_gray(cg, crop: int) -> np.ndarray:
    """Grayscale array of a native-resolution centre crop (for noise)."""
    W = Quartz.CGImageGetWidth(cg)
    H = Quartz.CGImageGetHeight(cg)
    cw = min(crop, W)
    ch = min(crop, H)
    x = (W - cw) // 2
    y = (H - ch) // 2
    rect = Quartz.CGRectMake(x, y, cw, ch)
    sub = Quartz.CGImageCreateWithImageInRect(cg, rect)
    if sub is None:
        return _cg_to_gray(cg, None)
    return _cg_to_gray(sub, None)


# --------------------------------------------------------------------------
# Classical metrics
# --------------------------------------------------------------------------
def _laplacian_variance(g: np.ndarray) -> float:
    """Variance of the 4-neighbour Laplacian. Low value => blurry."""
    lap = (
        -4.0 * g
        + np.roll(g, 1, 0) + np.roll(g, -1, 0)
        + np.roll(g, 1, 1) + np.roll(g, -1, 1)
    )[1:-1, 1:-1]
    return float(lap.var())


def _noise_sigma(g: np.ndarray) -> float:
    """Immerkaer fast Gaussian-noise standard-deviation estimate (0-255)."""
    if g.shape[0] < 3 or g.shape[1] < 3:
        return 0.0
    m = (
        4.0 * g
        - 2.0 * (np.roll(g, 1, 0) + np.roll(g, -1, 0)
                 + np.roll(g, 1, 1) + np.roll(g, -1, 1))
        + np.roll(np.roll(g, 1, 0), 1, 1) + np.roll(np.roll(g, 1, 0), -1, 1)
        + np.roll(np.roll(g, -1, 0), 1, 1) + np.roll(np.roll(g, -1, 0), -1, 1)
    )[1:-1, 1:-1]
    h, w = g.shape
    return float(math.sqrt(math.pi / 2.0) * np.sum(np.abs(m))
                 / (6.0 * (w - 2) * (h - 2)))


def compute_classical(cg, m: Metrics) -> None:
    g = _cg_to_gray(cg, ANALYSIS_LONGEST_EDGE)
    m.sharpness = round(_laplacian_variance(g), 1)
    m.brightness = round(float(g.mean()), 1)
    m.contrast = round(float(g.std()), 1)
    total = g.size
    m.black_frac = round(float(np.count_nonzero(g <= 5)) / total, 3)
    m.white_frac = round(float(np.count_nonzero(g >= 250)) / total, 3)
    crop = _native_centre_crop_gray(cg, NOISE_CROP)
    m.noise = round(_noise_sigma(crop), 2)


# --------------------------------------------------------------------------
# Apple Vision signals
# --------------------------------------------------------------------------
def compute_vision(cg, m: Metrics, want_faces: bool, want_fprint: bool) -> None:
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, {})

    aesth = Vision.VNCalculateImageAestheticsScoresRequest.alloc().init()
    requests = [aesth]

    face_req = None
    if want_faces:
        face_req = Vision.VNDetectFaceCaptureQualityRequest.alloc().init()
        requests.append(face_req)

    fprint_req = None
    if want_fprint:
        fprint_req = Vision.VNGenerateImageFeaturePrintRequest.alloc().init()
        requests.append(fprint_req)

    ok, err = handler.performRequests_error_(requests, None)
    if not ok:
        raise DecodeError(f"Vision request failed: {err}")

    for obs in aesth.results() or []:
        m.aesthetic = round(float(obs.overallScore()), 3)
        m.is_utility = bool(obs.isUtility())
        break

    if face_req is not None:
        qualities = []
        for obs in face_req.results() or []:
            q = obs.faceCaptureQuality()
            if q is not None:
                qualities.append(float(q))
        if qualities:
            # Worst face in the frame is what matters for "is this a keeper".
            m.face_quality = round(min(qualities), 3)

    if fprint_req is not None:
        for obs in fprint_req.results() or []:
            m.fprint = _fprint_bytes(obs)
            break


def _fprint_bytes(obs) -> bytes | None:
    """Extract a VNFeaturePrintObservation as raw float32 bytes."""
    data = obs.data()
    if data is None:
        return None
    raw = data.bytes().tobytes() if hasattr(data, "bytes") else bytes(data)
    # Feature prints are float32; keep only that (defensive against padding).
    n = (len(raw) // 4) * 4
    return raw[:n]


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------
def decide(m: Metrics, t: Thresholds, enabled: "set[str] | None" = None) -> None:
    """Turn metrics into reasons + a recommendation tier.

    `enabled` is the set of active signal keys (see SIGNALS). A disabled signal
    never fires: it contributes neither a reason nor weight to the verdict, so
    callers can target specific kinds of defects. When `enabled` is None every
    signal is active (the historical behaviour).
    """
    if enabled is None:
        enabled = set(SIGNALS)
    on = enabled.__contains__
    reasons: list[str] = []

    very_blurry = on("blur") and m.sharpness < t.blur_hard
    blurry = on("blur") and m.sharpness < t.blur
    very_dark = on("dark") and m.brightness < t.dark_hard
    dark = on("dark") and m.brightness < t.dark
    bad_exposure = on("exposure") and ((m.black_frac >= t.clip)
                                       or (m.white_frac >= t.clip))
    low_contrast = on("contrast") and m.contrast < t.contrast
    noisy = on("noise") and m.noise > t.noise
    low_aesthetic = on("aesthetic") and (m.aesthetic is not None
                                         and m.aesthetic < t.aesthetic)
    utility = on("utility") and bool(m.is_utility)
    poor_face = on("face") and (m.face_quality is not None
                                and m.face_quality < t.face)

    if blurry:
        reasons.append("very_blurry" if very_blurry else "blurry")
    if dark:
        reasons.append("very_dark" if very_dark else "dark")
    if low_contrast:
        reasons.append("low_contrast")
    if noisy:
        reasons.append("noisy")
    if bad_exposure:
        reasons.append("bad_exposure")
    if low_aesthetic:
        reasons.append("low_aesthetic")
    if utility:
        reasons.append("utility")
    if poor_face:
        reasons.append("low_face_quality")

    # Recommendation tiers.
    #   delete  - strong, objective defect: clearly blurry or clearly dark, or
    #             a face shot Vision rates as a poor capture.
    #   review  - milder/subjective issues worth a human glance.
    #   keep    - no flags.
    strong = (
        very_blurry
        or very_dark
        or (blurry and (dark or bad_exposure or low_contrast))
        or poor_face
    )
    if strong:
        m.recommendation = "delete"
    elif reasons:
        m.recommendation = "review"
    else:
        m.recommendation = "keep"
    m.reasons = reasons


# --------------------------------------------------------------------------
# Per-file analysis
# --------------------------------------------------------------------------
def analyse(path: Path, use_vision: bool, want_faces: bool,
            want_fprint: bool) -> Metrics:
    m = Metrics(path=str(path))
    try:
        st = path.stat()
        m.file_bytes = st.st_size
    except OSError:
        pass
    # Drain Objective-C autoreleased objects (CGImages, bitmap contexts, NSData
    # buffers, Vision result arrays) after every image. Without this they
    # accumulate for the life of the process, leaking memory and steadily
    # slowing the scan down on large libraries.
    try:
        with objc.autorelease_pool():
            cg = _load_cgimage(str(path))
            m.width = Quartz.CGImageGetWidth(cg)
            m.height = Quartz.CGImageGetHeight(cg)
            m.megapixels = round(m.width * m.height / 1e6, 2)
            compute_classical(cg, m)
            if use_vision:
                compute_vision(cg, m, want_faces, want_fprint)
    except (DecodeError, Exception) as e:  # noqa: BLE001 - report & continue
        m.error = str(e)
        m.recommendation = "error"
    return m


# --------------------------------------------------------------------------
# Input discovery
# --------------------------------------------------------------------------
def collect_inputs(paths, recursive: bool, skip_dir: Path | None):
    files, missing, seen = [], [], set()

    def add(p: Path):
        key = p.resolve()
        if key not in seen:
            seen.add(key)
            files.append(p)

    skip_resolved = skip_dir.resolve() if skip_dir else None

    def under_skip(p: Path) -> bool:
        if skip_resolved is None:
            return False
        try:
            p.resolve().relative_to(skip_resolved)
            return True
        except ValueError:
            return False

    for path in paths:
        if not path.exists():
            missing.append(path)
        elif path.is_dir():
            pattern = "**/*" if recursive else "*"
            for entry in sorted(path.glob(pattern)):
                if (entry.is_file()
                        and entry.suffix.lower() in IMAGE_EXTS
                        and not under_skip(entry)):
                    add(entry)
        else:
            add(path)
    return files, missing


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
CSV_FIELDS = [
    "path", "recommendation", "reasons", "sharpness", "brightness",
    "contrast", "noise", "black_frac", "white_frac", "aesthetic",
    "is_utility", "face_quality", "cluster_id", "cluster_size", "is_keeper",
    "width", "height", "megapixels", "file_bytes", "asset_id", "error",
]


def write_csv(results: list[Metrics], out_path: Path) -> None:
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for m in results:
            row = asdict(m)
            row["reasons"] = ";".join(m.reasons)
            w.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def _human_bytes(n: int) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step:
            return f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} PB"


def print_summary(results: list[Metrics], dd_stats: dict | None = None) -> None:
    tiers = {"delete": [], "duplicate": [], "review": [], "keep": [], "error": []}
    for m in results:
        tiers.setdefault(m.recommendation, []).append(m)

    total = len(results)
    print("\n" + "=" * 60)
    print(f"Scanned {total} image(s)")
    print("-" * 60)
    for tier in ("delete", "duplicate", "review", "keep", "error"):
        items = tiers.get(tier, [])
        if not items:
            continue
        size = sum(m.file_bytes for m in items)
        print(f"  {tier:9s}: {len(items):6d}  ({_human_bytes(size)})")

    if dd_stats and dd_stats.get("clusters"):
        print("-" * 60)
        print(f"Near-duplicate groups: {dd_stats['clusters']}  "
              f"(keeping the best of each)")
        print(f"Redundant copies flagged: {dd_stats['redundant']}  "
              f"-> {_human_bytes(dd_stats['reclaim'])} reclaimable")

    delete = tiers.get("delete", [])
    if delete:
        reclaim = sum(m.file_bytes for m in delete)
        print("-" * 60)
        print(f"Reclaimable from low-quality 'delete' candidates: "
              f"{_human_bytes(reclaim)}")

        counts: dict[str, int] = {}
        for m in delete:
            for r in m.reasons:
                counts[r] = counts.get(r, 0) + 1
        if counts:
            tally = ", ".join(f"{k}={v}" for k, v in
                              sorted(counts.items(), key=lambda kv: -kv[1]))
            print(f"Delete reasons: {tally}")

    grand = sum(m.file_bytes for m in tiers.get("delete", [])) \
        + sum(m.file_bytes for m in tiers.get("duplicate", []))
    if grand:
        print("-" * 60)
        print(f"Total reclaimable (delete + duplicate): {_human_bytes(grand)}")
    print("=" * 60)


def quarantine(results: list[Metrics], dest: Path, tiers: set[str],
               roots: list[Path], dry_run: bool) -> None:
    """Move flagged files into dest, preserving structure. Never deletes."""
    moved = 0
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    for m in results:
        if m.recommendation not in tiers:
            continue
        src = Path(m.path)
        if not src.exists():
            continue
        # Try to preserve a meaningful relative path under one of the roots.
        rel = None
        for root in roots:
            try:
                rel = src.resolve().relative_to(root.resolve())
                break
            except ValueError:
                continue
        target = dest / (rel if rel else src.name)
        if dry_run:
            print(f"[dry-run] would move {src} -> {target}")
            moved += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # Avoid clobbering existing files in the quarantine.
        if target.exists():
            stem, suf = target.stem, target.suffix
            i = 1
            while target.exists():
                target = target.with_name(f"{stem}__{i}{suf}")
                i += 1
        shutil.move(str(src), str(target))
        moved += 1
    verb = "would move" if dry_run else "moved"
    print(f"Quarantine: {verb} {moved} file(s) -> {dest}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _perf_cores() -> int | None:
    """Number of performance (P) cores on Apple Silicon, or None elsewhere.

    Efficiency cores are much slower, so basing the worker count on P-cores is
    a better default than total logical cores.
    """
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.perflevel0.logicalcpu"],
            capture_output=True, text=True, timeout=1)
        val = out.stdout.strip()
        if out.returncode == 0 and val.isdigit() and int(val) > 0:
            return int(val)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _default_workers(use_vision: bool = True) -> int:
    """Auto-pick a worker count from host capabilities.

    Uses performance-core count on Apple Silicon (falling back to logical
    cores minus a couple elsewhere). With Vision enabled the result is capped,
    because the aesthetics/feature-print passes share the Neural Engine/GPU and
    stop scaling past roughly 8 workers; a pure-classical (--no-vision) run can
    use all performance cores.
    """
    cpu = os.cpu_count() or 1
    pcores = _perf_cores()
    base = pcores if pcores else max(1, cpu - 2)
    if use_vision:
        return max(1, min(8, base))
    return max(1, min(cpu, base))


class ProgressReporter:
    """Single progress channel shared by every source.

    Renders a rate/ETA line to stderr (matching the historical format). A GUI
    front-end can subclass and override ``_emit`` to also write a progress file
    the UI polls, without the pipeline needing to know.
    """

    def __init__(self, total: int, label: str = "  ", stream=sys.stderr):
        self.total = total
        self.done = 0
        self.label = label
        self.stream = stream
        self.t0 = time.time()

    def advance(self, n: int = 1) -> None:
        self.done += n
        if self.done % 50 == 0 or self.done >= self.total:
            self._emit()

    def _emit(self) -> None:
        rate = self.done / max(1e-6, time.time() - self.t0)
        eta = (self.total - self.done) / rate if rate else 0
        print(f"{self.label}{self.done}/{self.total}  "
              f"({rate:.1f}/s, eta {eta/60:.1f} min)", file=self.stream)


# --------------------------------------------------------------------------
# On-disk result cache (SQLite). Stores the *expensive* outputs (decode +
# Vision metrics + feature print) keyed on (path, size, mtime). The cheap
# verdict (decide()) is always recomputed, so changing thresholds never
# requires a rescan. Only the main process touches the DB -> no locking issues.
# --------------------------------------------------------------------------
_CACHE_COLS = [
    "width", "height", "megapixels", "file_bytes", "sharpness", "brightness",
    "contrast", "noise", "black_frac", "white_frac", "aesthetic",
    "is_utility", "face_quality",
]


class Cache:
    def __init__(self, path: Path):
        import sqlite3
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS cache (
                   path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, ver INTEGER,
                   width INTEGER, height INTEGER, megapixels REAL, file_bytes INTEGER,
                   sharpness REAL, brightness REAL, contrast REAL, noise REAL,
                   black_frac REAL, white_frac REAL, aesthetic REAL,
                   is_utility INTEGER, face_quality REAL, fprint BLOB
               )"""
        )
        self.conn.commit()

    def get(self, path: str, size: int, mtime: int,
            need_fprint: bool) -> Metrics | None:
        cur = self.conn.execute(
            "SELECT size, mtime, ver, width, height, megapixels, file_bytes, "
            "sharpness, brightness, contrast, noise, black_frac, white_frac, "
            "aesthetic, is_utility, face_quality, fprint FROM cache WHERE path=?",
            (path,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        if row[0] != size or row[1] != mtime or row[2] != METRICS_VERSION:
            return None
        if need_fprint and row[16] is None:
            return None
        m = Metrics(path=path)
        (m.width, m.height, m.megapixels, m.file_bytes, m.sharpness,
         m.brightness, m.contrast, m.noise, m.black_frac, m.white_frac) = row[3:13]
        m.aesthetic = row[13]
        m.is_utility = None if row[14] is None else bool(row[14])
        m.face_quality = row[15]
        m.fprint = bytes(row[16]) if row[16] is not None else None
        return m

    def put_many(self, results: list[Metrics], stat_map: dict) -> None:
        rows = []
        for m in results:
            if m.error:  # don't cache failures; retry them next run
                continue
            size, mtime = stat_map.get(m.path, (m.file_bytes, 0))
            rows.append((
                m.path, size, mtime, METRICS_VERSION,
                m.width, m.height, m.megapixels, m.file_bytes,
                m.sharpness, m.brightness, m.contrast, m.noise,
                m.black_frac, m.white_frac, m.aesthetic,
                None if m.is_utility is None else int(m.is_utility),
                m.face_quality, m.fprint,
            ))
        if rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO cache VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows,
            )
            self.conn.commit()

    def put_rows(self, rows_in) -> None:
        """Store results keyed on an explicit cache id.

        ``rows_in`` is an iterable of ``(cache_id, size, mtime, Metrics)``. The
        cache id is the stable identity chosen by the Source (a file path for
        the filesystem, a PhotoKit localIdentifier for Photos), so the cache no
        longer has to borrow ``Metrics.path`` as its key.
        """
        rows = []
        for cache_id, size, mtime, m in rows_in:
            if m.error:  # don't cache failures; retry them next run
                continue
            rows.append((
                cache_id, size, mtime, METRICS_VERSION,
                m.width, m.height, m.megapixels, m.file_bytes,
                m.sharpness, m.brightness, m.contrast, m.noise,
                m.black_frac, m.white_frac, m.aesthetic,
                None if m.is_utility is None else int(m.is_utility),
                m.face_quality, m.fprint,
            ))
        if rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO cache VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows,
            )
            self.conn.commit()

    def close(self):
        self.conn.close()


# --------------------------------------------------------------------------
# Near-duplicate / burst clustering via Vision feature prints
# --------------------------------------------------------------------------
def _keep_score(m: Metrics):
    """Higher is a better keeper. Face capture quality (Apple's own best-shot
    signal) leads when faces are present, then sharpness, then aesthetics and
    resolution as tie-breakers."""
    return (
        0 if m.error else 1,
        m.face_quality if m.face_quality is not None else -1.0,
        m.sharpness,
        m.aesthetic if m.aesthetic is not None else 0.0,
        m.megapixels,
        m.file_bytes,
    )


def cluster_near_duplicates(results: list[Metrics], threshold: float) -> dict:
    """Cluster near-identical photos by feature-print L2 distance.

    Marks every member with a cluster_id/size and is_keeper flag; non-keepers
    get recommendation 'duplicate'. Returns summary stats. O(n^2) distances are
    computed with blocked BLAS matrix products, which handles tens of thousands
    of images in seconds.
    """
    have = [i for i, m in enumerate(results) if m.fprint]
    # All feature prints should be the same length; keep the dominant length.
    if len(have) >= 2:
        lengths = [len(results[i].fprint) for i in have]
        common = max(set(lengths), key=lengths.count)
        have = [i for i in have if len(results[i].fprint) == common]
    if len(have) < 2:
        return {"clusters": 0, "redundant": 0, "reclaim": 0}

    F = np.stack([np.frombuffer(results[i].fprint, dtype=np.float32)
                  for i in have]).astype(np.float32)
    n = F.shape[0]

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    sq = (F * F).sum(axis=1)
    thr2 = float(threshold) ** 2
    block = 2048
    for a in range(0, n, block):
        blk = F[a:a + block]
        # squared L2: ||x||^2 + ||y||^2 - 2 x.y
        d2 = sq[a:a + blk.shape[0], None] + sq[None, :] - 2.0 * (blk @ F.T)
        for r in range(blk.shape[0]):
            gi = a + r
            row = d2[r]
            js = np.nonzero(row[gi + 1:] < thr2)[0]
            for off in js:
                union(gi, gi + 1 + int(off))

    groups: dict[int, list[int]] = {}
    for k in range(n):
        groups.setdefault(find(k), []).append(k)

    clusters = redundant = reclaim = 0
    cid = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        ridx = [have[k] for k in members]
        keeper = max(ridx, key=lambda i: _keep_score(results[i]))
        keeper_name = Path(results[keeper].path).name
        for i in ridx:
            results[i].cluster_id = cid
            results[i].cluster_size = len(ridx)
            results[i].is_keeper = (i == keeper)
            if i != keeper:
                results[i].recommendation = "duplicate"
                results[i].reasons = [f"near_dup_of:{keeper_name}"]
                redundant += 1
                reclaim += results[i].file_bytes
        clusters += 1
        cid += 1
    return {"clusters": clusters, "redundant": redundant, "reclaim": reclaim}


# --------------------------------------------------------------------------
# Engine: a Source feeds items in, run_pipeline analyses/decides/dedupes them,
# and Sinks consume the classified results. The filesystem and Apple Photos
# front-ends differ ONLY in their Source (how items are enumerated/decoded) and
# their Sinks (what happens to flagged items); everything between is shared.
# --------------------------------------------------------------------------
@dataclass
class RunOptions:
    thresholds: Thresholds
    enabled_signals: set
    use_vision: bool = True
    want_faces: bool = True
    want_fprint: bool = False
    workers: int = 1
    dedupe: bool = False
    dedupe_threshold: float = 0.3


class Source:
    """Enumerates items and turns each into a Metrics. Subclasses supply the
    medium-specific bits (file glob + CGImageSource, or PhotoKit fetch)."""

    name = "source"
    parallel = False  # may analyse across worker processes

    def collect(self) -> list:
        """Return opaque per-item references (paths, assets, …)."""
        raise NotImplementedError

    def cache_id(self, ref):
        """Return ``(id, size, mtime)`` for the result cache, or None to skip
        caching this item. ``id`` is the stable identity used as the cache key."""
        return None

    def hydrate_cached(self, m: Metrics, ref) -> None:
        """Fix up a Metrics restored from cache so it carries this item's
        display identity (path / asset id)."""

    def analyse_one(self, ref, opts: RunOptions) -> Metrics:
        """Analyse a single item in-process."""
        raise NotImplementedError

    def parallel_task(self, opts: RunOptions):
        """Return a picklable callable ``ref -> Metrics`` for the process pool,
        or None to force in-process analysis."""
        return None


class FilesystemSource(Source):
    name = "files"
    parallel = True

    def __init__(self, paths, recursive: bool, skip_dir=None):
        self.paths = paths
        self.recursive = recursive
        self.skip_dir = skip_dir
        self.missing: list = []

    def collect(self) -> list:
        files, missing = collect_inputs(self.paths, self.recursive, self.skip_dir)
        self.missing = missing
        return files

    def cache_id(self, ref):
        try:
            st = ref.stat()
        except OSError:
            return None
        return (str(ref), st.st_size, st.st_mtime_ns)

    def hydrate_cached(self, m: Metrics, ref) -> None:
        m.path = str(ref)

    def analyse_one(self, ref, opts: RunOptions) -> Metrics:
        return analyse(ref, opts.use_vision, opts.want_faces, opts.want_fprint)

    def parallel_task(self, opts: RunOptions):
        return partial(analyse, use_vision=opts.use_vision,
                       want_faces=opts.want_faces, want_fprint=opts.want_fprint)


def run_pipeline(source: Source, opts: RunOptions, cache: "Cache | None",
                 refs=None, progress_label: str = "  "):
    """Analyse a source's items and return ``(results, dedupe_stats)``.

    Owns the whole shared middle of the journey: the cache split, the (optional
    parallel) analysis loop, the verdict, near-duplicate clustering, and the
    worst-first sort. Source-specific behaviour is delegated entirely to the
    ``source`` object; the result is identical whichever medium fed it.
    """
    if refs is None:
        refs = source.collect()

    cached: list[Metrics] = []
    to_compute: list = []
    compute_ids: list = []
    for ref in refs:
        cid = source.cache_id(ref)
        m = None
        if cid is not None and cache is not None:
            id_, size, mtime = cid
            m = cache.get(id_, size, mtime, opts.want_fprint)
        if m is not None:
            source.hydrate_cached(m, ref)
            cached.append(m)
        else:
            to_compute.append(ref)
            compute_ids.append(cid)

    total = len(refs)
    if cache is not None:
        print(f"  {len(cached)} from cache, {len(to_compute)} to compute",
              file=sys.stderr)
    progress = ProgressReporter(total, progress_label)
    progress.done = len(cached)

    fresh: list[Metrics] = []
    task = source.parallel_task(opts) if source.parallel else None
    if task is not None and opts.workers > 1 and len(to_compute) > 1:
        # chunksize batches items per IPC round-trip to cut overhead.
        chunksize = max(1, min(16, len(to_compute) // (opts.workers * 8) or 1))
        with ProcessPoolExecutor(max_workers=opts.workers) as ex:
            for m in ex.map(task, to_compute, chunksize=chunksize):
                fresh.append(m)
                progress.advance()
    else:
        for ref in to_compute:
            fresh.append(source.analyse_one(ref, opts))
            progress.advance()

    if cache is not None:
        cache.put_rows((cid[0], cid[1], cid[2], m)
                       for cid, m in zip(compute_ids, fresh) if cid is not None)

    results = cached + fresh

    # Verdict is recomputed for everything so current thresholds/signals always
    # apply, even to cached rows.
    for m in results:
        if m.recommendation != "error":
            decide(m, opts.thresholds, opts.enabled_signals)

    dd_stats = None
    if opts.dedupe:
        print("Clustering near-duplicates...", file=sys.stderr)
        dd_stats = cluster_near_duplicates(results, opts.dedupe_threshold)

    order = {"delete": 0, "duplicate": 1, "review": 2, "keep": 3, "error": 4}
    results.sort(key=lambda m: (order.get(m.recommendation, 9), m.sharpness))
    return results, dd_stats


# --------------------------------------------------------------------------
# Sinks: consume the classified results. CSV + console summary are shared; the
# medium-specific actions (quarantine move, Photos album curation) subclass too.
# --------------------------------------------------------------------------
class Sink:
    def emit(self, results: list, dd_stats, source: Source) -> None:
        raise NotImplementedError


class CsvReportSink(Sink):
    def __init__(self, out_path):
        self.out_path = out_path

    def emit(self, results, dd_stats, source) -> None:
        write_csv(results, self.out_path)
        print(f"\nWrote report: {self.out_path}", file=sys.stderr)


class ConsoleSummarySink(Sink):
    def emit(self, results, dd_stats, source) -> None:
        print_summary(results, dd_stats)


class QuarantineSink(Sink):
    def __init__(self, dest, tiers, roots, dry_run):
        self.dest = dest
        self.tiers = tiers
        self.roots = roots
        self.dry_run = dry_run

    def emit(self, results, dd_stats, source) -> None:
        quarantine(results, self.dest, self.tiers, self.roots, self.dry_run)
        if not self.dry_run:
            print("Files were MOVED (not deleted). Review them, then delete "
                  "manually when you're sure.", file=sys.stderr)


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="photocull",
        description="Flag blurry/dark/low-quality photos for cleanup "
                    "(macOS, Apple Vision). Never deletes.",
    )
    p.add_argument("paths", nargs="+", type=Path,
                   help="image file(s) and/or folder(s) to scan")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="descend into subfolders")
    p.add_argument("-o", "--output", type=Path, default=Path("photo_quality_report.csv"),
                   help="CSV report path (default: photo_quality_report.csv)")
    p.add_argument("--no-vision", action="store_true",
                   help="skip Apple Vision (faster; classical metrics only)")
    p.add_argument("--no-faces", action="store_true",
                   help="skip per-face capture-quality (a bit faster)")
    p.add_argument("-j", "--workers", type=int, default=None,
                   help="parallel worker processes (default: auto — based on "
                        "your CPU's performance cores; use 1 to disable "
                        "parallelism)")

    dd = p.add_argument_group("near-duplicate detection")
    dd.add_argument("--dedupe", action="store_true",
                    help="find near-duplicate/burst photos (Vision feature "
                         "prints) and flag all but the best in each group")
    dd.add_argument("--dedupe-threshold", type=float, default=0.3,
                    help="feature-print L2 distance below which two photos are "
                         "near-duplicates (default 0.3; lower = stricter/safer, "
                         "higher risks merging distinct shots of the same scene)")

    c = p.add_argument_group("cache")
    c.add_argument("--cache", type=Path, default=Path(".photo_quality_cache.sqlite"),
                   help="result cache DB path (default .photo_quality_cache.sqlite)")
    c.add_argument("--no-cache", action="store_true",
                   help="disable the on-disk result cache")

    g = p.add_argument_group("thresholds")
    d = Thresholds()
    g.add_argument("--blur", type=float, default=d.blur,
                   help=f"sharpness below this is blurry (default {d.blur})")
    g.add_argument("--blur-hard", type=float, default=d.blur_hard,
                   help=f"sharpness below this is very blurry (default {d.blur_hard})")
    g.add_argument("--dark", type=float, default=d.dark,
                   help=f"mean luminance below this is dark (default {d.dark})")
    g.add_argument("--dark-hard", type=float, default=d.dark_hard,
                   help=f"mean luminance below this is very dark (default {d.dark_hard})")
    g.add_argument("--contrast", type=float, default=d.contrast,
                   help=f"luminance std below this is low-contrast (default {d.contrast})")
    g.add_argument("--noise", type=float, default=d.noise,
                   help=f"noise sigma above this is noisy (default {d.noise})")
    g.add_argument("--aesthetic", type=float, default=d.aesthetic,
                   help=f"Vision score below this is low-aesthetic (default {d.aesthetic})")

    s = p.add_argument_group(
        "detection signals (choose which kinds of photos to flag)")
    add_signal_cli(s, d)

    q = p.add_argument_group("quarantine (optional, moves files; never deletes)")
    q.add_argument("--quarantine", type=Path, metavar="DIR",
                   help="move flagged files into DIR for review")
    q.add_argument("--include-review", action="store_true",
                   help="also quarantine 'review' tier, not just 'delete'")
    q.add_argument("--dry-run", action="store_true",
                   help="with --quarantine, only print what would move")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    t = Thresholds(
        blur=args.blur, blur_hard=args.blur_hard, dark=args.dark,
        dark_hard=args.dark_hard, contrast=args.contrast, noise=args.noise,
        aesthetic=args.aesthetic, face=args.face,
    )

    try:
        enabled_signals = resolve_signals(args.signals, args.exclude_signals)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not enabled_signals:
        print("error: no detection signals enabled (check --signals / "
              "--exclude-signals).", file=sys.stderr)
        return 2

    skip_dir = args.quarantine if args.quarantine else None
    source = FilesystemSource(args.paths, args.recursive, skip_dir)
    files = source.collect()

    for p in source.missing:
        print(f"skip: {p} (not found)", file=sys.stderr)
    if not files:
        print("no image files to process", file=sys.stderr)
        return 1

    use_vision = not args.no_vision
    want_faces = use_vision and not args.no_faces and ("face" in enabled_signals)
    want_fprint = args.dedupe and use_vision

    if not use_vision:
        vis_on = [s for s in SIGNALS if s in enabled_signals
                  and SIGNALS[s].needs_vision]
        if vis_on:
            print(f"note: signal(s) {', '.join(vis_on)} need Vision; "
                  "they won't fire under --no-vision.", file=sys.stderr)

    if args.dedupe and not use_vision:
        print("note: --dedupe needs Vision; ignoring --no-vision for it.",
              file=sys.stderr)
        use_vision = True
        want_fprint = True

    # Worker count: honour an explicit -j, otherwise auto-detect from the host
    # (performance cores, adjusted for whether Vision is in play).
    if args.workers is None:
        workers = _default_workers(use_vision)
        worker_note = f"{workers} worker(s) (auto)"
    else:
        workers = max(1, args.workers)
        worker_note = f"{workers} worker(s)"

    cache = None
    if not args.no_cache:
        try:
            cache = Cache(args.cache)
        except Exception as e:  # noqa: BLE001
            print(f"warning: could not open cache {args.cache}: {e}",
                  file=sys.stderr)

    classical_note = " (classical only)" if not use_vision else ""
    print(f"Analysing {len(files)} image(s) with {worker_note}"
          f"{classical_note}...", file=sys.stderr)

    opts = RunOptions(
        thresholds=t, enabled_signals=enabled_signals, use_vision=use_vision,
        want_faces=want_faces, want_fprint=want_fprint, workers=workers,
        dedupe=args.dedupe, dedupe_threshold=args.dedupe_threshold,
    )
    results, dd_stats = run_pipeline(source, opts, cache, refs=files)
    if cache is not None:
        cache.close()

    sinks: list[Sink] = [CsvReportSink(args.output), ConsoleSummarySink()]
    if args.quarantine:
        roots = [p for p in args.paths if p.is_dir()]
        tiers = {"delete", "duplicate"} | ({"review"} if args.include_review else set())
        sinks.append(QuarantineSink(args.quarantine, tiers, roots, args.dry_run))
    for sink in sinks:
        sink.emit(results, dd_stats, source)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
