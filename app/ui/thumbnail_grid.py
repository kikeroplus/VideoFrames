"""サムネイル一覧グリッド。仕様書 §5.2 参照（QListView + IconMode + QStandardItemModel）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QListView

from app.core.models import VideoItem

THUMB_SIZES = (160, 240, 320)

PATH_ROLE = Qt.ItemDataRole.UserRole + 1
STATUS_ROLE = Qt.ItemDataRole.UserRole + 2  # "loading" | "ready" | "error"
VIDEO_ITEM_ROLE = Qt.ItemDataRole.UserRole + 3


def _duration_label(seconds: float) -> str:
    total = int(round(seconds))
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _solid_pixmap(size: int, color: Qt.GlobalColor) -> QPixmap:
    pixmap = QPixmap(size, max(1, int(size * 9 / 16)))
    pixmap.fill(color)
    return pixmap


class ThumbnailGrid(QListView):
    """動画のサムネイル一覧。選択されると video_selected(path) を発行する。"""

    video_selected = Signal(Path)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(8)
        self._thumb_size = THUMB_SIZES[1]
        self._apply_grid_size()

        self.selectionModel().currentChanged.connect(self._on_current_changed)

    def set_thumb_size(self, size: int) -> None:
        if size not in THUMB_SIZES:
            raise ValueError(f"unsupported thumb size: {size}")
        self._thumb_size = size
        self._apply_grid_size()

    def _apply_grid_size(self) -> None:
        icon_h = max(1, int(self._thumb_size * 9 / 16))
        self.setIconSize(QSize(self._thumb_size, icon_h))
        self.setGridSize(QSize(self._thumb_size + 16, icon_h + 56))

    def clear(self) -> None:
        self._model.clear()

    def add_placeholder(self, path: Path) -> None:
        item = QStandardItem()
        item.setIcon(QIcon(_solid_pixmap(self._thumb_size, Qt.GlobalColor.gray)))
        item.setText(f"{path.name}\n読み込み中…")
        item.setData(path, PATH_ROLE)
        item.setData("loading", STATUS_ROLE)
        item.setEditable(False)
        self._model.appendRow(item)

    def _find_item(self, path: Path) -> QStandardItem | None:
        for row in range(self._model.rowCount()):
            item = self._model.item(row)
            if item.data(PATH_ROLE) == path:
                return item
        return None

    def update_loaded(self, path: Path, video_item: VideoItem) -> None:
        item = self._find_item(path)
        if item is None:
            return

        if video_item.thumb_path is not None:
            pixmap = QPixmap(str(video_item.thumb_path))
            if not pixmap.isNull():
                pixmap = pixmap.scaledToWidth(
                    self._thumb_size, Qt.TransformationMode.SmoothTransformation
                )
                item.setIcon(QIcon(pixmap))

        label = (
            f"{path.name}\n"
            f"{_duration_label(video_item.duration)}  {video_item.width}x{video_item.height}"
        )
        item.setText(label)
        item.setData("ready", STATUS_ROLE)
        item.setData(video_item, VIDEO_ITEM_ROLE)

    def update_failed(self, path: Path, message: str) -> None:
        item = self._find_item(path)
        if item is None:
            return
        item.setIcon(QIcon(_solid_pixmap(self._thumb_size, Qt.GlobalColor.darkGray)))
        item.setText(f"{path.name}\n読み込み不可")
        item.setData("error", STATUS_ROLE)
        item.setData(message, VIDEO_ITEM_ROLE)

    def get_video_item(self, path: Path) -> VideoItem | None:
        item = self._find_item(path)
        if item is None or item.data(STATUS_ROLE) != "ready":
            return None
        return item.data(VIDEO_ITEM_ROLE)

    def get_status(self, path: Path) -> str | None:
        item = self._find_item(path)
        return None if item is None else item.data(STATUS_ROLE)

    def get_error_message(self, path: Path) -> str | None:
        item = self._find_item(path)
        if item is None or item.data(STATUS_ROLE) != "error":
            return None
        return item.data(VIDEO_ITEM_ROLE)

    def _on_current_changed(self, current, _previous) -> None:
        if not current.isValid():
            return
        item = self._model.itemFromIndex(current)
        if item is None:
            return
        self.video_selected.emit(item.data(PATH_ROLE))
