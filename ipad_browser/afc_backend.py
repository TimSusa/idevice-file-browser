"""FileBackend implementation for a real iPhone or iPad via AFC (pymobiledevice3).

AFC (com.apple.afc) exposes the device's Media partition (/var/mobile/Media):
DCIM (photos/videos), Books, Recordings, Downloads, iTunes media, ...
It requires the device to be paired ("Trust This Computer").

pymobiledevice3's AfcService is fully async (it multiplexes concurrent
operations over one connection with a background reader task). We therefore
drive it on a dedicated event loop in its own thread; the synchronous backend
methods simply block on results via run_coroutine_threadsafe, which is safe to
call from any of our worker threads.
"""
from __future__ import annotations

import asyncio
import inspect
import stat as stat_mod
import threading
from typing import BinaryIO, Optional

from .backend import (BackendError, FileBackend, FileEntry, NotFoundError,
                      join_path, norm_path)

try:
    from pymobiledevice3.exceptions import AfcException, AfcFileNotFoundError
    from pymobiledevice3.services.afc import AfcService
    _IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover - pymobiledevice3 missing
    AfcException = AfcFileNotFoundError = Exception  # type: ignore
    AfcService = None  # type: ignore
    _IMPORT_ERROR = exc

_CHUNK = 256 * 1024

_PRETTY_CLASS = {"iphone": "iPhone", "ipad": "iPad", "ipod": "iPod",
                 "apple tv": "Apple TV", "apple watch": "Apple Watch",
                 "vision pro": "Vision Pro"}


async def _maybe_await(value):
    """Await `value` if it is awaitable (pymobiledevice3 >= 11), else return it."""
    if inspect.isawaitable(value):
        return await value
    return value


def _call_with_supported_kwargs(fn, **candidates):
    """Call fn with only the kwargs its signature actually accepts.

    Keeps us compatible across pymobiledevice3 versions with slightly
    different create_using_* signatures.
    """
    params = inspect.signature(fn).parameters
    kwargs = {k: v for k, v in candidates.items()
              if k in params and v is not None}
    return fn(**kwargs)


class _AfcFileIO:
    """File-object adapter over the AFC handle API (fopen/fread/fwrite/fclose).

    `run` is AfcBackend._run: it schedules a coroutine on the loop thread and
    blocks until the result is available.
    """

    def __init__(self, afc: "AfcService", run, handle: int):
        self._afc = afc
        self._run = run
        self._handle = handle
        self._closed = False

    def read(self, n: int = -1) -> bytes:
        if self._closed:
            raise ValueError("read on closed AFC file")
        if n is None or n < 0:
            chunks = []
            while True:
                buf = self._run(self._afc.fread(self._handle, _CHUNK))
                if not buf:
                    break
                chunks.append(buf)
            return b"".join(chunks)
        return self._run(self._afc.fread(self._handle, n)) or b""

    def write(self, data: bytes) -> int:
        if self._closed:
            raise ValueError("write on closed AFC file")
        self._run(self._afc.fwrite(self._handle, data))
        return len(data)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._run(self._afc.fclose(self._handle))
            except Exception:
                pass

    def __enter__(self) -> "_AfcFileIO":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


