# iDevice File Browser

A macOS app to browse the file system of a connected **iPhone or iPad** — copy,
move, delete or drag & drop files, including drag & drop to and from **Finder
windows**.

Built with Python + PySide6 (Qt) + [pymobiledevice3](https://github.com/doronz88/pymobiledevice3)
(a pure-Python implementation of Apple's usbmux/lockdown/AFC protocols).
**No kernel extensions, no macFUSE, no jailbreak, no iTunes.**

## Features

- Browse the device's AFC-exposed file system (the *Media* partition: `DCIM`,
  `Books`, `Downloads`, `Recordings`, iTunes media, …) — works identically on
  iPhone and iPad (same usbmux/AFC protocol stack)
- **Browse app Documents folders** (iTunes File Sharing) — files you put there
  appear in the iOS **Files app** under *On My iPhone → \<App\>*
- Copy files device → Mac and Mac → device (toolbar buttons, dialogs, or drag & drop)
- Move/rename/delete directly on the device (drag onto folders, F2 to rename,
  Del to delete)
- Drag & drop **both directions**:
  - **Finder → app:** drop files/folders onto any folder to upload them
  - **app → Finder:** drag items into any Finder window (downloaded to a cache first)
- Background transfers with progress bars + cancel
- Double-click a file to download & open it with the default app
- Demo mode to try everything without a device
- iOS 17+ supported via a pymobiledevice3 tunnel

## Build from source

### Prerequisites

| Requirement | How to check / install |
|---|---|
| macOS (Apple Silicon or Intel) | tested on macOS 26 / Apple Silicon |
| Python **3.10+** | `python3 --version` — if missing: `brew install python@3.12` or install from [python.org](https://www.python.org/downloads/) |
| Xcode Command Line Tools (only if pip needs to compile something) | `xcode-select --install` |
| For real use: iPhone/iPad + USB cable | — |

You do **not** need: iTunes, macFUSE, libimobiledevice, a jailbreak, or an
Apple Developer account.

### 1. Clone

```bash
git clone https://github.com/TimSusa/idevice-file-browser.git
cd idevice-file-browser
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

This installs:

- **PySide6-Essentials** — the Qt6 GUI toolkit (large download, ~100 MB)
- **pymobiledevice3** — the iOS protocol stack (usbmux / lockdown / AFC)
- **pytest** — only needed to run the test suite

> All dependencies ship as pre-built wheels for macOS arm64/x86_64, so no
> compilation is normally required. If pip still tries to build something
> (e.g. `cryptography`), run `xcode-select --install` first.

### 3. Run

```bash
python main.py            # normal mode: auto-detects a connected device
python main.py --demo     # demo mode: browses a local folder, no device needed
```

Alternatively `./run.sh` performs steps 2+3 automatically (creates the venv on
first run). Any extra arguments are passed through, e.g. `./run.sh --demo`.

### 4. Connect your iPhone or iPad

1. Connect the device via USB.
2. Unlock it and keep it on the Home Screen.
3. Tap **"Trust This Computer"** when prompted (first time only).

The app polls for devices every 2 s and auto-connects when exactly one USB
device is found; otherwise pick it in the device combo box. Once connected, the
window title shows the real device, e.g. `Tim's iPhone (iPhone, iOS 18.1)`.

#### iOS 17+

Newer iOS versions may require a CoreDevice tunnel:

```bash
sudo pymobiledevice3 remote start-tunnel
# keep it running, note the printed address/port, then:
python main.py --tunnel <address>:<port>
```

### 5. Run the tests (optional)

```bash
# unit + smoke tests, no device needed, runs offscreen:
QT_QPA_PLATFORM=offscreen python -m pytest tests/ -q

# GUI smoke test: starts the app in demo mode and quits after 2 s:
python main.py --demo --selftest
```

## Install as a macOS app (optional)

### Standalone bundle (recommended)

Build a fully self-contained `/Applications/iDevice File Browser.app` with
PyInstaller — it embeds Python, Qt and pymobiledevice3, so it keeps working
even if the repo folder is moved or deleted:

```bash
packaging/build_app.sh     # builds dist/… and installs to /Applications
```

(The script also regenerates the icon via `packaging/make_icon.py` when
`packaging/AppIcon.icns` is missing.)

### Lightweight launcher (alternative)

Instead of a full bundle, a small launcher app in `/Applications` can simply
run `main.py` with the project's virtualenv — it always uses your current repo
checkout, but breaks if the folder moves:

```
/Applications/iDevice File Browser.app
└── Contents
    ├── Info.plist
    ├── MacOS/launcher       # bash: cd <repo> && exec .venv/bin/python main.py
    └── Resources/AppIcon.icns
```

## Usage

| Action             | How                                                            |
|--------------------|----------------------------------------------------------------|
| Browse             | expand folders in the tree; type a path in the path bar        |
| Upload files/dirs  | drag from Finder into a folder, or **Import…**                 |
| Download           | select → **Export…**, or drag into any Finder window           |
| Move on device     | drag items onto a folder                                       |
| Copy on device     | Option-drag (⌥) onto a folder                                  |
| Rename             | F2 / Enter or context menu                                     |
| Delete             | Del / ⌫ / ⌘⌫, toolbar or context menu (with confirmation)      |
| New folder         | toolbar or context menu                                        |
| Open a file        | double-click (downloads to a cache and opens it locally)       |
| Refresh            | context menu → Refresh                                         |

Notes:

- Dragging **out** of the app downloads the selection to
  `~/Library/Caches/iPadFileBrowser` first (a progress dialog shows for larger
  items); Finder then copies from there. Caches older than 24 h are removed
  automatically.
- On-device **copy** streams through the Mac (AFC has no server-side copy).
  **Move** is instant (a rename).

## Where did my transferred file go? (Media vs. App Documents)

The **Media (System)** location is the iOS media partition: `DCIM`, `Books`,
`Recordings`, iTunes media… Its `/Downloads` folder is an *internal* iTunes
Store area (that's why you only see `downloads.*.sqlitedb` files there) — it is
**not** the Downloads folder shown in the iOS Files app.

To transfer files so they appear in the iOS **Files app**
(*On My iPhone → \<App\>*):

1. Pick an app in the **location combo** (next to the device picker), e.g.
   `VLC (Documents)` — it lists all installed apps with iTunes File Sharing
   enabled (VLC, Documents by Readdle, Pages, GarageBand, …).
2. Copy/move files as usual (drag & drop or Import…).
3. On the device, open **Files → On My iPhone → the app's folder**.

> If the combo stays hidden, no file-sharing apps are installed — grab a free
> one from the App Store (e.g. VLC).

**Photos caveat:** copying image files into `/DCIM` does *not* add them to the
Photos app — the Photos library requires its own import (use iCloud Photos, or
copy into a file-sharing app such as VLC to view them there).

## What you can see (and what not)

- **Media (System)** — the device's Media partition (`/var/mobile/Media`):
  `DCIM` (photos & videos), `Books`, `Recordings`, iTunes media, …
- **\<App\> (Documents)** — the Documents folder of any app with iTunes File
  Sharing (via the `house_arrest` service); visible in the iOS Files app.

Not accessible without a jailbreak: full app sandboxes (only their Documents
folder is shared), system files, and iCloud Drive contents.

## Development

### Project layout

| File                            | Purpose                                                                 |
|---------------------------------|-------------------------------------------------------------------------|
| `ipad_browser/backend.py`       | file-system abstraction (`FileBackend`, `FileEntry`, streaming copy)    |
| `ipad_browser/afc_backend.py`   | real device via AFC; async pymobiledevice3 bridged onto a private loop thread |
| `ipad_browser/mock_backend.py`  | demo device backed by a local folder                                    |
| `ipad_browser/devices.py`       | usbmux discovery, pairing, tunnel connect (friendly error messages)     |
| `ipad_browser/model.py`         | lazy Qt tree model + drag & drop (Finder both ways)                     |
| `ipad_browser/transfers.py`     | QThread workers: pull/push/move/copy/delete with progress + cancel      |
| `ipad_browser/mainwindow.py`    | UI: toolbar, tree, transfers dock                                       |
| `main.py`                       | entry point (`--demo`, `--tunnel`, `--selftest`)                        |
| `tests/test_all.py`             | unit + smoke tests (no device required)                                 |

### Troubleshooting (device)

- **"Could not pair"** — unlock the device, replug the cable, tap *Trust*, retry.
- **"requires a CoreDevice tunnel"** — see *iOS 17+* above.
- **Device not listed** — check `system_profiler SPUSBDataType` shows the device;
  make sure no other tool (iMazing, Finder sync, …) holds the connection.

## License

MIT — see [LICENSE](LICENSE).
