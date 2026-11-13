"""Main window: device picker, file tree, transfers dock, drag & drop glue."""
from __future__ import annotations

import shutil
import time
import traceback
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QComboBox, QDockWidget, QFileDialog,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow,
                               QMenu, QMessageBox, QProgressBar,
                               QProgressDialog, QStyle, QToolBar, QToolButton,
                               QTreeView, QWidget)

from .backend import FileBackend, join_path, parent_path
from .mock_backend import MockBackend
from .model import DeviceTreeModel
from .transfers import (CopyWorker, DeleteWorker, MoveWorker, PullWorker,
                        PushWorker, Worker)

CACHE_DIR = Path.home() / "Library" / "Caches" / "iPadFileBrowser"


class ConnectWorker(QThread):
    """Connects to a device in the background (pairing can block the UI)."""

    connected = Signal(object)  # FileBackend
    failed = Signal(str)

    def __init__(self, serial: Optional[str], tunnel: Optional[str],
                 bundle_id: Optional[str] = None, app_name: Optional[str] = None,
                 parent=None):
        super().__init__(parent)
        self.serial = serial
        self.tunnel = tunnel
        self.bundle_id = bundle_id
        self.app_name = app_name

    def run(self) -> None:
        try:
            from .devices import connect
            backend = connect(self.serial, self.tunnel,
                              bundle_id=self.bundle_id, app_name=self.app_name)
        except Exception:
            self.failed.emit(traceback.format_exc(limit=4))
        else:
            self.connected.emit(backend)


class AppListWorker(QThread):
    """Lists installed apps with iTunes File Sharing in the background."""

    done = Signal(list)  # [(bundle_id, display_name)]
    failed = Signal(str)

    def __init__(self, backend, parent=None):
        super().__init__(parent)
        self.backend = backend

    def run(self) -> None:
        try:
            self.done.emit(self.backend.list_file_sharing_apps())
        except Exception:
            self.failed.emit(traceback.format_exc(limit=4))


class _TransferRow(QWidget):
    """One row in the Transfers dock: label + progress bar + cancel button."""

    def __init__(self, worker: Worker, title: str, parent=None):
        super().__init__(parent)
        self.worker = worker
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 2, 6, 2)
        self.label = QLabel(title)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.cancel = QToolButton()
        self.cancel.setText("✕")
        self.cancel.setToolTip("Cancel")
        self.cancel.clicked.connect(worker.cancel)
        lay.addWidget(self.label, 2)
        lay.addWidget(self.bar, 3)
        lay.addWidget(self.cancel)
        worker.progressed.connect(self._on_progress)
        worker.status.connect(self.label.setToolTip)

    def _on_progress(self, done: int, total: int) -> None:
        self.bar.setValue(min(100, int(done * 100 / max(total, 1))))

    def finish(self, ok: bool) -> None:
        self.cancel.setEnabled(False)
        if ok:
            self.bar.setValue(100)
            self.label.setText("✓ " + self.label.text())



