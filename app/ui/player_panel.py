"""プレビュー・再生パネル。仕様書 §5.3 参照。

QMediaPlayer + QVideoWidget による再生・シーク・コマ送りと、
現在位置の HH:MM:SS.mmm ＋ フレーム番号表示を提供する。
IN/OUT点の設定はここで受け付けるが、抜き出し/削除タブ（Phase 3/4）で使う
ための内部状態として保持するだけで、タブ側の処理はまだ実装しない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QImage, QPainter, QPen, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

from app.core.keyframes import nearest_keyframe, next_keyframe, prev_keyframe
from app.core.models import VideoItem
from app.utils.timecode import seconds_to_frame, seconds_to_timecode

IN_OUT_THUMB_SIZE = (96, 54)  # 幅, 高さ(px)

# QVideoWidget が他ウィジェットに埋め込まれていると、一度も play() していない
# 状態では videoFrameChanged が発火せず、シークだけでは映像が更新されない
# (トップレベルウィンドウとして単体表示した場合のみ例外的に動く)。
# ロード直後に極短時間だけ無音で再生してレンダリングパイプラインを
# 初期化する(「プライミング」)ことで、以後は再生なしのシークでも
# 正しく映像・サムネイルが得られるようにする。
PRIME_DURATION_MS = 120

# OUT点のサムネイル取得のためにシークする際、動画末尾ぴったりへのシークだと
# フレームが更新されない場合があるため、末尾からこの分だけ手前を狙う。
THUMB_END_SEEK_MARGIN_MS = 200


class KeyframeSlider(QSlider):
    """シークバー。キーフレーム位置を細い縦線で描画する（仕様書 §8.4）。"""

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._keyframe_positions_ms: list[int] = []
        # 既定のノブが細く見づらいため、太い縁取り付きの円で強調する
        self.setStyleSheet(
            """
            QSlider::groove:horizontal { height: 4px; background: palette(mid); border-radius: 2px; }
            QSlider::sub-page:horizontal { background: palette(highlight); border-radius: 2px; }
            QSlider::handle:horizontal {
                background: palette(base);
                border: 2px solid palette(highlight);
                width: 16px;
                height: 16px;
                margin: -7px 0;
                border-radius: 9px;
            }
            """
        )

    def set_keyframes(self, keyframes_s: list[float]) -> None:
        self._keyframe_positions_ms = [round(k * 1000) for k in keyframes_s]
        self.update()

    def clear_keyframes(self) -> None:
        self._keyframe_positions_ms = []
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._keyframe_positions_ms or self.maximum() <= self.minimum():
            return

        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderGroove, self
        )
        span = max(1, groove_rect.width())

        painter = QPainter(self)
        painter.setPen(QPen(Qt.GlobalColor.darkYellow, 1))
        for pos_ms in self._keyframe_positions_ms:
            if pos_ms < self.minimum() or pos_ms > self.maximum():
                continue
            x = groove_rect.x() + QStyle.sliderPositionFromValue(
                self.minimum(), self.maximum(), pos_ms, span, option.upsideDown
            )
            painter.drawLine(x, groove_rect.y(), x, groove_rect.y() + groove_rect.height())
        painter.end()


class ClickableThumbLabel(QLabel):
    """クリックでシグナルを発行するサムネイル表示用ラベル。"""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class PlayerPanel(QWidget):
    in_point_changed = Signal(object)  # float | None
    out_point_changed = Signal(object)  # float | None
    keyframes_changed = Signal()
    video_changed = Signal()  # 選択中の動画が切り替わった(読込/読込中/エラー/クリアの全パターン)
    register_requested = Signal()  # 「登録」ボタン: 現在のIN/OUTを抜き出しポイントリストへ追加

    def __init__(self, parent=None):
        super().__init__(parent)
        self._item: VideoItem | None = None
        self._in_point_s: float | None = None
        self._out_point_s: float | None = None
        self._in_locked = False  # 削除タブの冒頭から/末尾までモードなど、外部からの一時的な操作禁止
        self._out_locked = False
        self._seeking_by_user = False
        self._latest_frame_image: QImage | None = None
        self._keyframes: list[float] | None = None
        self._keyframe_state: str = "none"  # "none" | "loading" | "ready" | "unavailable"

        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)

        self._video_widget = QVideoWidget(self)
        self._video_widget.setMinimumHeight(240)
        self._player.setVideoOutput(self._video_widget)
        self._video_widget.videoSink().videoFrameChanged.connect(self._on_video_frame)

        self._header_label = QLabel("動画を選択してください")
        self._header_label.setStyleSheet("color: palette(mid);")
        self._header_label.setWordWrap(True)

        self._slider = KeyframeSlider()
        self._slider.setRange(0, 0)
        self._slider.sliderMoved.connect(self._on_slider_moved)
        self._slider.sliderPressed.connect(self._on_slider_pressed)
        self._slider.sliderReleased.connect(self._on_slider_released)

        self._back10_button = QPushButton("◄◄ 10s")
        self._back1_button = QPushButton("◄ 1s")
        self._play_button = QPushButton("▶")
        self._fwd1_button = QPushButton("1s ▶")
        self._fwd10_button = QPushButton("10s ▶▶")
        self._back10_button.clicked.connect(lambda: self.step_seconds(-10))
        self._back1_button.clicked.connect(lambda: self.step_seconds(-1))
        self._play_button.clicked.connect(self.toggle_play_pause)
        self._fwd1_button.clicked.connect(lambda: self.step_seconds(1))
        self._fwd10_button.clicked.connect(lambda: self.step_seconds(10))

        self._time_label = QLabel("00:00:00.000 (frame 0) / 00:00:00.000")

        self._frame_back_button = QPushButton("◄コマ")
        self._frame_fwd_button = QPushButton("コマ▶")
        self._frame_back_button.clicked.connect(lambda: self.step_frame(-1))
        self._frame_fwd_button.clicked.connect(lambda: self.step_frame(1))

        self._keyframe_status_label = QLabel("キーフレーム: -")
        self._keyframe_status_label.setStyleSheet("color: palette(mid);")

        self._force_keyframe_checkbox = QCheckBox("IN/OUT設定をキーフレームに強制的に揃える")
        self._force_keyframe_checkbox.setToolTip(
            "オンの場合、「IN設定」/「OUT設定」ボタン(Iキー/Oキー)を押した時点で、\n"
            "現在位置ではなく最も近いキーフレームの位置を採用する。"
        )

        self._in_button = QPushButton("IN設定")
        self._out_button = QPushButton("OUT設定")
        for button in (self._in_button, self._out_button):
            button.setMinimumHeight(44)
            button.setStyleSheet("font-size: 14pt; font-weight: bold;")
        self._in_button.clicked.connect(self.set_in_point)
        self._out_button.clicked.connect(self.set_out_point)

        self._in_thumb_label = self._make_thumb_label()
        self._out_thumb_label = self._make_thumb_label()
        self._in_thumb_label.clicked.connect(lambda: self._seek_to_marked_point(self._in_point_s))
        self._out_thumb_label.clicked.connect(lambda: self._seek_to_marked_point(self._out_point_s))
        self._in_text_label = QLabel("IN: -")
        self._out_text_label = QLabel("OUT: -")

        self._in_out_length_label = QLabel("長さ: -")
        self._in_out_length_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._preview_button = QPushButton("▶ IN-OUT再生")
        self._preview_button.setEnabled(False)
        self._preview_button.clicked.connect(self.toggle_preview_in_out)
        self._previewing = False
        self._preview_watcher = None

        self._register_button = QPushButton("登録")
        self._register_button.setEnabled(False)
        self._register_button.setToolTip("現在のIN/OUTを抜き出しポイントリストに追加する")
        self._register_button.clicked.connect(self.register_requested.emit)

        controls_row = QHBoxLayout()
        controls_row.addWidget(self._back10_button)
        controls_row.addWidget(self._back1_button)
        controls_row.addWidget(self._play_button)
        controls_row.addWidget(self._fwd1_button)
        controls_row.addWidget(self._fwd10_button)
        controls_row.addWidget(self._time_label, 1)

        frame_row = QHBoxLayout()
        frame_row.addWidget(self._frame_back_button)
        frame_row.addWidget(self._frame_fwd_button)
        frame_row.addStretch(1)
        frame_row.addWidget(self._force_keyframe_checkbox)
        frame_row.addWidget(self._keyframe_status_label)

        in_out_row = QHBoxLayout()
        in_out_row.addWidget(self._in_thumb_label)
        in_out_row.setAlignment(self._in_thumb_label, Qt.AlignmentFlag.AlignBottom)
        in_col = QVBoxLayout()
        in_col.addWidget(self._in_text_label)
        in_col.addWidget(self._in_button)
        in_out_row.addLayout(in_col)
        in_out_row.setAlignment(in_col, Qt.AlignmentFlag.AlignBottom)
        in_out_row.addStretch(1)
        preview_col = QVBoxLayout()
        preview_col.addWidget(self._in_out_length_label)
        preview_col.addWidget(self._preview_button)
        preview_col.addWidget(self._register_button)
        in_out_row.addLayout(preview_col)
        in_out_row.setAlignment(preview_col, Qt.AlignmentFlag.AlignBottom)
        in_out_row.addStretch(1)
        in_out_row.addWidget(self._out_thumb_label)
        in_out_row.setAlignment(self._out_thumb_label, Qt.AlignmentFlag.AlignBottom)
        out_col = QVBoxLayout()
        out_col.addWidget(self._out_text_label)
        out_col.addWidget(self._out_button)
        in_out_row.addLayout(out_col)
        in_out_row.setAlignment(out_col, Qt.AlignmentFlag.AlignBottom)

        layout = QVBoxLayout(self)
        layout.addWidget(self._header_label)
        layout.addWidget(self._video_widget, 1)
        layout.addWidget(self._slider)
        layout.addLayout(controls_row)
        layout.addLayout(frame_row)
        layout.addLayout(in_out_row)

        self._pending_prime = False
        self._priming_active = False

        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._player.errorOccurred.connect(self._on_error)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)

        self._set_controls_enabled(False)

    # ------------------------------------------------------------- 読み込み

    def load_video(self, item: VideoItem) -> None:
        self._cancel_preview_watch()
        self._item = item
        self._in_point_s = None
        self._out_point_s = None
        self._latest_frame_image = None
        self.in_point_changed.emit(None)
        self.out_point_changed.emit(None)
        self._update_in_out_label()
        self._in_thumb_label.clear()
        self._out_thumb_label.clear()
        self._keyframes = None
        self._keyframe_state = "loading"
        self._slider.clear_keyframes()
        self._keyframe_status_label.setText("キーフレーム: 判定中…")
        self.keyframes_changed.emit()

        self._header_label.setText(
            f"{item.path.name}  ({item.width}x{item.height}  {item.fps:.2f}fps  "
            f"video:{item.vcodec} audio:{item.acodec or 'なし'})"
        )
        self._player.stop()
        self._pending_prime = True
        self._player.setSource(QUrl.fromLocalFile(str(item.path)))
        self._set_controls_enabled(True)
        self._update_time_label(0.0)
        self.video_changed.emit()

    def show_loading(self, path: Path) -> None:
        self._cancel_preview_watch()
        self._item = None
        self._pending_prime = False
        self._player.stop()
        self._player.setSource(QUrl())
        self._header_label.setText(f"{path.name}  読み込み中…")
        self._set_controls_enabled(False)
        self._reset_keyframe_display()
        self.video_changed.emit()

    def show_error(self, path: Path, message: str) -> None:
        self._cancel_preview_watch()
        self._item = None
        self._pending_prime = False
        self._player.stop()
        self._player.setSource(QUrl())
        self._header_label.setText(f"{path.name}  読み込み不可: {message}")
        self._set_controls_enabled(False)
        self._reset_keyframe_display()
        self.video_changed.emit()

    def clear(self) -> None:
        self._cancel_preview_watch()
        self._item = None
        self._pending_prime = False
        self._player.stop()
        self._player.setSource(QUrl())
        self._header_label.setText("動画を選択してください")
        self._set_controls_enabled(False)
        self._reset_keyframe_display()
        self.video_changed.emit()

    def _reset_keyframe_display(self) -> None:
        self._keyframes = None
        self._keyframe_state = "none"
        self._slider.clear_keyframes()
        self._keyframe_status_label.setText("キーフレーム: -")
        self.keyframes_changed.emit()

    # --------------------------------------------------------------- 操作

    def toggle_play_pause(self) -> None:
        if self._item is None:
            return
        self._cancel_preview_watch()
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def step_frame(self, delta: int) -> None:
        if self._item is None or self._item.fps <= 0:
            return
        self._cancel_preview_watch()
        self._priming_active = False
        self._player.pause()
        frame_ms = 1000.0 / self._item.fps
        new_ms = max(0, round(self._player.position() + delta * frame_ms))
        self._player.setPosition(new_ms)

    def step_seconds(self, delta: float) -> None:
        if self._item is None:
            return
        self._cancel_preview_watch()
        self._priming_active = False
        new_ms = max(0, round(self._player.position() + delta * 1000))
        self._player.setPosition(new_ms)

    def set_in_point(self) -> None:
        if self._item is None or self._in_locked:
            return
        position_s = self._player.position() / 1000.0
        if self._force_keyframe_checkbox.isChecked() and self._keyframes:
            snapped = nearest_keyframe(position_s, self._keyframes)
            if snapped is not None:
                position_s = snapped
        # set_in_point_value() のように一時停止・シークはしない(再生中に押しても
        # 再生が止まらないようにするため)。サムネは現在表示中のライブフレームを
        # そのまま使う(キーフレーム位置ぴったりではなく近似になる)。
        self._in_point_s = position_s
        self._update_in_out_label()
        self._set_thumb(self._in_thumb_label)
        self.in_point_changed.emit(self._in_point_s)

    def set_in_point_value(self, seconds: float) -> None:
        """IN点を任意の秒数へ移動する(§8.4 のキーフレームスナップ用)。

        実際にその位置までシークし、届いたフレームでサムネも更新する。
        """
        if self._item is None:
            return
        self._cancel_preview_watch()
        self._priming_active = False
        self._in_point_s = max(0.0, min(seconds, self._item.duration))
        self._update_in_out_label()
        self.in_point_changed.emit(self._in_point_s)
        self._player.pause()
        self._seek_and_capture_thumb(round(self._in_point_s * 1000), self._in_thumb_label)

    def _capture_thumb_on_next_frame(self, label: QLabel) -> None:
        def once(frame: QVideoFrame) -> None:
            if frame.isValid():
                self._latest_frame_image = frame.toImage()
                self._set_thumb(label)
            self._video_widget.videoSink().videoFrameChanged.disconnect(once)

        self._video_widget.videoSink().videoFrameChanged.connect(once)

    def _seek_and_capture_thumb(self, position_ms: int, label: QLabel) -> None:
        """指定位置へシークし、届いたフレームでサムネを更新する。

        既にその位置にいる場合は setPosition() が新しいフレームイベントを発火せず、
        _capture_thumb_on_next_frame() が永遠に待ち続けてサムネが古いまま(または
        空のまま)になってしまう。冒頭から/末尾までモードの切り替え時など、IN/OUT点が
        既に固定値になっている状態で再度同じ値をセットするケースがこれに当たるため、
        その場合は現在保持しているフレームをそのまま反映する。
        """
        if self._player.position() == position_ms:
            self._set_thumb(label)
        else:
            self._player.setPosition(position_ms)
            self._capture_thumb_on_next_frame(label)

    def capture_thumbnail_at(self, seconds: float, callback: Callable[[QPixmap | None], None]) -> None:
        """指定した時刻のフレームを取得してコールバックに渡す。

        IN/OUT点の状態やサムネイル表示は変更しない(抜き出しポイントリストの
        サムネイル再取得など、既存のIN/OUTとは無関係に任意の時刻のフレームが
        必要な場合に使う)。動画が読み込まれていない場合は None を渡す。
        """
        if self._item is None:
            callback(None)
            return

        target_ms = round(max(0.0, min(seconds, self._item.duration)) * 1000)

        def deliver() -> None:
            if self._latest_frame_image is None:
                callback(None)
            else:
                callback(QPixmap.fromImage(self._latest_frame_image))

        self._cancel_preview_watch()
        self._priming_active = False
        self._player.pause()

        if self._player.position() == target_ms:
            deliver()
            return

        def once(frame: QVideoFrame) -> None:
            if frame.isValid():
                self._latest_frame_image = frame.toImage()
            self._video_widget.videoSink().videoFrameChanged.disconnect(once)
            deliver()

        self._video_widget.videoSink().videoFrameChanged.connect(once)
        self._player.setPosition(target_ms)

    def set_out_point(self) -> None:
        if self._item is None or self._out_locked:
            return
        position_s = self._player.position() / 1000.0
        if self._force_keyframe_checkbox.isChecked() and self._keyframes:
            snapped = nearest_keyframe(position_s, self._keyframes)
            if snapped is not None:
                position_s = snapped
        # set_out_point_value() のように一時停止・シークはしない(再生中に押しても
        # 再生が止まらないようにするため)。サムネは現在表示中のライブフレームを
        # そのまま使う(キーフレーム位置ぴったりではなく近似になる)。
        self._out_point_s = position_s
        self._update_in_out_label()
        self._set_thumb(self._out_thumb_label)
        self.out_point_changed.emit(self._out_point_s)

    def set_in_point_locked(self, locked: bool) -> None:
        """外部(削除タブの冒頭から/末尾までモードなど)からIN点操作を一時的に禁止する。

        「IN設定」ボタンとIN表示をグレーアウトし、set_in_point()(ボタン/Iキー)を
        無効化する。set_in_point_value() による明示的な値の設定には影響しない。
        """
        self._in_locked = locked
        self._in_button.setEnabled(not locked and self._item is not None)
        self._in_text_label.setEnabled(not locked)

    def set_out_point_locked(self, locked: bool) -> None:
        """set_in_point_locked() のOUT点版。"""
        self._out_locked = locked
        self._out_button.setEnabled(not locked and self._item is not None)
        self._out_text_label.setEnabled(not locked)

    def set_out_point_value(self, seconds: float) -> None:
        """OUT点を任意の秒数へ移動する(§8.4 のキーフレームスナップ用)。

        実際にその位置までシークし、届いたフレームでサムネも更新する。
        """
        if self._item is None:
            return
        self._cancel_preview_watch()
        self._priming_active = False
        self._out_point_s = max(0.0, min(seconds, self._item.duration))
        self._update_in_out_label()
        self.out_point_changed.emit(self._out_point_s)
        self._player.pause()
        target_ms = round(self._out_point_s * 1000)
        duration_ms = round(self._item.duration * 1000)
        # 動画末尾ぴったり(またはその近辺)へのシークは、バックエンドが
        # EndOfMedia扱いにしてしまい新しいフレームが届かないことがある
        # (末尾までモードでOUT点を動画末尾に固定する際など)。サムネイル
        # 取得用のシーク位置だけ、末尾から少し手前にずらして確実にフレーム
        # を取得する(OUT点の値自体・カット処理には影響しない)。
        seek_ms = min(target_ms, max(0, duration_ms - THUMB_END_SEEK_MARGIN_MS))
        self._seek_and_capture_thumb(seek_ms, self._out_thumb_label)

    def load_in_out(self, in_s: float, out_s: float) -> None:
        """IN/OUT点をまとめて設定する(抜き出しポイントリストの選択時など)。

        set_in_point_value()/set_out_point_value() を単純に連続で呼ぶと、2つの
        シーク要求がほぼ同時に発行されてしまい、先に出したOUT側のサムネイル取得が
        後から出したIN側のフレーム到着イベントに反応して誤ったフレームを捉えて
        しまう(1回のフレーム到着で両方のコールバックが同時に発火するため)。
        ここではOUT→INの順で、前のキャプチャが完了するのを待ってから次のシークを
        出す(直列化する)ことでこれを防ぐ。最終的な再生位置はIN(区間の先頭)になる。
        """
        if self._item is None:
            return
        self._out_point_s = max(0.0, min(out_s, self._item.duration))
        self._in_point_s = max(0.0, min(in_s, self._item.duration))
        self._update_in_out_label()
        self.out_point_changed.emit(self._out_point_s)
        self.in_point_changed.emit(self._in_point_s)

        duration_ms = round(self._item.duration * 1000)
        out_target_ms = round(self._out_point_s * 1000)
        out_seek_s = min(out_target_ms, max(0, duration_ms - THUMB_END_SEEK_MARGIN_MS)) / 1000.0

        def after_in(_pixmap: QPixmap | None) -> None:
            self._set_thumb(self._in_thumb_label)

        def after_out(_pixmap: QPixmap | None) -> None:
            self._set_thumb(self._out_thumb_label)
            self.capture_thumbnail_at(self._in_point_s, after_in)

        self.capture_thumbnail_at(out_seek_s, after_out)

    @property
    def in_point(self) -> float | None:
        return self._in_point_s

    @property
    def out_point(self) -> float | None:
        return self._out_point_s

    @property
    def current_item(self) -> VideoItem | None:
        return self._item

    @property
    def keyframes(self) -> list[float] | None:
        return self._keyframes

    @property
    def keyframe_state(self) -> str:
        """"none" | "loading" | "ready" | "unavailable" """
        return self._keyframe_state

    def current_in_out_pixmaps(self) -> tuple[QPixmap | None, QPixmap | None]:
        """現在表示中のIN/OUTサムネイルの複製を返す(抜き出しポイントリストへの登録用)。"""
        in_pixmap = self._in_thumb_label.pixmap()
        out_pixmap = self._out_thumb_label.pixmap()
        return (
            None if in_pixmap is None or in_pixmap.isNull() else QPixmap(in_pixmap),
            None if out_pixmap is None or out_pixmap.isNull() else QPixmap(out_pixmap),
        )

    def current_position_seconds(self) -> float:
        return self._player.position() / 1000.0

    def set_keyframes(self, path: Path, keyframes: list[float]) -> None:
        """path が現在選択中の動画と一致する場合のみ、キーフレーム情報を反映する。"""
        if self._item is None or self._item.path != path:
            return
        self._keyframes = keyframes
        self._keyframe_state = "ready"
        self._slider.set_keyframes(keyframes)
        self._keyframe_status_label.setText(f"キーフレーム: {len(keyframes)}個")
        self.keyframes_changed.emit()

    def set_keyframes_unavailable(self, path: Path) -> None:
        if self._item is None or self._item.path != path:
            return
        self._keyframes = None
        self._keyframe_state = "unavailable"
        self._slider.clear_keyframes()
        self._keyframe_status_label.setText("キーフレーム: 判定不能")
        self.keyframes_changed.emit()

    def jump_to_prev_keyframe(self) -> None:
        if self._item is None or not self._keyframes:
            return
        target = prev_keyframe(self.current_position_seconds(), self._keyframes)
        if target is not None:
            self._cancel_preview_watch()
            self._priming_active = False
            self._player.pause()
            self._player.setPosition(round(target * 1000))

    def jump_to_next_keyframe(self) -> None:
        if self._item is None or not self._keyframes:
            return
        target = next_keyframe(self.current_position_seconds(), self._keyframes)
        if target is not None:
            self._cancel_preview_watch()
            self._priming_active = False
            self._player.pause()
            self._player.setPosition(round(target * 1000))

    def seek_to(self, seconds: float) -> None:
        """任意の位置へシークする(削除タブなど他パネルからの利用も想定した公開API)。"""
        self._cancel_preview_watch()
        if self._item is None:
            return
        self._priming_active = False
        self._player.pause()
        clamped = max(0.0, min(seconds, self._item.duration))
        self._player.setPosition(round(clamped * 1000))

    def _seek_to_marked_point(self, seconds: float | None) -> None:
        """IN/OUTサムネイルクリック時、その位置へ再生ヘッドを移動する。"""
        if seconds is None:
            return
        self.seek_to(seconds)

    def toggle_preview_in_out(self) -> None:
        """IN点からOUT点までを再生し、OUT点で自動的に一時停止する。"""
        if self._previewing:
            self._stop_preview()
            return
        if self._item is None or self._in_point_s is None or self._out_point_s is None:
            return
        if self._out_point_s <= self._in_point_s:
            return

        self._priming_active = False
        self._player.setPosition(round(self._in_point_s * 1000))
        self._player.play()

        out_ms = round(self._out_point_s * 1000)
        self._previewing = True
        self._preview_button.setText("■ プレビュー停止")

        def watcher(position_ms: int) -> None:
            if self._previewing and position_ms >= out_ms:
                self._stop_preview()

        self._preview_watcher = watcher
        self._player.positionChanged.connect(watcher)

    def _stop_preview(self) -> None:
        self._cancel_preview_watch()
        self._player.pause()

    def _cancel_preview_watch(self) -> None:
        if not self._previewing:
            return
        self._previewing = False
        self._preview_button.setText("▶ IN-OUT再生")
        if self._preview_watcher is not None:
            try:
                self._player.positionChanged.disconnect(self._preview_watcher)
            except (RuntimeError, TypeError):
                pass
            self._preview_watcher = None

    # -------------------------------------------------------------- 内部

    def _make_thumb_label(self) -> ClickableThumbLabel:
        label = ClickableThumbLabel()
        label.setFixedSize(*IN_OUT_THUMB_SIZE)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("background: palette(dark); border: 1px solid palette(mid);")
        label.setToolTip("クリックでこの位置に移動")
        return label

    def _set_thumb(self, label: QLabel) -> None:
        if self._latest_frame_image is None:
            return
        pixmap = QPixmap.fromImage(self._latest_frame_image).scaled(
            *IN_OUT_THUMB_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        label.setPixmap(pixmap)

    def _on_video_frame(self, frame: QVideoFrame) -> None:
        if frame.isValid():
            self._latest_frame_image = frame.toImage()

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self._slider,
            self._back10_button, self._back1_button, self._play_button,
            self._fwd1_button, self._fwd10_button,
            self._frame_back_button, self._frame_fwd_button,
        ):
            widget.setEnabled(enabled)
        # IN/OUTボタンはロック中(set_in_point_locked/set_out_point_locked)なら
        # 動画の読込状態に関わらず無効のままにする。
        self._in_button.setEnabled(enabled and not self._in_locked)
        self._out_button.setEnabled(enabled and not self._out_locked)
        if not enabled:
            # プレビューボタンは IN/OUT が両方揃った時だけ有効化する
            # (_update_in_out_label 側で管理)。無効化のみここで反映する。
            self._preview_button.setEnabled(False)

    def _update_in_out_label(self) -> None:
        in_text = seconds_to_timecode(self._in_point_s) if self._in_point_s is not None else "-"
        out_text = seconds_to_timecode(self._out_point_s) if self._out_point_s is not None else "-"
        self._in_text_label.setText(f"IN: {in_text}")
        self._out_text_label.setText(f"OUT: {out_text}")
        valid_range = (
            self._in_point_s is not None
            and self._out_point_s is not None
            and self._out_point_s > self._in_point_s
        )
        if valid_range:
            length_s = self._out_point_s - self._in_point_s
            self._in_out_length_label.setText(f"長さ: {seconds_to_timecode(length_s)}")
        else:
            self._in_out_length_label.setText("長さ: -")
        self._preview_button.setEnabled(valid_range)
        self._register_button.setEnabled(valid_range)
        if not valid_range:
            self._cancel_preview_watch()

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

    def _on_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if not self._pending_prime:
            return
        loaded_statuses = (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        )
        if status in loaded_statuses:
            self._pending_prime = False
            self._prime_video_pipeline()

    def _prime_video_pipeline(self) -> None:
        was_muted = self._audio_output.isMuted()
        self._audio_output.setMuted(True)
        self._priming_active = True
        self._player.play()

        def finish_priming() -> None:
            self._player.pause()
            # プライミング中にユーザー操作(シーク等)が入っていた場合は
            # 0への巻き戻しで上書きしない(各シーク操作が _priming_active を
            # False にする)。
            if self._priming_active:
                self._player.setPosition(0)
            self._priming_active = False
            self._audio_output.setMuted(was_muted)

        QTimer.singleShot(PRIME_DURATION_MS, finish_priming)

    def _on_slider_pressed(self) -> None:
        self._seeking_by_user = True

    def _on_slider_moved(self, position_ms: int) -> None:
        self._cancel_preview_watch()
        self._priming_active = False
        self._player.setPosition(position_ms)

    def _on_slider_released(self) -> None:
        self._seeking_by_user = False
