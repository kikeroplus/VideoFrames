"""削除タブ（機能1）。仕様書 §6, §8.4 参照。"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.edit_ops import (
    CutMode,
    DeleteMode,
    Strategy,
    decide_cut_strategy,
    format_cut_status,
    residual_ranges_head,
    residual_ranges_middle,
    residual_ranges_tail,
)
from app.core.keyframes import next_keyframe, prev_keyframe
from app.ui.player_panel import PlayerPanel
from app.utils.timecode import seconds_to_timecode


@dataclass
class DeleteRequest:
    mode: DeleteMode
    ranges: list[tuple[float, float]]  # 実行に使う最終的な残す区間(スナップ後)
    strategy: Strategy
    snapped: bool
    snapped_point: float | None  # スナップ後の実際の開始点(結果表示用)


class DeletePanel(QWidget):
    """削除タブ。途中削除の開始/終了は抜き出しタブのIN/OUTとは独立して保持する。"""

    execute_requested = Signal(object)  # DeleteRequest

    def __init__(self, player_panel: PlayerPanel, parent=None):
        super().__init__(parent)
        self._player_panel = player_panel
        self._pending_request: DeleteRequest | None = None
        # 途中削除用のローカル状態(抜き出しタブのIN/OUTとは別概念のため共有しない)
        self._middle_start: float | None = None
        self._middle_end: float | None = None

        self._build_ui()
        self._connect_signals()
        self._on_video_changed()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("削除モード:"))
        mode_row = QHBoxLayout()
        self._mode_head_radio = QRadioButton("冒頭から")
        self._mode_tail_radio = QRadioButton("末尾から")
        self._mode_middle_radio = QRadioButton("途中を指定")
        self._mode_head_radio.setChecked(True)
        self._mode_group = QButtonGroup(self)
        for rb in (self._mode_head_radio, self._mode_tail_radio, self._mode_middle_radio):
            self._mode_group.addButton(rb)
            mode_row.addWidget(rb)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        # 長さ(冒頭/末尾用)
        length_row = QHBoxLayout()
        length_row.addWidget(QLabel("長さ:"))
        self._length_seconds_spin = QDoubleSpinBox()
        self._length_seconds_spin.setRange(0.001, 999999.0)
        self._length_seconds_spin.setDecimals(3)
        self._length_seconds_spin.setValue(5.0)
        self._length_seconds_spin.setSuffix(" 秒")
        self._length_frames_spin = QSpinBox()
        self._length_frames_spin.setRange(1, 100_000_000)
        self._length_frames_spin.setValue(150)
        self._length_frames_spin.setSuffix(" フレーム")
        self._length_stack = QStackedWidget()
        self._length_stack.addWidget(self._length_seconds_spin)
        self._length_stack.addWidget(self._length_frames_spin)
        self._length_unit_seconds_radio = QRadioButton("秒")
        self._length_unit_frames_radio = QRadioButton("フレーム")
        self._length_unit_seconds_radio.setChecked(True)
        self._length_unit_group = QButtonGroup(self)
        self._length_unit_group.addButton(self._length_unit_seconds_radio)
        self._length_unit_group.addButton(self._length_unit_frames_radio)
        length_row.addWidget(self._length_stack)
        length_row.addWidget(self._length_unit_seconds_radio)
        length_row.addWidget(self._length_unit_frames_radio)
        length_row.addStretch(1)
        layout.addLayout(length_row)

        self._vfr_warning_label = QLabel(
            "⚠ この動画は可変フレームレート(VFR)のため、フレーム指定は概算になります。"
        )
        self._vfr_warning_label.setStyleSheet("color: darkorange;")
        self._vfr_warning_label.setWordWrap(True)
        self._vfr_warning_label.setVisible(False)
        layout.addWidget(self._vfr_warning_label)

        # 開始/終了(途中削除用)
        start_row = QHBoxLayout()
        start_row.addWidget(QLabel("開始:"))
        self._start_label = QLabel("-")
        self._start_get_button = QPushButton("現在位置を取得")
        start_row.addWidget(self._start_label, 1)
        start_row.addWidget(self._start_get_button)
        layout.addLayout(start_row)

        end_row = QHBoxLayout()
        end_row.addWidget(QLabel("終了:"))
        self._end_label = QLabel("-")
        self._end_get_button = QPushButton("現在位置を取得")
        end_row.addWidget(self._end_label, 1)
        end_row.addWidget(self._end_get_button)
        layout.addLayout(end_row)

        self._result_label = QLabel("結果: -")
        layout.addWidget(self._result_label)

        # カット方式(§8.4 共通UI)
        cut_box = QGroupBox("カット方式")
        cut_layout = QVBoxLayout(cut_box)
        mode_radio_row = QHBoxLayout()
        self._cut_auto_radio = QRadioButton("自動")
        self._cut_copy_priority_radio = QRadioButton("コピー優先")
        self._cut_always_encode_radio = QRadioButton("常に再エンコード")
        self._cut_auto_radio.setChecked(True)
        self._cut_mode_group = QButtonGroup(self)
        for rb in (self._cut_auto_radio, self._cut_copy_priority_radio, self._cut_always_encode_radio):
            self._cut_mode_group.addButton(rb)
            mode_radio_row.addWidget(rb)
        mode_radio_row.addStretch(1)
        cut_layout.addLayout(mode_radio_row)

        self._cut_status_label = QLabel("")
        self._cut_status_label.setWordWrap(True)
        cut_layout.addWidget(self._cut_status_label)

        snap_row = QHBoxLayout()
        self._snap_prev_button = QPushButton("← 手前に合わせる")
        self._snap_next_button = QPushButton("奥に合わせる →")
        snap_row.addWidget(self._snap_prev_button)
        snap_row.addWidget(self._snap_next_button)
        snap_row.addStretch(1)
        cut_layout.addLayout(snap_row)
        self._snap_prev_button.setVisible(False)
        self._snap_next_button.setVisible(False)

        layout.addWidget(cut_box)

        self._reason_label = QLabel("")
        self._reason_label.setStyleSheet("color: firebrick;")
        self._reason_label.setWordWrap(True)
        layout.addWidget(self._reason_label)

        self._execute_button = QPushButton("削除を実行")
        self._execute_button.setMinimumHeight(40)
        layout.addWidget(self._execute_button)

        layout.addStretch(1)

        self._on_mode_changed()

    def _connect_signals(self) -> None:
        self._player_panel.video_changed.connect(self._on_video_changed)
        self._player_panel.keyframes_changed.connect(self._revalidate)

        for rb in (self._mode_head_radio, self._mode_tail_radio, self._mode_middle_radio):
            rb.toggled.connect(self._on_mode_changed)

        self._length_unit_seconds_radio.toggled.connect(self._on_length_unit_changed)
        self._length_seconds_spin.valueChanged.connect(self._revalidate)
        self._length_frames_spin.valueChanged.connect(self._revalidate)

        self._start_get_button.clicked.connect(self._on_get_start)
        self._end_get_button.clicked.connect(self._on_get_end)

        for rb in (self._cut_auto_radio, self._cut_copy_priority_radio, self._cut_always_encode_radio):
            rb.toggled.connect(self._revalidate)

        self._snap_prev_button.clicked.connect(lambda: self._apply_snap(prev_keyframe))
        self._snap_next_button.clicked.connect(lambda: self._apply_snap(next_keyframe))

        self._execute_button.clicked.connect(self._on_execute_clicked)

    # --------------------------------------------------------------- 状態

    def set_running(self, running: bool) -> None:
        for widget in (
            self._mode_head_radio, self._mode_tail_radio, self._mode_middle_radio,
            self._length_seconds_spin, self._length_frames_spin,
            self._length_unit_seconds_radio, self._length_unit_frames_radio,
            self._start_get_button, self._end_get_button,
            self._cut_auto_radio, self._cut_copy_priority_radio, self._cut_always_encode_radio,
            self._snap_prev_button, self._snap_next_button,
            self._execute_button,
        ):
            widget.setEnabled(not running)
        if not running:
            self._revalidate()

    def _on_video_changed(self) -> None:
        self._middle_start = None
        self._middle_end = None
        self._update_start_end_labels()
        self._revalidate()

    def _on_mode_changed(self) -> None:
        is_middle = self._mode_middle_radio.isChecked()
        self._length_stack.setEnabled(not is_middle)
        self._length_unit_seconds_radio.setEnabled(not is_middle)
        self._length_unit_frames_radio.setEnabled(not is_middle)
        self._start_get_button.setEnabled(is_middle)
        self._end_get_button.setEnabled(is_middle)
        self._update_vfr_warning()
        self._revalidate()

    def _on_length_unit_changed(self) -> None:
        item = self._player_panel.current_item
        fps = item.fps if item else 0.0
        if self._length_unit_seconds_radio.isChecked():
            if fps > 0:
                self._length_seconds_spin.setValue(self._length_frames_spin.value() / fps)
            self._length_stack.setCurrentWidget(self._length_seconds_spin)
        else:
            if fps > 0:
                self._length_frames_spin.setValue(max(1, round(self._length_seconds_spin.value() * fps)))
            self._length_stack.setCurrentWidget(self._length_frames_spin)
        self._update_vfr_warning()
        self._revalidate()

    def _update_vfr_warning(self) -> None:
        item = self._player_panel.current_item
        show = bool(
            item is not None and item.is_vfr
            and self._length_unit_frames_radio.isChecked()
            and not self._mode_middle_radio.isChecked()
        )
        self._vfr_warning_label.setVisible(show)

    def _set_length_seconds(self, value: float) -> None:
        self._length_seconds_spin.setValue(value)
        item = self._player_panel.current_item
        if item is not None and item.fps > 0:
            self._length_frames_spin.setValue(max(1, round(value * item.fps)))

    def _get_length_seconds(self) -> float:
        if self._length_unit_seconds_radio.isChecked():
            return self._length_seconds_spin.value()
        item = self._player_panel.current_item
        fps = item.fps if item else 0.0
        if fps <= 0:
            return 0.0
        return self._length_frames_spin.value() / fps

    def _get_cut_mode(self) -> CutMode:
        if self._cut_copy_priority_radio.isChecked():
            return "copy_priority"
        if self._cut_always_encode_radio.isChecked():
            return "always_encode"
        return "auto"

    def _get_mode(self) -> DeleteMode:
        if self._mode_head_radio.isChecked():
            return "head"
        if self._mode_tail_radio.isChecked():
            return "tail"
        return "middle"

    def _on_get_start(self) -> None:
        if self._player_panel.current_item is None:
            return
        self._middle_start = self._player_panel.current_position_seconds()
        self._update_start_end_labels()
        self._revalidate()

    def _on_get_end(self) -> None:
        if self._player_panel.current_item is None:
            return
        self._middle_end = self._player_panel.current_position_seconds()
        self._update_start_end_labels()
        self._revalidate()

    def _update_start_end_labels(self) -> None:
        self._start_label.setText(
            seconds_to_timecode(self._middle_start) if self._middle_start is not None else "-"
        )
        self._end_label.setText(
            seconds_to_timecode(self._middle_end) if self._middle_end is not None else "-"
        )

    def _current_decision_start(self) -> float | None:
        """§8.3 の判定表に基づく、このモードでコピー可否を左右する開始点。"""
        mode = self._get_mode()
        if mode == "head":
            n = self._get_length_seconds()
            return n if n > 0 else None
        if mode == "tail":
            return 0.0
        return self._middle_end  # middle

    def _apply_snap(self, finder) -> None:
        item = self._player_panel.current_item
        if item is None or self._player_panel.keyframes is None:
            return
        start = self._current_decision_start()
        if start is None:
            return
        target = finder(start, self._player_panel.keyframes)
        if target is None:
            return
        mode = self._get_mode()
        if mode == "head":
            self._set_length_seconds(target)
        elif mode == "middle":
            self._middle_end = target
            self._update_start_end_labels()
            self._player_panel.seek_to(target)
        self._revalidate()

    # ------------------------------------------------------------- 検証

    def _invalid(self, reason: str) -> None:
        self._reason_label.setText(reason)
        self._execute_button.setEnabled(False)
        self._pending_request = None

    def _clear_cut_status(self) -> None:
        self._cut_status_label.setText("")
        self._snap_prev_button.setVisible(False)
        self._snap_next_button.setVisible(False)

    def _revalidate(self) -> None:
        item = self._player_panel.current_item
        self._reason_label.setText("")

        if item is None:
            self._invalid("動画を選択してください。")
            self._clear_cut_status()
            self._result_label.setText("結果: -")
            return

        self._update_vfr_warning()
        mode = self._get_mode()
        duration = item.duration

        if mode in ("head", "tail"):
            n = self._get_length_seconds()
            if n <= 0:
                self._invalid("長さを正しく入力してください。")
                self._clear_cut_status()
                return
            if n >= duration:
                self._invalid("長さが動画の長さ以上になっています。")
                self._clear_cut_status()
                return
            decision_start = n if mode == "head" else 0.0
        else:
            a, b = self._middle_start, self._middle_end
            if a is None or b is None:
                self._invalid("開始と終了の両方を設定してください(「現在位置を取得」)。")
                self._clear_cut_status()
                return
            if not (0.0 < a < b < duration):
                self._invalid("開始と終了は「0 < 開始 < 終了 < 動画長」を満たす必要があります。")
                self._clear_cut_status()
                return
            decision_start = b

        state = self._player_panel.keyframe_state
        if state == "loading":
            self._cut_status_label.setText("キーフレーム判定中…しばらくお待ちください。")
            self._snap_prev_button.setVisible(False)
            self._snap_next_button.setVisible(False)
            self._invalid("キーフレームの判定が完了するまでお待ちください。")
            return

        keyframes = self._player_panel.keyframes if state == "ready" else []
        cut_mode = self._get_cut_mode()
        decision = decide_cut_strategy(decision_start, keyframes, item.fps, cut_mode, snap_direction="prev")

        if mode == "head":
            final_ranges = residual_ranges_head(decision.start, duration)
        elif mode == "tail":
            final_ranges = residual_ranges_tail(n, duration)
        else:
            if decision.strategy == "copy" and decision.start <= a:
                self._invalid(
                    "コピー優先のスナップ先が開始位置より前になるため実行できません。"
                    "カット方式を「自動」または「常に再エンコード」にしてください。"
                )
                self._clear_cut_status()
                return
            final_ranges = residual_ranges_middle(a, decision.start, duration)

        text, show_prev, show_next = format_cut_status(
            decision, cut_mode, state, can_snap=(mode != "tail"),
        )
        self._cut_status_label.setText(text)
        self._snap_prev_button.setVisible(show_prev)
        self._snap_next_button.setVisible(show_next)

        self._update_result_label(final_ranges, duration)

        self._execute_button.setEnabled(True)
        self._pending_request = DeleteRequest(
            mode=mode, ranges=final_ranges, strategy=decision.strategy,
            snapped=decision.snapped, snapped_point=decision.start if decision.snapped else None,
        )

    def _update_result_label(self, ranges: list[tuple[float, float]], duration: float) -> None:
        new_duration = sum(end - start for start, end in ranges)
        delta = new_duration - duration
        self._result_label.setText(
            f"結果: {seconds_to_timecode(duration)} → {seconds_to_timecode(new_duration)}"
            f"（{delta:+.1f}秒 / 残り区間 {len(ranges)}つ）"
        )

    # --------------------------------------------------------------- 実行

    def _on_execute_clicked(self) -> None:
        if self._pending_request is not None:
            self.execute_requested.emit(self._pending_request)

    def trigger_execute(self) -> None:
        """Enterキー等、外部からの実行トリガー用。ボタンが有効な場合のみ実行する。"""
        if self._execute_button.isEnabled():
            self._execute_button.click()
