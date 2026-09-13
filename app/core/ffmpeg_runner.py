"""ffmpeg / ffprobe 実行ファイルの探索と subprocess 実行。仕様書 §10 参照。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

# app/core/ffmpeg_runner.py から見てプロジェクトルート（VideoTrimmer/ 相当）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Windows でサブプロセス起動時にコンソールウィンドウを出さない
_CREATE_NO_WINDOW = 0x08000000


def _find_executable(name: str) -> Path | None:
    """PATH → ./bin/{name}.exe の順で実行ファイルを探す。

    見つからない場合は None を返す（呼び出し側で手動指定を促す）。
    """
    found = shutil.which(name)
    if found:
        return Path(found)

    local = PROJECT_ROOT / "bin" / f"{name}.exe"
    if local.is_file():
        return local

    return None


def find_ffmpeg() -> Path | None:
    return _find_executable("ffmpeg")


def find_ffprobe() -> Path | None:
    return _find_executable("ffprobe")


class FFmpegSignals(QObject):
    progress = Signal(float)  # 0.0-1.0
    finished = Signal(bool, str)  # 成功可否, メッセージ(失敗時はエラー末尾/キャンセル理由)


class FFmpegJob(QRunnable):
    """ffmpeg を別スレッドで実行し、進捗をパースしてキャンセルにも対応するジョブ。"""

    def __init__(self, cmd: list[str], total_duration_s: float, dst: Path):
        super().__init__()
        self.cmd = cmd
        self.total_duration_s = total_duration_s
        self.dst = dst
        self.signals = FFmpegSignals()
        self._process: subprocess.Popen | None = None
        self._cancel_requested = False

    def cancel(self) -> None:
        """呼び出し側(GUIスレッド)から呼ぶ。terminate() のみ行い、キル待ちは
        run() 側(バックグラウンドスレッド)で行うためブロックしない。"""
        self._cancel_requested = True
        if self._process is not None:
            try:
                self._process.terminate()
            except OSError:
                pass

    def run(self) -> None:
        cmd = [*self.cmd, "-progress", "pipe:1", "-nostats"]
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                bufsize=1,
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self.signals.finished.emit(False, f"ffmpeg の起動に失敗しました: {exc}")
            return

        log_lines: list[str] = []
        assert self._process.stdout is not None
        for raw_line in self._process.stdout:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            key, sep, value = line.partition("=")
            if sep and key in ("out_time_ms", "progress"):
                self._handle_progress(key, value)
            else:
                log_lines.append(line)
                del log_lines[:-20]  # 末尾20行だけ保持(§11)

        if self._cancel_requested:
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            self._cleanup_partial_output()
            self.signals.finished.emit(False, "キャンセルされました")
            return

        self._process.wait()

        if self._process.returncode != 0:
            self._cleanup_partial_output()
            self.signals.finished.emit(False, "\n".join(log_lines))
            return

        self.signals.finished.emit(True, "")

    def _handle_progress(self, key: str, value: str) -> None:
        if key == "out_time_ms":
            try:
                out_time_ms_value = float(value)
            except ValueError:
                return
            if self.total_duration_s > 0:
                # ffmpeg の out_time_ms は実際にはマイクロ秒単位(仕様書 §10 の式に準拠)
                fraction = out_time_ms_value / (self.total_duration_s * 1_000_000)
                self.signals.progress.emit(max(0.0, min(1.0, fraction)))
        elif key == "progress" and value == "end":
            self.signals.progress.emit(1.0)

    def _cleanup_partial_output(self) -> None:
        try:
            if self.dst.is_file():
                self.dst.unlink()
        except OSError:
            pass
