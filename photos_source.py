#!/usr/bin/env python3
"""
photocull (Apple Photos source) - analyse the macOS Photos library via PhotoKit
instead of a filesystem folder.

It scans a chosen subset of your library, runs photocull's existing quality +
near-duplicate analysis, writes the same CSV report, and then curates **review
albums** inside Photos (one per recommendation tier) so you can browse the
candidates in Photos.app and delete them yourself.

SAFETY: this never deletes or moves photos. It only *adds* flagged photos to
photocull review albums (removing a photo from an album, or deleting the album,
never deletes the photo). Favourites are excluded by default.

Requires Photos access, which on macOS means running from a signed .app bundle
that carries an NSPhotoLibraryUsageDescription (see build_photos_app.sh).

Usage (inside the app bundle, or from a terminal already granted Photos access):
    python photos_source.py --smart-album recently-added --limit 500 --dedupe
    python photos_source.py --album "Iceland 2024" --dedupe
    python photos_source.py --since 2022-01-01 --until 2022-12-31
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime, timezone

import objc
import Quartz
import Photos
from Foundation import (NSRunLoop, NSDate, NSDefaultRunLoopMode,
                        NSPredicate, NSSortDescriptor)

from photo_quality import (
    Metrics, Thresholds, compute_classical, compute_vision, decide,
    cluster_near_duplicates, write_csv, print_summary, Cache, _human_bytes,
    SIGNALS, REASON_TO_SIGNAL, resolve_signals, add_signal_cli,
    Source, Sink, RunOptions, run_pipeline, CsvReportSink, ConsoleSummarySink,
)


# Smart-album name -> PHAssetCollectionSubtype
SMART_ALBUMS = {
    "recently-added": "PHAssetCollectionSubtypeSmartAlbumRecentlyAdded",
    "screenshots": "PHAssetCollectionSubtypeSmartAlbumScreenshots",
    "selfies": "PHAssetCollectionSubtypeSmartAlbumSelfPortraits",
    "bursts": "PHAssetCollectionSubtypeSmartAlbumBursts",
    "favorites": "PHAssetCollectionSubtypeSmartAlbumFavorites",
    "panoramas": "PHAssetCollectionSubtypeSmartAlbumPanoramas",
}

TIER_LABELS = {
    "delete": "Delete candidates",
    "duplicate": "Duplicates",
    "review": "Review",
}


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------
def _status_name(s) -> str:
    return {0: "notDetermined", 1: "restricted", 2: "denied",
            3: "authorized", 4: "limited"}.get(int(s), str(s))


def ensure_authorized(timeout: float = 60.0) -> int:
    level = Photos.PHAccessLevelReadWrite
    st = Photos.PHPhotoLibrary.authorizationStatusForAccessLevel_(level)
    if int(st) in (3, 4):
        return int(st)
    print("[auth] requesting Photos access (a system prompt may appear)...",
          file=sys.stderr)
    done = threading.Event()
    res = {}

    def handler(new_status):
        res["st"] = int(new_status)
        done.set()

    Photos.PHPhotoLibrary.requestAuthorizationForAccessLevel_handler_(level, handler)
    rl = NSRunLoop.currentRunLoop()
    deadline = time.time() + timeout
    while not done.is_set() and time.time() < deadline:
        rl.runMode_beforeDate_(NSDefaultRunLoopMode,
                               NSDate.dateWithTimeIntervalSinceNow_(0.1))
    return int(res.get("st", st))


# --------------------------------------------------------------------------
# Subset enumeration
# --------------------------------------------------------------------------
def _date(s: str) -> NSDate:
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def build_fetch_options(args) -> "Photos.PHFetchOptions":
    opts = Photos.PHFetchOptions.alloc().init()
    fmt = [f"mediaType == {int(Photos.PHAssetMediaTypeImage)}"]
    fargs = []
    if not args.include_favorites:
        fmt.append("favorite == NO")
    if args.since:
        fmt.append("creationDate >= %@")
        fargs.append(_date(args.since))
    if args.until:
        fmt.append("creationDate <= %@")
        fargs.append(_date(args.until))
    opts.setPredicate_(
        NSPredicate.predicateWithFormat_argumentArray_(" AND ".join(fmt), fargs))
    opts.setSortDescriptors_(
        [NSSortDescriptor.sortDescriptorWithKey_ascending_("creationDate", False)])
    if args.limit and args.limit > 0:
        opts.setFetchLimit_(int(args.limit))
    return opts


def _find_user_album(title: str):
    res = Photos.PHAssetCollection.fetchAssetCollectionsWithType_subtype_options_(
        Photos.PHAssetCollectionTypeAlbum,
        Photos.PHAssetCollectionSubtypeAlbumRegular, None)
    for i in range(res.count()):
        coll = res.objectAtIndex_(i)
        if coll.localizedTitle() == title:
            return coll
    return None


def _smart_album(name: str):
    subtype = getattr(Photos, SMART_ALBUMS[name])
    res = Photos.PHAssetCollection.fetchAssetCollectionsWithType_subtype_options_(
        Photos.PHAssetCollectionTypeSmartAlbum, subtype, None)
    return res.firstObject() if res.count() else None


def enumerate_assets(args):
    opts = build_fetch_options(args)
    if args.album:
        coll = _find_user_album(args.album)
        if coll is None:
            print(f"error: album not found: {args.album}", file=sys.stderr)
            return None
        return Photos.PHAsset.fetchAssetsInAssetCollection_options_(coll, opts)
    if args.smart_album:
        coll = _smart_album(args.smart_album)
        if coll is None:
            print(f"error: smart album unavailable: {args.smart_album}",
                  file=sys.stderr)
            return None
        return Photos.PHAsset.fetchAssetsInAssetCollection_options_(coll, opts)
    return Photos.PHAsset.fetchAssetsWithOptions_(opts)


# --------------------------------------------------------------------------
# Image acquisition + analysis
# --------------------------------------------------------------------------
def _request_cgimage(asset, allow_network: bool):
    """Returns (CGImage|None, in_cloud_bool)."""
    opts = Photos.PHImageRequestOptions.alloc().init()
    opts.setVersion_(Photos.PHImageRequestOptionsVersionCurrent)
    opts.setDeliveryMode_(Photos.PHImageRequestOptionsDeliveryModeHighQualityFormat)
    opts.setNetworkAccessAllowed_(allow_network)
    opts.setSynchronous_(True)
    out = {"data": None, "cloud": False}

    def handler(data, uti, orientation, info):
        out["data"] = data
        if info is not None:
            out["cloud"] = bool(info.objectForKey_(Photos.PHImageResultIsInCloudKey))

    Photos.PHImageManager.defaultManager() \
        .requestImageDataAndOrientationForAsset_options_resultHandler_(asset, opts, handler)
    if out["data"] is None:
        return None, out["cloud"]
    src = Quartz.CGImageSourceCreateWithData(out["data"], None)
    if src is None:
        return None, out["cloud"]
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None), out["cloud"]


def _filename(asset) -> str:
    res = list(Photos.PHAssetResource.assetResourcesForAsset_(asset) or [])
    return res[0].originalFilename() if res else asset.localIdentifier()


def analyse_asset(asset, use_vision: bool, want_faces: bool,
                  want_fprint: bool, allow_network: bool) -> Metrics:
    m = Metrics(path=_filename(asset))
    m.asset_id = asset.localIdentifier()
    m.width = int(asset.pixelWidth())
    m.height = int(asset.pixelHeight())
    m.megapixels = round(m.width * m.height / 1e6, 2)
    try:
        with objc.autorelease_pool():
            cg, in_cloud = _request_cgimage(asset, allow_network)
            if cg is None:
                m.error = "could not load image" + (" (in iCloud)" if in_cloud else "")
                m.recommendation = "error"
                return m
            compute_classical(cg, m)
            if use_vision:
                compute_vision(cg, m, want_faces, want_fprint)
    except Exception as e:  # noqa: BLE001
        m.error = str(e)
        m.recommendation = "error"
    return m


# --------------------------------------------------------------------------
# Apple Photos Source: feeds PhotoKit assets into the shared run_pipeline. The
# cache is keyed on the asset's stable localIdentifier (+ pixel count +
# modification date) -- no more borrowing Metrics.path as the cache key.
# --------------------------------------------------------------------------
class PhotosSource(Source):
    name = "photos"
    parallel = False  # PhotoKit is tied to one process

    def __init__(self, fetch_result, allow_network: bool):
        self._result = fetch_result
        self.allow_network = allow_network
        self.assets_by_id: dict = {}   # localIdentifier -> PHAsset (for sinks)

    def collect(self) -> list:
        res = self._result
        refs = [res.objectAtIndex_(i) for i in range(res.count())]
        for a in refs:
            self.assets_by_id[a.localIdentifier()] = a
        return refs

    def cache_id(self, asset):
        mod = asset.modificationDate()
        mtime = int(mod.timeIntervalSince1970() * 1e6) if mod else 0
        size = int(asset.pixelWidth()) * int(asset.pixelHeight())
        return (asset.localIdentifier(), size, mtime)

    def hydrate_cached(self, m: Metrics, asset) -> None:
        m.asset_id = asset.localIdentifier()
        m.path = _filename(asset)

    def analyse_one(self, asset, opts: RunOptions) -> Metrics:
        return analyse_asset(asset, opts.use_vision, opts.want_faces,
                             opts.want_fprint, self.allow_network)


# --------------------------------------------------------------------------
# Review-album curation (non-destructive)
# --------------------------------------------------------------------------
def curate_albums(tier_assets: dict, titles: dict, prefix: str,
                  dry_run: bool, replace: bool) -> None:
    """Create one album per tier (titles supplied by the caller) and add the
    flagged assets. Never deletes photos -- only manages album membership.
    Previous albums are kept unless `replace` is set (matched by `prefix`)."""
    if dry_run:
        for tier, assets in tier_assets.items():
            if assets:
                print(f"[dry-run] would add {len(assets)} photo(s) to "
                      f"'{titles[tier]}'", file=sys.stderr)
        return

    # Optionally clear earlier albums sharing the prefix. Never deletes photos.
    prior = []
    if replace and prefix:
        res = Photos.PHAssetCollection.fetchAssetCollectionsWithType_subtype_options_(
            Photos.PHAssetCollectionTypeAlbum,
            Photos.PHAssetCollectionSubtypeAlbumRegular, None)
        for i in range(res.count()):
            coll = res.objectAtIndex_(i)
            if (coll.localizedTitle() or "").startswith(prefix):
                prior.append(coll)

    lib = Photos.PHPhotoLibrary.sharedPhotoLibrary()

    def changes():
        for coll in prior:
            Photos.PHAssetCollectionChangeRequest.deleteAssetCollections_([coll])
        for tier, assets in tier_assets.items():
            if not assets:
                continue
            req = Photos.PHAssetCollectionChangeRequest \
                .creationRequestForAssetCollectionWithTitle_(titles[tier])
            req.addAssets_(assets)

    ok, err = lib.performChangesAndWait_error_(changes, None)
    if not ok:
        print(f"warning: album curation failed: {err}", file=sys.stderr)
    else:
        for tier, assets in tier_assets.items():
            if assets:
                print(f"  '{titles[tier]}': {len(assets)} photo(s)",
                      file=sys.stderr)


# --------------------------------------------------------------------------
# Album grouping. Two strategies, both returning (groups, titles) where groups
# maps an arbitrary key -> ordered list of PHAssets and titles maps the same
# key -> album title. curate_albums() is agnostic to what the keys mean, so the
# only difference between modes is how we bucket the flagged photos.
# --------------------------------------------------------------------------
def _title_for(template: str, prefix: str, session: str, label: str) -> str:
    return template.format(prefix=prefix, date=session, tier=label)


def group_by_tier(results, asset_by_id, template, prefix, session):
    """One album per recommendation tier (the historical behaviour)."""
    titles = {tier: _title_for(template, prefix, session, label)
              for tier, label in TIER_LABELS.items()}
    groups = {tier: [] for tier in TIER_LABELS}
    for m in sorted(results, key=lambda m: m.sharpness):
        if m.recommendation in groups and m.asset_id in asset_by_id:
            groups[m.recommendation].append(asset_by_id[m.asset_id])
    return groups, titles


def group_by_signal(results, asset_by_id, template, prefix, session, enabled):
    """One album per detection signal, plus a Duplicates album.

    A photo is added to every signal album it triggered, so the same shot can
    appear in both 'Blurry' and 'Too dark' if it is both. Only signals the user
    actually enabled get an album. Duplicates (from --dedupe) stay in their own
    album since they aren't a quality signal.
    """
    titles = {key: _title_for(template, prefix, session, SIGNALS[key].label)
              for key in SIGNALS if key in enabled}
    titles["duplicate"] = _title_for(template, prefix, session,
                                     TIER_LABELS["duplicate"])
    groups = {key: [] for key in titles}
    for m in sorted(results, key=lambda m: m.sharpness):
        asset = asset_by_id.get(m.asset_id)
        if asset is None:
            continue
        if m.recommendation == "duplicate":
            groups["duplicate"].append(asset)
        elif m.recommendation in ("delete", "review"):
            for r in m.reasons:
                key = REASON_TO_SIGNAL.get(r)
                if key in groups:
                    groups[key].append(asset)
    return groups, titles


class PhotosAlbumsSink(Sink):
    """Curate the per-tier or per-signal review albums (non-destructive)."""

    def __init__(self, args, enabled_signals):
        self.args = args
        self.enabled = enabled_signals

    def emit(self, results, dd_stats, source) -> None:
        args = self.args
        try:
            session = datetime.now().strftime(args.album_date_format)
        except ValueError:
            session = datetime.now().strftime("%Y-%m-%d-%H-%M")
        try:
            if args.albums_by == "signal":
                groups, titles = group_by_signal(
                    results, source.assets_by_id, args.album_template,
                    args.album_prefix, session, self.enabled)
            else:
                groups, titles = group_by_tier(
                    results, source.assets_by_id, args.album_template,
                    args.album_prefix, session)
        except (KeyError, IndexError) as e:
            print(f"error: unknown placeholder in --album-template: {e} "
                  "(allowed: {prefix} {date} {tier})", file=sys.stderr)
            return
        print("\nCurating Photos review albums (non-destructive)...",
              file=sys.stderr)
        curate_albums(groups, titles, args.album_prefix,
                      args.dry_run, args.replace_albums)
        if not args.dry_run:
            example = next((titles[k] for k, v in groups.items() if v), None)
            if example:
                print(f"Open Photos.app -> e.g. '{example}' to review and "
                      "delete what you choose.", file=sys.stderr)
            print("Tip: inside an album, press Cmd-Delete (\u2318\u232b) to delete a "
                  "photo to Recently Deleted. Plain Delete (\u232b) only removes it "
                  "from the album. Deleting a photocull album never deletes "
                  "photos.", file=sys.stderr)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="photocull-photos",
        description="Analyse the Apple Photos library and curate review albums "
                    "of low-quality / duplicate candidates. Never deletes.")

    g = p.add_argument_group("subset (what to scan)")
    g.add_argument("--album", metavar="NAME", help="scan a single user album by name")
    g.add_argument("--smart-album", choices=sorted(SMART_ALBUMS),
                   help="scan a built-in smart album")
    g.add_argument("--since", metavar="YYYY-MM-DD", help="only photos on/after this date")
    g.add_argument("--until", metavar="YYYY-MM-DD", help="only photos on/before this date")
    g.add_argument("--include-favorites", action="store_true",
                   help="include favourites (excluded by default for safety)")
    g.add_argument("--limit", type=int, default=0, help="cap the number of photos")

    a = p.add_argument_group("analysis")
    a.add_argument("--no-vision", action="store_true", help="skip Apple Vision")
    a.add_argument("--no-faces", action="store_true", help="skip face capture quality")
    a.add_argument("--dedupe", action="store_true", help="detect near-duplicates/bursts")
    a.add_argument("--dedupe-threshold", type=float, default=0.3)
    a.add_argument("--no-download", action="store_true",
                   help="don't download iCloud-only originals (skip them instead)")
    d = Thresholds()
    a.add_argument("--blur", type=float, default=d.blur)
    a.add_argument("--blur-hard", type=float, default=d.blur_hard)
    a.add_argument("--dark", type=float, default=d.dark)
    a.add_argument("--dark-hard", type=float, default=d.dark_hard)
    a.add_argument("--contrast", type=float, default=d.contrast)
    a.add_argument("--noise", type=float, default=d.noise)
    a.add_argument("--aesthetic", type=float, default=d.aesthetic)
    add_signal_cli(a, d)

    o = p.add_argument_group("output")
    o.add_argument("-o", "--output", default="photo_quality_report.csv",
                   help="CSV report path")
    o.add_argument("--cache", default=".photo_quality_cache.sqlite")
    o.add_argument("--no-cache", action="store_true")
    o.add_argument("--no-albums", action="store_true",
                   help="don't create Photos review albums (report only)")
    o.add_argument("--albums-by", choices=("tier", "signal"), default="tier",
                   help="how to group review albums. 'tier' (default): one "
                        "album per recommendation (Delete candidates / "
                        "Duplicates / Review). 'signal': one album per "
                        "detection signal (Blurry / Too dark / Noisy / ...), "
                        "plus a Duplicates album; a photo appears in every "
                        "album whose signal it triggered.")
    o.add_argument("--album-prefix", default="photocull",
                   help="value for {prefix} in --album-template (default 'photocull')")
    o.add_argument("--album-template", default="{prefix} {date} {tier}",
                   help="album title template; placeholders {prefix} {date} "
                        "{tier}. {tier} is required (it stays a fixed label: "
                        "Delete candidates / Duplicates / Review).")
    o.add_argument("--album-date-format", default="%Y-%m-%d-%H-%M",
                   help="strftime format for {date}; default gives e.g. "
                        "2026-06-17-15-19")
    o.add_argument("--replace-albums", action="store_true",
                   help="delete earlier albums whose title starts with the "
                        "prefix first (default: keep each session)")
    o.add_argument("--dry-run", action="store_true",
                   help="report what albums would be made, but don't modify Photos")
    return p


def main(argv=None) -> int:
    from pathlib import Path
    args = build_parser().parse_args(argv)
    t = Thresholds(blur=args.blur, blur_hard=args.blur_hard, dark=args.dark,
                   dark_hard=args.dark_hard, contrast=args.contrast,
                   noise=args.noise, aesthetic=args.aesthetic, face=args.face)

    try:
        enabled_signals = resolve_signals(args.signals, args.exclude_signals)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not enabled_signals:
        print("error: no detection signals enabled (check --signals / "
              "--exclude-signals).", file=sys.stderr)
        return 2

    st = ensure_authorized()
    if int(st) not in (3, 4):
        print(f"Photos access not granted ({_status_name(st)}). Run from the "
              "photocull app and click Allow.", file=sys.stderr)
        return 2

    if not args.no_albums and "{tier}" not in args.album_template:
        print("error: --album-template must include the {tier} placeholder.",
              file=sys.stderr)
        return 2

    result = enumerate_assets(args)
    if result is None:
        return 2
    total = result.count()
    print(f"[scan] {total} photo(s) match the subset.", file=sys.stderr)
    if total == 0:
        return 0

    use_vision = not args.no_vision
    want_faces = use_vision and not args.no_faces and ("face" in enabled_signals)
    want_fprint = args.dedupe and use_vision
    allow_network = not args.no_download

    if not use_vision:
        vis_on = [s for s in SIGNALS if s in enabled_signals
                  and SIGNALS[s].needs_vision]
        if vis_on:
            print(f"note: signal(s) {', '.join(vis_on)} need Vision; "
                  "they won't fire under --no-vision.", file=sys.stderr)

    source = PhotosSource(result, allow_network)
    cache = None if args.no_cache else Cache(Path(args.cache))
    opts = RunOptions(
        thresholds=t, enabled_signals=enabled_signals, use_vision=use_vision,
        want_faces=want_faces, want_fprint=want_fprint, workers=1,
        dedupe=args.dedupe, dedupe_threshold=args.dedupe_threshold,
    )
    results, dd = run_pipeline(source, opts, cache)
    if cache is not None:
        cache.close()

    sinks: list[Sink] = [CsvReportSink(Path(args.output)), ConsoleSummarySink()]
    if not args.no_albums:
        sinks.append(PhotosAlbumsSink(args, enabled_signals))
    for sink in sinks:
        sink.emit(results, dd, source)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
