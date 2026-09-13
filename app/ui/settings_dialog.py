"""設定ダイアログ。仕様書 §2/§5.1/§9 参照。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from app.core.app_settings import DEFAULT_EXTENSIONS, AppSettings
from app.core.ffmpeg_runner import find_ffmpeg, find_ffprobe


class SettingsDialog(QDialog):
    """ffmpeg/ffprobeパス、出力先フォルダ、対象拡張子を設定する。"""

    def __init__(self, current: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.resize(640, 0)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._ffmpeg_edit = QLineEdit(current.ffmpeg_path)
        auto_ffmpeg = find_ffmpeg()
        self._ffmpeg_edit.setPlaceholderText(
            f"自動検出: {auto_ffmpeg}" if auto_ffmpeg else "自動検出できませんでした。手動で指定してください"
        )
        ffmpeg_row = self._make_path_row(self._ffmpeg_edit, self._browse_ffmpeg)
        form.addRow("ffmpeg:", ffmpeg_row)

        self._ffprobe_edit = QLineEdit(current.ffprobe_path)
        auto_ffprobe = find_ffprobe()
        self._ffprobe_edit.setPlaceholderText(
            f"自動検出: {auto_ffprobe}" if auto_ffprobe else "自動検出できませんでした。手動で指定してください"
        )
        ffprobe_row = self._make_path_row(self._ffprobe_edit, self._browse_ffprobe)
        form.addRow("ffprobe:", ffprobe_row)

        self._output_dir_edit = QLineEdit(current.output_dir)
        self._output_dir_edit.setPlaceholderText("既定: 元動画フォルダ直下の output フォルダ")
        output_row = self._make_path_row(self._output_dir_edit, self._browse_output_dir, is_dir=True)
        form.addRow("出力先フォルダ:", output_row)

        self._extensions_edit = QLineEdit(",".join(current.extensions))
        ext_reset = QPushButton("既定に戻す")
        ext_reset.clicked.connect(
            lambda: self._extensions_edit.setText(",".join(DEFAULT_EXTENSIONS))
        )
        ext_row = QHBoxLayout()
        ext_row.addWidget(self._extensions_edit, 1)
        ext_row.addWidget(ext_reset)
        form.addRow("対象拡張子(カンマ区切り):", ext_row)

        layout.addLayout(form)

        note = QLabel("※ 対象拡張子と出力先フォルダの変更は、次にフォルダを開いたときから反映されます。")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _make_path_row(self, edit: QLineEdit, browse_slot, is_dir: bool = False) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        browse_button = QPushButton("参照…")
        browse_button.clicked.connect(browse_slot)
        row.addWidget(browse_button)
        return row

    def _browse_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "ffmpeg.exe を選択", "", "実行ファイル (*.exe);;すべてのファイル (*)")
        if path:
            self._ffmpeg_edit.setText(path)

    def _browse_ffprobe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "ffprobe.exe を選択", "", "実行ファイル (*.exe);;すべてのファイル (*)")
        if path:
            self._ffprobe_edit.setText(path)

    def _browse_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "出力先フォルダを選択")
        if path:
            self._output_dir_edit.setText(path)

    def result_settings(self) -> AppSettings:
        extensions = [e.strip().lower() for e in self._extensions_edit.text().split(",") if e.strip()]
        if not extensions:
            extensions = list(DEFAULT_EXTENSIONS)
        # 先頭に "." が無ければ補う
        extensions = [e if e.startswith(".") else f".{e}" for e in extensions]
        return AppSettings(
            ffmpeg_path=self._ffmpeg_edit.text().strip(),
            ffprobe_path=self._ffprobe_edit.text().strip(),
            output_dir=self._output_dir_edit.text().strip(),
            extensions=extensions,
        )
