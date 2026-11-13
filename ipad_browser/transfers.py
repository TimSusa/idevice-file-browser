"""Background file operations (pull/push/delete/move/copy) with progress + cancel.

All workers are QThreads so the UI stays responsive. Progress is reported via
signals; cancellation is best-effort and checked between chunks/items.
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .backend import (CancelledError, FileBackend, FileEntry, join_path,
                      stream_copy)


class Worker(QThread):
    """Base class for cancellable background operations."""

    progressed = Signal(int, int)  # bytes/items done, total (>= 1)
    status = Signal(str)           # description of the current item
    failed = Signal(str)           # traceback/message on error
    succeeded = Signal()

    def __init__(self, backend: FileBackend, parent=None):
        super().__init__(parent)
        self.backend = backend
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:  # QThread entry point
        try:
            self._work()
            if not self.cancelled:
                self.succeeded.emit()
        except CancelledError:
            pass
        except Exception:
            self.failed.emit(traceback.format_exc(limit=6))

    def _work(self) -> None:
        raise NotImplementedError

    # helpers --------------------------------------------------------------
    def _progress(self, done: int, total: int) -> bool:
        self.progressed.emit(done, max(total, 1))
        return not self.cancelled

    def _check_cancel(self) -> None:
        if self.cancelled:
            raise CancelledError()

    def _device_to_local(self, remote: str, local: Path,
                         done_base: int, total: int) -> int:
        self.status.emit(remote)
        local.parent.mkdir(parents=True, exist_ok=True)
        with self.backend.open_read(remote) as src, open(local, "wb") as dst:
            copied = stream_copy(
                src, dst, progress=lambda n: self._progress(done_base + n, total))
        return done_base + copied

    def _local_to_device(self, local: Path, remote: str,
                         done_base: int, total: int) -> int:
        self.status.emit(str(local))
        with open(local, "rb") as src, self.backend.open_write(remote) as dst:
            copied = stream_copy(
                src, dst, progress=lambda n: self._progress(done_base + n, total))
        return done_base + copied


class PullWorker(Worker):
    """Download a device file or directory tree to the Mac."""

    def __init__(self, backend: FileBackend, remote: str, local: Path, parent=None):
        super().__init__(backend, parent)
        self.remote = remote
        self.local = Path(local)

    def _work(self) -> None:
        entries = list(self.backend.walk(self.remote))
        files = [e for e in entries if not e.is_dir]
        total = max(sum(e.size for e in files), 1)
        root_is_dir = entries[0].is_dir
        done = 0
        for entry in entries:
            self._check_cancel()
            if root_is_dir:
                rel = entry.path[len(self.remote):].lstrip("/")
                target = self.local / rel if rel else self.local
            else:
                target = self.local
            if entry.is_dir:
                target.mkdir(parents=True, exist_ok=True)
            else:
                done = self._device_to_local(entry.path, target, done, total)


class PushWorker(Worker):
    """Upload a local file or directory tree onto the device."""

    def __init__(self, backend: FileBackend, local: Path, remote: str, parent=None):
        super().__init__(backend, parent)
        self.local = Path(local)
        self.remote = remote  # final destination path on the device

    def _work(self) -> None:
        if self.local.is_dir():
            files = [p for p in self.local.rglob("*") if p.is_file()]
            total = max(sum(p.stat().st_size for p in files), 1)
            self.backend.mkdir(self.remote)
            done = 0
            for p in sorted(self.local.rglob("*")):
                self._check_cancel()
                target = join_path(self.remote,
                                   p.relative_to(self.local).as_posix())
                if p.is_dir():
                    self.backend.mkdir(target)
                else:
                    done = self._local_to_device(p, target, done, total)
        else:
            size = self.local.stat().st_size
            self._local_to_device(self.local, self.remote, 0, max(size, 1))



class DeleteWorker(Worker):
    """Recursively delete device paths."""

    def __init__(self, backend: FileBackend, paths: list, parent=None):
        super().__init__(backend, parent)
        self.paths = list(paths)

    def _work(self) -> None:
        total = max(len(self.paths), 1)
        for i, path in enumerate(self.paths):
            self._check_cancel()
            self.status.emit(path)
            self.backend.remove(path)
            self._progress(i + 1, total)


class MoveWorker(Worker):
    """Move device paths into another device directory (server-side rename)."""

    def __init__(self, backend: FileBackend, paths: list, dst_dir: str, parent=None):
        super().__init__(backend, parent)
        self.paths = list(paths)
        self.dst_dir = dst_dir

    def _work(self) -> None:
        total = max(len(self.paths), 1)
        for i, path in enumerate(self.paths):
            self._check_cancel()
            name = path.rsplit("/", 1)[-1]
            target = join_path(self.dst_dir, name)
            if target != path:
                self.status.emit(f"{path} -> {target}")
                self.backend.rename(path, target)
            self._progress(i + 1, total)


class CopyWorker(Worker):
    """Copy device paths into another device directory.

    AFC has no server-side copy, so this streams through the backend.
    """

    def __init__(self, backend: FileBackend, paths: list, dst_dir: str, parent=None):
        super().__init__(backend, parent)
        self.paths = list(paths)
        self.dst_dir = dst_dir

    def _work(self) -> None:
        trees: list[list[FileEntry]] = [list(self.backend.walk(p))
                                        for p in self.paths]
        total = max(sum(e.size for t in trees for e in t if not e.is_dir), 1)
        done = 0
        for src_root, tree in zip(self.paths, trees):
            root_is_dir = tree[0].is_dir
            dst_root = join_path(self.dst_dir, src_root.rsplit("/", 1)[-1])
            for entry in tree:
                self._check_cancel()
                if root_is_dir:
                    rel = entry.path[len(src_root):].lstrip("/")
                    target = join_path(dst_root, rel) if rel else dst_root
                else:
                    target = dst_root
                if entry.is_dir:
                    self.backend.mkdir(target)
                else:
                    self.status.emit(entry.path)
                    with self.backend.open_read(entry.path) as src, \
                            self.backend.open_write(target) as dst:
                        done += stream_copy(
                            src, dst,
                            progress=lambda n: self._progress(done + n, total))
