#!/usr/bin/env bash
# Build a self-contained PhotoCull.app that can analyse the Apple Photos library.
#
# The app is needed because macOS only grants Photos access to a code-signed app
# bundle that carries an NSPhotoLibraryUsageDescription -- a bare script in
# Terminal cannot get it. The bundle embeds its own Python venv and code, so it
# reads nothing from your Documents/Downloads/Desktop; it only needs the Photos
# permission you grant on first launch.
#
# Usage:
#   ./build_photos_app.sh [OUTPUT_APP_PATH]
# Then MOVE the app out of Downloads/Desktop/Documents (e.g. to /Applications)
# and double-click it. On launch it asks you to choose a working folder (for
# photos.args, the report and the cache); you can also fix the folder with
#   open -a PhotoCull --args /path/to/folder      (or set $PHOTOCULL_HOME).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${1:-$HERE/dist/PhotoCull.app}"
RES="$APP/Contents/Resources"
MACOS="$APP/Contents/MacOS"

echo "Building $APP"
rm -rf "$APP"
mkdir -p "$MACOS" "$RES"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>PhotoCull</string>
  <key>CFBundleDisplayName</key><string>PhotoCull</string>
  <key>CFBundleIdentifier</key><string>com.photocull.app</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>photocull</string>
  <key>LSUIElement</key><true/>
  <key>NSPhotoLibraryUsageDescription</key>
  <string>PhotoCull analyses your photo library to flag low-quality and duplicate photos and gather them into review albums. It never deletes anything.</string>
</dict>
</plist>
PLIST

echo "Copying code..."
cp "$HERE/photo_quality.py" "$HERE/photos_source.py" "$RES/"

echo "Creating embedded venv + dependencies (this takes a minute)..."
python3 -m venv "$RES/venv"
"$RES/venv/bin/python" -m pip install --quiet --upgrade pip
"$RES/venv/bin/pip" install --quiet \
  pyobjc-framework-Photos pyobjc-framework-Vision pyobjc-framework-Quartz numpy

cat > "$MACOS/photocull" <<'LAUNCH'
#!/bin/bash
# Launcher: resolves a *working folder* (which holds photos.args, report.csv and
# the cache), runs the Photos analyser, and opens the run log when finished.
# Review albums appear in Photos.app.
#
# The working folder is chosen, in priority order:
#   1) the first launch argument:  open -a PhotoCull --args /path/to/folder
#   2) the $PHOTOCULL_HOME environment variable
#   3) an interactive "choose folder" prompt shown on launch (double-click)
#   4) the historical default ~/.photocull  (if the prompt is cancelled, or no
#      GUI is available, e.g. headless/automated runs)
set -u
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
STATE_DIR="$HOME/Library/Application Support/PhotoCull"
PTR="$STATE_DIR/last_home"
DEFAULT_HOME="$HOME/.photocull"

OUT=""
if [ "${1:-}" != "" ]; then
  OUT="$1"                                    # explicit path argument wins
elif [ "${PHOTOCULL_HOME:-}" != "" ]; then
  OUT="$PHOTOCULL_HOME"                        # environment override
else
  # No path supplied -> ask the user upon launch, starting at the last-used dir.
  START="$HOME"
  if [ -f "$PTR" ]; then
    LAST="$(cat "$PTR" 2>/dev/null || true)"
    [ -n "$LAST" ] && [ -d "$LAST" ] && START="$LAST"
  fi
  CHOSEN="$(osascript - "$START" 2>/dev/null <<'OSA'
on run argv
  try
    set startLoc to ((POSIX file (item 1 of argv)) as alias)
  on error
    set startLoc to (path to home folder)
  end try
  try
    set f to choose folder with prompt "Choose PhotoCull's working folder (holds photos.args, report.csv and the cache):" default location startLoc
    return POSIX path of f
  on error number -128
    return ""
  end try
end run
OSA
)"
  if [ -n "$CHOSEN" ]; then
    OUT="$CHOSEN"
  else
    OUT="$DEFAULT_HOME"                         # cancelled / headless -> default
  fi
fi

OUT="${OUT%/}"                                  # strip any trailing slash
[ -z "$OUT" ] && OUT="$DEFAULT_HOME"
mkdir -p "$OUT"
# Remember the choice so the next launch's picker opens here.
mkdir -p "$STATE_DIR" 2>/dev/null && printf '%s\n' "$OUT" > "$PTR" 2>/dev/null || true
CONF="$OUT/photos.args"
if [ ! -f "$CONF" ]; then
  cat > "$CONF" <<'DEFAULTS'
# photocull (Photos) scan options -- one per line, '#' comments allowed.
# Examples: --album "Iceland 2024" | --since 2022-01-01 --until 2022-12-31
--smart-album recently-added
--limit 1000
--dedupe
DEFAULTS
fi
ARGS="$(grep -v '^[[:space:]]*#' "$CONF" | tr '\n' ' ')"
{
  echo "PhotoCull run: $(date)"
  echo "options: $ARGS"
  echo
  "$RES/venv/bin/python" -u "$RES/photos_source.py" \
      -o "$OUT/report.csv" --cache "$OUT/cache.sqlite" $ARGS
  echo
  echo "exit: $?"
} > "$OUT/run.log" 2>&1
open -e "$OUT/run.log" 2>/dev/null || true
LAUNCH
chmod +x "$MACOS/photocull"

echo "Code-signing (ad-hoc)..."
codesign --force --deep --sign - --identifier com.photocull.app "$APP"

echo
echo "Built: $APP"
echo "Next:"
echo "  1) Move it OUT of Downloads/Desktop/Documents (e.g. to /Applications)."
echo "  2) Double-click it; click Allow on the Photos prompt (first run only)."
echo "  3) On each launch it asks for a working folder. (Skip the prompt with"
echo "     'open -a PhotoCull --args /path/to/folder' or by setting PHOTOCULL_HOME.)"
echo "  4) Edit photos.args inside that folder to choose what to scan, then run again."
echo "  Review albums appear in Photos.app, named with a 'photocull' prefix."
