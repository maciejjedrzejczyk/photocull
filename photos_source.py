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

    o = p.add_argument_group("output")
    o.add_argument("-o", "--output", default="photo_quality_report.csv",
                   help="CSV report path")
    o.add_argument("--cache", default=".photo_quality_cache.sqlite")
    o.add_argument("--no-cache", action="store_true")
    o.add_argument("--no-albums", action="store_true",
                   help="don't create Photos review albums (report only)")
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
                   noise=args.noise, aesthetic=args.aesthetic)

    st = ensure_authorized()
    if int(st) not in (3, 4):
        print(f"Photos access not granted ({_status_name(st)}). Run from the "
              "photocull app and click Allow.", file=sys.stderr)
        return 2

    result = enumerate_assets(args)
    if result is None:
        return 2
    total = result.count()
    print(f"[scan] {total} photo(s) match the subset.", file=sys.stderr)
    if total == 0:
        return 0

    use_vision = not args.no_vision
    want_faces = use_vision and not args.no_faces
    want_fprint = args.dedupe and use_vision
    allow_network = not args.no_download

    cache = None if args.no_cache else Cache(Path(args.cache))

    results: list[Metrics] = []
    asset_by_id: dict[str, object] = {}
    fresh_for_cache: list[tuple] = []   # (metrics, aid, size, mtime)
    t0 = time.time()
    for i in range(total):
        asset = result.objectAtIndex_(i)
        aid = asset.localIdentifier()
        asset_by_id[aid] = asset
        # Cache key: localIdentifier + modificationDate (+ pixel count as size).
        mod = asset.modificationDate()
        mtime = int(mod.timeIntervalSince1970() * 1e6) if mod else 0
        size = int(asset.pixelWidth()) * int(asset.pixelHeight())
        m = cache.get(aid, size, mtime, want_fprint) if cache else None
        if m is not None:
            m.asset_id = aid
            m.path = _filename(asset)
        else:
            m = analyse_asset(asset, use_vision, want_faces, want_fprint, allow_network)
            if cache and not m.error:
                fresh_for_cache.append((m, aid, size, mtime))
        results.append(m)
        if (i + 1) % 50 == 0 or i + 1 == total:
            rate = (i + 1) / max(1e-6, time.time() - t0)
            print(f"  {i+1}/{total}  ({rate:.1f}/s)", file=sys.stderr)

    if cache:
        # Cache.put_many keys rows on m.path; temporarily set it to the asset id
        # so the row is keyed on the (stable) localIdentifier, then restore.
        stat_map = {}
        saved = []
        for m, aid, size, mtime in fresh_for_cache:
            saved.append((m, m.path))
            m.path = aid
            stat_map[aid] = (size, mtime)
        cache.put_many([m for m, _, _, _ in fresh_for_cache], stat_map)
        for m, orig in saved:
            m.path = orig
        cache.close()

    for m in results:
        if m.recommendation != "error":
            decide(m, t)

    dd = None
    if args.dedupe:
        print("[dedupe] clustering near-duplicates...", file=sys.stderr)
        dd = cluster_near_duplicates(results, args.dedupe_threshold)

    order = {"delete": 0, "duplicate": 1, "review": 2, "keep": 3, "error": 4}
    results.sort(key=lambda m: (order.get(m.recommendation, 9), m.sharpness))
    write_csv(results, Path(args.output))
    print(f"\nWrote report: {args.output}", file=sys.stderr)
    print_summary(results, dd)

    if not args.no_albums:
        if "{tier}" not in args.album_template:
            print("error: --album-template must include the {tier} placeholder.",
                  file=sys.stderr)
            return 2
        try:
            session = datetime.now().strftime(args.album_date_format)
        except ValueError:
            session = datetime.now().strftime("%Y-%m-%d-%H-%M")
        try:
            titles = {tier: args.album_template.format(
                          prefix=args.album_prefix, date=session, tier=label)
                      for tier, label in TIER_LABELS.items()}
        except (KeyError, IndexError) as e:
            print(f"error: unknown placeholder in --album-template: {e} "
                  "(allowed: {prefix} {date} {tier})", file=sys.stderr)
            return 2
        tier_assets = {tier: [] for tier in TIER_LABELS}
        for m in sorted(results, key=lambda m: m.sharpness):
            if m.recommendation in tier_assets and m.asset_id in asset_by_id:
                tier_assets[m.recommendation].append(asset_by_id[m.asset_id])
        print("\nCurating Photos review albums (non-destructive)...",
              file=sys.stderr)
        curate_albums(tier_assets, titles, args.album_prefix,
                      args.dry_run, args.replace_albums)
        if not args.dry_run:
            example = next((titles[t] for t in TIER_LABELS if tier_assets[t]),
                           titles["review"])
            print(f"Open Photos.app -> e.g. '{example}' to review and delete "
                  "what you choose.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
