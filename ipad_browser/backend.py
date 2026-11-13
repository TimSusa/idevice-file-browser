"""Backend abstraction for browsable file systems.

A backend represents something that looks like a file system - a connected
iPad (via AFC) or a local demo folder - and exposes the small set of
operations the UI needs. All paths are absolute and '/'-separated.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterator, Optional


@dataclass
class FileEntry:
    """A single file or directory on the device."""

    path: str           # absolute device path, '/'-separated
    name: str
    is_dir: bool
    size: int = 0       # bytes (0 for directories)
    mtime: float = 0.0  # seconds since epoch (0 = unknown)


class BackendError(Exception):
    """Any backend operation failure."""


class NotFoundError(BackendError):
    """The requested path does not exist."""


class CancelledError(Exception):
    """Raised inside workers when the user cancelled the operation."""


CHUNK = 256 * 1024


def norm_path(path: str) -> str:
    """Collapse duplicate slashes and resolve '.'/'..'. Result is absolute."""
    if not path:
        return "/"
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts)


def join_path(*parts: str) -> str:
    return norm_path("/".join(p for p in parts if p))


def parent_path(path: str) -> str:
    path = norm_path(path)
    if path == "/":
        return "/"
    head = path.rsplit("/", 1)[0]
    return head if head else "/"


class FileBackend(abc.ABC):
    """Synchronous file-system interface used by the UI.

    Implementations are called from worker threads; one operation at a time
    per backend instance is the assumed usage pattern.
    """

    display_name: str = "Device"

    @abc.abstractmethod
    def listdir(self, path: str) -> list[FileEntry]:
        """List the children of directory `path` (not recursive)."""

    @abc.abstractmethod
    def stat(self, path: str) -> FileEntry:
        """Return metadata for `path`; raise NotFoundError if missing."""

    @abc.abstractmethod
    def mkdir(self, path: str) -> None: ...

    @abc.abstractmethod
    def remove(self, path: str) -> None:
        """Remove a file or a directory recursively."""

    @abc.abstractmethod
    def rename(self, src: str, dst: str) -> None:
        """Move/rename `src` to `dst` (dst is the full new path)."""

    @abc.abstractmethod
    def open_read(self, path: str) -> BinaryIO: ...

    @abc.abstractmethod
    def open_write(self, path: str) -> BinaryIO:
        """Open for writing, creating/truncating the file."""

    # ---- convenience helpers shared by all backends ----

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except BackendError:
            return False

    def is_dir(self, path: str) -> bool:
        try:
            return self.stat(path).is_dir
        except BackendError:
            return False

    def walk(self, path: str) -> Iterator[FileEntry]:
        """Depth-first iteration; yields the root entry itself first."""
        root = self.stat(path)
        yield root
        if root.is_dir:
            for child in self.listdir(path):
                yield from self.walk(child.path)

    def close(self) -> None:
        """Release any resources. Default: nothing to do."""


def stream_copy(src: BinaryIO, dst: BinaryIO,
                progress: Optional[Callable[[int], bool]] = None) -> int:
    """Copy everything from `src` to `dst` in chunks.

    `progress(bytes_done_for_this_file)` may return False to cancel the copy.
    Returns the number of bytes copied.
    """
    done = 0
    while True:
        buf = src.read(CHUNK)
        if not buf:
            break
        dst.write(buf)
        done += len(buf)
        if progress is not None and not progress(done):
            raise CancelledError()
    return done
