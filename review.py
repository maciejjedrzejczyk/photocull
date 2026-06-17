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
            Quartz.kCGImageSourceCreateThumbnailFromImageIfAbsent: True,
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
# --------------------------------------------------------------------------
def move_to_trash(path: str) -> tuple[bool, str]:
    fm = NSFileManager.defaultManager()
    ok, _new, err = fm.trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(path), None, None)
    return bool(ok), ("" if ok else str(err))


def move_to_quarantine(path: str) -> tuple[bool, str]:
    if QUARANTINE is None:
        return False, "no quarantine folder configured"
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
                                  "thumb_quality": THUMB_QUALITY})
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
        if action not in ("trash", "quarantine"):
            return self._send(400, '{"error":"bad action"}', "application/json")

        results = []
        freed = 0
        for i in ids:
            if not isinstance(i, int) or not (0 <= i < len(ITEMS)):
                results.append({"id": i, "ok": False, "error": "bad id"})
                continue
            it = ITEMS[i]
            p = it["abspath"]
            if p not in ALLOWED or it.get("removed"):
                results.append({"id": i, "ok": False, "error": "not allowed"})
                continue
            if action == "trash":
                ok, err = move_to_trash(p)
            else:
                ok, err = move_to_quarantine(p)
            if ok:
                with _LOCK:
                    it["removed"] = True
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
  .bar2 { margin-top:8px; color:var(--muted); }
  #grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(var(--tile,190px),1fr)); gap:12px; padding:14px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:9px; overflow:hidden; position:relative; }
  .card.keeper { border-color:#caa23a; }
  .card.sel { outline:2px solid #3b82f6; }
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
  /* lightbox */
  #lb { position:fixed; inset:0; background:rgba(0,0,0,.92); display:none; z-index:20; flex-direction:column; }
  #lb.show { display:flex; }
  #lbimg { flex:1; min-height:0; display:flex; align-items:center; justify-content:center; }
  #lbimg img { max-width:96vw; max-height:82vh; object-fit:contain; }
  #lbbar { padding:10px 16px; background:var(--panel); border-top:1px solid var(--line); display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
  .nav { position:absolute; top:50%; transform:translateY(-50%); font-size:34px; padding:6px 16px; background:rgba(0,0,0,.4); }
  #lbprev{left:6px;} #lbnext{right:6px;}
  .toast { position:fixed; bottom:18px; left:50%; transform:translateX(-50%); background:#262a33; border:1px solid var(--line); padding:10px 16px; border-radius:8px; z-index:30; display:none; }
</style></head>
<body>
<header>
  <div class="row">
    <h1>photocull review</h1>
    <span id="filters" class="row"></span>
    <input id="q" type="search" placeholder="filter by filename…" style="width:180px">
    <label class="f">sort
      <select id="sort">
        <option value="recommendation">tier</option>
        <option value="cluster_id">cluster</option>
        <option value="sharpness">sharpness</option>
        <option value="brightness">brightness</option>
        <option value="aesthetic">aesthetic</option>
        <option value="file_bytes">file size</option>
        <option value="name">name</option>
      </select>
    </label>
    <label class="f">size
      <select id="thumbsize">
        <option value="320">XS</option>
        <option value="480">S</option>
        <option value="640">M</option>
        <option value="900">L</option>
        <option value="1200">XL</option>
        <option value="1600">XXL</option>
      </select>
    </label>
    <label class="f">quality
      <select id="thumbq">
        <option value="0.6">low</option>
        <option value="0.75">medium</option>
        <option value="0.85">high</option>
        <option value="0.95">max</option>
      </select>
    </label>
    <div class="spacer"></div>
    <button id="selpage">Select page</button>
    <button id="selnone">Clear</button>
    <button id="btnTrash" class="danger" disabled>Move to Trash</button>
    <button id="btnQuar" class="primary" disabled>Quarantine</button>
  </div>
  <div class="bar2 row"><span id="stat"></span><span class="spacer"></span><span id="selstat"></span></div>
</header>

<div id="grid"></div>
<div id="pager"></div>

<div id="lb">
  <div id="lbimg"><img id="lbpic" alt=""></div>
  <div id="lbprev" class="nav" style="cursor:pointer">‹</div>
  <div id="lbnext" class="nav" style="cursor:pointer">›</div>
  <div id="lbbar">
    <b id="lbname"></b><span id="lbmeta" class="muted"></span>
    <span class="spacer"></span>
    <button id="lbtrash" class="danger">Trash this</button>
    <button id="lbclose">Close</button>
  </div>
</div>
<div id="toast" class="toast"></div>

<script>
const TOKEN = "__TOKEN__";
let ALL = [], HAS_QUAR = false, sel = new Set(), page = 0;
let thumbSize = 640, thumbQ = 0.85;
const PAGE = 120;
const tiers = ["delete","duplicate","review","keep"];
const tierOn = {delete:true, duplicate:true, review:true, keep:false};
const $ = s => document.querySelector(s);
const fmtBytes = n => { n=+n||0; const u=["B","KB","MB","GB","TB"]; let i=0; while(n>=1024&&i<u.length-1){n/=1024;i++;} return n.toFixed(1)+" "+u[i]; };
const thumbURL = id => `/thumb?i=${id}&size=${thumbSize}&q=${thumbQ}`;
function applyTile(){
  // Display tile ~ half the fetched resolution (retina-friendly), clamped.
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

function buildFilters(){
  const c = $("#filters"); c.innerHTML="";
  tiers.forEach(t=>{
    const n = ALL.filter(x=>x.recommendation===t).length;
    const l = document.createElement("label"); l.className="f";
    l.innerHTML = `<input type="checkbox" ${tierOn[t]?"checked":""} data-t="${t}"> ${t} <span class="muted">(${n})</span>`;
    l.querySelector("input").onchange = e => { tierOn[t]=e.target.checked; page=0; render(); };
    c.appendChild(l);
  });
}
function visible(){
  const q = $("#q").value.trim().toLowerCase();
  let v = ALL.filter(x=>!x.removed && tierOn[x.recommendation] && (!q || x.name.toLowerCase().includes(q)));
  const s = $("#sort").value;
  const num = ["sharpness","brightness","contrast","noise","aesthetic","file_bytes","cluster_id"];
  v.sort((a,b)=>{
    if(s==="recommendation"){ const o={delete:0,duplicate:1,review:2,keep:3};
      return (o[a.recommendation]-o[b.recommendation]) || ((+a.sharpness||0)-(+b.sharpness||0)); }
    if(s==="name") return a.name.localeCompare(b.name);
    if(num.includes(s)) return (parseFloat(a[s])||0)-(parseFloat(b[s])||0);
    return 0;
  });
  return v;
}
function render(){
  const v = visible();
  const pages = Math.max(1, Math.ceil(v.length/PAGE));
  if(page>=pages) page=pages-1;
  const slice = v.slice(page*PAGE,(page+1)*PAGE);
  const g = $("#grid"); g.innerHTML="";
  slice.forEach(x=>{
    const d = document.createElement("div");
    d.className = "card" + (x.is_keeper==="True"&&x.cluster_size&&+x.cluster_size>1?" keeper":"") + (sel.has(x.id)?" sel":"");
    const clu = (x.cluster_size && +x.cluster_size>1) ? `· cluster ${x.cluster_id} (${x.cluster_size})` : "";
    const kp = (x.is_keeper==="True"&&x.cluster_size&&+x.cluster_size>1) ? `<span class="kp">KEEP</span>`:"";
    d.innerHTML = `
      <input type="checkbox" class="chk" ${sel.has(x.id)?"checked":""}>
      ${kp}
      <div class="thumbwrap"><img loading="lazy" src="${thumbURL(x.id)}" alt=""></div>
      <div class="meta">
        <div class="name" title="${x.name}">${x.name}</div>
        <div><span class="badge b-${x.recommendation}">${x.recommendation}</span>
          <span class="muted">${fmtBytes(x.file_bytes)}</span></div>
        <div class="muted" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${x.reasons||""} ${clu}</div>
        <div class="muted">sharp ${x.sharpness||"–"} · aes ${x.aesthetic||"–"}</div>
      </div>`;
    d.querySelector(".chk").onchange = e => { e.target.checked?sel.add(x.id):sel.delete(x.id); d.classList.toggle("sel",e.target.checked); updateSel(); };
    d.querySelector(".thumbwrap").onclick = () => openLB(x.id);
    g.appendChild(d);
  });
  $("#stat").textContent = `${v.length} shown · ${ALL.filter(x=>!x.removed).length} total`;
  renderPager(pages);
  updateSel();
}
function renderPager(pages){
  const p = $("#pager"); p.innerHTML="";
  if(pages<=1) return;
  const mk=(t,fn,dis)=>{const b=document.createElement("button");b.textContent=t;b.disabled=dis;b.onclick=fn;return b;};
  p.appendChild(mk("‹ prev",()=>{page--;render();window.scrollTo(0,0);},page<=0));
  const s=document.createElement("span"); s.textContent=` page ${page+1} / ${pages} `; p.appendChild(s);
  p.appendChild(mk("next ›",()=>{page++;render();window.scrollTo(0,0);},page>=pages-1));
}
function updateSel(){
  const ids=[...sel]; let bytes=0;
  ids.forEach(i=>{const it=ALL[i]; if(it&&!it.removed) bytes+=(+it.file_bytes||0);});
  $("#selstat").textContent = ids.length ? `${ids.length} selected · ${fmtBytes(bytes)}` : "";
  $("#btnTrash").disabled = !ids.length;
  $("#btnQuar").disabled = !ids.length || !HAS_QUAR;
}
// lightbox
let lbList=[], lbPos=0;
function openLB(id){ lbList=visible().map(x=>x.id); lbPos=lbList.indexOf(id); showLB(); }
function showLB(){
  const id=lbList[lbPos]; const x=ALL[id]; if(!x) return;
  $("#lbpic").src = `/full?i=${id}`;
  $("#lbname").textContent = x.name;
  $("#lbmeta").textContent = ` ${x.recommendation} · ${x.reasons||""} · sharp ${x.sharpness||"–"} · bright ${x.brightness||"–"} · aes ${x.aesthetic||"–"} · ${fmtBytes(x.file_bytes)} · ${x.megapixels||"?"} MP`;
  $("#lb").classList.add("show");
}
function closeLB(){ $("#lb").classList.remove("show"); $("#lbpic").src=""; }
$("#lbclose").onclick=closeLB;
$("#lbprev").onclick=()=>{ if(lbPos>0){lbPos--;showLB();} };
$("#lbnext").onclick=()=>{ if(lbPos<lbList.length-1){lbPos++;showLB();} };
$("#lbtrash").onclick=async()=>{ const id=lbList[lbPos]; await act("trash",[id]); lbList.splice(lbPos,1); if(!lbList.length){closeLB();} else {if(lbPos>=lbList.length)lbPos--; showLB();} };
document.onkeydown=e=>{ if(!$("#lb").classList.contains("show"))return;
  if(e.key==="Escape")closeLB(); if(e.key==="ArrowLeft")$("#lbprev").click(); if(e.key==="ArrowRight")$("#lbnext").click(); };

function toast(msg){ const t=$("#toast"); t.textContent=msg; t.style.display="block"; setTimeout(()=>t.style.display="none",3000); }

async function act(action, ids){
  if(!ids.length) return;
  const verb = action==="trash" ? "Move to Trash" : "Move to Quarantine";
  if(ids.length>1 && !confirm(`${verb}: ${ids.length} photo(s)?\n\nThis is recoverable (files are moved, not erased).`)) return;
  const r = await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({token:TOKEN,action,ids})});
  const j = await r.json();
  let ok=0; (j.results||[]).forEach(res=>{ if(res.ok){ ok++; ALL[res.id].removed=true; sel.delete(res.id); } });
  toast(`${verb}: ${ok}/${ids.length} done · ${fmtBytes(j.freed||0)} freed`);
  buildFilters(); render();
}
$("#btnTrash").onclick=()=>act("trash",[...sel].filter(i=>!ALL[i].removed));
$("#btnQuar").onclick=()=>act("quarantine",[...sel].filter(i=>!ALL[i].removed));
$("#selpage").onclick=()=>{ visible().slice(page*PAGE,(page+1)*PAGE).forEach(x=>sel.add(x.id)); render(); };
$("#selnone").onclick=()=>{ sel.clear(); render(); };
$("#q").oninput=()=>{page=0;render();};
$("#sort").onchange=()=>{page=0;render();};
$("#thumbsize").onchange=e=>{ thumbSize=+e.target.value; savePrefs(); applyTile(); render(); };
$("#thumbq").onchange=e=>{ thumbQ=+e.target.value; savePrefs(); render(); };

function syncThumbControls(){
  // Snap the selects to the nearest available option for the current values.
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
  thumbSize = j.thumb_size || thumbSize; thumbQ = j.thumb_quality || thumbQ;
  loadPrefs();              // a saved preference overrides the server default
  syncThumbControls();
  applyTile();
  buildFilters(); render();
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
