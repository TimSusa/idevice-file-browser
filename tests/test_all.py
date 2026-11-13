"""Unit + smoke tests. Run with:  QT_QPA_PLATFORM=offscreen pytest -q"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QMimeData, QUrl, Qt  # noqa: E402

from ipad_browser.backend import (CancelledError, join_path, norm_path,  # noqa: E402
                                  parent_path, stream_copy)
from ipad_browser.mock_backend import MockBackend  # noqa: E402
from ipad_browser.model import MIME_PATHS, DeviceTreeModel  # noqa: E402
from ipad_browser.transfers import (DeleteWorker, MoveWorker, PullWorker,  # noqa: E402
                                    PushWorker)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def backend(tmp_path):
    return MockBackend(tmp_path / "device", seed=True)


def run_worker(worker):
    worker.start()
    assert worker.wait(15000), "worker timed out"


# --------------------------------------------------------------------- paths
def test_path_helpers():
    assert norm_path("//a///b/../c") == "/a/c"
    assert norm_path("") == "/"
    assert join_path("/", "DCIM") == "/DCIM"
    assert join_path("/a/", "/b/") == "/a/b"
    assert parent_path("/a/b/c.txt") == "/a/b"
    assert parent_path("/") == "/"


# ------------------------------------------------------------------- backend
def test_mock_backend_crud(backend):
    names = [e.name for e in backend.listdir("/")]
    assert "DCIM" in names and "Documents" in names

    entry = backend.stat("/DCIM/100APPLE/IMG_0001.HEIC")
    assert not entry.is_dir and entry.size > 0 and entry.mtime > 0

    backend.mkdir("/Documents/Sub")
    assert backend.is_dir("/Documents/Sub")

    with backend.open_write("/Documents/Sub/hello.txt") as f:
        f.write(b"hello ipad")
    with backend.open_read("/Documents/Sub/hello.txt") as f:
        assert f.read() == b"hello ipad"

    backend.rename("/Documents/Sub/hello.txt", "/Documents/Sub/hi.txt")
    assert backend.exists("/Documents/Sub/hi.txt")

    backend.remove("/Documents/Sub")
    assert not backend.exists("/Documents/Sub")


def test_stream_copy_cancel():
    src = io.BytesIO(b"x" * (1024 * 1024))
    dst = io.BytesIO()
    calls = []

    def progress(n):
        calls.append(n)
        return len(calls) < 2  # cancel after the second chunk

    with pytest.raises(CancelledError):
        stream_copy(src, dst, progress=progress)


# ------------------------------------------------------------------ workers
def test_push_pull_roundtrip(qapp, backend, tmp_path):
    local = tmp_path / "local"
    (local / "sub").mkdir(parents=True)
    payload = b"payload-123" * 1000
    (local / "sub" / "file.bin").write_bytes(payload)
    (local / "top.txt").write_bytes(b"top")

    run_worker(PushWorker(backend, local, "/Documents/imported"))
    assert backend.exists("/Documents/imported/top.txt")
    assert backend.exists("/Documents/imported/sub/file.bin")

    out = tmp_path / "out"
    run_worker(PullWorker(backend, "/Documents/imported", out))
    assert (out / "sub" / "file.bin").read_bytes() == payload
    assert (out / "top.txt").read_bytes() == b"top"


def test_move_and_delete_workers(qapp, backend):
    run_worker(MoveWorker(backend, ["/Books"], "/Documents"))
    assert backend.exists("/Books") is False
    assert backend.is_dir("/Documents/Books")

    run_worker(DeleteWorker(backend, ["/Documents/Books", "/Recordings"]))
    assert not backend.exists("/Documents/Books")
    assert not backend.exists("/Recordings")



# --------------------------------------------------------------------- model
def _index_of(model, parent, name):
    for row in range(model.rowCount(parent)):
        idx = model.index(row, 0, parent)
        if model.entry_at(idx).name == name:
            return idx
    raise AssertionError(f"{name} not found under {model.entry_at(parent).path}")


def test_model_browse_rename(qapp, backend):
    model = DeviceTreeModel()
    model.set_backend(backend)

    root = model.index(0, 0)
    assert root.isValid() and model.entry_at(root).path == "/"
    assert model.canFetchMore(root)
    model.fetchMore(root)
    assert model.rowCount(root) >= 4

    dcim = _index_of(model, root, "DCIM")
    assert model.entry_at(dcim).is_dir

    assert model.setData(dcim, "Photos", Qt.EditRole)  # rename via F2
    assert backend.exists("/Photos")

    photos = _index_of(model, root, "Photos")
    model.fetchMore(photos)
    assert model.rowCount(photos) == 1


def test_model_drop_internal_move(qapp, backend):
    model = DeviceTreeModel()
    model.set_backend(backend)
    root = model.index(0, 0)
    model.fetchMore(root)
    documents = _index_of(model, root, "Documents")

    captured = []
    model.requestMove.connect(lambda paths, dst: captured.append((paths, dst)))

    mime = QMimeData()
    mime.setData(MIME_PATHS, b'["/Books"]')
    assert model.dropMimeData(mime, Qt.MoveAction, -1, -1, documents)
    assert captured == [(["/Books"], "/Documents")]


def test_model_drop_from_finder(qapp, backend):
    model = DeviceTreeModel()
    model.set_backend(backend)
    root = model.index(0, 0)
    model.fetchMore(root)
    downloads = _index_of(model, root, "Downloads")

    captured = []
    model.requestImport.connect(lambda paths, dst: captured.append((paths, dst)))

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile("/tmp/some-file.txt")])
    assert model.dropMimeData(mime, Qt.CopyAction, -1, -1, downloads)
    assert captured == [(["/tmp/some-file.txt"], "/Downloads")]


def test_model_mimedata_for_finder_drag(qapp, backend):
    model = DeviceTreeModel()
    model.set_backend(backend)
    model.exporter = lambda paths: [QUrl.fromLocalFile("/tmp/cached-file.txt")]
    root = model.index(0, 0)
    model.fetchMore(root)
    books = _index_of(model, root, "Books")

    mime = model.mimeData([books])
    assert mime.hasFormat(MIME_PATHS)
    assert mime.hasUrls()
    assert mime.urls()[0].toLocalFile() == "/tmp/cached-file.txt"


# ----------------------------------------------------------------- mainwindow
def test_mainwindow_smoke(qapp, tmp_path):
    from ipad_browser.mainwindow import MainWindow

    window = MainWindow(demo=True, demo_root=tmp_path / "demo")
    try:
        assert window.backend is not None
        assert "Demo" in window.windowTitle()
        root = window.model.index(0, 0)
        window.model.fetchMore(root)
        assert window.model.rowCount(root) >= 4

        idx = window.model.ensure_path("/Documents")
        assert idx.isValid()
        window.tree.setCurrentIndex(idx)
        assert window._target_dir() == "/Documents"

        assert (window._unique_remote("/Documents", "Notes.txt")
                != "/Documents/Notes.txt")
        assert (window._unique_remote("/Documents", "new.txt")
                == "/Documents/new.txt")
    finally:
        window.close()


# -------------------------------------------------------------------- devices
def test_devices_listing_never_raises(qapp):
    from ipad_browser.devices import list_devices
    devices = list_devices()  # no device attached in CI -> just must not crash
    assert isinstance(devices, list)
    for dev in devices:
        assert dev.serial and dev.label


def test_afc_display_name_description(qapp):
    from ipad_browser.afc_backend import AfcBackend

    class FakeLockdown:
        display_name = "Tim's iPhone"
        device_class = "iPhone"
        product_version = "18.1"

    name = AfcBackend._describe_lockdown(FakeLockdown())
    assert name == "Tim's iPhone (iPhone, iOS 18.1)"

    class BrokenLockdown:
        @property
        def display_name(self):
            raise RuntimeError("no value")

    assert AfcBackend._describe_lockdown(BrokenLockdown()) == "iOS Device"

    import enum

    class DeviceClass(str, enum.Enum):  # pymobiledevice3 returns a str-enum
        IPHONE = "iPhone"

    class EnumLockdown:
        display_name = "X"
        device_class = DeviceClass.IPHONE
        product_version = "27.2.1"

    assert AfcBackend._describe_lockdown(EnumLockdown()) == "X (iPhone, iOS 27.2.1)"


def test_device_generic_branding(qapp, tmp_path):
    from ipad_browser.mainwindow import MainWindow
    window = MainWindow(demo=True, demo_root=tmp_path / "demo2")
    try:
        assert "iDevice" in window.windowTitle()
    finally:
        window.close()


def test_build_info_flag():
    """main.py --build-info prints the version line and exits 0 (no GUI)."""
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "main.py", "--build-info"],
                            cwd=root, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0
    assert "iDevice File Browser" in result.stdout
    assert "build" in result.stdout


def test_end_to_end_import_into_demo(qapp, tmp_path):
    """Simulates a Finder drop: requestImport -> _on_import -> PushWorker."""
    from ipad_browser.mainwindow import MainWindow
    window = MainWindow(demo=True, demo_root=tmp_path / "demo4")
    try:
        src = tmp_path / "song.wav"
        src.write_bytes(b"RIFF" + b"x" * 1000)
        window._on_import([str(src), str(tmp_path / "missing.wav")], "/")
        assert window._workers, "no import worker was started"
        for w in list(window._workers):
            w.wait(10000)
        assert (tmp_path / "demo4" / "song.wav").read_bytes() == \
            b"RIFF" + b"x" * 1000
    finally:
        window.close()


def test_location_combo(qapp, tmp_path):
    from ipad_browser.mainwindow import MainWindow
    window = MainWindow(demo=True, demo_root=tmp_path / "demo3")
    try:
        assert window.location_combo.isHidden()

        window._populate_location_combo(
            [("com.vlc", "VLC"), ("com.docs", "Documents")])
        combo = window.location_combo
        assert not combo.isHidden()
        assert combo.count() == 3
        assert combo.itemData(0) is None                    # Media (System)
        assert combo.itemData(1) == "com.vlc"
        assert "VLC" in combo.itemText(1)
        assert combo.itemData(2) == "com.docs"

        window._populate_location_combo([])
        assert window.location_combo.isHidden()
    finally:
        window.close()
