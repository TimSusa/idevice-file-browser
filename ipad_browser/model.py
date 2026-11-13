"""Qt tree model exposing a FileBackend with lazy loading and drag & drop.

Drag & drop design:
  * internal drags carry MIME_PATHS (a JSON list of device paths)
  * drags out to Finder additionally carry file:// URLs; the model asks its
    `exporter` callback to download the selection to a local cache first
  * drops from Finder arrive as file:// URLs -> requestImport
  * drops of internal paths -> requestMove (or requestCopy with Copy action)

The model itself never performs file operations; it emits request* signals and
the MainWindow executes them (usually in a worker thread) and refreshes.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from PySide6.QtCore import (QAbstractItemModel, QDateTime, QLocale, QMimeData,
                            QModelIndex, Qt, Signal)
from PySide6.QtWidgets import QApplication, QStyle

from .backend import (FileBackend, FileEntry, join_path, norm_path,
                      parent_path)

MIME_PATHS = "application/x-ipad-browser-paths"
COL_NAME, COL_SIZE, COL_MTIME = 0, 1, 2


class _Node:
    __slots__ = ("entry", "parent", "children", "populated")

    def __init__(self, entry: FileEntry, parent: "Optional[_Node]"):
        self.entry = entry
        self.parent = parent
        self.children: list[_Node] = []
        self.populated = False  # children fetched at least once?


class DeviceTreeModel(QAbstractItemModel):
    requestMove = Signal(list, str)    # device paths, destination dir
    requestCopy = Signal(list, str)    # device paths, destination dir
    requestImport = Signal(list, str)  # local paths, destination dir
    backendError = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.backend: Optional[FileBackend] = None
        self._root: Optional[_Node] = None   # the "/" node
        self._top: list[_Node] = []          # children of the invisible root
        # callback(paths) -> list[QUrl]; downloads selection to a local cache
        self.exporter: Optional[Callable[[list], list]] = None
        self._sort_col = COL_NAME
        self._sort_desc = False

    # ------------------------------------------------------------------ setup
    def set_backend(self, backend: Optional[FileBackend]) -> None:
        self.beginResetModel()
        self.backend = backend
        if backend is not None:
            self._root = _Node(FileEntry("/", backend.display_name, True), None)
            self._top = [self._root]
        else:
            self._root = None
            self._top = []
        self.endResetModel()

    def entry_at(self, index: QModelIndex) -> Optional[FileEntry]:
        node = self._node(index)
        return node.entry if node else None

    # ------------------------------------------------------ index <-> node
    def _node(self, index: QModelIndex) -> Optional[_Node]:
        if not index.isValid():
            return None
        node = index.internalPointer()
        return node if isinstance(node, _Node) else None

    def _children(self, node: Optional[_Node]) -> list[_Node]:
        return self._top if node is None else node.children

    def index(self, row: int, column: int,
              parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        children = self._children(self._node(parent))
        if 0 <= row < len(children):
            return self.createIndex(row, column, children[row])
        return QModelIndex()

    def parent(self, index: QModelIndex) -> QModelIndex:
        node = self._node(index)
        if node is None or node.parent is None:
            return QModelIndex()
        siblings = self._children(node.parent.parent)
        try:
            row = siblings.index(node.parent)
        except ValueError:
            return QModelIndex()
        return self.createIndex(row, 0, node.parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        node = self._node(parent)
        if node is not None and not node.populated:
            return 0
        return len(self._children(node))

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 3

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        node = self._node(parent)
        if node is None:
            return bool(self._top)
        return node.entry.is_dir

    # ---------------------------------------------------------------- lazy load
    def canFetchMore(self, parent: QModelIndex) -> bool:
        node = self._node(parent)
        return bool(node and node.entry.is_dir and not node.populated)

    def fetchMore(self, parent: QModelIndex) -> None:
        node = self._node(parent)
        if node is None or not self.backend or node.populated:
            return
        try:
            entries = self.backend.listdir(node.entry.path)
        except Exception as exc:  # keep the UI alive, report once
            node.populated = True
            self.backendError.emit(f"Could not list {node.entry.path}:\n{exc}")
            return
        children = [_Node(e, node) for e in entries]
        self._sort(children)
        if children:
            self.beginInsertRows(parent, 0, len(children) - 1)
            node.children = children
            node.populated = True
            self.endInsertRows()
        else:
            node.children = []
            node.populated = True

    # --------------------------------------------------------------------- data
    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        node = self._node(index)
        if node is None:
            return None
        e = node.entry
        col = index.column()
        if role in (Qt.DisplayRole, Qt.EditRole):
            if col == COL_NAME:
                return e.name
            if role == Qt.EditRole:
                return None
            if col == COL_SIZE:
                return "" if e.is_dir else QLocale().formattedDataSize(e.size)
            if col == COL_MTIME:
                if not e.mtime:
                    return ""
                return QLocale().toString(
                    QDateTime.fromSecsSinceEpoch(int(e.mtime)),
                    QLocale.ShortFormat)
        elif role == Qt.DecorationRole and col == COL_NAME:
            style = QApplication.style()
            return style.standardIcon(
                QStyle.SP_DirIcon if e.is_dir else QStyle.SP_FileIcon)
        elif role == Qt.ToolTipRole:
            return e.path
        elif role == Qt.TextAlignmentRole and col == COL_SIZE:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def setData(self, index: QModelIndex, value, role: int = Qt.EditRole) -> bool:
        node = self._node(index)
        if (node is None or role != Qt.EditRole
                or index.column() != COL_NAME or not self.backend):
            return False
        new_name = str(value).strip()
        e = node.entry
        if not new_name or "/" in new_name or new_name in (".", "..") \
                or e.path == "/" or new_name == e.name:
            return False
        dst = join_path(parent_path(e.path), new_name)
        try:
            self.backend.rename(e.path, dst)
        except Exception as exc:
            self.backendError.emit(f"Could not rename {e.path}:\n{exc}")
            return False
        node.entry = FileEntry(dst, new_name, e.is_dir, e.size, e.mtime)
        self.dataChanged.emit(index, index, [Qt.DisplayRole])
        return True

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return ("Name", "Size", "Modified")[section]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        node = self._node(index)
        if node is None:
            return base | Qt.ItemIsDropEnabled  # empty area -> root
        if node.entry.path != "/":
            base |= Qt.ItemIsDragEnabled
        if node.entry.is_dir:
            base |= Qt.ItemIsDropEnabled
        if index.column() == COL_NAME and node.entry.path != "/":
            base |= Qt.ItemIsEditable
        return base

    # ------------------------------------------------------------------ sorting
    def sort(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder) -> None:
        self._sort_col = column
        self._sort_desc = order == Qt.DescendingOrder
        self.layoutAboutToBeChanged.emit()
        self._sort_recursive(self._top)
        self.layoutChanged.emit()

    def _sort_recursive(self, nodes: list) -> None:
        self._sort(nodes)
        for n in nodes:
            if n.populated:
                self._sort_recursive(n.children)

    def _sort(self, nodes: list) -> None:
        col = self._sort_col
        if col == COL_SIZE:
            key = lambda n: (not n.entry.is_dir, n.entry.size,
                             n.entry.name.lower())  # noqa: E731
        elif col == COL_MTIME:
            key = lambda n: (not n.entry.is_dir, n.entry.mtime,
                             n.entry.name.lower())  # noqa: E731
        else:
            key = lambda n: (not n.entry.is_dir, n.entry.name.lower())  # noqa: E731
        nodes.sort(key=key, reverse=self._sort_desc)

    # ------------------------------------------------------------------ refresh
    def index_for_path(self, path: str) -> QModelIndex:
        """Index for a path if it is already populated, else an invalid index."""
        if self._root is None:
            return QModelIndex()
        path = norm_path(path)
        if path == "/":
            return self.index(0, 0)
        node = self._root
        for part in [p for p in path.split("/") if p]:
            if not node.populated:
                return QModelIndex()
            node = next((c for c in node.children if c.entry.name == part), None)
            if node is None:
                return QModelIndex()
        siblings = self._children(node.parent)
        return self.createIndex(siblings.index(node), 0, node)

    def ensure_path(self, path: str) -> QModelIndex:
        """Like index_for_path, but fetches directories on the way down."""
        if self._root is None:
            return QModelIndex()
        path = norm_path(path)
        idx = self.index(0, 0)
        if path == "/":
            return idx
        for part in [p for p in path.split("/") if p]:
            if self.canFetchMore(idx):
                self.fetchMore(idx)
            node = self._node(idx)
            child = next((c for c in node.children if c.entry.name == part), None)
            if child is None:
                return QModelIndex()
            idx = self.index(node.children.index(child), 0, idx)
        return idx

    def refresh_path(self, path: str) -> None:
        """Discard and re-fetch a directory's children (keeps expansion state)."""
        idx = self.index_for_path(path)
        if not idx.isValid():
            return
        node = self._node(idx)
        if node is None:
            return
        if node.populated and node.children:
            self.beginRemoveRows(idx, 0, len(node.children) - 1)
            node.children = []
            self.endRemoveRows()
        node.populated = False
        self.fetchMore(idx)

    # --------------------------------------------------------------- drag & drop
    def supportedDragActions(self):
        return Qt.CopyAction | Qt.MoveAction

    def supportedDropActions(self):
        return Qt.CopyAction | Qt.MoveAction

    def mimeTypes(self) -> list:
        return [MIME_PATHS, "text/uri-list"]

    def mimeData(self, indexes) -> QMimeData:
        paths = sorted({
            node.entry.path
            for i in indexes if i.isValid() and i.column() == COL_NAME
            for node in [self._node(i)] if node is not None
        })
        mime = QMimeData()
        if not paths:
            return mime
        mime.setData(MIME_PATHS, json.dumps(paths).encode("utf-8"))
        if self.exporter is not None:
            try:
                urls = self.exporter(paths)  # downloads selection to a cache
            except Exception as exc:
                self.backendError.emit(f"Export for drag & drop failed:\n{exc}")
            else:
                if urls:
                    mime.setUrls(urls)
        return mime

    def canDropMimeData(self, data: QMimeData, action, row: int, column: int,
                        parent: QModelIndex) -> bool:
        if not (data.hasFormat(MIME_PATHS) or data.hasUrls()):
            return False
        return self._drop_dir(parent) is not None

    def dropMimeData(self, data: QMimeData, action, row: int, column: int,
                     parent: QModelIndex) -> bool:
        target = self._drop_dir(parent)
        if target is None:
            return False
        if data.hasFormat(MIME_PATHS):
            try:
                paths = json.loads(bytes(data.data(MIME_PATHS)).decode("utf-8"))
            except (ValueError, TypeError):
                return False
            # ignore invalid drops (root, folder into itself or its own child)
            paths = [p for p in paths if p and p != "/"
                     and target != p and not target.startswith(p + "/")]
            if not paths:
                return False
            if action == Qt.CopyAction:
                self.requestCopy.emit(paths, target)
            else:
                self.requestMove.emit(paths, target)
            return True
        if data.hasUrls():
            local = [u.toLocalFile() for u in data.urls() if u.isLocalFile()]
            if local:
                self.requestImport.emit(local, target)
                return True
        return False

    def _drop_dir(self, parent: QModelIndex) -> Optional[str]:
        node = self._node(parent)
        if node is None:
            return "/" if self._root is not None else None
        if node.entry.is_dir:
            return node.entry.path
        return parent_path(node.entry.path)
