"""抜き出しタブ（機能2）。仕様書 §7, §8.4 参照。"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
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

from app.core.app_settings import KEY_EXTRACT_CUT_MODE, make_settings
from app.core.edit_ops import CutDecision, CutMode, decide_cut_strategy, format_cut_status
from app.core.keyframes import next_keyframe, prev_keyframe
from app.ui.player_panel import PlayerPanel
from app.utils.timecode import seconds_to_timecode


@dataclass
class ExtractRequest:
    in_s: float
    out_s: float
    exclude_audio: bool
    decision: CutDecision


class ExtractPanel(QWidget):
    """抜き出しタブ。IN/OUT自体は PlayerPanel が保持する値を共有して使う。"""

    execute_requested = Signal(object)  # ExtractRequest | None (Noneは登録リスト使用時)

    def __init__(self, player_panel: PlayerPanel, parent=None):
        super().__init__(parent)
        self._player_panel = player_panel
        self._pending_request: ExtractRequest | None = None
        # 抜き出しポイントリストに登録があるか(main_window から通知される)。
        # 登録があれば、現在のIN/OUTが未設定でも実行ボタンを有効にする。
        self._has_registered_points = False

        self._build_ui()
        self._restore_cut_mode()
        self._connect_signals()
        self._revalidate()

    def _restore_cut_mode(self) -> None:
        settings = make_settings()
        mode = settings.value(KEY_EXTRACT_CUT_MODE, "auto", str)
        {
            "auto": self._cut_auto_radio,
            "copy_priority": self._cut_copy_priority_radio,
            "always_encode": self._cut_always_encode_radio,
        }.get(mode, self._cut_auto_radio).setChecked(True)

    def _save_cut_mode(self) -> None:
        make_settings().setValue(KEY_EXTRACT_CUT_MODE, self._get_cut_mode())

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # IN点
        in_row = QHBoxLayout()
        in_row.addWidget(QLabel("IN点:"))
        self._in_label = QLabel("-")
        self._in_get_button = QPushButton("現在位置を取得")
        in_row.addWidget(self._in_label, 1)
        in_row.addWidget(self._in_get_button)
        layout.addLayout(in_row)

        # 指定方法
        mode_row = QHBoxLayout()
        self._mode_out_radio = QRadioButton("OUT点を指定")
        self._mode_duration_radio = QRadioButton("IN点からの長さ")
        self._mode_out_radio.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._mode_out_radio)
        self._mode_group.addButton(self._mode_duration_radio)
        mode_row.addWidget(QLabel("指定方法:"))
        mode_row.addWidget(self._mode_out_radio)
        mode_row.addWidget(self._mode_duration_radio)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        # OUT点
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("OUT点:"))
        self._out_label = QLabel("-")
        self._out_get_button = QPushButton("現在位置を取得")
        out_row.addWidget(self._out_label, 1)
        out_row.addWidget(self._out_get_button)
        layout.addLayout(out_row)

        # 長さ(IN点からの長さモード用)
        length_row = QHBoxLayout()
        length_row.addWidget(QLabel("長さ:"))
        self._length_seconds_spin = QDoubleSpinBox()
        self._length_seconds_spin.setRange(0.001, 999999.0)
        self._length_seconds_spin.setDecimals(3)
        self._length_seconds_spin.setValue(30.0)
        self._length_seconds_spin.setSuffix(" 秒")
        self._length_frames_spin = QSpinBox()
        self._length_frames_spin.setRange(1, 100_000_000)
        self._length_frames_spin.setValue(900)
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

        self._clip_length_label = QLabel("切り出し長: -")
        layout.addWidget(self._clip_length_label)
        self._clamp_note_label = QLabel("")
        self._clamp_note_label.setStyleSheet("color: darkorange;")
        layout.addWidget(self._clamp_note_label)

        self._exclude_audio_checkbox = QCheckBox("音声を含めない")
        layout.addWidget(self._exclude_audio_checkbox)

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

        self._execute_button = QPushButton("抜き出しを実行")
        self._execute_button.setMinimumHeight(40)
        layout.addWidget(self._execute_button)

        layout.addStretch(1)

    def _connect_signals(self) -> None:
        self._player_panel.in_point_changed.connect(lambda _v: self._revalidate())
        self._player_panel.out_point_changed.connect(lambda _v: self._revalidate())
        self._player_panel.keyframes_changed.connect(self._revalidate)

        self._in_get_button.clicked.connect(self._player_panel.set_in_point)
        self._out_get_button.clicked.connect(self._player_panel.set_out_point)

        self._mode_out_radio.toggled.connect(self._on_mode_changed)
        self._length_unit_seconds_radio.toggled.connect(self._on_length_unit_changed)
        self._length_seconds_spin.valueChanged.connect(self._revalidate)
        self._length_frames_spin.valueChanged.connect(self._revalidate)
        self._exclude_audio_checkbox.toggled.connect(self._revalidate)
        for rb in (self._cut_auto_radio, self._cut_copy_priority_radio, self._cut_always_encode_radio):
            rb.toggled.connect(self._revalidate)
            rb.toggled.connect(lambda checked: self._save_cut_mode() if checked else None)

        self._snap_prev_button.clicked.connect(self._on_snap_prev)
        self._snap_next_button.clicked.connect(self._on_snap_next)

        self._execute_button.clicked.connect(self._on_execute_clicked)

        self._on_mode_changed()
        self._on_length_unit_changed()

    # --------------------------------------------------------------- 状態

    def set_running(self, running: bool) -> None:
        """処理中はタブ内の操作をすべて無効化する。"""
        for widget in (
            self._in_get_button, self._out_get_button,
            self._mode_out_radio, self._mode_duration_radio,
            self._length_seconds_spin, self._length_frames_spin,
            self._length_unit_seconds_radio, self._length_unit_frames_radio,
            self._exclude_audio_checkbox,
            self._cut_auto_radio, self._cut_copy_priority_radio, self._cut_always_encode_radio,
            self._snap_prev_button, self._snap_next_button,
            self._execute_button,
        ):
            widget.setEnabled(not running)
        if not running:
            self._revalidate()

    def _on_mode_changed(self) -> None:
        is_out_mode = self._mode_out_radio.isChecked()
        self._out_get_button.setEnabled(is_out_mode)
        self._length_stack.setEnabled(not is_out_mode)
        self._length_unit_seconds_radio.setEnabled(not is_out_mode)
        self._length_unit_frames_radio.setEnabled(not is_out_mode)
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
        self._revalidate()

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

    def _on_snap_prev(self) -> None:
        in_s = self._player_panel.in_point
        if in_s is None or self._player_panel.keyframes is None:
            return
        target = prev_keyframe(in_s, self._player_panel.keyframes)
        if target is not None:
            self._player_panel.set_in_point_value(target)

    def _on_snap_next(self) -> None:
        in_s = self._player_panel.in_point
        if in_s is None or self._player_panel.keyframes is None:
            return
        target = next_keyframe(in_s, self._player_panel.keyframes)
        if target is not None:
            self._player_panel.set_in_point_value(target)

    # ------------------------------------------------------------- 検証

    def _invalid(self, reason: str) -> None:
        self._reason_label.setText(reason)
        self._execute_button.setEnabled(False)
        self._pending_request = None

    def _revalidate(self) -> None:
        """現在のIN/OUT設定を検証して _pending_request を更新したうえで、
        抜き出しポイントリストに登録があれば実行ボタンを補助的に有効化する。
        """
        self._revalidate_current()
        if self._has_registered_points and self._pending_request is None:
            # 現在のIN/OUTは無効だが、抜き出しポイントリストに登録があるので
            # そちらを使って実行できる(main_window 側が実行時にリストを優先する)。
            self._reason_label.setText("")
            self._cut_status_label.setText("登録済みの抜き出しポイント一覧を使って実行します。")
            self._snap_prev_button.setVisible(False)
            self._snap_next_button.setVisible(False)
            self._execute_button.setEnabled(True)

    def _revalidate_current(self) -> None:
        """現在のIN/OUT設定のみに基づいて _pending_request を計算する。"""
        item = self._player_panel.current_item
        in_s = self._player_panel.in_point

        self._in_label.setText(seconds_to_timecode(in_s) if in_s is not None else "-")
        out_s_display = self._player_panel.out_point
        self._out_label.setText(seconds_to_timecode(out_s_display) if out_s_display is not None else "-")

        self._reason_label.setText("")
        self._clamp_note_label.setText("")

        if item is None:
            self._invalid("動画を選択してください。")
            self._cut_status_label.setText("")
            self._snap_prev_button.setVisible(False)
            self._snap_next_button.setVisible(False)
            return

        if in_s is None:
            self._invalid("IN点を設定してください(Iキーまたは「現在位置を取得」)。")
            self._cut_status_label.setText("")
            self._snap_prev_button.setVisible(False)
            self._snap_next_button.setVisible(False)
            return

        clamped = False
        if self._mode_out_radio.isChecked():
            out_s = self._player_panel.out_point
            if out_s is None:
                self._invalid("OUT点を設定してください(Oキーまたは「現在位置を取得」)。")
                self._cut_status_label.setText("")
                self._snap_prev_button.setVisible(False)
                self._snap_next_button.setVisible(False)
                return
        else:
            length_s = self._get_length_seconds()
            if length_s <= 0:
                self._invalid("長さを正しく入力してください。")
                return
            raw_out = in_s + length_s
            clamped = raw_out > item.duration + 1e-9
            out_s = min(raw_out, item.duration)

        if out_s <= in_s:
            self._invalid("OUTはINより後の位置にしてください。")
            return

        clip_len = out_s - in_s
        self._clip_length_label.setText(f"切り出し長: {clip_len:.3f}秒")
        if clamped:
            self._clamp_note_label.setText(
                f"OUTが動画長を超えたため末尾でクランプしました(OUT: {seconds_to_timecode(out_s)})"
            )

        # キーフレーム状態に応じた判定
        state = self._player_panel.keyframe_state
        if state == "loading":
            self._cut_status_label.setText("キーフレーム判定中…しばらくお待ちください。")
            self._snap_prev_button.setVisible(False)
            self._snap_next_button.setVisible(False)
            self._invalid("キーフレームの判定が完了するまでお待ちください。")
            return

        keyframes = self._player_panel.keyframes if state == "ready" else []
        cut_mode = self._get_cut_mode()
        decision = decide_cut_strategy(in_s, keyframes, item.fps, cut_mode, snap_direction="prev")
        self._render_cut_status(decision, cut_mode, state)

        exclude_audio = self._exclude_audio_checkbox.isChecked()
        self._reason_label.setText("")
        self._execute_button.setEnabled(True)
        self._pending_request = ExtractRequest(
            in_s=in_s, out_s=out_s, exclude_audio=exclude_audio, decision=decision,
        )

    def _render_cut_status(self, decision: CutDecision, cut_mode: CutMode, keyframe_state: str) -> None:
        text, show_prev, show_next = format_cut_status(decision, cut_mode, keyframe_state)
        self._cut_status_label.setText(text)
        self._snap_prev_button.setVisible(show_prev)
        self._snap_next_button.setVisible(show_next)

    # --------------------------------------------------------------- 実行

    def current_request(self) -> ExtractRequest | None:
        """現在有効なIN/OUT設定に基づくリクエストを返す(登録リストへの追加用)。

        IN/OUT未設定やキーフレーム判定中など、実行できない状態では None を返す。
        """
        return self._pending_request

    def set_exclude_audio(self, exclude: bool) -> None:
        """抜き出しポイントリストからの呼び出し用: 「音声を含めない」の状態を復元する。"""
        self._exclude_audio_checkbox.setChecked(exclude)

    def set_has_registered_points(self, has_points: bool) -> None:
        """main_window から、抜き出しポイントリストに登録があるかどうかを通知する。

        登録があれば、現在のIN/OUTが未設定でも一覧を使って実行できるようにする。
        """
        self._has_registered_points = has_points
        self._revalidate()

    def _on_execute_clicked(self) -> None:
        if self._pending_request is not None:
            self.execute_requested.emit(self._pending_request)
        elif self._has_registered_points:
            # 現在のIN/OUTは無効だが、リストの登録分で実行する
            # (main_window 側はリストが非空ならこの値を使わない)。
            self.execute_requested.emit(None)

    def trigger_execute(self) -> None:
        """Enterキー等、外部からの実行トリガー用。ボタンが有効な場合のみ実行する。"""
        if self._execute_button.isEnabled():
            self._execute_button.click()
