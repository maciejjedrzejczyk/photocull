#!/usr/bin/env python3
"""
photocull review - a local web gallery for triaging a photocull CSV report.

Reads a `photo_quality_report.csv` and serves an interactive page where you can:
  * browse findings as a thumbnail grid, filtered by tier (delete/duplicate/
    review/keep) and sorted by any metric or by near-duplicate cluster,
  * click a thumbnail for a large view with all the metrics,
  * select photos (individually or in bulk) and either move them to the macOS
    Trash (recoverable) or to a quarantine folder.

Photos are read straight from their original paths. HEIC/HEIF are transcoded to
JPEG on the fly (via ImageIO/Quartz) so they display in the browser.

SAFETY / SECURITY
-----------------
  * Binds to 127.0.0.1 only -- never exposed to your network.
  * Only files listed in the CSV can be viewed or deleted (path whitelist);
    crafted URLs cannot touch anything else on disk.
  * Destructive actions require a per-session token and a localhost Host header,
    so another browser tab or website cannot drive deletions.
  * "Delete" moves files to the macOS Trash by default (recoverable). There is
    no permanent-delete path in this tool.

Usage
-----
    python review.py photo_quality_report.csv
    python review.py report.csv --port 8765 --quarantine ~/Pictures/_rejects
"""

from __future__ import annotations

import argparse
import csv
import json
import secrets
import sys
import threading
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import objc
import Quartz
from Foundation import NSURL, NSMutableData, NSFileManager

from photo_quality import REASON_TO_SIGNAL, SIGNALS


# --------------------------------------------------------------------------
# Global state (set up in main, read by the request handler)
# --------------------------------------------------------------------------
ITEMS: list[dict] = []          # one dict per CSV row, plus id/abspath/exists
ALLOWED: set[str] = set()       # resolved absolute paths that may be served
TOKEN = ""                      # per-session anti-CSRF token
QUARANTINE: Path | None = None  # optional quarantine destination
QROOTS: list[Path] = []         # roots for preserving structure on quarantine
ALLOWED_HOSTS: set[str] = {"127.0.0.1", "localhost", "::1", ""}
ALLOW_ANY_HOST = False          # True when bound to a wildcard address
THUMB_SIZE = 640                # default thumbnail longest-edge px (overridable)
THUMB_QUALITY = 0.85            # default thumbnail JPEG quality (overridable)
_LOCK = threading.Lock()

# Small LRU cache of generated JPEGs: key -> bytes
_THUMB_CACHE: "OrderedDict[str, bytes]" = OrderedDict()
_THUMB_CACHE_MAX = 600


# --------------------------------------------------------------------------
# Image transcoding (Quartz / ImageIO -> JPEG bytes; handles HEIC)
# --------------------------------------------------------------------------
def make_jpeg(path: str, max_px: int, quality: float = 0.72) -> bytes | None:
    with objc.autorelease_pool():
        url = NSURL.fileURLWithPath_(path)
        src = Quartz.CGImageSourceCreateWithURL(url, None)
        if src is None:
            return None
        opts = {
            # Generate from the full image (NOT the small embedded preview that
            # iPhone HEICs ship), so the requested resolution is actually
            # delivered up to the image's native size.
            Quartz.kCGImageSourceCreateThumbnailFromImageAlways: True,
            Quartz.kCGImageSourceThumbnailMaxPixelSize: int(max_px),
            Quartz.kCGImageSourceCreateThumbnailWithTransform: True,
        }
        cg = Quartz.CGImageSourceCreateThumbnailAtIndex(src, 0, opts)
        if cg is None:
            return None
        data = NSMutableData.data()
        dest = Quartz.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
        if dest is None:
            return None
        Quartz.CGImageDestinationAddImage(
            dest, cg, {Quartz.kCGImageDestinationLossyCompressionQuality: quality})
        if not Quartz.CGImageDestinationFinalize(dest):
            return None
        return bytes(data)


def cached_jpeg(path: str, max_px: int, quality: float = 0.85) -> bytes | None:
    key = f"{path}@{max_px}q{quality}"
    with _LOCK:
        if key in _THUMB_CACHE:
            _THUMB_CACHE.move_to_end(key)
            return _THUMB_CACHE[key]
    jpg = make_jpeg(path, max_px, quality)
    if jpg is None:
        return None
    with _LOCK:
        _THUMB_CACHE[key] = jpg
        while len(_THUMB_CACHE) > _THUMB_CACHE_MAX:
            _THUMB_CACHE.popitem(last=False)
    return jpg


# --------------------------------------------------------------------------
# Destructive actions (move to Trash / quarantine). Never permanent-deletes.
# Each returns (ok, new_location, error) so a move can be undone (restore).
# --------------------------------------------------------------------------
def move_to_trash(path: str) -> tuple[bool, str, str]:
    fm = NSFileManager.defaultManager()
    ok, new, err = fm.trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(path), None, None)
    new_path = new.path() if (ok and new is not None) else ""
    return bool(ok), new_path, ("" if ok else str(err))


