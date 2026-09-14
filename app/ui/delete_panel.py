"""削除タブ（機能1）。仕様書 §6, §8.4 参照。"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.app_settings import KEY_DELETE_CUT_MODE, make_settings
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
    overwrite: bool = False  # True の場合、出力先フォルダではなく元ファイルを上書きする


class DeletePanel(QWidget):
    """削除タブ。途中削除のIN/OUT点は抜き出しタブと共有する(PlayerPanel の状態)。"""

    execute_requested = Signal(object)  # DeleteRequest

    def __init__(self, player_panel: PlayerPanel, parent=None):
        super().__init__(parent)
        self._player_panel = player_panel
        self._pending_request: DeleteRequest | None = None
        # このタブが表示中かどうか(タブが非表示の間は、動画プレビュー側のIN/OUT
        # 設定を抜き出しタブが自由に使えるよう、ロックしない)。
        self._tab_active = False

        self._build_ui()
        self._restore_cut_mode()
        self._connect_signals()
        self._on_video_changed()

    def _restore_cut_mode(self) -> None:
        settings = make_settings()
        mode = settings.value(KEY_DELETE_CUT_MODE, "auto", str)
        {
            "auto": self._cut_auto_radio,
            "copy_priority": self._cut_copy_priority_radio,
            "always_encode": self._cut_always_encode_radio,
        }.get(mode, self._cut_auto_radio).setChecked(True)

    def _save_cut_mode(self) -> None:
        make_settings().setValue(KEY_DELETE_CUT_MODE, self._get_cut_mode())

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("削除モード:"))
        mode_row = QHBoxLayout()
        self._mode_head_radio = QRadioButton("冒頭から")
        self._mode_tail_radio = QRadioButton("末尾まで")
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
        # 埋め込みの小さい上下スピンボタンは押しづらいため隠し、代わりに
        # 大きな +/- ボタンを外側に置く(stepUp/stepDown を呼ぶだけ)。
        for spin in (self._length_seconds_spin, self._length_frames_spin):
            spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self._length_stack = QStackedWidget()
        self._length_stack.addWidget(self._length_seconds_spin)
        self._length_stack.addWidget(self._length_frames_spin)
        self._length_minus_button = QPushButton("−")
        self._length_plus_button = QPushButton("＋")
        for btn in (self._length_minus_button, self._length_plus_button):
            btn.setFixedWidth(32)
        self._length_unit_seconds_radio = QRadioButton("秒")
        self._length_unit_frames_radio = QRadioButton("フレーム")
        self._length_unit_seconds_radio.setChecked(True)
        self._length_unit_group = QButtonGroup(self)
        self._length_unit_group.addButton(self._length_unit_seconds_radio)
        self._length_unit_group.addButton(self._length_unit_frames_radio)
        length_row.addWidget(self._length_minus_button)
        length_row.addWidget(self._length_stack)
        length_row.addWidget(self._length_plus_button)
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

        # IN/OUT点(途中削除用。抜き出しタブと同じ PlayerPanel の状態を共有する)
        in_row = QHBoxLayout()
        in_row.addWidget(QLabel("IN点:"))
        self._in_label = QLabel("-")
        self._in_get_button = QPushButton("現在位置を取得")
        in_row.addWidget(self._in_label, 1)
        in_row.addWidget(self._in_get_button)
        layout.addLayout(in_row)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("OUT点:"))
        self._out_label = QLabel("-")
        self._out_get_button = QPushButton("現在位置を取得")
        out_row.addWidget(self._out_label, 1)
        out_row.addWidget(self._out_get_button)
        layout.addLayout(out_row)

        self._result_label = QLabel("結果: -")
        layout.addWidget(self._result_label)

        self._overwrite_checkbox = QCheckBox("元のファイルに上書きする(元に戻せません)")
        self._overwrite_checkbox.setStyleSheet("color: firebrick;")
        layout.addWidget(self._overwrite_checkbox)

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
        self._player_panel.in_point_changed.connect(lambda _v: self._revalidate())
        self._player_panel.out_point_changed.connect(lambda _v: self._revalidate())
        self._player_panel.keyframes_changed.connect(self._revalidate)

        for rb in (self._mode_head_radio, self._mode_tail_radio, self._mode_middle_radio):
            rb.toggled.connect(self._on_mode_changed)

        self._length_unit_seconds_radio.toggled.connect(self._on_length_unit_changed)
        self._length_seconds_spin.valueChanged.connect(self._revalidate)
        self._length_frames_spin.valueChanged.connect(self._revalidate)
        self._length_minus_button.clicked.connect(lambda: self._length_stack.currentWidget().stepDown())
        self._length_plus_button.clicked.connect(lambda: self._length_stack.currentWidget().stepUp())

        self._in_get_button.clicked.connect(self._player_panel.set_in_point)
        self._out_get_button.clicked.connect(self._player_panel.set_out_point)

        self._overwrite_checkbox.toggled.connect(self._revalidate)

        for rb in (self._cut_auto_radio, self._cut_copy_priority_radio, self._cut_always_encode_radio):
            rb.toggled.connect(self._revalidate)
            rb.toggled.connect(lambda checked: self._save_cut_mode() if checked else None)

        self._snap_prev_button.clicked.connect(lambda: self._apply_snap(prev_keyframe))
        self._snap_next_button.clicked.connect(lambda: self._apply_snap(next_keyframe))

        self._execute_button.clicked.connect(self._on_execute_clicked)

    # --------------------------------------------------------------- 状態

    def set_running(self, running: bool) -> None:
        for widget in (
            self._mode_head_radio, self._mode_tail_radio, self._mode_middle_radio,
            self._length_seconds_spin, self._length_frames_spin,
            self._length_minus_button, self._length_plus_button,
            self._length_unit_seconds_radio, self._length_unit_frames_radio,
            self._in_get_button, self._out_get_button,
            self._overwrite_checkbox,
            self._cut_auto_radio, self._cut_copy_priority_radio, self._cut_always_encode_radio,
            self._snap_prev_button, self._snap_next_button,
            self._execute_button,
        ):
            widget.setEnabled(not running)
        if not running:
            self._revalidate()

    def _on_video_changed(self) -> None:
        self._apply_fixed_endpoint()
        self._revalidate()

    def _on_mode_changed(self) -> None:
        is_middle = self._mode_middle_radio.isChecked()
        self._length_stack.setEnabled(not is_middle)
        self._length_minus_button.setEnabled(not is_middle)
        self._length_plus_button.setEnabled(not is_middle)
        self._length_unit_seconds_radio.setEnabled(not is_middle)
        self._length_unit_frames_radio.setEnabled(not is_middle)
        # 冒頭からモードではIN点(常に0固定)、末尾までモードではOUT点(常に動画末尾固定)
        # だけをグレーアウトする。もう一方は途中を指定モードと同様に操作可能なままにする。
        mode = self._get_mode()
        self._in_label.setEnabled(mode != "head")
        self._in_get_button.setEnabled(mode != "head")
        self._out_label.setEnabled(mode != "tail")
        self._out_get_button.setEnabled(mode != "tail")
        self._update_vfr_warning()
        self._apply_fixed_endpoint()
        self._apply_lock_state()
        self._revalidate()

    def set_tab_active(self, active: bool) -> None:
        """削除タブが表示中かどうかをメインウィンドウから受け取る。

        非表示の間は動画プレビュー側のIN/OUT設定をロックしない
        (抜き出しタブで自由にIN/OUTを使えるようにするため)。
        """
        self._tab_active = active
        self._apply_lock_state()

    def _apply_lock_state(self) -> None:
        """このタブが表示中の間、モードに応じて動画プレビュー側の「IN設定」/「OUT設定」
        ボタンをグレーアウトする。冒頭からモードはIN設定のみ、末尾までモードはOUT設定のみ
        をロックし、もう一方は操作可能なままにする。"""
        mode = self._get_mode()
        self._player_panel.set_in_point_locked(self._tab_active and mode == "head")
        self._player_panel.set_out_point_locked(self._tab_active and mode == "tail")

    def _apply_fixed_endpoint(self) -> None:
        """冒頭からモードではIN点を動画先頭(0)に、末尾までモードではOUT点を
        動画末尾に固定する(抜き出しタブと共有のIN/OUTを、このモードでは
        ユーザーが動かす必要がないため自動的に合わせておく)。"""
        item = self._player_panel.current_item
        if item is None:
            return
        mode = self._get_mode()
        if mode == "head":
            self._player_panel.set_in_point_value(0.0)
        elif mode == "tail":
            self._player_panel.set_out_point_value(item.duration)

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

    def _current_decision_start(self) -> float | None:
        """§8.3 の判定表に基づく、このモードでコピー可否を左右する開始点。"""
        mode = self._get_mode()
        if mode == "head":
            n = self._get_length_seconds()
            return n if n > 0 else None
        if mode == "tail":
            return 0.0
        return self._player_panel.out_point  # middle

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
            self._player_panel.set_out_point_value(target)
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

        in_s = self._player_panel.in_point
        out_s = self._player_panel.out_point
        self._in_label.setText(seconds_to_timecode(in_s) if in_s is not None else "-")
        self._out_label.setText(seconds_to_timecode(out_s) if out_s is not None else "-")

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
            a, b = in_s, out_s
            if a is None or b is None:
                self._invalid("IN点とOUT点の両方を設定してください(「現在位置を取得」)。")
                self._clear_cut_status()
                return
            # 途中削除は冒頭(IN=0)や末尾(OUT=動画長)ぎりぎりまで許容する。
            if not (0.0 <= a < b <= duration):
                self._invalid("IN点とOUT点は「0 ≦ IN点 < OUT点 ≦ 動画長」を満たす必要があります。")
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
            # IN=0(冒頭)や OUT=動画長(末尾)ぎりぎりの場合、片側の残存区間が
            # 長さ0になる。これは「冒頭から」/「末尾まで」と等価な結果なので
            # 実行時の区間には含めない(main_window 側は区間数で単発/2区間を判定する)。
            final_ranges = [
                r for r in residual_ranges_middle(a, decision.start, duration) if r[1] - r[0] > 1e-6
            ]
            if not final_ranges:
                self._invalid("動画全体が削除対象になるため実行できません。")
                self._clear_cut_status()
                return

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
            overwrite=self._overwrite_checkbox.isChecked(),
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
        if self._pending_request is None:
            return
        if self._pending_request.overwrite:
            reply = QMessageBox.question(
                self, "元のファイルを上書き",
                "元の動画ファイルを上書きします。この操作は元に戻せません。実行しますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.execute_requested.emit(self._pending_request)

    def trigger_execute(self) -> None:
        """Enterキー等、外部からの実行トリガー用。ボタンが有効な場合のみ実行する。"""
        if self._execute_button.isEnabled():
            self._execute_button.click()
