"""メインウィンドウ。Phase 5: フォルダ選択 + 一覧 + プレビュー + 抜き出し/削除 + 設定。"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtGui import QCloseEvent, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.app_settings import KEY_LAST_FOLDER, KEY_THUMB_SIZE, load_app_settings, make_settings, save_app_settings
from app.core.edit_ops import Strategy, build_cut_cmd
from app.core.ffmpeg_runner import DeleteMiddleJob, FFmpegJob, find_ffmpeg, find_ffprobe
from app.core.keyframes import KeyframeWorker
from app.core.models import VideoItem
from app.core.video_loader import LoadVideoWorker, scan_folder
from app.ui.delete_panel import DeletePanel, DeleteRequest
from app.ui.extract_panel import ExtractPanel, ExtractRequest
from app.ui.extract_points_panel import ExtractPointEntry, ExtractPointsPanel
from app.ui.player_panel import PlayerPanel
from app.ui.project_io import PROJECT_FILE_FILTER, ProjectError, load_project, save_project
from app.ui.settings_dialog import SettingsDialog
from app.ui.thumbnail_grid import THUMB_SIZES, ThumbnailGrid
from app.ui.toast import Toast
from app.utils.paths import next_output_path, resource_path
from app.utils.timecode import seconds_to_timecode

MAX_PARALLEL_LOADS = 4
LOG_DIR = Path(os.environ["LOCALAPPDATA"]) / "VideoTrimmer" / "logs"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VideoTrimmer")
        self.setWindowIcon(QIcon(str(resource_path("assets/icon.ico"))))
        # 左(動画一覧)・右(抜き出しポイント一覧)を幅固定にした分、中央のプレビュー
        # 領域が窮屈にならないよう既定の幅を広めにしておく。
        self.resize(1400, 750)

        self._settings = make_settings()
        self._app_settings = load_app_settings(self._settings)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(MAX_PARALLEL_LOADS)
        self._job_pool = QThreadPool(self)
        self._job_pool.setMaxThreadCount(1)  # 編集ジョブは同時に1つだけ
        self._current_job: FFmpegJob | DeleteMiddleJob | None = None
        self._job_start_time: float = 0.0
        self._generation = 0
        self._current_folder: Path | None = None
        self._current_toast: Toast | None = None
        self._active_workers: list[LoadVideoWorker] = []
        self._keyframe_worker: KeyframeWorker | None = None
        self._extract_queue: list[ExtractRequest] = []
        self._extract_queue_item: VideoItem | None = None
        self._extract_queue_index = 0
        self._extract_queue_total = 0
        # 抜き出しポイントはフォルダ内の動画ごとに保持する(動画を切り替えても消えない)。
        self._extract_points_by_video: dict[Path, list[ExtractPointEntry]] = {}
        self._current_video_path: Path | None = None
        self._pending_project_video_select: Path | None = None
        self._thumb_refresh_queue: list[int] = []
        self._thumb_refresh_resume_position: float | None = None

        self._resolve_ffmpeg_paths()

        self._build_ui()
        self._setup_shortcuts()

        if self._ffmpeg_path is None or self._ffprobe_path is None:
            QMessageBox.warning(
                self,
                "ffmpeg / ffprobe が見つかりません",
                "ffmpeg / ffprobe が見つかりませんでした。\n"
                "[設定] から実行ファイルのパスを指定するか、PATH に追加してください。\n"
                "見つかるまでサムネイル生成・動画の読み込み・編集機能は使用できません。",
            )

        self._restore_last_folder()

    def _resolve_ffmpeg_paths(self) -> None:
        """設定で手動指定されていればそれを、無ければ自動検出したパスを使う。"""
        if self._app_settings.ffmpeg_path and Path(self._app_settings.ffmpeg_path).is_file():
            self._ffmpeg_path = Path(self._app_settings.ffmpeg_path)
        else:
            self._ffmpeg_path = find_ffmpeg()

        if self._app_settings.ffprobe_path and Path(self._app_settings.ffprobe_path).is_file():
            self._ffprobe_path = Path(self._app_settings.ffprobe_path)
        else:
            self._ffprobe_path = find_ffprobe()

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

        self._settings_button = QPushButton("設定")
        self._settings_button.clicked.connect(self._on_settings_clicked)
        self._open_output_button = QPushButton("出力先を開く")
        self._open_output_button.clicked.connect(self._on_open_output_clicked)

        self._save_project_button = QPushButton("プロジェクトを保存")
        self._save_project_button.clicked.connect(self._on_save_project_clicked)
        self._open_project_button = QPushButton("プロジェクトを開く")
        self._open_project_button.clicked.connect(self._on_open_project_clicked)

        saved_thumb_size = self._settings.value(KEY_THUMB_SIZE, 240, int)
        self._size_combo = QComboBox()
        for size in THUMB_SIZES:
            self._size_combo.addItem(f"{size}px", size)
        self._size_combo.setCurrentIndex(
            THUMB_SIZES.index(saved_thumb_size) if saved_thumb_size in THUMB_SIZES else THUMB_SIZES.index(240)
        )
        self._size_combo.currentIndexChanged.connect(self._on_thumb_size_changed)

        top_bar.addWidget(self._open_button)
        top_bar.addWidget(self._path_label, 1)
        top_bar.addWidget(QLabel("サムネサイズ:"))
        top_bar.addWidget(self._size_combo)
        top_bar.addWidget(self._settings_button)
        top_bar.addWidget(self._open_output_button)
        top_bar.addWidget(self._save_project_button)
        top_bar.addWidget(self._open_project_button)
        root_layout.addLayout(top_bar)

        # 左: サムネ一覧 / 右: プレビュー+タブ
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._grid = ThumbnailGrid()
        self._grid.video_selected.connect(self._on_video_selected)
        self._player_panel = PlayerPanel()

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self._player_panel, 1)

        self._tab_widget = QTabWidget()
        self._extract_panel = ExtractPanel(self._player_panel)
        self._extract_panel.execute_requested.connect(self._on_extract_requested)
        self._tab_widget.addTab(self._extract_panel, "抜き出し")
        self._delete_panel = DeletePanel(self._player_panel)
        self._delete_panel.execute_requested.connect(self._on_delete_requested)
        self._tab_widget.addTab(self._delete_panel, "削除")
        self._tab_widget.currentChanged.connect(self._on_tab_changed)
        self._on_tab_changed(self._tab_widget.currentIndex())
        right_layout.addWidget(self._tab_widget)

        self._extract_points_panel = ExtractPointsPanel()
        self._player_panel.register_requested.connect(self._on_register_requested)
        self._extract_points_panel.entry_selected.connect(self._on_extract_point_selected)
        self._extract_points_panel.refresh_requested.connect(self._on_refresh_thumbnails_clicked)
        self._extract_points_panel.entries_changed.connect(self._on_extract_points_changed)

        splitter.addWidget(self._grid)
        splitter.addWidget(right_widget)
        splitter.addWidget(self._extract_points_panel)
        # 左(動画一覧)と右(抜き出しポイント一覧)は幅固定のため、余った幅は
        # 中央(プレビュー+タブ)だけが伸縮する。
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        root_layout.addWidget(splitter, 1)

        # 下部: 進捗バー
        progress_row = QHBoxLayout()
        self._progress_label = QLabel("進捗:")
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._remaining_label = QLabel("")
        self._cancel_button = QPushButton("キャンセル")
        self._cancel_button.clicked.connect(self._on_cancel_clicked)
        progress_row.addWidget(self._progress_label)
        progress_row.addWidget(self._progress_bar, 1)
        progress_row.addWidget(self._remaining_label)
        progress_row.addWidget(self._cancel_button)
        root_layout.addLayout(progress_row)
        self._set_progress_row_visible(False)

    def _set_progress_row_visible(self, visible: bool) -> None:
        for widget in (
            self._progress_label, self._progress_bar, self._remaining_label, self._cancel_button,
        ):
            widget.setVisible(visible)

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
        bind("Return", self._on_enter_pressed)
        bind("Enter", self._on_enter_pressed)  # テンキーのEnter

    def _on_enter_pressed(self) -> None:
        # 数値入力欄にフォーカスがある間は、値の確定用のEnterとして扱い実行しない
        focused = QApplication.focusWidget()
        if isinstance(focused, (QAbstractSpinBox, QLineEdit)):
            return
        current_tab = self._tab_widget.currentWidget()
        trigger = getattr(current_tab, "trigger_execute", None)
        if trigger is not None:
            trigger()

    def _on_tab_changed(self, index: int) -> None:
        # 削除タブが表示されている間だけ、そのモードに応じて動画プレビュー側の
        # IN/OUT設定もロックする(抜き出しタブ表示中は常にロック解除)。
        self._delete_panel.set_tab_active(self._tab_widget.widget(index) is self._delete_panel)

    # ------------------------------------------------------------- フォルダ

    def _restore_last_folder(self) -> None:
        last = self._settings.value(KEY_LAST_FOLDER, "", str)
        if last:
            folder = Path(last)
            if folder.is_dir():
                self._open_folder(folder)

    def _on_open_folder_clicked(self) -> None:
        start_dir = str(self._current_folder) if self._current_folder else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "フォルダを選択", start_dir)
        if folder:
            self._open_folder(Path(folder))

    def _on_settings_clicked(self) -> None:
        dialog = SettingsDialog(self._app_settings, self)
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            return

        self._app_settings = dialog.result_settings()
        save_app_settings(self._settings, self._app_settings)
        self._resolve_ffmpeg_paths()

        if self._ffmpeg_path is None or self._ffprobe_path is None:
            QMessageBox.warning(
                self, "ffmpeg / ffprobe が見つかりません",
                "指定されたパスが見つかりませんでした。設定を確認してください。",
            )
        else:
            self._show_toast("設定を保存しました。次にフォルダを開いたときから反映されます。")

    def _on_open_output_clicked(self) -> None:
        item = self._player_panel.current_item
        if item is not None:
            output_dir = self._effective_output_dir(item.path.parent)
        elif self._current_folder is not None:
            output_dir = self._effective_output_dir(self._current_folder)
        else:
            QMessageBox.information(self, "出力先を開く", "先にフォルダを選択するか、動画を選んでください。")
            return

        if not output_dir.is_dir():
            QMessageBox.information(self, "出力先を開く", f"まだ出力先フォルダがありません:\n{output_dir}")
            return
        try:
            subprocess.Popen(["explorer", str(output_dir)])
        except OSError:
            pass

    # ------------------------------------------------------------- プロジェクト

    def _on_save_project_clicked(self) -> None:
        if self._current_folder is None:
            QMessageBox.information(self, "プロジェクトを保存", "先にフォルダを選択してください。")
            return

        self._save_extract_points_for(self._current_video_path)

        default_path = self._current_folder / f"{self._current_folder.name}.vtproj"
        path_str, _ = QFileDialog.getSaveFileName(
            self, "プロジェクトを保存", str(default_path), PROJECT_FILE_FILTER,
        )
        if not path_str:
            return

        try:
            save_project(Path(path_str), self._current_folder, self._current_video_path, self._extract_points_by_video)
        except ProjectError as exc:
            QMessageBox.warning(self, "プロジェクトの保存に失敗しました", str(exc))
            return
        self._show_toast("プロジェクトを保存しました。")

    def _on_open_project_clicked(self) -> None:
        start_dir = str(self._current_folder) if self._current_folder else str(Path.home())
        path_str, _ = QFileDialog.getOpenFileName(
            self, "プロジェクトを開く", start_dir, PROJECT_FILE_FILTER,
        )
        if not path_str:
            return

        try:
            project = load_project(Path(path_str))
        except ProjectError as exc:
            QMessageBox.warning(self, "プロジェクトを読み込めません", str(exc))
            return

        if not project.folder.is_dir():
            QMessageBox.warning(
                self, "プロジェクトを読み込めません",
                f"保存されている動画フォルダが見つかりません:\n{project.folder}",
            )
            return

        self._open_folder(project.folder)  # ここで _extract_points_by_video 等はリセットされる
        self._extract_points_by_video = project.points_by_video

        if project.current_video is not None and self._grid.select_path(project.current_video):
            # フォルダを開いた直後は動画の読込が非同期のため、対象がまだ「読込中」の場合が
            # ある。読込完了時に _select_pending_project_video() が改めて選択し直す。
            self._pending_project_video_select = project.current_video

        self._show_toast("プロジェクトを読み込みました。")

    def _cancel_active_workers(self) -> None:
        """実行中の ffprobe/ffmpeg を直ちに終了させる(フォルダ切り替え・アプリ終了時に呼ぶ)。

        QThreadPool.clear() は未着手タスクの取消のみで、既に実行中のワーカーは
        最後まで動き続けてしまう(結果は世代チェックで捨てられるだけでプロセスは
        止まらない)。これがフォルダを切り替えてもディスクアクセスが止まらない原因
        だったため、ここで各ワーカーの cancel() を明示的に呼ぶ。
        """
        for worker in self._active_workers:
            worker.cancel()
        self._active_workers = []
        if self._keyframe_worker is not None:
            self._keyframe_worker.cancel()
            self._keyframe_worker = None
        self._pool.clear()

    def _open_folder(self, folder: Path) -> None:
        self._generation += 1
        generation = self._generation
        self._cancel_active_workers()

        self._current_folder = folder
        self._path_label.setText(str(folder))
        self._settings.setValue(KEY_LAST_FOLDER, str(folder))
        self._grid.clear()
        self._player_panel.clear()
        self._extract_points_panel.clear()
        self._extract_points_by_video = {}
        self._current_video_path = None
        self._pending_project_video_select = None

        try:
            files = scan_folder(folder, set(self._app_settings.extensions))
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
            self._active_workers.append(worker)
            self._pool.start(worker)

    def _reload_single_video(self, path: Path) -> None:
        """上書き保存後など、変更のあった動画1本だけを再読み込みする。

        以前は _open_folder() でフォルダ全体を再スキャンしていたが、これだと
        上書きした1本のためにフォルダ内の全動画へ ffprobe/ffmpeg が再実行され、
        大量のディスクアクセスが発生してしまっていた(動画本数が多いフォルダでは
        ディスク使用率が高止まりし続け、アプリが操作しづらくなるほどだった)。
        変更があった動画だけを対象にすることでこれを避ける。
        """
        if self._ffmpeg_path is None or self._ffprobe_path is None:
            return
        self._grid.set_loading(path)
        if self._current_video_path == path:
            self._player_panel.show_loading(path)
            self._pending_project_video_select = path
        worker = LoadVideoWorker(path, self._generation, self._ffmpeg_path, self._ffprobe_path)
        worker.signals.loaded.connect(self._on_video_loaded)
        worker.signals.failed.connect(self._on_video_failed)
        self._active_workers.append(worker)
        self._pool.start(worker)

    # --------------------------------------------------------------- 読込

    def _on_video_loaded(self, generation: int, path: Path, item: VideoItem) -> None:
        if generation != self._generation:
            return
        self._grid.update_loaded(path, item)
        self._select_pending_project_video(path)

    def _on_video_failed(self, generation: int, path: Path, message: str) -> None:
        if generation != self._generation:
            return
        self._grid.update_failed(path, message)
        self._select_pending_project_video(path)

    def _select_pending_project_video(self, loaded_path: Path) -> None:
        """プロジェクト読込直後、対象動画の読込(成功/失敗)が完了した時点で選択する。

        フォルダを開いた直後は各動画の読込が非同期のため、対象がまだ「読込中」の
        うちに選択しても、後で読込が終わった際にプレイヤーへ反映されない
        (このアプリの通常の仕様: 読込完了は選択とは独立して進む)。そのため、
        読込完了を待ってから選び直す。

        すでに「読込中」のうちに一度選択済み(グリッドの現在項目は既にこの動画)の
        ため、ここで select_path() を呼んでも QListView の currentChanged は
        再発火しない(選択インデックスが変化しないため)。それに依存すると
        いつまでもプレイヤーに反映されず「フリーズしたように見える」状態になって
        いたため、_on_video_selected() を直接呼んで確実に反映する。
        """
        if self._pending_project_video_select != loaded_path:
            return
        self._pending_project_video_select = None
        self._grid.select_path(loaded_path)
        self._on_video_selected(loaded_path)

    def _on_thumb_size_changed(self, index: int) -> None:
        size = self._size_combo.itemData(index)
        self._grid.set_thumb_size(size)
        self._settings.setValue(KEY_THUMB_SIZE, size)

    # --------------------------------------------------------------- 選択

    def _on_video_selected(self, path: Path) -> None:
        # 抜き出しポイントは動画ごとに保持する。切り替え前に表示中の分をしまい、
        # 切り替え先の動画用の一覧を呼び出す。
        self._save_extract_points_for(self._current_video_path)
        self._current_video_path = path
        self._load_extract_points_for(path)

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

    def _save_extract_points_for(self, path: Path | None) -> None:
        """表示中の抜き出しポイント一覧を、指定した動画用として保持しておく。"""
        if path is None:
            return
        entries = self._extract_points_panel.entries()
        if entries:
            self._extract_points_by_video[path] = entries
        else:
            self._extract_points_by_video.pop(path, None)

    def _load_extract_points_for(self, path: Path) -> None:
        """指定した動画用に保持している抜き出しポイント一覧を表示する(無ければ空)。"""
        self._extract_points_panel.load_entries(self._extract_points_by_video.get(path, []))

    # --------------------------------------------------------- 抜き出しポイント

    def _on_register_requested(self) -> None:
        request = self._extract_panel.current_request()
        if request is None:
            self._show_toast("IN/OUTを正しく設定してください。")
            return
        in_pixmap, out_pixmap = self._player_panel.current_in_out_pixmaps()
        self._extract_points_panel.add_entry(request, in_pixmap, out_pixmap)

    def _on_extract_point_selected(self, entry: ExtractPointEntry) -> None:
        """リストの行を選択したら、そのIN/OUTをプレイヤーパネルに呼び出す。"""
        request = entry.request
        self._extract_panel.set_exclude_audio(request.exclude_audio)
        self._player_panel.load_in_out(request.in_s, request.out_s)

    def _on_extract_points_changed(self) -> None:
        """抜き出しポイントリストの登録数が変化した(main_window はこれを使って
        抜き出しタブの実行ボタンの有効/無効を判定する。現在のIN/OUTが未設定でも、
        リストに登録があれば実行できるようにするため)。"""
        self._extract_panel.set_has_registered_points(not self._extract_points_panel.is_empty())

    def _on_refresh_thumbnails_clicked(self) -> None:
        """「更新」ボタン: 現在表示中の一覧の全項目について、現在の動画から
        サムネイルを再取得する(プロジェクト読込直後などサムネイルが無い項目用)。
        """
        if self._thumb_refresh_queue or self._current_job is not None:
            return  # 実行中(念のため。ボタンは無効化されているはず)
        if self._player_panel.current_item is None:
            self._show_toast("動画が読み込まれるまでお待ちください。")
            return
        entries = self._extract_points_panel.entries()
        if not entries:
            return

        self._thumb_refresh_resume_position = self._player_panel.current_position_seconds()
        self._thumb_refresh_queue = list(range(len(entries)))
        self._grid.setEnabled(False)
        self._extract_points_panel.set_controls_enabled(False)
        self._process_next_thumb_refresh()

    def _process_next_thumb_refresh(self) -> None:
        if not self._thumb_refresh_queue:
            if self._thumb_refresh_resume_position is not None:
                self._player_panel.seek_to(self._thumb_refresh_resume_position)
                self._thumb_refresh_resume_position = None
            self._grid.setEnabled(True)
            self._extract_points_panel.set_controls_enabled(True)
            self._show_toast("サムネイルを更新しました。")
            return

        index = self._thumb_refresh_queue.pop(0)
        entries = self._extract_points_panel.entries()
        if index >= len(entries):
            self._process_next_thumb_refresh()
            return
        request = entries[index].request
        out_pixmap_holder: list = [None]  # コールバック間で値を受け渡すための入れ物

        def on_in_captured(in_pixmap) -> None:
            self._extract_points_panel.update_thumbnails(index, in_pixmap, out_pixmap_holder[0])
            self._process_next_thumb_refresh()

        def on_out_captured(out_pixmap) -> None:
            out_pixmap_holder[0] = out_pixmap
            self._player_panel.capture_thumbnail_at(request.in_s, on_in_captured)

        self._player_panel.capture_thumbnail_at(request.out_s, on_out_captured)

    # ----------------------------------------------------------- キーフレーム

    def _start_keyframe_fetch(self, item: VideoItem) -> None:
        if self._keyframe_worker is not None:
            self._keyframe_worker.cancel()
            self._keyframe_worker = None
        if self._ffprobe_path is None:
            self._player_panel.set_keyframes_unavailable(item.path)
            return
        worker = KeyframeWorker(item.path, self._ffprobe_path)
        worker.signals.loaded.connect(self._on_keyframes_loaded)
        worker.signals.failed.connect(self._on_keyframes_failed)
        self._keyframe_worker = worker
        self._pool.start(worker)

    def _on_keyframes_loaded(self, path: Path, keyframes: list) -> None:
        self._player_panel.set_keyframes(path, keyframes)

    def _on_keyframes_failed(self, path: Path, message: str) -> None:
        self._player_panel.set_keyframes_unavailable(path)

    # --------------------------------------------------------------- 実行

    def _effective_output_dir(self, containing_folder: Path) -> Path:
        """設定で出力先が指定されていればそれを、無ければ元動画フォルダ直下の output/ を使う。"""
        if self._app_settings.output_dir:
            return Path(self._app_settings.output_dir)
        return containing_folder / "output"

    def _prepare_output(self, item: VideoItem, suffix: str) -> Path | None:
        """出力先フォルダを用意し、出力ファイルパスを返す。書き込めない場合は None。"""
        src = item.path
        output_dir = self._effective_output_dir(src.parent)
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "出力先に書き込めません", str(exc))
            return None

        dst = next_output_path(src, output_dir, suffix)
        assert dst != src, "出力先が元ファイルと同じになっています"
        return dst

    def _prepare_overwrite_temp(self, src: Path) -> Path:
        """上書きモード用の一時出力パスを返す(元ファイルと同じフォルダ、別名)。

        ffmpeg は読み込み元と書き込み先を同じファイルにできないため、
        一旦別名で書き出してから、成功時に元ファイルへ置き換える(_on_job_finished)。
        """
        return next_output_path(src, src.parent, "overwrite_tmp")

    def _on_extract_requested(self, request: ExtractRequest | None) -> None:
        if self._current_job is not None:
            return  # 実行中は無視(ボタンは無効化されているはずだが念のため)

        item = self._player_panel.current_item
        if item is None or self._ffmpeg_path is None:
            return

        # 抜き出しポイントリストに登録があればその全件を、無ければ現在のIN/OUT1件を処理する。
        # (request が None なのは、現在のIN/OUTが未設定でもリスト実行するケースのみ。)
        if self._extract_points_panel.is_empty():
            if request is None:
                return
            requests = [request]
        else:
            requests = [entry.request for entry in self._extract_points_panel.entries()]

        if len(requests) > 1 and self._extract_points_panel.combine_output():
            self._run_combined_extract(item, requests)
            return

        self._extract_queue = requests
        self._extract_queue_item = item
        self._extract_queue_total = len(requests)
        self._extract_queue_index = 0
        self._run_next_extract_in_queue()

    def _run_combined_extract(self, item: VideoItem, requests: list[ExtractRequest]) -> None:
        """登録済みの全区間を結合し、1本の動画として出力する
        (「1つの動画にまとめて出力する」チェック時)。

        各区間は個別には copy/encode どちらでも切り出せるが、結合(concat)は
        全区間が同じコーデック仕様である必要があるため、全区間が copy 可能な
        場合のみ copy、それ以外は安全側で全区間を encode に統一する。
        copy の場合は各区間のキーフレームスナップ後の開始点(decision.start)を、
        encode の場合は元のIN点をそのまま使う(encodeはキーフレーム整合が不要)。
        """
        if self._ffmpeg_path is None:
            return
        dst = self._prepare_output(item, "combined")
        if dst is None:
            return

        all_copy = all(r.decision.strategy == "copy" for r in requests)
        if all_copy:
            strategy: Strategy = "copy"
            ranges = [(r.decision.start, r.out_s) for r in requests]
        else:
            strategy = "encode"
            ranges = [(r.in_s, r.out_s) for r in requests]
        exclude_audio = any(r.exclude_audio for r in requests)

        job = DeleteMiddleJob(
            self._ffmpeg_path, item.path, dst, ranges,
            strategy, item.vcodec, item.acodec is not None, exclude_audio,
        )
        self._start_job(job, dst, "抜き出し(結合)", f"\n({len(requests)}区間を結合)")

    def _run_next_extract_in_queue(self) -> None:
        """抜き出しポイントリストの次の1件を処理する。全件処理後は何もしない。"""
        if not self._extract_queue or self._ffmpeg_path is None:
            self._extract_queue = []
            return

        item = self._extract_queue_item
        request = self._extract_queue.pop(0)
        self._extract_queue_index += 1

        dst = self._prepare_output(item, "clip")
        if dst is None:
            self._extract_queue = []  # 出力先を用意できない場合は残りも中断する
            return

        decision = request.decision
        cmd = build_cut_cmd(
            self._ffmpeg_path, item.path, dst,
            start=decision.start, end=request.out_s,
            strategy=decision.strategy, vcodec=item.vcodec,
            has_audio=item.acodec is not None, exclude_audio=request.exclude_audio,
        )

        job = FFmpegJob(cmd, total_duration_s=request.out_s - decision.start, dst=dst)
        snap_note = (
            f"\n(開始点を {seconds_to_timecode(decision.start)} にスナップして処理しました)"
            if decision.snapped else ""
        )
        if self._extract_queue_total > 1:
            snap_note += f"\n({self._extract_queue_index}/{self._extract_queue_total}件)"
        self._start_job(job, dst, "抜き出し", snap_note, continue_queue=bool(self._extract_queue))

    def _on_delete_requested(self, request: DeleteRequest) -> None:
        if self._current_job is not None:
            return

        item = self._player_panel.current_item
        if item is None or self._ffmpeg_path is None:
            return

        overwrite_target = item.path if request.overwrite else None
        if request.overwrite:
            dst = self._prepare_overwrite_temp(item.path)
        else:
            dst = self._prepare_output(item, "cut")
        if dst is None:
            return

        has_audio = item.acodec is not None
        snap_note = (
            f"\n(開始点を {seconds_to_timecode(request.snapped_point)} にスナップして処理しました)"
            if request.snapped and request.snapped_point is not None else ""
        )

        if len(request.ranges) == 1:
            start, end = request.ranges[0]
            cmd = build_cut_cmd(
                self._ffmpeg_path, item.path, dst,
                start=start, end=end, strategy=request.strategy,
                vcodec=item.vcodec, has_audio=has_audio,
            )
            job = FFmpegJob(cmd, total_duration_s=end - start, dst=dst)
        else:
            job = DeleteMiddleJob(
                self._ffmpeg_path, item.path, dst, request.ranges,
                request.strategy, item.vcodec, has_audio,
            )

        self._start_job(job, dst, "削除", snap_note, overwrite_target=overwrite_target)

    def _start_job(
        self,
        job: FFmpegJob | DeleteMiddleJob,
        dst: Path,
        operation_label: str,
        snap_note: str,
        overwrite_target: Path | None = None,
        continue_queue: bool = False,
    ) -> None:
        job.signals.progress.connect(self._on_job_progress)
        job.signals.finished.connect(
            lambda ok, msg: self._on_job_finished(
                ok, msg, dst, operation_label, snap_note, overwrite_target, continue_queue,
            )
        )
        self._current_job = job
        self._job_start_time = time.monotonic()
        self._set_ui_busy(True)
        self._job_pool.start(job)

    def _on_job_progress(self, fraction: float) -> None:
        self._progress_bar.setValue(round(fraction * 100))
        elapsed = time.monotonic() - self._job_start_time
        if fraction > 0.02:
            remaining = elapsed * (1.0 - fraction) / fraction
            self._remaining_label.setText(f"残り約 {round(remaining)}秒")
        else:
            self._remaining_label.setText("")

    def _on_job_finished(
        self,
        success: bool,
        message: str,
        dst: Path,
        operation_label: str,
        snap_note: str,
        overwrite_target: Path | None = None,
        continue_queue: bool = False,
    ) -> None:
        self._current_job = None
        self._set_ui_busy(False)
        self._set_progress_row_visible(False)

        if success:
            final_path = dst
            if overwrite_target is not None:
                final_path = self._finish_overwrite(dst, overwrite_target)
            self._show_toast(
                f"{operation_label}が完了しました: {final_path.name}{snap_note}",
                action_text="フォルダを開く",
                on_action=lambda: self._open_in_explorer(final_path),
            )
            if continue_queue:
                self._run_next_extract_in_queue()
            return

        # 失敗・キャンセル時は抜き出しポイントリストの残りの処理も中断する。
        self._extract_queue = []

        if message == "キャンセルされました":
            self._show_toast("処理をキャンセルしました。")
            return

        log_path = self._write_error_log(operation_label, message)
        tail = "\n".join(message.splitlines()[-20:])
        log_note = f"\n\n詳細ログ: {log_path}" if log_path is not None else ""
        QMessageBox.critical(
            self, "処理に失敗しました", (tail or "不明なエラーが発生しました。") + log_note,
        )

    def _finish_overwrite(self, tmp_path: Path, target: Path) -> Path:
        """上書きモード: 一時出力ファイルを元ファイルへ置き換える。成功時は元ファイルの
        パスを、失敗時は一時ファイルのパスを返す(呼び出し側はこれを最終的な結果として扱う)。

        置き換え前にプレイヤーを解放しておく必要がある(元ファイルを再生中のまま
        だとロックされて置き換えに失敗する場合がある)。置き換え後は変更のあった
        動画1本だけを再読込みし、サムネイル・メタ情報を新しい内容に合わせて更新する
        (フォルダ全体を再読込みすると他の動画すべてに ffprobe/ffmpeg が再実行され、
        ディスクアクセスが止まらなくなるため)。
        """
        if self._player_panel.current_item is not None and self._player_panel.current_item.path == target:
            self._player_panel.clear()
        try:
            os.replace(str(tmp_path), str(target))
        except OSError as exc:
            QMessageBox.warning(
                self, "元ファイルの置き換えに失敗しました",
                f"処理結果は作成できましたが、元ファイルへの置き換えに失敗しました:\n{exc}\n\n"
                f"処理結果はここに残っています: {tmp_path}",
            )
            return tmp_path

        # 上書きにより内容が変わったため、この動画に紐づく抜き出しポイントは無効化する。
        self._extract_points_by_video.pop(target, None)
        if self._current_video_path == target:
            self._extract_points_panel.clear()

        if self._current_folder is not None and target.parent == self._current_folder:
            self._reload_single_video(target)
        return target

    def _show_toast(self, message: str, action_text: str | None = None, on_action=None) -> None:
        if self._current_toast is not None:
            # 直前のトーストは自動タイマーや×ボタンで既にC++側が破棄されている
            # ことがある(WA_DeleteOnClose)。その場合 close() は例外になるため無視する。
            try:
                self._current_toast.close()
            except RuntimeError:
                pass
        self._current_toast = Toast(self, message, action_text=action_text, on_action=on_action)

    def _write_error_log(self, operation_label: str, message: str) -> Path | None:
        """§11: ffmpeg が非ゼロ終了した際の全文ログを保存する(ダイアログには末尾20行のみ表示)。"""
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = LOG_DIR / f"{timestamp}_{operation_label}.log"
            log_path.write_text(message, encoding="utf-8")
            return log_path
        except OSError:
            return None

    def _open_in_explorer(self, path: Path) -> None:
        try:
            subprocess.Popen(["explorer", "/select,", str(path)])
        except OSError:
            pass

    def _on_cancel_clicked(self) -> None:
        if self._current_job is not None:
            self._current_job.cancel()
            self._cancel_button.setEnabled(False)

    def closeEvent(self, event: QCloseEvent) -> None:
        """アプリ終了時に実行中の ffprobe/ffmpeg を確実に終了させる。

        これを行わないと、フォルダ読み込み中(probe/サムネイル生成)や編集ジョブ実行中に
        アプリを閉じた場合、子プロセスが孤児化してディスクアクセスを続けてしまう
        (subprocess は親プロセスの終了で自動的には終了しない)。
        """
        self._cancel_active_workers()
        if self._current_job is not None:
            self._current_job.cancel()
        self._job_pool.clear()
        self._pool.waitForDone(3000)
        self._job_pool.waitForDone(3000)
        super().closeEvent(event)

    def _set_ui_busy(self, busy: bool) -> None:
        self._grid.setEnabled(not busy)
        self._open_button.setEnabled(not busy)
        self._save_project_button.setEnabled(not busy)
        self._open_project_button.setEnabled(not busy)
        self._extract_panel.set_running(busy)
        self._delete_panel.set_running(busy)
        self._extract_points_panel.set_controls_enabled(not busy)
        self._set_progress_row_visible(busy)
        if busy:
            self._progress_bar.setValue(0)
            self._remaining_label.setText("")
            self._cancel_button.setEnabled(True)
