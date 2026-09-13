"""メインウィンドウ。Phase 2: フォルダ選択 + サムネ一覧 + プレビュー再生。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, QThreadPool, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.core.ffmpeg_runner import find_ffmpeg, find_ffprobe
from app.core.keyframes import KeyframeWorker
from app.core.models import VideoItem
from app.core.video_loader import LoadVideoWorker, scan_folder
from app.ui.player_panel import PlayerPanel
from app.ui.thumbnail_grid import THUMB_SIZES, ThumbnailGrid

ORG_NAME = "VideoTrimmer"
APP_NAME = "VideoTrimmer"
SETTINGS_LAST_FOLDER = "last_folder"
MAX_PARALLEL_LOADS = 4


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VideoTrimmer")
        self.resize(1100, 700)

        self._settings = QSettings(ORG_NAME, APP_NAME)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(MAX_PARALLEL_LOADS)
        self._generation = 0
        self._current_folder: Path | None = None

        self._ffmpeg_path = find_ffmpeg()
        self._ffprobe_path = find_ffprobe()

        self._build_ui()
        self._setup_shortcuts()

        if self._ffmpeg_path is None or self._ffprobe_path is None:
            QMessageBox.warning(
                self,
                "ffmpeg / ffprobe が見つかりません",
                "ffmpeg / ffprobe が見つかりませんでした。\n"
                "PATH に追加するか、プロジェクト直下の bin フォルダに配置してください。\n"
                "見つかるまでサムネイル生成・動画の読み込み・編集機能は使用できません。",
            )

        self._restore_last_folder()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)

        # 上部バー
        top_bar = QHBoxLayout()
        self._open_button = QPushButton("フォルダを選択…")
        self._open_button.clicked.connect(self._on_open_folder_clicked)
        self._path_label = QLabel("(フォルダ未選択)")
        self._path_label.setStyleSheet("color: palette(mid);")

        self._size_combo = QComboBox()
        for size in THUMB_SIZES:
            self._size_combo.addItem(f"{size}px", size)
        self._size_combo.setCurrentIndex(THUMB_SIZES.index(240))
        self._size_combo.currentIndexChanged.connect(self._on_thumb_size_changed)

        top_bar.addWidget(self._open_button)
        top_bar.addWidget(self._path_label, 1)
        top_bar.addWidget(QLabel("サムネサイズ:"))
        top_bar.addWidget(self._size_combo)
        root_layout.addLayout(top_bar)

        # 左: サムネ一覧 / 右: プレビュー
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._grid = ThumbnailGrid()
        self._grid.video_selected.connect(self._on_video_selected)
        self._player_panel = PlayerPanel()

        splitter.addWidget(self._grid)
        splitter.addWidget(self._player_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root_layout.addWidget(splitter, 1)

    def _setup_shortcuts(self) -> None:
        # 仕様書 §5.3 のキーボードショートカット。
        # フォーカスがサムネ一覧（QListView）にあっても常に効くよう ApplicationShortcut にする。
        def bind(key: str, slot) -> None:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(slot)

        bind("Space", self._player_panel.toggle_play_pause)
        bind("Left", lambda: self._player_panel.step_frame(-1))
        bind("Right", lambda: self._player_panel.step_frame(1))
        bind("Shift+Left", lambda: self._player_panel.step_seconds(-1))
        bind("Shift+Right", lambda: self._player_panel.step_seconds(1))
        bind("Ctrl+Left", lambda: self._player_panel.step_seconds(-10))
        bind("Ctrl+Right", lambda: self._player_panel.step_seconds(10))
        bind("I", self._player_panel.set_in_point)
        bind("O", self._player_panel.set_out_point)
        bind(",", self._player_panel.jump_to_prev_keyframe)
        bind(".", self._player_panel.jump_to_next_keyframe)
        # Enter での実行は、抜き出し/削除タブが揃う Phase 3/4 で実装する

    # ------------------------------------------------------------- フォルダ

    def _restore_last_folder(self) -> None:
        last = self._settings.value(SETTINGS_LAST_FOLDER, "", str)
        if last:
            folder = Path(last)
            if folder.is_dir():
                self._open_folder(folder)

    def _on_open_folder_clicked(self) -> None:
        start_dir = str(self._current_folder) if self._current_folder else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "フォルダを選択", start_dir)
        if folder:
            self._open_folder(Path(folder))

    def _open_folder(self, folder: Path) -> None:
        self._generation += 1
        generation = self._generation
        self._pool.clear()  # 未処理タスクをキャンセル

        self._current_folder = folder
        self._path_label.setText(str(folder))
        self._settings.setValue(SETTINGS_LAST_FOLDER, str(folder))
        self._grid.clear()
        self._player_panel.clear()

        try:
            files = scan_folder(folder)
        except OSError as exc:
            QMessageBox.warning(self, "フォルダを読み込めません", str(exc))
            return

        if self._ffmpeg_path is None or self._ffprobe_path is None:
            for path in files:
                self._grid.add_placeholder(path)
                self._grid.update_failed(path, "ffmpeg / ffprobe が見つかりません")
            return

        for path in files:
            self._grid.add_placeholder(path)
            worker = LoadVideoWorker(path, generation, self._ffmpeg_path, self._ffprobe_path)
            worker.signals.loaded.connect(self._on_video_loaded)
            worker.signals.failed.connect(self._on_video_failed)
            self._pool.start(worker)

    # --------------------------------------------------------------- 読込

    def _on_video_loaded(self, generation: int, path: Path, item: VideoItem) -> None:
        if generation != self._generation:
            return
        self._grid.update_loaded(path, item)

    def _on_video_failed(self, generation: int, path: Path, message: str) -> None:
        if generation != self._generation:
            return
        self._grid.update_failed(path, message)

    def _on_thumb_size_changed(self, index: int) -> None:
        size = self._size_combo.itemData(index)
        self._grid.set_thumb_size(size)

    # --------------------------------------------------------------- 選択

    def _on_video_selected(self, path: Path) -> None:
        status = self._grid.get_status(path)
        if status == "ready":
            item = self._grid.get_video_item(path)
            if item is not None:
                self._player_panel.load_video(item)
                self._start_keyframe_fetch(item)
            return
        if status == "error":
            message = self._grid.get_error_message(path) or ""
            self._player_panel.show_error(path, message)
            return
        self._player_panel.show_loading(path)

    # ----------------------------------------------------------- キーフレーム

    def _start_keyframe_fetch(self, item: VideoItem) -> None:
        if self._ffprobe_path is None:
            self._player_panel.set_keyframes_unavailable(item.path)
            return
        worker = KeyframeWorker(item.path, self._ffprobe_path)
        worker.signals.loaded.connect(self._on_keyframes_loaded)
        worker.signals.failed.connect(self._on_keyframes_failed)
        self._pool.start(worker)

    def _on_keyframes_loaded(self, path: Path, keyframes: list) -> None:
        self._player_panel.set_keyframes(path, keyframes)

    def _on_keyframes_failed(self, path: Path, message: str) -> None:
        self._player_panel.set_keyframes_unavailable(path)