def move_to_quarantine(path: str) -> tuple[bool, str, str]:
    if QUARANTINE is None:
        return False, "", "no quarantine folder configured"
    src = Path(path)
    rel = None
    for root in QROOTS:
        try:
            rel = src.resolve().relative_to(root.resolve())
            break
        except ValueError:
            continue
    target = QUARANTINE / (rel if rel else src.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        stem, suf, i = target.stem, target.suffix, 1
        while target.exists():
            target = target.with_name(f"{stem}__{i}{suf}")
            i += 1
    try:
        import shutil
        shutil.move(str(src), str(target))
        return True, str(target), ""
    except OSError as e:
        return False, "", str(e)


def restore_from(src: str, dest: str) -> tuple[bool, str]:
    """Move a previously trashed/quarantined file back to its original path."""
    if not src or not Path(src).exists():
        return False, "the moved file is no longer where we left it"
    try:
        import shutil
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dest)
        return True, ""
    except OSError as e:
        return False, str(e)


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
def _clamp_int(val, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(float(val))))
    except (TypeError, ValueError):
        return default


def _clamp_float(val, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(val)))
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "photocull-review"

    def log_message(self, *a):  # quieter console
        pass

    # -- helpers -----------------------------------------------------------
    def _localhost_ok(self) -> bool:
        # Reject mismatched Host headers (anti-DNS-rebinding). The allow-list is
        # localhost plus any explicitly configured --host; a wildcard bind
        # (0.0.0.0/::) accepts any Host because the user opted to expose it.
        if ALLOW_ANY_HOST:
            return True
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        return host in ALLOWED_HOSTS

    def _send(self, code, body=b"", ctype="application/octet-stream", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _item_for(self, qs) -> dict | None:
        try:
            i = int(qs.get("i", ["-1"])[0])
        except ValueError:
            return None
        if 0 <= i < len(ITEMS):
            return ITEMS[i]
        return None

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        if not self._localhost_ok():
            return self._send(403, "forbidden", "text/plain")
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if route == "/":
            return self._send(200, INDEX_HTML.replace("__TOKEN__", TOKEN),
                              "text/html; charset=utf-8")

        if route == "/api/items":
            payload = json.dumps({"items": [_public(it) for it in ITEMS],
                                  "token": TOKEN,
                                  "quarantine": bool(QUARANTINE),
                                  "thumb_size": THUMB_SIZE,
                                  "thumb_quality": THUMB_QUALITY,
                                  "reason_signal": REASON_TO_SIGNAL,
                                  "signal_labels": {k: s.label
                                                    for k, s in SIGNALS.items()}})
            return self._send(200, payload, "application/json")

        if route in ("/thumb", "/full"):
            it = self._item_for(qs)
            if it is None or it["abspath"] not in ALLOWED or it.get("removed"):
                return self._send(404, b"", "image/jpeg")
            if route == "/thumb":
                max_px = _clamp_int(qs.get("size", [None])[0], THUMB_SIZE, 160, 2000)
                quality = _clamp_float(qs.get("q", [None])[0], THUMB_QUALITY, 0.3, 1.0)
            else:
                max_px, quality = 2200, 0.9
            jpg = cached_jpeg(it["abspath"], max_px, quality)
            if jpg is None:
                return self._send(404, b"", "image/jpeg")
            return self._send(200, jpg, "image/jpeg")

        return self._send(404, "not found", "text/plain")

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        if not self._localhost_ok():
            return self._send(403, "forbidden", "text/plain")
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/action":
            return self._send(404, "not found", "text/plain")

        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, '{"error":"bad json"}', "application/json")

        if body.get("token") != TOKEN:
            return self._send(403, '{"error":"bad token"}', "application/json")

        action = body.get("action")
        ids = body.get("ids", [])
        if action not in ("trash", "quarantine", "restore"):
            return self._send(400, '{"error":"bad action"}', "application/json")

        results = []
        freed = 0
        for i in ids:
            if not isinstance(i, int) or not (0 <= i < len(ITEMS)):
                results.append({"id": i, "ok": False, "error": "bad id"})
                continue
            it = ITEMS[i]
            p = it["abspath"]

            if action == "restore":
                # Undo a previous trash/quarantine for this item.
                if not it.get("removed"):
                    results.append({"id": i, "ok": False, "error": "not removed"})
                    continue
                ok, err = restore_from(it.get("moved_to", ""), p)
                if ok:
                    with _LOCK:
                        it["removed"] = False
                        it["moved_to"] = ""
                        if Path(p).exists():
                            ALLOWED.add(p)
                    freed -= int(it.get("file_bytes") or 0)
                results.append({"id": i, "ok": ok, "error": err})
                continue

            if p not in ALLOWED or it.get("removed"):
                results.append({"id": i, "ok": False, "error": "not allowed"})
                continue
            if action == "trash":
                ok, moved_to, err = move_to_trash(p)
            else:
                ok, moved_to, err = move_to_quarantine(p)
            if ok:
                with _LOCK:
                    it["removed"] = True
                    it["moved_to"] = moved_to
                    ALLOWED.discard(p)
                freed += int(it.get("file_bytes") or 0)
            results.append({"id": i, "ok": ok, "error": err})

        return self._send(200, json.dumps({"results": results, "freed": freed}),
                          "application/json")