class MainWindow(QMainWindow):
    def __init__(self, demo: bool = False, tunnel: Optional[str] = None,
                 demo_root: Optional[Path] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("iDevice File Browser")
        self.resize(980, 640)
        self.backend: Optional[FileBackend] = None
        self._tunnel = tunnel
        self._workers: list = []
        self._auto_connect_failed = False
        self._connecting_serial: Optional[str] = None
        self._current_serial: Optional[str] = None
        self._current_bundle_id: Optional[str] = None
        self._app_names: dict = {}
        self._media_hint_shown = False

        self.model = DeviceTreeModel(self)
        self.model.exporter = self._export_for_drag
        self.model.requestMove.connect(self._on_move)
        self.model.requestCopy.connect(self._on_copy)
        self.model.requestImport.connect(self._on_import)
        self.model.backendError.connect(self._show_error)

        self.tree = QTreeView(self)
        self.tree.setModel(self.model)
        self.tree.setSelectionMode(QTreeView.ExtendedSelection)
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)
        self.tree.setDefaultDropAction(Qt.MoveAction)
        self.tree.setDragDropMode(QTreeView.DragDrop)
        self.tree.setEditTriggers(QTreeView.EditKeyPressed)  # F2/Enter renames
        self.tree.setUniformRowHeights(True)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.doubleClicked.connect(self._double_clicked)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.setCentralWidget(self.tree)

        # Delete/Backspace only while the tree has focus (never in the path bar)
        delete_sc = QAction(self.tree)
        delete_sc.setShortcuts([QKeySequence.Delete, QKeySequence("Backspace"),
                                QKeySequence("Meta+Backspace")])
        delete_sc.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        delete_sc.triggered.connect(self._delete_selected)
        self.tree.addAction(delete_sc)

        self._build_toolbar()
        self._build_transfers_dock()
        self.statusBar().showMessage("No device connected")
        from . import COMMIT, __version__
        self.statusBar().addPermanentWidget(QLabel(f"v{__version__} · {COMMIT}"))

        if demo:
            self._use_backend(MockBackend(demo_root, seed=True))
        else:
            self._refresh_devices()
            self._device_timer = QTimer(self)
            self._device_timer.timeout.connect(self._refresh_devices)
            self._device_timer.start(2000)

        self._cleanup_cache()

    # ---------------------------------------------------------------- UI setup
    def _build_toolbar(self) -> None:
        tb = QToolBar("Main", self)
        tb.setMovable(False)
        self.addToolBar(tb)
        style = self.style()

        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(240)
        self.device_combo.activated.connect(self._device_chosen)
        tb.addWidget(self.device_combo)

        # location selector: Media partition vs. app Documents (Files app)
        tb.addWidget(QLabel(" Location: "))
        self.location_combo = QComboBox()
        self.location_combo.setMinimumWidth(200)
        self.location_combo.addItem("Media (System)", None)
        self.location_combo.setVisible(False)
        self.location_combo.activated.connect(self._location_chosen)
        tb.addWidget(self.location_combo)

        act = QAction(style.standardIcon(QStyle.SP_BrowserReload), "Devices", self)
        act.setToolTip("Re-detect connected devices (and retry connecting)")
        act.triggered.connect(self._manual_refresh)
        tb.addAction(act)
        tb.addSeparator()

        act = QAction(style.standardIcon(QStyle.SP_ArrowUp), "Up", self)
        act.triggered.connect(self._go_up)
        tb.addAction(act)

        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("/")
        self.path_edit.returnPressed.connect(self._path_entered)
        tb.addWidget(self.path_edit)

        act = QAction(style.standardIcon(QStyle.SP_FileDialogNewFolder),
                      "New Folder", self)
        act.triggered.connect(self._new_folder)
        tb.addAction(act)

        act = QAction(style.standardIcon(QStyle.SP_ArrowForward), "Import…", self)
        act.setToolTip("Copy files from the Mac into the current folder")
        act.triggered.connect(self._import_dialog)
        tb.addAction(act)

        act = QAction(style.standardIcon(QStyle.SP_ArrowBack), "Export…", self)
        act.setToolTip("Copy the selected items to the Mac")
        act.triggered.connect(self._export_dialog)
        tb.addAction(act)

        act = QAction(style.standardIcon(QStyle.SP_TrashIcon), "Delete", self)
        act.setToolTip("Delete the selected items from the device")
        act.triggered.connect(self._delete_selected)
        tb.addAction(act)

    def _build_transfers_dock(self) -> None:
        self.dock = QDockWidget("Transfers", self)
        self.dock.setObjectName("transfersDock")
        self.transfer_list = QListWidget()
        self.dock.setWidget(self.transfer_list)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.dock)
        self.dock.hide()

    # ---------------------------------------------------------- device wiring
    def _use_backend(self, backend: Optional[FileBackend]) -> None:
        if self.backend is not None:
            self.backend.close()
        self.backend = backend
        self.model.set_backend(backend)
        if backend is not None:
            self.setWindowTitle(f"iDevice File Browser — {backend.display_name}")
            self.statusBar().showMessage(f"Connected: {backend.display_name}")
            self.tree.expand(self.model.index(0, 0))
            self.tree.setCurrentIndex(self.model.index(0, 0))
            self.path_edit.setText("/")
            # show the real device name in the combo instead of the serial
            if self._connecting_serial:
                for i in range(self.device_combo.count()):
                    if self.device_combo.itemData(i) == self._connecting_serial:
                        self.device_combo.setItemText(i, backend.display_name)
            # track location; AFC backends always show the location selector
            self._current_bundle_id = getattr(backend, "bundle_id", None)
            if hasattr(backend, "list_file_sharing_apps"):
                self.location_combo.setVisible(True)
                if (self._current_bundle_id is None
                        and self.location_combo.count() <= 1):
                    self.statusBar().showMessage(
                        f"Connected: {backend.display_name} — "
                        "loading apps with file sharing…")
                    self._start_app_listing(backend)
            idx = self.location_combo.findData(self._current_bundle_id)
            self.location_combo.blockSignals(True)
            self.location_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.location_combo.blockSignals(False)
        else:
            self.setWindowTitle("iDevice File Browser")
            self.statusBar().showMessage("No device connected")

    def _manual_refresh(self) -> None:
        """Reload button: retry auto-connect even after a previous failure."""
        self._auto_connect_failed = False
        self._refresh_devices()

    def _refresh_devices(self) -> None:
        try:
            from .devices import list_devices
            found = list_devices()
        except Exception:
            found = []
        # device set changed (e.g. unplugged/replugged)? -> allow auto-retry
        serials = frozenset(d.serial for d in found)
        if serials != getattr(self, "_last_seen_serials", None):
            self._last_seen_serials = serials
            self._auto_connect_failed = False
        if self.device_combo.count() == 0:
            self.device_combo.addItem("Select device…", None)
        known = {self.device_combo.itemData(i)
                 for i in range(self.device_combo.count())}
        for dev in found:
            if dev.serial not in known:
                self.device_combo.addItem(dev.label, dev.serial)
        # auto-connect: exactly one device, or exactly one USB device
        # (Wi-Fi listed alongside USB is just a duplicate of the same device)
        usb = [d for d in found if d.connection_type.lower() == "usb"]
        auto = found[0].serial if len(found) == 1 \
            else (usb[0].serial if len(usb) == 1 else None)
        if self.backend is None and auto and not self._auto_connect_failed:
            self._connect(auto)

    def _device_chosen(self, index: int) -> None:
        serial = self.device_combo.itemData(index)
        if serial:
            self._auto_connect_failed = False
            self._current_bundle_id = None
            self.location_combo.setVisible(False)
            self._connect(serial)

    def _connect(self, serial: Optional[str],
                 bundle_id: Optional[str] = None,
                 app_name: Optional[str] = None) -> None:
        self.statusBar().showMessage(
            "Connecting… (unlock the device and tap \"Trust\" if asked)")
        self._connecting_serial = serial
        self._current_serial = serial
        worker = ConnectWorker(serial, self._tunnel, bundle_id, app_name, self)
        worker.connected.connect(self._use_backend)
        worker.failed.connect(self._connect_failed)
        worker.start()

    def _connect_failed(self, message: str) -> None:
        self._auto_connect_failed = True
        # revert the location combo to whatever the backend actually is
        self._current_bundle_id = getattr(self.backend, "bundle_id", None)
        idx = self.location_combo.findData(self._current_bundle_id)
        self.location_combo.blockSignals(True)
        self.location_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.location_combo.blockSignals(False)
        self.statusBar().showMessage("Connection failed")
        QMessageBox.warning(self, "Could not connect", message)

    # -------------------------------------------------------- location (apps)
    def _start_app_listing(self, backend: FileBackend) -> None:
        worker = AppListWorker(backend, self)
        worker.done.connect(
            lambda apps, b=backend: self._apps_loaded(b, apps))
        worker.failed.connect(
            lambda msg: self.statusBar().showMessage(
                "Could not list apps: "
                + msg.strip().splitlines()[-1][:140]))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _apps_loaded(self, backend: FileBackend, apps: list) -> None:
        if backend is not self.backend:
            return  # device/location changed while listing
        self._populate_location_combo(apps)

    def _populate_location_combo(self, apps: list) -> None:
        combo = self.location_combo
        self._app_names = dict(apps)
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Media (System)", None)
        for bundle_id, name in apps:
            combo.addItem(f"{name} (Documents)", bundle_id)
        idx = combo.findData(self._current_bundle_id)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)
        combo.setVisible(bool(apps)
                         or hasattr(self.backend, "list_file_sharing_apps"))
        if not apps and hasattr(self.backend, "list_file_sharing_apps"):
            self.statusBar().showMessage(
                "No apps with File Sharing found — install e.g. VLC from the "
                "App Store to transfer files into the iOS Files app.")

    def _location_chosen(self, index: int) -> None:
        bundle_id = self.location_combo.itemData(index)
        if bundle_id == self._current_bundle_id or not self._current_serial:
            return
        app_name = self._app_names.get(bundle_id) if bundle_id else None
        self._current_bundle_id = bundle_id
        self._connect(self._current_serial, bundle_id=bundle_id,
                      app_name=app_name)

    # ------------------------------------------------------ navigation helpers
    def _selected_paths(self) -> list:
        paths = set()
        for idx in self.tree.selectionModel().selectedRows(0):
            entry = self.model.entry_at(idx)
            if entry is not None and entry.path != "/":
                paths.add(entry.path)
        return sorted(paths)

    def _target_dir(self) -> str:
        entry = self.model.entry_at(self.tree.currentIndex())
        if entry is None:
            return "/"
        return entry.path if entry.is_dir else parent_path(entry.path)

    def _select_path(self, path: str) -> None:
        idx = self.model.ensure_path(path)
        if idx.isValid():
            self.tree.expand(idx)
            self.tree.setCurrentIndex(idx)
            self.tree.scrollTo(idx)
        self.path_edit.setText(path)

    def _go_up(self) -> None:
        self._select_path(parent_path(self._target_dir()))

    def _path_entered(self) -> None:
        self._select_path(self.path_edit.text().strip() or "/")

    # ------------------------------------------------------------------ actions
    def _new_folder(self) -> None:
        if not self.backend:
            return
        base = self._target_dir()
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        name = name.strip()
        if not ok or not name or "/" in name:
            return
        try:
            self.backend.mkdir(join_path(base, name))
        except Exception as exc:
            self._show_error(f"Could not create folder:\n{exc}")
            return
        self.model.refresh_path(base)

    def _import_dialog(self) -> None:
        if not self.backend:
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Import to device")
        if files:
            self._on_import(files, self._target_dir())

    def _export_dialog(self) -> None:
        paths = self._selected_paths()
        if not paths or not self.backend:
            return
        dest = QFileDialog.getExistingDirectory(self, "Export to…")
        if not dest:
            return
        for path in paths:
            name = path.rsplit("/", 1)[-1]
            local = self._unique_local(Path(dest) / name)
            worker = PullWorker(self.backend, path, local, self)
            self._start_worker(worker, f"Export {name}")

    def _delete_selected(self) -> None:
        paths = self._selected_paths()
        if not paths or not self.backend:
            return
        preview = "\n".join(paths[:12])
        if len(paths) > 12:
            preview += f"\n… and {len(paths) - 12} more"
        answer = QMessageBox.question(
            self, "Delete",
            f"Delete {len(paths)} item(s) from the device?\n\n{preview}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        parents = tuple(sorted({parent_path(p) for p in paths}))
        worker = DeleteWorker(self.backend, paths, self)
        self._start_worker(worker, f"Delete {len(paths)} item(s)",
                           refresh=parents)

    # --------------------------------------------------- model request slots
    def _on_move(self, paths: list, dst_dir: str) -> None:
        if not self.backend:
            return
        refresh = tuple(sorted({parent_path(p) for p in paths} | {dst_dir}))
        self._start_worker(MoveWorker(self.backend, paths, dst_dir, self),
                           f"Move {len(paths)} item(s)", refresh=refresh)

    def _on_copy(self, paths: list, dst_dir: str) -> None:
        if not self.backend:
            return
        self._start_worker(CopyWorker(self.backend, paths, dst_dir, self),
                           f"Copy {len(paths)} item(s)", refresh=(dst_dir,))

    def _on_import(self, locals_: list, dst_dir: str) -> None:
        if not self.backend:
            return
        count = 0
        for local in locals_:
            p = Path(local)
            if not p.exists():
                continue
            remote = self._unique_remote(dst_dir, p.name)
            self._start_worker(PushWorker(self.backend, p, remote, self),
                               f"Import {p.name}", refresh=(dst_dir,))
            count += 1
        if not count:
            return
        if getattr(self.backend, "bundle_id", None):
            app = self.backend.display_name.removesuffix(" (Documents)")
            self.statusBar().showMessage(
                f"Importing {count} file(s) → {self.backend.display_name} — "
                f"then on the iPhone: Files → On My iPhone → {app}")
        elif self.isVisible() and not self._media_hint_shown:
            self._media_hint_shown = True
            QMessageBox.information(
                self, "Where do the files go?",
                "Files copied to 'Media (System)' are NOT visible in the iOS "
                "Files app.\n\nTo find them on the iPhone (Files → On My "
                "iPhone → <App>): first pick an app like 'AUM (Documents)' in "
                "the Location dropdown (next to the device picker), then drag "
                "the files into the tree.")

    # --------------------------------------------------------- worker plumbing
    def _start_worker(self, worker: Worker, title: str,
                      refresh: tuple = ()) -> None:
        self._workers.append(worker)
        row = _TransferRow(worker, title)
        item = QListWidgetItem()
        item.setSizeHint(row.sizeHint())
        self.transfer_list.addItem(item)
        self.transfer_list.setItemWidget(item, row)
        self.dock.show()

        def cleanup(ok: bool) -> None:
            row.finish(ok)
            if worker in self._workers:
                self._workers.remove(worker)
            for path in refresh:
                self.model.refresh_path(path)

        worker.succeeded.connect(lambda: cleanup(True))
        worker.failed.connect(
            lambda msg: (self._show_error(f"{title} failed:\n{msg}"),
                         cleanup(False)))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    # -------------------------------------------------------------------- misc
    def _context_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("New Folder…", self._new_folder)
        idx = self.tree.indexAt(pos)
        entry = self.model.entry_at(idx)
        if entry is not None and entry.path != "/":
            menu.addAction("Rename",
                           lambda: self.tree.edit(idx.siblingAtColumn(0)))
            menu.addAction("Export to Mac…", self._export_dialog)
            menu.addSeparator()
            menu.addAction("Delete…", self._delete_selected)
        menu.addSeparator()
        target = self._target_dir()
        menu.addAction("Refresh", lambda: self.model.refresh_path(target))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _double_clicked(self, index) -> None:
        entry = self.model.entry_at(index)
        if entry is None or entry.is_dir or not self.backend:
            return
        dest = CACHE_DIR / "open"
        dest.mkdir(parents=True, exist_ok=True)
        local = self._unique_local(dest / entry.name)
        try:
            self._pull_sync(entry.path, local)
        except Exception as exc:
            self._show_error(f"Could not open {entry.path}:\n{exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(local)))

    # ------------------------------------------------------------- drag export
    def _export_for_drag(self, paths: list) -> list:
        """Download the dragged selection to a cache dir; return local QUrls.

        Called synchronously when a drag starts. Finder then copies from those
        cache files; stale caches are cleaned up on the next app start.
        """
        if not self.backend:
            return []
        dest = CACHE_DIR / time.strftime("drag-%Y%m%d-%H%M%S")
        dest.mkdir(parents=True, exist_ok=True)
        progress = QProgressDialog("Preparing files for drag & drop…",
                                   "Cancel", 0, len(paths), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(400)
        urls = []
        for i, path in enumerate(paths):
            if progress.wasCanceled():
                break
            progress.setValue(i)
            progress.setLabelText(path)
            name = path.rsplit("/", 1)[-1]
            local = self._unique_local(dest / name)
            try:
                self._pull_sync(path, local)
            except Exception as exc:
                self._show_error(f"Could not export {path}:\n{exc}")
                continue
            urls.append(QUrl.fromLocalFile(str(local)))
        progress.setValue(len(paths))
        return urls

    def _pull_sync(self, remote: str, local: Path) -> None:
        """Synchronous download used for drag export and double-click open."""
        for entry in self.backend.walk(remote):
            rel = entry.path[len(remote):].lstrip("/")
            target = local / rel if rel else local
            if entry.is_dir:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with self.backend.open_read(entry.path) as src, \
                        open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _unique_local(path: Path) -> Path:
        if not path.exists():
            return path
        stem, suffix = path.stem, path.suffix
        for i in range(2, 1000):
            candidate = path.with_name(f"{stem} {i}{suffix}")
            if not candidate.exists():
                return candidate
        return path

    def _unique_remote(self, directory: str, name: str) -> str:
        candidate = join_path(directory, name)
        if not self.backend.exists(candidate):
            return candidate
        if "." in name and not name.startswith("."):
            stem, ext = name.rsplit(".", 1)
            ext = "." + ext
        else:
            stem, ext = name, ""
        for i in range(2, 1000):
            candidate = join_path(directory, f"{stem} {i}{ext}")
            if not self.backend.exists(candidate):
                return candidate
        return candidate

    @staticmethod
    def _cleanup_cache() -> None:
        """Drop drag/open caches older than a day."""
        if not CACHE_DIR.exists():
            return
        cutoff = time.time() - 24 * 3600
        for child in CACHE_DIR.iterdir():
            try:
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                pass

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self, "iDevice File Browser", message)

    def closeEvent(self, event) -> None:
        for worker in list(self._workers):
            worker.cancel()
        for worker in list(self._workers):
            worker.wait(3000)
        if self.backend is not None:
            self.backend.close()
        event.accept()
