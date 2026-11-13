#!/bin/bash
# Build a fully standalone "iDevice File Browser.app" (PyInstaller: embeds
# Python, Qt and pymobiledevice3) and install it to /Applications.
#
# Usage:  packaging/build_app.sh            (run from anywhere)
set -euo pipefail
cd "$(dirname "$0")/.."

[ -x .venv/bin/python ] || { echo "run ./run.sh once first (creates .venv)"; exit 1; }
.venv/bin/pip install -q pyinstaller

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

# --- build -------------------------------------------------------------------
.venv/bin/pyinstaller --noconfirm --windowed --name "iDevice File Browser" \
    --icon packaging/AppIcon.icns \
    --osx-bundle-identifier com.timsusa.idevice-file-browser \
    --collect-all pymobiledevice3 --collect-all ipsw_parser \
    --collect-all pyimg4 --collect-all developer_disk_image \
    --copy-metadata pymobiledevice3 --copy-metadata ipsw_parser \
    --copy-metadata pyimg4 \
    main.py

# --- install -----------------------------------------------------------------
rm -rf "/Applications/iDevice File Browser.app"
cp -R "dist/iDevice File Browser.app" /Applications/
echo "Installed: /Applications/iDevice File Browser.app"