class AfcBackend(FileBackend):
    """Backend talking to a paired iPad over usbmux/AFC."""

    def __init__(self, serial: Optional[str] = None,
                 tunnel: Optional[str] = None,
                 service_name: Optional[str] = None,
                 display_name: Optional[str] = None,
                 bundle_id: Optional[str] = None,
                 app_name: Optional[str] = None):
        """Connect to a device by serial (usbmux) or tunnel ("host:port").

        With `bundle_id`, the backend is rooted at that app's Documents folder
        (house_arrest / iTunes File Sharing) instead of the Media partition.

        Raises pymobiledevice3 exceptions (pairing/connection/service errors);
        callers are expected to map them to user-friendly messages.
        """
        self.bundle_id = bundle_id
        # VendDocuments sessions are rooted at the app container but only allow
        # access below /Documents; map our "/" onto it (like the Files app).
        self._root_prefix = "/Documents" if bundle_id else "/"
        if AfcService is None:  # pragma: no cover
            raise BackendError(f"pymobiledevice3 unavailable: {_IMPORT_ERROR}")
        # Drive the whole async stack (lockdown client + AFC service) on a
        # private event loop in its own thread: create_using_* are coroutines
        # in pymobiledevice3 >= 11, and the AFC service multiplexes over a
        # background reader task. Everything must live on the same loop.
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, name="afc-event-loop", daemon=True)
        self._loop_thread.start()

        async def _open():
            from pymobiledevice3.lockdown import (create_using_tcp,
                                                  create_using_usbmux)
            if tunnel:
                host, _, port = tunnel.rpartition(":")
                lockdown = await _maybe_await(_call_with_supported_kwargs(
                    create_using_tcp, hostname=host,
                    port=int(port) if port else None,
                    autopair=True, keep_alive=True))
            else:
                lockdown = await _maybe_await(_call_with_supported_kwargs(
                    create_using_usbmux, serial=serial, autopair=True))
            if bundle_id:
                # house_arrest vends the app's sandbox and speaks AFC on the
                # same connection; create() connects + vends immediately.
                from pymobiledevice3.services.house_arrest import (
                    HouseArrestService)
                service = await HouseArrestService.create(
                    lockdown, bundle_id, documents_only=True)
            else:
                service = AfcService(lockdown, service_name=service_name)
                aenter = getattr(service, "__aenter__", None)
                if aenter is not None:
                    entered = await aenter()  # connect + start the demux reader
                    if entered is not None:
                        service = entered
            return lockdown, service

        self.lockdown, self.afc = self._submit(_open())
        if display_name is None:
            if bundle_id:
                display_name = f"{app_name or bundle_id} (Documents)"
            else:
                display_name = self._describe_lockdown(self.lockdown)
        self.display_name = str(display_name)

    # -- installed apps with iTunes File Sharing ------------------------------
    def list_file_sharing_apps(self) -> list:
        """Return [(bundle_id, display_name)] for apps that expose their
        Documents folder via iTunes File Sharing (visible in the Files app)."""
        return self._submit(self._list_file_sharing_apps())

    async def _list_file_sharing_apps(self):
        from pymobiledevice3.services.installation_proxy import (
            InstallationProxyService)
        proxy = InstallationProxyService(self.lockdown)
        try:
            apps = await proxy.get_apps(application_type="Any")
        finally:
            close = (getattr(proxy, "aclose", None)
                     or getattr(proxy, "close", None))
            if close is not None:
                try:
                    await _maybe_await(close())
                except Exception:
                    pass
        result = []
        for bundle_id, info in (apps or {}).items():
            if not info.get("UIFileSharingEnabled"):
                continue
            name = (info.get("CFBundleDisplayName")
                    or info.get("CFBundleName") or bundle_id)
            result.append((bundle_id, str(name)))
        result.sort(key=lambda item: item[1].lower())
        return result

    @staticmethod
    def _describe_lockdown(lockdown) -> str:
        """e.g. 'Tim's iPhone (iPhone, iOS 18.1)' — best effort, never raises."""
        def safe(name):
            try:
                return getattr(lockdown, name, None)
            except Exception:
                return None

        name = safe("display_name") or safe("udid") or "iOS Device"
        device_class = safe("device_class")
        if device_class is not None:
            raw = getattr(device_class, "value", None)      # unwrap enums
            if not isinstance(raw, str):
                raw = getattr(device_class, "name", None)
            if not isinstance(raw, str):
                raw = str(device_class)
            device_class = _PRETTY_CLASS.get(raw.lower(), raw)
        version = safe("product_version")
        details = ", ".join(filter(None, [
            str(device_class) if device_class else "",
            f"iOS {version}" if version else "",
        ])).strip()
        return f"{name} ({details})" if details else str(name)

    # -- asyncio bridge -------------------------------------------------------
    def _submit(self, coro):
        """Schedule a coroutine on the loop thread and block for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def _run(self, coro):
        """Like _submit, but translates AFC errors into BackendErrors."""
        try:
            return self._submit(coro)
        except AfcFileNotFoundError as exc:
            raise NotFoundError(str(exc)) from exc
        except AfcException as exc:
            raise BackendError(str(exc)) from exc

    @staticmethod
    def _is_dir_info(info: dict) -> bool:
        fmt = info.get("st_ifmt")
        if isinstance(fmt, str):
            return fmt == "S_IFDIR"
        try:
            return stat_mod.S_ISDIR(int(fmt))
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _to_epoch(value) -> float:
        """st_mtime is a datetime in pymobiledevice3 >= 11; stay tolerant."""
        if value is None:
            return 0.0
        if hasattr(value, "timestamp"):
            try:
                return float(value.timestamp())
            except Exception:
                return 0.0
        try:
            v = float(value)
        except (TypeError, ValueError):
            return 0.0
        return v / 1e9 if v > 1e12 else v  # nanoseconds vs seconds

    def _device_path(self, path: str) -> str:
        """Translate an app-facing path to the real device path (root prefix)."""
        if self._root_prefix == "/":
            return norm_path(path)
        return norm_path(self._root_prefix + norm_path(path))

    def _entry(self, path: str) -> FileEntry:
        path = norm_path(path)
        info = self._run(self.afc.stat(self._device_path(path)))
        is_dir = self._is_dir_info(info)
        return FileEntry(
            path=path,
            name="/" if path == "/" else path.rsplit("/", 1)[-1],
            is_dir=is_dir,
            size=0 if is_dir else int(info.get("st_size", 0) or 0),
            mtime=self._to_epoch(info.get("st_mtime")),
        )

    # -- FileBackend API ------------------------------------------------------
    def listdir(self, path: str) -> list[FileEntry]:
        names = self._run(self.afc.listdir(self._device_path(path)))
        entries = []
        for name in names:
            if name in (".", ".."):
                continue
            try:
                entries.append(self._entry(join_path(path, name)))
            except BackendError:
                continue  # skip entries we cannot stat (permissions, races)
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def stat(self, path: str) -> FileEntry:
        return self._entry(path)

    def mkdir(self, path: str) -> None:
        self._run(self.afc.makedirs(self._device_path(path)))

    def remove(self, path: str) -> None:
        self._run(self.afc.rm(self._device_path(path), force=True))

    def rename(self, src: str, dst: str) -> None:
        self._run(self.afc.rename(self._device_path(src),
                                  self._device_path(dst)))

    def open_read(self, path: str) -> BinaryIO:
        handle = self._run(self.afc.fopen(self._device_path(path), "r"))
        return _AfcFileIO(self.afc, self._run, handle)

    def open_write(self, path: str) -> BinaryIO:
        handle = self._run(self.afc.fopen(self._device_path(path), "w"))
        return _AfcFileIO(self.afc, self._run, handle)

    def close(self) -> None:
        service = getattr(self, "afc", None)
        if service is not None:
            try:
                if hasattr(service, "aclose"):
                    self._submit(service.aclose())
                elif hasattr(service, "__aexit__"):
                    self._submit(service.__aexit__(None, None, None))
            except Exception:
                pass
        lockdown = getattr(self, "lockdown", None)
        if lockdown is not None and hasattr(lockdown, "close"):
            try:
                self._submit(_maybe_await(lockdown.close()))
            except Exception:
                pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join(timeout=3)
        except Exception:
            pass