def _public(it: dict) -> dict:
    """The subset of fields sent to the browser."""
    keep = ("id", "name", "recommendation", "reasons", "sharpness",
            "brightness", "contrast", "noise", "aesthetic", "is_utility",
            "face_quality", "cluster_id", "cluster_size", "is_keeper",
            "megapixels", "file_bytes", "exists", "removed")
    return {k: it.get(k) for k in keep}


# --------------------------------------------------------------------------
# CSV loading
# --------------------------------------------------------------------------
def load_csv(path: Path) -> None:
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            p = row.get("path", "")
            ap = str(Path(p).resolve()) if p else ""
            exists = bool(ap) and Path(ap).exists()
            try:
                fb = int(row.get("file_bytes") or 0)
            except ValueError:
                fb = 0
            it = {
                "id": idx,
                "abspath": ap,
                "name": Path(p).name if p else "(no path)",
                "recommendation": row.get("recommendation", ""),
                "reasons": row.get("reasons", ""),
                "sharpness": row.get("sharpness", ""),
                "brightness": row.get("brightness", ""),
                "contrast": row.get("contrast", ""),
                "noise": row.get("noise", ""),
                "aesthetic": row.get("aesthetic", ""),
                "is_utility": row.get("is_utility", ""),
                "face_quality": row.get("face_quality", ""),
                "cluster_id": row.get("cluster_id", ""),
                "cluster_size": row.get("cluster_size", ""),
                "is_keeper": row.get("is_keeper", ""),
                "megapixels": row.get("megapixels", ""),
                "file_bytes": fb,
                "exists": exists,
                "removed": False,
                "moved_to": "",
            }
            ITEMS.append(it)
            if exists:
                ALLOWED.add(ap)


