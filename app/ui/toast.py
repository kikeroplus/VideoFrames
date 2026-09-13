"""非モーダルな完了通知(トースト)。仕様書 §9 参照。"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

DEFAULT_DURATION_MS = 6000


class Toast(QWidget):
    """親ウィンドウの右下に浮かぶ非モーダルな通知。一定時間で自動的に消える。"""

    def __init__(
        self,
        parent: QWidget,
        message: str,
        action_text: str | None = None,
        on_action: Callable[[], None] | None = None,
        duration_ms: int = DEFAULT_DURATION_MS,
    ):
        super().__init__(parent, Qt.WindowType.ToolTip)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setStyleSheet(
            """
            Toast { background: palette(base); border: 1px solid palette(mid); border-radius: 6px; }
            QLabel { color: palette(text); }
            """
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 10, 10)
        label = QLabel(message)
        label.setWordWrap(True)
        label.setMaximumWidth(360)
        layout.addWidget(label, 1)

        if action_text and on_action is not None:
            action_button = QPushButton(action_text)
            action_button.clicked.connect(lambda: (on_action(), self.close()))
            layout.addWidget(action_button)

        close_button = QPushButton("×")
        close_button.setFixedWidth(28)
        close_button.clicked.connect(self.close)
        layout.addWidget(close_button)

        QTimer.singleShot(duration_ms, self.close)
        self._reposition()
        self.show()

    def _reposition(self) -> None:
        self.adjustSize()
        parent = self.parentWidget()
        if parent is None:
            return
        top_left = parent.mapToGlobal(parent.rect().bottomRight())
        x = top_left.x() - self.width() - 24
        y = top_left.y() - self.height() - 24
        self.move(x, y)
