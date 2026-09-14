"""フォルダのスキャンと、動画1本ごとの非同期ロード（メタ情報＋サムネイル）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from app.core.models import VideoItem
from app.core.probe import ProbeError, probe_video
from app.core.thumbnail import get_or_create_thumbnail

# 仕様書 §5.1
SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".m4v", ".ts", ".webm"}


def scan_folder(folder: Path, extensions: set[str] | None = None) -> list[Path]:
    """フォルダ直下の対応拡張子の動画ファイル一覧を返す（サブフォルダは走査しない）。

    extensions を省略した場合は既定の対応拡張子(SUPPORTED_EXTENSIONS)を使う。
    """
    exts = extensions if extensions is not None else SUPPORTED_EXTENSIONS
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in exts
    )


class LoadSignals(QObject):
    loaded = Signal(int, Path, object)  # generation, path, VideoItem
    failed = Signal(int, Path, str)  # generation, path, エラーメッセージ


class LoadVideoWorker(QRunnable):
    """1本の動画についてメタ情報取得とサムネイル生成を行うバックグラウンドタスク。"""

    def __init__(self, path: Path, generation: int, ffmpeg_path: Path, ffprobe_path: Path):
        super().__init__()
        self.path = path
        self.generation = generation
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.signals = LoadSignals()
        self._process: subprocess.Popen | None = None
        self._cancelled = False

    def cancel(self) -> None:
        """呼び出し側(GUIスレッド)から呼ぶ。実行中の ffprobe/ffmpeg を直ちに終了させる。

        QThreadPool.clear() は未着手タスクの取消のみで、既に実行中のワーカーは
        最後まで動き続けてしまう(結果は世代チェックで捨てられるだけでプロセスは
        止まらない)ため、フォルダ切り替え・アプリ終了時にはこちらを呼ぶ必要がある。
        """
        self._cancelled = True
        if self._process is not None:
            try:
                self._process.terminate()
            except OSError:
                pass

    def run(self) -> None:
        def set_process(p: subprocess.Popen | None) -> None:
            self._process = p

        try:
            item = probe_video(self.path, self.ffprobe_path, set_process=set_process)
        except ProbeError as exc:
            if not self._cancelled:
                self.signals.failed.emit(self.generation, self.path, str(exc))
            return

        if self._cancelled:
            return

        item.thumb_path = get_or_create_thumbnail(
            self.path, item.duration, self.ffmpeg_path, set_process=set_process,
        )
        if self._cancelled:
            return
        self.signals.loaded.emit(self.generation, self.path, item)
