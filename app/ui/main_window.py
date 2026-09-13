"""メインウィンドウ。Phase 5: フォルダ選択 + 一覧 + プレビュー + 抜き出し/削除 + 設定。"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
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
from app.core.edit_ops import build_cut_cmd
from app.core.ffmpeg_runner import DeleteMiddleJob, FFmpegJob, find_ffmpeg, find_ffprobe
from app.core.keyframes import KeyframeWorker
from app.core.models import VideoItem
from app.core.video_loader import LoadVideoWorker, scan_folder
from app.ui.delete_panel import DeletePanel, DeleteRequest
from app.ui.extract_panel import ExtractPanel, ExtractRequest
from app.ui.player_panel import PlayerPanel
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
        self.resize(1100, 700)

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
        right_layout.addWidget(self._tab_widget)

        splitter.addWidget(self._grid)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
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

    def _open_folder(self, folder: Path) -> None:
        self._generation += 1
        generation = self._generation
        self._pool.clear()  # 未処理タスクをキャンセル

        self._current_folder = folder
        self._path_label.setText(str(folder))
        self._settings.setValue(KEY_LAST_FOLDER, str(folder))
        self._grid.clear()
        self._player_panel.clear()

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
        self._settings.setValue(KEY_THUMB_SIZE, size)

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

    def _on_extract_requested(self, request: ExtractRequest) -> None:
        if self._current_job is not None:
            return  # 実行中は無視(ボタンは無効化されているはずだが念のため)

        item = self._player_panel.current_item
        if item is None or self._ffmpeg_path is None:
            return

        dst = self._prepare_output(item, "clip")
        if dst is None:
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
        self._start_job(job, dst, "抜き出し", snap_note)

    def _on_delete_requested(self, request: DeleteRequest) -> None:
        if self._current_job is not None:
            return

        item = self._player_panel.current_item
        if item is None or self._ffmpeg_path is None:
            return

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

        self._start_job(job, dst, "削除", snap_note)

    def _start_job(self, job: FFmpegJob | DeleteMiddleJob, dst: Path, operation_label: str, snap_note: str) -> None:
        job.signals.progress.connect(self._on_job_progress)
        job.signals.finished.connect(
            lambda ok, msg: self._on_job_finished(ok, msg, dst, operation_label, snap_note)
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
        self, success: bool, message: str, dst: Path, operation_label: str, snap_note: str,
    ) -> None:
        self._current_job = None
        self._set_ui_busy(False)
        self._set_progress_row_visible(False)

        if success:
            self._show_toast(
                f"{operation_label}が完了しました: {dst.name}{snap_note}",
                action_text="フォルダを開く",
                on_action=lambda: self._open_in_explorer(dst),
            )
            return

        if message == "キャンセルされました":
            self._show_toast("処理をキャンセルしました。")
            return

        log_path = self._write_error_log(operation_label, message)
        tail = "\n".join(message.splitlines()[-20:])
        log_note = f"\n\n詳細ログ: {log_path}" if log_path is not None else ""
        QMessageBox.critical(
            self, "処理に失敗しました", (tail or "不明なエラーが発生しました。") + log_note,
        )

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

    def _set_ui_busy(self, busy: bool) -> None:
        self._grid.setEnabled(not busy)
        self._open_button.setEnabled(not busy)
        self._extract_panel.set_running(busy)
        self._delete_panel.set_running(busy)
        self._set_progress_row_visible(busy)
        if busy:
            self._progress_bar.setValue(0)
            self._remaining_label.setText("")
            self._cancel_button.setEnabled(True)
