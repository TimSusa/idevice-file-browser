#!/bin/bash
# Build a fully standalone "iDevice File Browser.app" (PyInstaller: embeds
# Python, Qt and pymobiledevice3) and install it to /Applications.
#
# Fresh-build guarantees:
#   1. build/ and dist/ are deleted first and PyInstaller runs with --clean
#   2. the current git commit ("-dirty" if there are uncommitted changes) is
#      stamped into the bundle and shown in the app's status bar
#   3. after building, the bundle must report that exact commit back via
#      --build-info and boot successfully (--demo --selftest), otherwise the
#      install aborts
#
# Usage:  packaging/build_app.sh            (run from anywhere)
set -euo pipefail
cd "$(dirname "$0")/.."

APP_NAME="iDevice File Browser"
[ -x .venv/bin/python ] || { echo "run ./run.sh once first (creates .venv)"; exit 1; }
.venv/bin/pip install -q pyinstaller

# --- stamp build info (git commit + time) ------------------------------------
COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "nogit")
if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    COMMIT="$COMMIT-dirty"
fi
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf 'COMMIT = "%s"\nTIMESTAMP = "%s"\n' "$COMMIT" "$TIMESTAMP" > ipad_browser/_build_info.py
echo "==> building commit $COMMIT ($TIMESTAMP)"

# --- icon --------------------------------------------------------------------
if [ ! -f packaging/AppIcon.icns ]; then
    echo "generating icon..."
    QT_QPA_PLATFORM=offscreen .venv/bin/python packaging/make_icon.py /tmp/idevice_icon_1024.png
    ICONSET=/tmp/idevice.iconset
    rm -rf "$ICONSET" && mkdir -p "$ICONSET"
    for spec in '16 icon_16x16' '32 icon_16x16@2x' '32 icon_32x32' \
                '64 icon_32x32@2x' '128 icon_128x128' '256 icon_128x128@2x' \
                '256 icon_256x256' '512 icon_256x256@2x' '512 icon_512x512'; do
        set -- $spec
        sips -z "$1" "$1" /tmp/idevice_icon_1024.png --out "$ICONSET/$2.png" > /dev/null
    done
    cp /tmp/idevice_icon_1024.png "$ICONSET/icon_512x512@2x.png"
    iconutil -c icns "$ICONSET" -o packaging/AppIcon.icns
fi

# --- clean build --------------------------------------------------------------
rm -rf build dist
.venv/bin/pyinstaller --clean --noconfirm --windowed --name "$APP_NAME" \
    --icon packaging/AppIcon.icns \
    --osx-bundle-identifier com.timsusa.idevice-file-browser \
    --collect-all pymobiledevice3 --collect-all ipsw_parser \
    --collect-all pyimg4 --collect-all developer_disk_image \
    --copy-metadata pymobiledevice3 --copy-metadata ipsw_parser \
    --copy-metadata pyimg4 \
    main.py

# --- verify: bundle must report back this exact commit ------------------------
EXE="dist/$APP_NAME.app/Contents/MacOS/$APP_NAME"
REPORTED=$("$EXE" --build-info)
echo "==> bundle reports: $REPORTED"
echo "$REPORTED" | grep -q "$COMMIT" || {
    echo "ERROR: bundle does NOT contain commit $COMMIT — aborting install!"
    exit 1
}

# --- verify: bundle boots -----------------------------------------------------
"$EXE" --demo --selftest

# --- install -----------------------------------------------------------------
rm -rf "/Applications/$APP_NAME.app"
cp -R "dist/$APP_NAME.app" /Applications/
echo "==> Installed: /Applications/$APP_NAME.app"
echo "==> $REPORTED"
