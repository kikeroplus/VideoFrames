"""プレビュー・再生パネル。仕様書 §5.3 参照。

QMediaPlayer + QVideoWidget による再生・シーク・コマ送りと、
現在位置の HH:MM:SS.mmm ＋ フレーム番号表示を提供する。
IN/OUT点の設定はここで受け付けるが、抜き出し/削除タブ（Phase 3/4）で使う
ための内部状態として保持するだけで、タブ側の処理はまだ実装しない。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

from app.core.models import VideoItem
from app.utils.timecode import seconds_to_frame, seconds_to_timecode


class PlayerPanel(QWidget):
    in_point_changed = Signal(object)  # float | None
    out_point_changed = Signal(object)  # float | None

    def __init__(self, parent=None):
        super().__init__(parent)
        self._item: VideoItem | None = None
        self._in_point_s: float | None = None
        self._out_point_s: float | None = None
        self._seeking_by_user = False

        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)

        self._video_widget = QVideoWidget(self)
        self._video_widget.setMinimumHeight(240)
        self._player.setVideoOutput(self._video_widget)

        self._header_label = QLabel("動画を選択してください")
        self._header_label.setStyleSheet("color: palette(mid);")
        self._header_label.setWordWrap(True)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 0)
        self._slider.sliderMoved.connect(self._on_slider_moved)
        self._slider.sliderPressed.connect(self._on_slider_pressed)
        self._slider.sliderReleased.connect(self._on_slider_released)

        self._back10_button = QPushButton("◄◄ 10s")
        self._play_button = QPushButton("▶")
        self._fwd10_button = QPushButton("10s ▶▶")
        self._back10_button.clicked.connect(lambda: self.step_seconds(-10))
        self._play_button.clicked.connect(self.toggle_play_pause)
        self._fwd10_button.clicked.connect(lambda: self.step_seconds(10))

        self._time_label = QLabel("00:00:00.000 (frame 0) / 00:00:00.000")

        self._frame_back_button = QPushButton("◄コマ")
        self._frame_fwd_button = QPushButton("コマ▶")
        self._frame_back_button.clicked.connect(lambda: self.step_frame(-1))
        self._frame_fwd_button.clicked.connect(lambda: self.step_frame(1))

        self._in_button = QPushButton("IN設定")
        self._out_button = QPushButton("OUT設定")
        self._in_button.clicked.connect(self.set_in_point)
        self._out_button.clicked.connect(self.set_out_point)
        self._in_out_label = QLabel("IN: -   OUT: -")

        controls_row = QHBoxLayout()
        controls_row.addWidget(self._back10_button)
        controls_row.addWidget(self._play_button)
        controls_row.addWidget(self._fwd10_button)
        controls_row.addWidget(self._time_label, 1)

        frame_row = QHBoxLayout()
        frame_row.addWidget(self._frame_back_button)
        frame_row.addWidget(self._frame_fwd_button)
        frame_row.addStretch(1)
        frame_row.addWidget(self._in_button)
        frame_row.addWidget(self._out_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self._header_label)
        layout.addWidget(self._video_widget, 1)
        layout.addWidget(self._slider)
        layout.addLayout(controls_row)
        layout.addLayout(frame_row)
        layout.addWidget(self._in_out_label)

        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._player.errorOccurred.connect(self._on_error)

        self._set_controls_enabled(False)

    # ------------------------------------------------------------- 読み込み

    def load_video(self, item: VideoItem) -> None:
        self._item = item
        self._in_point_s = None
        self._out_point_s = None
        self.in_point_changed.emit(None)
        self.out_point_changed.emit(None)
        self._update_in_out_label()

        self._header_label.setText(
            f"{item.path.name}  ({item.width}x{item.height}  {item.fps:.2f}fps  "
            f"video:{item.vcodec} audio:{item.acodec or 'なし'})"
        )
        self._player.stop()
        self._player.setSource(QUrl.fromLocalFile(str(item.path)))
        self._set_controls_enabled(True)
        self._update_time_label(0.0)

    def show_loading(self, path: Path) -> None:
        self._item = None
        self._player.stop()
        self._player.setSource(QUrl())
        self._header_label.setText(f"{path.name}  読み込み中…")
        self._set_controls_enabled(False)

    def show_error(self, path: Path, message: str) -> None:
        self._item = None
        self._player.stop()
        self._player.setSource(QUrl())
        self._header_label.setText(f"{path.name}  読み込み不可: {message}")
        self._set_controls_enabled(False)

    def clear(self) -> None:
        self._item = None
        self._player.stop()
        self._player.setSource(QUrl())
        self._header_label.setText("動画を選択してください")
        self._set_controls_enabled(False)

    # --------------------------------------------------------------- 操作

    def toggle_play_pause(self) -> None:
        if self._item is None:
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def step_frame(self, delta: int) -> None:
        if self._item is None or self._item.fps <= 0:
            return
        self._player.pause()
        frame_ms = 1000.0 / self._item.fps
        new_ms = max(0, round(self._player.position() + delta * frame_ms))
        self._player.setPosition(new_ms)

    def step_seconds(self, delta: float) -> None:
        if self._item is None:
            return
        new_ms = max(0, round(self._player.position() + delta * 1000))
        self._player.setPosition(new_ms)

    def set_in_point(self) -> None:
        if self._item is None:
            return
        self._in_point_s = self._player.position() / 1000.0
        self._update_in_out_label()
        self.in_point_changed.emit(self._in_point_s)

    def set_out_point(self) -> None:
        if self._item is None:
            return
        self._out_point_s = self._player.position() / 1000.0
        self._update_in_out_label()
        self.out_point_changed.emit(self._out_point_s)

    @property
    def in_point(self) -> float | None:
        return self._in_point_s

    @property
    def out_point(self) -> float | None:
        return self._out_point_s

    @property
    def current_item(self) -> VideoItem | None:
        return self._item

    def current_position_seconds(self) -> float:
        return self._player.position() / 1000.0

    # -------------------------------------------------------------- 内部

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self._slider, self._back10_button, self._play_button, self._fwd10_button,
            self._frame_back_button, self._frame_fwd_button,
            self._in_button, self._out_button,
        ):
            widget.setEnabled(enabled)

    def _update_in_out_label(self) -> None:
        in_text = seconds_to_timecode(self._in_point_s) if self._in_point_s is not None else "-"
        out_text = seconds_to_timecode(self._out_point_s) if self._out_point_s is not None else "-"
        self._in_out_label.setText(f"IN: {in_text}   OUT: {out_text}")

    def _update_time_label(self, position_s: float) -> None:
        fps = self._item.fps if self._item else 0.0
        duration_s = self._item.duration if self._item else 0.0
        frame = seconds_to_frame(position_s, fps) if fps > 0 else 0
        self._time_label.setText(
            f"{seconds_to_timecode(position_s)} (frame {frame}) / {seconds_to_timecode(duration_s)}"
        )

    def _on_position_changed(self, position_ms: int) -> None:
        if not self._seeking_by_user:
            self._slider.blockSignals(True)
            self._slider.setValue(position_ms)
            self._slider.blockSignals(False)
        self._update_time_label(position_ms / 1000.0)

    def _on_duration_changed(self, duration_ms: int) -> None:
        self._slider.setRange(0, duration_ms)

    def _on_playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        is_playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._play_button.setText("❚❚" if is_playing else "▶")

    def _on_error(self, error, error_string: str) -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        self._header_label.setText(f"再生エラー: {error_string}")

    def _on_slider_pressed(self) -> None:
        self._seeking_by_user = True

    def _on_slider_moved(self, position_ms: int) -> None:
        self._player.setPosition(position_ms)

    def _on_slider_released(self) -> None:
        self._seeking_by_user = False
