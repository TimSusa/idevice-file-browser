"""Discovery of connected iPads and creation of AFC backends."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Optional


@dataclass
class DeviceInfo:
    serial: str
    label: str
    connection_type: str = ""


def list_devices() -> list[DeviceInfo]:
    """Return devices known to usbmuxd. Never raises (safe for polling)."""
    try:
        import asyncio

        from pymobiledevice3.usbmux import list_devices as _list
        devices = _list()
        if inspect.isawaitable(devices):  # pymobiledevice3 >= 11 is async
            devices = asyncio.run(devices)
    except Exception:
        return []
    out = []
    for dev in devices:
        serial = getattr(dev, "serial", None) or getattr(dev, "udid", None) or ""
        if not serial:
            continue
        conn = str(getattr(dev, "connection_type", "") or "")
        label = f"{serial[:8]}… ({conn})" if conn else serial
        out.append(DeviceInfo(serial=serial, label=label, connection_type=conn))
    return out


PAIRING_HELP = """Could not pair with the device.

1. Unlock the iPhone/iPad and keep it on the Home Screen.
2. Reconnect the USB cable.
3. Tap "Trust This Computer" when asked, then try again."""

TUNNEL_HELP = """This device requires a CoreDevice tunnel (typical on iOS 17+).

Run this in a Terminal and keep it open:
    sudo pymobiledevice3 remote start-tunnel

Then restart this app with the printed tunnel address, e.g.:
    python main.py --tunnel 192.0.2.1:49152"""

NO_DEVICE_HELP = ("No iPhone or iPad was found. Check the USB cable and make "
                  "sure the device is awake.")


APP_HELP = ("Could not open the app's Documents folder. Make sure the app is "
            "still installed and supports File Sharing (check in Finder: "
            "device → Files).")


def connect(serial: Optional[str] = None, tunnel: Optional[str] = None,
            bundle_id: Optional[str] = None, app_name: Optional[str] = None):
    """Create an AfcBackend for a device (or an app's Documents folder).

    Raises RuntimeError with an actionable, user-friendly message on failure.
    """
    from pymobiledevice3 import exceptions as exc

    from .afc_backend import AfcBackend

    try:
        return AfcBackend(serial=serial, tunnel=tunnel,
                          bundle_id=bundle_id, app_name=app_name)
    except exc.AppNotInstalledError as e:
        raise RuntimeError(f"{APP_HELP}\n\nDetails: {e}") from e
    except (exc.NoDeviceConnectedError, exc.DeviceNotFoundError,
            exc.ConnectionFailedError, exc.MuxException) as e:
        raise RuntimeError(f"{NO_DEVICE_HELP}\n\nDetails: {e}") from e
    except (exc.NotPairedError, exc.PairingError, exc.UserDeniedPairingError,
            exc.InvalidHostIDError, exc.FatalPairingError,
            exc.PairingDialogResponsePendingError, exc.PasswordRequiredError,
            exc.PasscodeRequiredError, exc.NotTrustedError) as e:
        raise RuntimeError(f"{PAIRING_HELP}\n\nDetails: {e}") from e
    except (exc.StartServiceError, exc.InvalidServiceError,
            exc.RSDRequiredError) as e:
        raise RuntimeError(f"{TUNNEL_HELP}\n\nDetails: {e}") from e
