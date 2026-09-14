"""抜き出しポイントリスト。複数のIN/OUT区間を登録し、まとめて抜き出す。"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.extract_panel import ExtractRequest
from app.utils.timecode import seconds_to_timecode

THUMB_SIZE = (72, 41)  # 幅, 高さ(px)
PANEL_WIDTH = 360  # サムネ2枚+矢印+時間表示が折り返し無しで収まる幅


@dataclass
class ExtractPointEntry:
    request: ExtractRequest
    in_pixmap: QPixmap | None
    out_pixmap: QPixmap | None


def _thumb_label(pixmap: QPixmap | None) -> QLabel:
    label = QLabel()
    label.setFixedSize(*THUMB_SIZE)
    label.setStyleSheet("background: palette(dark); border: 1px solid palette(mid);")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    if pixmap is not None:
        label.setPixmap(
            pixmap.scaled(
                *THUMB_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        )
    return label


class _EntryWidget(QWidget):
    """リスト1行分の表示(IN/OUTサムネイルの対 + 時間情報)。"""

    def __init__(self, entry: ExtractPointEntry, parent=None):
        super().__init__(parent)
        # クリックがこのウィジェットに吸収されず、下のQListWidgetの選択処理まで
        # 届くようにする(これが無いと行をクリックしても選択状態にならない)。
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(_thumb_label(entry.in_pixmap))
        layout.addWidget(QLabel("→"))
        layout.addWidget(_thumb_label(entry.out_pixmap))

        length_s = entry.request.out_s - entry.request.in_s
        text = QLabel(
            f"{seconds_to_timecode(entry.request.in_s)}\n→ {seconds_to_timecode(entry.request.out_s)}\n"
            f"({seconds_to_timecode(length_s)})"
        )
        text.setWordWrap(True)
        layout.addWidget(text, 1)


class ExtractPointsPanel(QWidget):
    """抜き出しタブの「登録」ボタンで追加されたIN/OUT区間の一覧(縦一列)。

    現在選択中の動画にのみ対応する(別の動画を選ぶと clear() で消去される想定)。
    リストが1件以上ある状態で「抜き出しを実行」すると、この一覧の全件をまとめて
    処理する(main_window 側の制御)。「1つの動画にまとめて出力する」(2件以上の
    ときのみ有効)がチェックされていれば、区間ごとの別ファイルではなく結合した
    1本の動画として出力する。

    行を選択すると entry_selected を発行する。main_window 側でこれを受けて、
    そのIN/OUTをプレイヤーパネルに呼び出す(再確認・再編集用)。

    「更新」ボタンで refresh_requested を発行する。プロジェクト読込直後など
    サムネイル画像を保持していない項目に対して、main_window 側が現在の動画を
    使ってサムネイルを再取得し、update_thumbnails() で反映する。
    """

    entry_selected = Signal(object)  # ExtractPointEntry
    refresh_requested = Signal()
    entries_changed = Signal()  # 登録数が変化した(登録/削除/一覧の呼び出し直し)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list[ExtractPointEntry] = []
        # サムネイル・時間表示が途中で切れないよう、幅を固定する
        # (スプリッタでドラッグしても変化しない)。
        self.setFixedWidth(PANEL_WIDTH)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("抜き出しポイント一覧"))

        self._list_widget = QListWidget()
        self._list_widget.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        layout.addWidget(self._list_widget, 1)

        self._combine_checkbox = QCheckBox("1つの動画にまとめて出力する")
        self._combine_checkbox.setToolTip(
            "抜き出しを実行すると、この一覧の全区間を結合して1本の動画として\n"
            "出力する(チェックを外すと、区間ごとに別ファイルとして出力する)。"
        )
        self._combine_checkbox.setEnabled(False)
        layout.addWidget(self._combine_checkbox)

        button_row = QHBoxLayout()
        self._refresh_button = QPushButton("更新")
        self._refresh_button.setToolTip(
            "現在の動画でサムネイルを再取得する(プロジェクト読込直後などサムネイルが\n"
            "無い場合に使う)。"
        )
        self._delete_button = QPushButton("削除")
        self._delete_button.setEnabled(False)
        button_row.addWidget(self._refresh_button)
        button_row.addWidget(self._delete_button)
        layout.addLayout(button_row)

        self._list_widget.itemSelectionChanged.connect(self._on_selection_changed)
        self._delete_button.clicked.connect(self._on_delete_clicked)
        self._refresh_button.clicked.connect(self.refresh_requested.emit)

    def add_entry(self, request: ExtractRequest, in_pixmap: QPixmap | None, out_pixmap: QPixmap | None) -> None:
        self._append_entry(ExtractPointEntry(request=request, in_pixmap=in_pixmap, out_pixmap=out_pixmap))

    def load_entries(self, entries: list[ExtractPointEntry]) -> None:
        """動画切り替え時などに、その動画用に保持していた一覧をまとめて表示し直す。"""
        self.clear()
        for entry in entries:
            self._append_entry(entry)

    def _append_entry(self, entry: ExtractPointEntry) -> None:
        self._entries.append(entry)
        widget = _EntryWidget(entry)
        item = QListWidgetItem()
        item.setSizeHint(widget.sizeHint())
        self._list_widget.addItem(item)
        self._list_widget.setItemWidget(item, widget)
        self._update_combine_checkbox_enabled()
        self.entries_changed.emit()

    def clear(self) -> None:
        self._entries.clear()
        self._list_widget.clear()
        self._update_combine_checkbox_enabled()
        self.entries_changed.emit()

    def entries(self) -> list[ExtractPointEntry]:
        return list(self._entries)

    def is_empty(self) -> bool:
        return not self._entries

    def combine_output(self) -> bool:
        """「1つの動画にまとめて出力する」がチェックされているか(2件以上の場合のみ有効)。"""
        return self._combine_checkbox.isEnabled() and self._combine_checkbox.isChecked()

    def _update_combine_checkbox_enabled(self) -> None:
        self._combine_checkbox.setEnabled(len(self._entries) >= 2)

    def update_thumbnails(self, index: int, in_pixmap: QPixmap | None, out_pixmap: QPixmap | None) -> None:
        """指定インデックスのサムネイルを差し替えて表示し直す(サムネイル再取得用)。"""
        if not (0 <= index < len(self._entries)):
            return
        entry = self._entries[index]
        entry.in_pixmap = in_pixmap
        entry.out_pixmap = out_pixmap
        self.load_entries(self.entries())

    def set_controls_enabled(self, enabled: bool) -> None:
        """処理実行中はリスト操作(選択・削除・更新・まとめて出力の切替)を禁止する。"""
        self._list_widget.setEnabled(enabled)
        self._refresh_button.setEnabled(enabled and not self.is_empty())
        self._delete_button.setEnabled(enabled and bool(self._list_widget.selectedItems()))
        self._combine_checkbox.setEnabled(enabled and len(self._entries) >= 2)

    def _on_selection_changed(self) -> None:
        has_selection = bool(self._list_widget.selectedItems())
        self._delete_button.setEnabled(has_selection)
        if has_selection:
            row = self._list_widget.currentRow()
            if 0 <= row < len(self._entries):
                self.entry_selected.emit(self._entries[row])

    def _on_delete_clicked(self) -> None:
        row = self._list_widget.currentRow()
        if row < 0:
            return
        self._list_widget.takeItem(row)
        del self._entries[row]
        self._update_combine_checkbox_enabled()
        self.entries_changed.emit()
