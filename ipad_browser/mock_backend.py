"""Demo 'device' backed by a local folder.

Lets you run and test the whole app (including Finder drag & drop) without a
physically connected iPad:  python main.py --demo
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import BinaryIO

from .backend import (BackendError, FileBackend, FileEntry, NotFoundError,
                      join_path, norm_path)

DEMO_ROOT = Path.home() / ".ipad-file-browser-demo"

# Folder/file skeleton created on first demo run so the browser is not empty.
SEED = {
    "DCIM/100APPLE": ["IMG_0001.HEIC", "IMG_0002.HEIC", "IMG_0003.MOV"],
    "Documents": ["Notes.txt", "Welcome.txt"],
    "Downloads": [],
    "Books": [],
    "Recordings": [],
}


class MockBackend(FileBackend):
    display_name = "Demo device (local folder)"

    def __init__(self, root: "os.PathLike[str] | str | None" = None,
                 seed: bool = False):
        self.root = Path(root) if root else DEMO_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        if seed:
            for folder, files in SEED.items():
                (self.root / folder).mkdir(parents=True, exist_ok=True)
                for name in files:
                    p = self.root / folder / name
                    if not p.exists():
                        p.write_text(f"Demo content of {name}\n" * 200)

    # -- path safety ------------------------------------------------------
    def _real(self, path: str) -> Path:
        real = (self.root / norm_path(path).lstrip("/")).resolve()
        root_resolved = self.root.resolve()
        if real != root_resolved and root_resolved not in real.parents:
            raise BackendError(f"path escapes root: {path}")
        return real

    def _entry(self, real: Path, path: str) -> FileEntry:
        st = real.stat()
        return FileEntry(
            path=norm_path(path),
            name="/" if norm_path(path) == "/" else real.name,
            is_dir=real.is_dir(),
            size=0 if real.is_dir() else st.st_size,
            mtime=st.st_mtime,
        )

    # -- FileBackend API --------------------------------------------------
    def listdir(self, path: str) -> list[FileEntry]:
        real = self._real(path)
        if not real.is_dir():
            raise NotFoundError(path)
        return [
            self._entry(child, join_path(path, child.name))
            for child in sorted(real.iterdir(),
                                key=lambda p: (not p.is_dir(), p.name.lower()))
        ]

    def stat(self, path: str) -> FileEntry:
        real = self._real(path)
        if not real.exists():
            raise NotFoundError(path)
        return self._entry(real, path)

    def mkdir(self, path: str) -> None:
        self._real(path).mkdir(parents=True, exist_ok=True)

    def remove(self, path: str) -> None:
        real = self._real(path)
        if not real.exists():
            raise NotFoundError(path)
        shutil.rmtree(real) if real.is_dir() else real.unlink()

    def rename(self, src: str, dst: str) -> None:
        s, d = self._real(src), self._real(dst)
        if not s.exists():
            raise NotFoundError(src)
        d.parent.mkdir(parents=True, exist_ok=True)
        s.rename(d)

    def open_read(self, path: str) -> BinaryIO:
        real = self._real(path)
        if not real.is_file():
            raise NotFoundError(path)
        return open(real, "rb")

    def open_write(self, path: str) -> BinaryIO:
        real = self._real(path)
        real.parent.mkdir(parents=True, exist_ok=True)
        return open(real, "wb")
