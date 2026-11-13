"""Entry point.

Usage:
    python main.py                # auto-detect a connected iPhone or iPad
    python main.py --demo         # browse a local demo folder (no device needed)
    python main.py --tunnel HOST:PORT   # connect via pymobiledevice3 tunnel
    python main.py --selftest     # start and quit after 2s (CI smoke test)
"""
from __future__ import annotations

import argparse
import sys


def _diag() -> int:
    """No-GUI diagnostics: find devices, connect, list file-sharing apps."""
    import traceback

    from ipad_browser.devices import connect, list_devices

    devices = list_devices()
    print("DEVICES:", devices)
    if not devices:
        return 1
    usb = [d for d in devices if d.connection_type.lower() == "usb"] or devices
    try:
        backend = connect(usb[0].serial)
    except Exception:
        traceback.print_exc()
        return 2
    print("BACKEND:", backend.display_name)
    try:
        apps = backend.list_file_sharing_apps()
        print(f"APPS ({len(apps)}):")
        for bid, name in apps:
            print("  -", name, bid)
    except Exception:
        traceback.print_exc()
        backend.close()
        return 3
    backend.close()
    print("DIAG_OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Browse & manage files on a connected iPhone or iPad.")
    parser.add_argument("--demo", action="store_true",
                        help="use a local demo folder instead of a real device")
    parser.add_argument("--tunnel", metavar="HOST:PORT",
                        help="connect through a pymobiledevice3 tunnel (iOS 17+)")
    parser.add_argument("--selftest", action="store_true",
                        help="start the app and quit automatically after 2 seconds")
    parser.add_argument("--build-info", action="store_true",
                        help="print version/build info and exit (no GUI)")
    parser.add_argument("--diag", action="store_true",
                        help="connect to a device, print diagnostics, exit (no GUI)")
    args = parser.parse_args()

    if args.build_info:
        from ipad_browser import COMMIT, TIMESTAMP, __version__
        print(f"iDevice File Browser v{__version__} build {COMMIT} ({TIMESTAMP})")
        return 0

    if args.diag:
        return _diag()

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from ipad_browser.mainwindow import MainWindow

    app = QApplication(sys.argv[:1])  # argparse already consumed our flags
    app.setApplicationName("iDevice File Browser")
    app.setOrganizationName("ipad-file-browser")

    window = MainWindow(demo=args.demo, tunnel=args.tunnel)
    window.show()

    if args.selftest:
        QTimer.singleShot(2000, app.quit)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