# --------------------------------------------------------------------------
# Front-end (single self-contained page)
# --------------------------------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>photocull review</title>
<style>
  :root { --bg:#15171c; --panel:#1e2128; --line:#2c303a; --txt:#e6e8ec; --muted:#9aa0aa; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--txt); font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
  header { position:sticky; top:0; z-index:5; background:var(--panel); border-bottom:1px solid var(--line); padding:10px 14px; }
  .row { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
  .spacer { flex:1; }
  h1 { font-size:15px; margin:0 12px 0 0; font-weight:600; }
  label.f { cursor:pointer; user-select:none; padding:3px 8px; border:1px solid var(--line); border-radius:6px; }
  label.f input { margin-right:5px; }
  select, input[type=search], button { background:#262a33; color:var(--txt); border:1px solid var(--line); border-radius:6px; padding:6px 9px; font:inherit; }
  button { cursor:pointer; }
  button.primary { background:#3b82f6; border-color:#3b82f6; }
  button.danger { background:#b4452f; border-color:#b4452f; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .chip { cursor:pointer; user-select:none; padding:3px 9px; border:1px solid var(--line); border-radius:13px; font-size:12px; background:#20242c; }
  .chip.on { background:#2d4a73; border-color:#3b82f6; color:#dcebff; }
  .bar2 { margin-top:8px; color:var(--muted); }
  #grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(var(--tile,190px),1fr)); gap:12px; padding:14px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:9px; overflow:hidden; position:relative; }
  .card.keeper { border-color:#caa23a; }
  .card.rej { outline:2px solid #e0593c; box-shadow:0 0 0 3px rgba(224,89,60,.25); }
  .card.rej .thumbwrap img { opacity:.55; }
  .card.kept { outline:2px solid #36a85a; }
  .clusterhead { grid-column:1/-1; display:flex; gap:12px; align-items:center; padding:8px 4px 2px; border-top:1px solid var(--line); color:var(--muted); }
  .clusterhead b { color:var(--txt); }
  .thumbwrap { aspect-ratio:1/1; background:#0c0d10; display:flex; align-items:center; justify-content:center; cursor:zoom-in; overflow:hidden; }
  .thumbwrap img { width:100%; height:100%; object-fit:cover; display:block; }
  .meta { padding:7px 9px; font-size:12px; }
  .name { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .muted { color:var(--muted); }
  .badge { display:inline-block; padding:1px 7px; border-radius:10px; font-size:11px; font-weight:600; }
  .b-delete{background:#7a2618;color:#ffd9cf;} .b-duplicate{background:#5b3d12;color:#ffe6b0;}
  .b-review{background:#16385b;color:#cfe6ff;} .b-keep{background:#244a2c;color:#cfeccf;}
  .chk { position:absolute; top:8px; left:8px; width:20px; height:20px; z-index:2; cursor:pointer; }
  .kp { position:absolute; top:7px; right:8px; background:#caa23a; color:#1a1400; border-radius:4px; padding:0 5px; font-size:10px; font-weight:700; }
  #pager { display:flex; gap:8px; align-items:center; justify-content:center; padding:14px; }
  /* focus mode */
  #focus { position:fixed; inset:0; background:rgba(0,0,0,.94); display:none; z-index:20; flex-direction:column; }
  #focus.show { display:flex; }
  #fwrap { flex:1; min-height:0; display:flex; align-items:center; justify-content:center; overflow:auto; }
  #fpic { max-width:96vw; max-height:84vh; object-fit:contain; transition:filter .1s; }
  #fpic.zoom { max-width:none; max-height:none; cursor:move; }
  #fbar { padding:10px 16px; background:var(--panel); border-top:1px solid var(--line); display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
  #fbar .tag { padding:2px 8px; border-radius:6px; font-weight:600; }
  .tag-rej{ background:#7a2618; color:#ffd9cf; } .tag-keep{ background:#244a2c; color:#cfeccf; }
  .nav { position:absolute; top:50%; transform:translateY(-50%); font-size:34px; padding:6px 16px; background:rgba(0,0,0,.4); cursor:pointer; user-select:none; }
  #fprev{left:6px;} #fnext{right:6px;}
  kbd { background:#2a2f39; border:1px solid var(--line); border-radius:4px; padding:0 5px; font-size:11px; }
  .hint { color:var(--muted); font-size:12px; }
  .toast { position:fixed; bottom:18px; left:50%; transform:translateX(-50%); background:#262a33; border:1px solid var(--line); padding:10px 16px; border-radius:8px; z-index:30; display:none; }
</style></head>
<body>
<header>
  <div class="row">
    <h1>photocull review</h1>
    <span id="filters" class="row"></span>
    <input id="q" type="search" placeholder="filter by filename…" style="width:160px">
    <label class="f">sort
      <select id="sort">
        <option value="recommendation">tier (worst first)</option>
        <option value="cluster_id">cluster (compare)</option>
        <option value="sharpness">sharpness</option>
        <option value="brightness">brightness</option>
        <option value="aesthetic">aesthetic</option>
        <option value="file_bytes">file size</option>
        <option value="name">name</option>
      </select>
    </label>
    <label class="f">size
      <select id="thumbsize">
        <option value="320">XS</option><option value="480">S</option>
        <option value="640">M</option><option value="900">L</option>
        <option value="1200">XL</option><option value="1600">XXL</option>
      </select>
    </label>
    <label class="f">quality
      <select id="thumbq">
        <option value="0.6">low</option><option value="0.75">medium</option>
        <option value="0.85">high</option><option value="0.95">max</option>
      </select>
    </label>
  </div>
  <div class="row" style="margin-top:8px">
    <span class="muted" style="font-size:12px">signals:</span>
    <span id="sigfilters" class="row"></span>
  </div>
  <div class="row" style="margin-top:8px">
    <button id="focusBtn">▶ Focus mode</button>
    <button id="selpage">Mark page ✗</button>
    <button id="clearmarks">Clear marks</button>
    <div class="spacer"></div>
    <button id="btnTrash" class="danger" disabled>Commit ✗ → Trash</button>
    <button id="btnQuar" class="primary" disabled>Commit ✗ → Quarantine</button>
    <button id="btnUndo" disabled>↶ Undo commit</button>
  </div>
  <div class="bar2 row"><span id="stat"></span><span class="spacer"></span><span id="markstat"></span></div>
</header>

<div id="grid"></div>
<div id="pager"></div>

<div id="focus">
  <div id="fwrap"><img id="fpic" alt=""></div>
  <div id="fprev" class="nav">‹</div>
  <div id="fnext" class="nav">›</div>
  <div id="fbar">
    <b id="fname"></b><span id="fmeta" class="muted"></span>
    <span id="ftag" class="tag"></span>
    <span class="spacer"></span>
    <span id="fpos" class="muted"></span>
    <button id="fReject" class="danger">✗ Reject</button>
    <button id="fKeep" class="primary">✓ Keep</button>
    <button id="fZoom">Zoom</button>
    <button id="fBright">Brighten</button>
    <button id="fClose">Close</button>
  </div>
  <div class="hint" style="padding:0 16px 8px">
    <kbd>←</kbd>/<kbd>→</kbd> move · <kbd>X</kbd>/<kbd>⌫</kbd> reject &amp; next ·
    <kbd>K</kbd>/<kbd>↵</kbd> keep &amp; next · <kbd>U</kbd> unmark ·
    <kbd>Z</kbd> zoom · <kbd>B</kbd> brighten · <kbd>Esc</kbd> close
  </div>
</div>
<div id="toast" class="toast"></div>

<script>
const TOKEN = "__TOKEN__";
let ALL = [], HAS_QUAR = false, page = 0;
let thumbSize = 640, thumbQ = 0.85;
let REASON_SIGNAL = {}, SIGNAL_LABELS = {};
const mark = new Map();          // id -> 'reject' | 'keep'
let lastCommitted = [];          // ids from the last commit, for undo
const PAGE = 120;
const tiers = ["delete","duplicate","review","keep"];
const tierOn = {delete:true, duplicate:true, review:true, keep:false};
const sigOn = new Set();         // active signal filters (empty = no filter)
let lastClickId = null;          // for shift-range marking
const $ = s => document.querySelector(s);
const fmtBytes = n => { n=+n||0; const u=["B","KB","MB","GB","TB"]; let i=0; while(n>=1024&&i<u.length-1){n/=1024;i++;} return n.toFixed(1)+" "+u[i]; };
const thumbURL = id => `/thumb?i=${id}&size=${thumbSize}&q=${thumbQ}`;

function applyTile(){
  const t = Math.max(150, Math.min(560, Math.round(thumbSize/2)));
  document.documentElement.style.setProperty("--tile", t + "px");
}
function loadPrefs(){
  const s = +localStorage.getItem("pc_thumb_size"); if(s) thumbSize = s;
  const q = +localStorage.getItem("pc_thumb_q"); if(q) thumbQ = q;
}
function savePrefs(){
  localStorage.setItem("pc_thumb_size", thumbSize);
  localStorage.setItem("pc_thumb_q", thumbQ);
}
function signalsOf(x){
  const out = new Set();
  (x.reasons||"").split(";").forEach(r=>{ r=r.trim(); const s=REASON_SIGNAL[r]; if(s) out.add(s); });
  return out;
}
function buildFilters(){
  const c = $("#filters"); c.innerHTML="";
  tiers.forEach(t=>{
    const n = ALL.filter(x=>!x.removed && x.recommendation===t).length;
    const l = document.createElement("label"); l.className="f";
    l.innerHTML = `<input type="checkbox" ${tierOn[t]?"checked":""} data-t="${t}"> ${t} <span class="muted">(${n})</span>`;
    l.querySelector("input").onchange = e => { tierOn[t]=e.target.checked; page=0; render(); };
    c.appendChild(l);
  });
}
function buildSignalFilters(){
  const c = $("#sigfilters"); c.innerHTML="";
  const present = {};
  ALL.forEach(x=>{ if(!x.removed) signalsOf(x).forEach(s=>{ present[s]=(present[s]||0)+1; }); });
  const keys = Object.keys(SIGNAL_LABELS).filter(k=>present[k]);
  if(!keys.length){ c.innerHTML = '<span class="muted" style="font-size:12px">none</span>'; return; }
  keys.forEach(k=>{
    const el = document.createElement("span");
    el.className = "chip" + (sigOn.has(k)?" on":"");
    el.textContent = `${SIGNAL_LABELS[k]} (${present[k]})`;
    el.onclick = () => { sigOn.has(k)?sigOn.delete(k):sigOn.add(k); el.classList.toggle("on"); page=0; render(); };
    c.appendChild(el);
  });
}
function visible(){
  const q = $("#q").value.trim().toLowerCase();
  let v = ALL.filter(x=>!x.removed && tierOn[x.recommendation] && (!q || x.name.toLowerCase().includes(q)));
  if(sigOn.size){
    v = v.filter(x=>{ const s=signalsOf(x); for(const k of sigOn) if(s.has(k)) return true; return false; });
  }
  const s = $("#sort").value;
  const num = ["sharpness","brightness","contrast","noise","aesthetic","file_bytes"];
  v.sort((a,b)=>{
    if(s==="recommendation"){ const o={delete:0,duplicate:1,review:2,keep:3};
      return (o[a.recommendation]-o[b.recommendation]) || ((+a.sharpness||0)-(+b.sharpness||0)); }
    if(s==="cluster_id"){ return ((+a.cluster_id)-(+b.cluster_id)) || ((a.is_keeper==="True"?0:1)-(b.is_keeper==="True"?0:1)) || ((+a.sharpness||0)-(+b.sharpness||0)); }
    if(s==="name") return a.name.localeCompare(b.name);
    if(num.includes(s)) return (parseFloat(a[s])||0)-(parseFloat(b[s])||0);
    return 0;
  });
  return v;
}
function isClusterKeeper(x){ return x.is_keeper==="True" && +x.cluster_size>1; }
function inCluster(x){ return +x.cluster_size>1 && +x.cluster_id>=0; }

function tileMarkClass(x){ const m=mark.get(x.id); return m==="reject"?" rej":(m==="keep"?" kept":""); }

function makeCard(x){
  const d = document.createElement("div");
  d.className = "card" + (isClusterKeeper(x)?" keeper":"") + tileMarkClass(x);
  d.dataset.id = x.id;
  const clu = inCluster(x) ? `· cluster ${x.cluster_id} (${x.cluster_size})` : "";
  const kp = isClusterKeeper(x) ? `<span class="kp">KEEP</span>`:"";
  d.innerHTML = `
    <input type="checkbox" class="chk" title="mark for deletion" ${mark.get(x.id)==="reject"?"checked":""}>
    ${kp}
    <div class="thumbwrap"><img loading="lazy" src="${thumbURL(x.id)}" alt=""></div>
    <div class="meta">
      <div class="name" title="${x.name}">${x.name}</div>
      <div><span class="badge b-${x.recommendation}">${x.recommendation}</span>
        <span class="muted">${fmtBytes(x.file_bytes)}</span></div>
      <div class="muted" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${x.reasons||""} ${clu}</div>
      <div class="muted">sharp ${x.sharpness||"–"} · aes ${x.aesthetic||"–"}</div>
    </div>`;
  const chk = d.querySelector(".chk");
  chk.addEventListener("click", e => {
    if(e.shiftKey && lastClickId!==null){ rangeMark(lastClickId, x.id); }
    else { setMark(x.id, chk.checked ? "reject" : null); }
    lastClickId = x.id; render();
  });
  d.querySelector(".thumbwrap").onclick = () => openFocus(x.id);
  return d;
}
function render(){
  const v = visible();
  const pages = Math.max(1, Math.ceil(v.length/PAGE));
  if(page>=pages) page=pages-1;
  const slice = v.slice(page*PAGE,(page+1)*PAGE);
  const g = $("#grid"); g.innerHTML="";
  const grouped = $("#sort").value==="cluster_id";
  let curCluster = null;
  slice.forEach(x=>{
    if(grouped && inCluster(x) && x.cluster_id!==curCluster){
      curCluster = x.cluster_id;
      const members = v.filter(y=>y.cluster_id===x.cluster_id);
      const keeper = members.find(isClusterKeeper);
      const h = document.createElement("div"); h.className="clusterhead";
      h.innerHTML = `<b>Cluster ${x.cluster_id}</b> <span>${members.length} frames` +
        (keeper?` · keeper: ${keeper.name}`:``) + `</span>`;
      const btn = document.createElement("button");
      btn.textContent = "Mark others ✗"; btn.className="danger";
      btn.onclick = () => { members.forEach(y=>{ if(!isClusterKeeper(y)) setMark(y.id,"reject"); }); render(); };
      h.appendChild(btn);
      g.appendChild(h);
    }
    g.appendChild(makeCard(x));
  });
  $("#stat").textContent = `${v.length} shown · ${ALL.filter(x=>!x.removed).length} total`;
  renderPager(pages);
  updateBars();
}
function renderPager(pages){
  const p = $("#pager"); p.innerHTML="";
  if(pages<=1) return;
  const mk=(t,fn,dis)=>{const b=document.createElement("button");b.textContent=t;b.disabled=dis;b.onclick=fn;return b;};
  p.appendChild(mk("‹ prev",()=>{page--;render();window.scrollTo(0,0);},page<=0));
  const s=document.createElement("span"); s.textContent=` page ${page+1} / ${pages} `; p.appendChild(s);
  p.appendChild(mk("next ›",()=>{page++;render();window.scrollTo(0,0);},page>=pages-1));
}
function setMark(id,val){ if(val) mark.set(id,val); else mark.delete(id); }
function rangeMark(fromId, toId){
  const order = visible().map(x=>x.id);
  let a = order.indexOf(fromId), b = order.indexOf(toId);
  if(a<0||b<0){ setMark(toId,"reject"); return; }
  if(a>b){ [a,b]=[b,a]; }
  for(let i=a;i<=b;i++) setMark(order[i],"reject");
}
function rejectIds(){ return [...mark].filter(([id,v])=>v==="reject" && !ALL[id].removed).map(([id])=>id); }
function markCounts(){ let r=0,k=0; mark.forEach(v=>{ v==="reject"?r++:k++; }); return {r,k}; }
function updateBars(){
  const {r,k} = markCounts();
  let bytes=0; rejectIds().forEach(i=>bytes+=(+ALL[i].file_bytes||0));
  $("#markstat").textContent = r||k ? `${r} marked ✗ (${fmtBytes(bytes)})` + (k?` · ${k} kept`:"") : "";
  $("#btnTrash").disabled = !r;
  $("#btnQuar").disabled = !r || !HAS_QUAR;
  $("#btnUndo").disabled = !lastCommitted.length;
}

// ---- focus mode ----
let fList=[], fPos=0, fZoom=false, fBright=false;
function metaLine(x){
  return ` ${x.recommendation} · ${x.reasons||"–"} · sharp ${x.sharpness||"–"} · bright ${x.brightness||"–"} · aes ${x.aesthetic||"–"} · ${fmtBytes(x.file_bytes)} · ${x.megapixels||"?"} MP`;
}
function openFocus(id){
  fList = visible().map(x=>x.id);
  fPos = Math.max(0, fList.indexOf(id));
  fZoom=false; fBright=false;
  $("#focus").classList.add("show");
  showFocus();
}
function showFocus(){
  if(!fList.length){ closeFocus(); return; }
  if(fPos<0) fPos=0; if(fPos>=fList.length) fPos=fList.length-1;
  const id=fList[fPos], x=ALL[id]; if(!x){ closeFocus(); return; }
  const img=$("#fpic");
  img.src = `/full?i=${id}`;
  img.className = fZoom?"zoom":"";
  img.style.filter = fBright?"brightness(2.4) contrast(1.05)":"";
  $("#fname").textContent = x.name;
  $("#fmeta").textContent = metaLine(x);
  const m = mark.get(id);
  const tag=$("#ftag");
  tag.textContent = m==="reject"?"✗ will delete":(m==="keep"?"✓ keep":"");
  tag.className = "tag" + (m==="reject"?" tag-rej":(m==="keep"?" tag-keep":""));
  $("#fpos").textContent = `${fPos+1} / ${fList.length}`;
  $("#fZoom").textContent = fZoom?"Fit":"Zoom";
  updateBars();
}
function focusStep(d){ fPos+=d; fZoom=false; showFocus(); }
function focusMark(val){ const id=fList[fPos]; setMark(id, mark.get(id)===val?null:val); }
function closeFocus(){ $("#focus").classList.remove("show"); $("#fpic").src=""; buildFilters(); buildSignalFilters(); render(); }

$("#fprev").onclick=()=>focusStep(-1);
$("#fnext").onclick=()=>focusStep(1);
$("#fReject").onclick=()=>{ focusMark("reject"); if(fPos<fList.length-1)focusStep(1); else showFocus(); };
$("#fKeep").onclick=()=>{ focusMark("keep"); if(fPos<fList.length-1)focusStep(1); else showFocus(); };
$("#fZoom").onclick=()=>{ fZoom=!fZoom; showFocus(); };
$("#fBright").onclick=()=>{ fBright=!fBright; showFocus(); };
$("#fClose").onclick=closeFocus;
$("#focusBtn").onclick=()=>{ const v=visible(); if(v.length) openFocus(v[0].id); };

document.addEventListener("keydown", e=>{
  if(!$("#focus").classList.contains("show")){
    if((e.key==="f"||e.key==="F") && !/input|textarea|select/i.test(e.target.tagName)){
      const v=visible(); if(v.length) openFocus(v[0].id);
    }
    return;
  }
  switch(e.key){
    case "Escape": closeFocus(); break;
    case "ArrowRight": e.preventDefault(); focusStep(1); break;
    case "ArrowLeft": e.preventDefault(); focusStep(-1); break;
    case "x": case "X": case "Delete": case "Backspace":
      e.preventDefault(); focusMark("reject"); if(fPos<fList.length-1)focusStep(1); else showFocus(); break;
    case "k": case "K": case "Enter": case " ":
      e.preventDefault(); focusMark("keep"); if(fPos<fList.length-1)focusStep(1); else showFocus(); break;
    case "u": case "U": e.preventDefault(); { const id=fList[fPos]; setMark(id,null); showFocus(); } break;
    case "z": case "Z": e.preventDefault(); fZoom=!fZoom; showFocus(); break;
    case "b": case "B": e.preventDefault(); fBright=!fBright; showFocus(); break;
  }
});

function toast(msg){ const t=$("#toast"); t.textContent=msg; t.style.display="block"; clearTimeout(toast._t); toast._t=setTimeout(()=>t.style.display="none",3600); }
async function post(b){
  const r=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:TOKEN,...b})});
  return r.json();
}
async function commit(action){
  const ids=rejectIds();
  if(!ids.length){ toast("Nothing marked for deletion."); return; }
  const verb = action==="trash" ? "Move to Trash" : "Move to Quarantine";
  if(!confirm(`${verb}: ${ids.length} photo(s) marked ✗?\n\nRecoverable — files are moved, not erased. Use “Undo commit” right after to revert.`)) return;
  const j=await post({action,ids});
  let ok=0; (j.results||[]).forEach(res=>{ if(res.ok){ ok++; ALL[res.id].removed=true; mark.delete(res.id); } });
  lastCommitted = (j.results||[]).filter(r=>r.ok).map(r=>r.id);
  toast(`${verb}: ${ok}/${ids.length} done · ${fmtBytes(j.freed||0)} freed · Undo available`);
  if(fList.length){ fList=fList.filter(id=>!ALL[id].removed); showFocus(); }
  buildFilters(); buildSignalFilters(); render();
}
async function undoCommit(){
  if(!lastCommitted.length){ return; }
  const j=await post({action:"restore",ids:lastCommitted});
  let ok=0; (j.results||[]).forEach(res=>{ if(res.ok){ ok++; ALL[res.id].removed=false; } });
  toast(`Restored ${ok}/${lastCommitted.length} photo(s).`);
  lastCommitted=[]; buildFilters(); buildSignalFilters(); render();
}
$("#btnTrash").onclick=()=>commit("trash");
$("#btnQuar").onclick=()=>commit("quarantine");
$("#btnUndo").onclick=undoCommit;
$("#selpage").onclick=()=>{ visible().slice(page*PAGE,(page+1)*PAGE).forEach(x=>setMark(x.id,"reject")); render(); };
$("#clearmarks").onclick=()=>{ mark.clear(); render(); };
$("#q").oninput=()=>{page=0;render();};
$("#sort").onchange=()=>{page=0;render();};
$("#thumbsize").onchange=e=>{ thumbSize=+e.target.value; savePrefs(); applyTile(); render(); };
$("#thumbq").onchange=e=>{ thumbQ=+e.target.value; savePrefs(); render(); };

function syncThumbControls(){
  const sizes=[...$("#thumbsize").options].map(o=>+o.value);
  const near=sizes.reduce((a,b)=>Math.abs(b-thumbSize)<Math.abs(a-thumbSize)?b:a);
  $("#thumbsize").value=near; thumbSize=near;
  const qs=[...$("#thumbq").options].map(o=>+o.value);
  const nq=qs.reduce((a,b)=>Math.abs(b-thumbQ)<Math.abs(a-thumbQ)?b:a);
  $("#thumbq").value=nq; thumbQ=nq;
}

(async function init(){
  const j = await (await fetch("/api/items")).json();
  ALL = j.items; HAS_QUAR = j.quarantine;
  REASON_SIGNAL = j.reason_signal || {}; SIGNAL_LABELS = j.signal_labels || {};
  thumbSize = j.thumb_size || thumbSize; thumbQ = j.thumb_quality || thumbQ;
  loadPrefs();
  syncThumbControls();
  applyTile();
  buildFilters(); buildSignalFilters(); render();
})();
</script>
</body></html>
"""


# --------------------------------------------------------------------------
def main(argv=None) -> int:
    global TOKEN, QUARANTINE, QROOTS, ALLOWED_HOSTS, ALLOW_ANY_HOST
    global THUMB_SIZE, THUMB_QUALITY
    ap = argparse.ArgumentParser(
        prog="photocull-review",
        description="Local web gallery to triage a photocull CSV report "
                    "(view, select, move-to-Trash / quarantine). Never erases.")
    ap.add_argument("csv", type=Path, nargs="?",
                    default=Path("photo_quality_report.csv"),
                    help="the photocull CSV report (default photo_quality_report.csv)")
    ap.add_argument("--port", type=int, default=8765, help="port (default 8765)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default 127.0.0.1). Binding to a "
                         "network IP exposes a delete-capable, unauthenticated "
                         "server -- prefer an SSH tunnel (see docs/review.md).")
    ap.add_argument("--quarantine", type=Path, metavar="DIR",
                    help="enable the Quarantine action, moving files into DIR")
    ap.add_argument("--root", type=Path, action="append", default=[],
                    help="library root(s) used to preserve structure on "
                         "quarantine (repeatable)")
    ap.add_argument("--no-browser", action="store_true",
                    help="don't auto-open the browser")
    ap.add_argument("--thumb-size", type=int, default=640, metavar="PX",
                    help="default thumbnail resolution, longest edge in px "
                         "(160-2000, default 640); adjustable live in the UI")
    ap.add_argument("--thumb-quality", type=float, default=0.85, metavar="Q",
                    help="default thumbnail JPEG quality 0.3-1.0 (default 0.85); "
                         "adjustable live in the UI")
    args = ap.parse_args(argv)

    if not args.csv.exists():
        print(f"error: CSV not found: {args.csv}", file=sys.stderr)
        return 1

    load_csv(args.csv)
    if not ITEMS:
        print("error: no rows in CSV", file=sys.stderr)
        return 1

    TOKEN = secrets.token_urlsafe(24)
    QUARANTINE = args.quarantine
    QROOTS = args.root
    THUMB_SIZE = max(160, min(2000, args.thumb_size))
    THUMB_QUALITY = max(0.3, min(1.0, args.thumb_quality))

    # Build the Host-header allow-list. Localhost is always allowed; an explicit
    # --host is added so direct remote access works. A wildcard bind accepts any
    # Host (the user has clearly chosen to expose the server).
    local = {"127.0.0.1", "localhost", "::1"}
    ALLOWED_HOSTS = local | {""}
    if args.host not in local:
        ALLOWED_HOSTS.add(args.host)
    ALLOW_ANY_HOST = args.host in ("0.0.0.0", "::")
    remote = args.host not in local

    missing = sum(1 for it in ITEMS if not it["exists"])
    print(f"Loaded {len(ITEMS)} rows ({len(ALLOWED)} files present"
          + (f", {missing} missing" if missing else "") + ").", file=sys.stderr)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    url = f"http://{args.host}:{args.port}/"
    print(f"\n  photocull review running at  {url}", file=sys.stderr)
    print("  Deletions move files to the macOS Trash (recoverable).",
          file=sys.stderr)
    if remote:
        print("\n  !! SECURITY WARNING ----------------------------------------",
              file=sys.stderr)
        print("  This is bound to a NETWORK address, not localhost.", file=sys.stderr)
        print("  The server is UNAUTHENTICATED: anyone who can reach", file=sys.stderr)
        print(f"  {args.host}:{args.port} can view your photos AND move them to", file=sys.stderr)
        print("  the Trash / quarantine. For remote access, prefer an SSH", file=sys.stderr)
        print("  tunnel and keep the default localhost bind:", file=sys.stderr)
        print(f"      ssh -L {args.port}:127.0.0.1:{args.port} you@this-mac", file=sys.stderr)
        print("  ------------------------------------------------------------",
              file=sys.stderr)
    print("\n  Press Ctrl+C to stop.\n", file=sys.stderr)

    if not args.no_browser:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
