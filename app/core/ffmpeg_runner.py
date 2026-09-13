"""ffmpeg / ffprobe 実行ファイルの探索と subprocess 実行。仕様書 §10 参照。"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, Signal

from app.core.edit_ops import Strategy, build_cut_cmd

# app/core/ffmpeg_runner.py から見てプロジェクトルート（VideoTrimmer/ 相当）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Windows でサブプロセス起動時にコンソールウィンドウを出さない
_CREATE_NO_WINDOW = 0x08000000

CANCELLED = "__cancelled__"  # _run_ffmpeg がキャンセルを示すために使う特別なメッセージ

# -progress pipe:1 が定期的に出力する key=value のうち、進捗計算に直接使わない
# フィールド。ログ(エラーダイアログの末尾20行)に混ざらないよう除外する。
_PROGRESS_KEYS = {
    "frame", "fps", "bitrate", "total_size",
    "out_time_us", "out_time", "dup_frames", "drop_frames", "speed",
}


def _is_progress_key(key: str) -> bool:
    if key in _PROGRESS_KEYS:
        return True
    return key.startswith("stream_") and key.endswith("_q")


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


def _run_ffmpeg(
    cmd: list[str],
    total_duration_s: float,
    is_cancelled: Callable[[], bool],
    set_process: Callable[[subprocess.Popen | None], None],
    on_progress: Callable[[float], None],
) -> tuple[bool, str]:
    """ffmpeg を1本実行し、進捗をパースする。(成功可否, メッセージ) を返す。

    メッセージはキャンセル時は CANCELLED、失敗時は stderr 末尾20行、成功時は空文字列。
    FFmpegJob / DeleteMiddleJob の双方から共通で使う。
    """
    full_cmd = [*cmd, "-progress", "pipe:1", "-nostats"]
    try:
        process = subprocess.Popen(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError as exc:
        return False, f"ffmpeg の起動に失敗しました: {exc}"

    set_process(process)
    log_lines: list[str] = []
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip("\n")
        if not line:
            continue
        key, sep, value = line.partition("=")
        if sep and key == "out_time_ms":
            try:
                out_time_ms_value = float(value)
            except ValueError:
                continue
            if total_duration_s > 0:
                # ffmpeg の out_time_ms は実際にはマイクロ秒単位(仕様書 §10 の式に準拠)
                fraction = out_time_ms_value / (total_duration_s * 1_000_000)
                on_progress(max(0.0, min(1.0, fraction)))
        elif sep and key == "progress" and value == "end":
            on_progress(1.0)
        elif sep and _is_progress_key(key):
            continue  # frame=/fps=/bitrate= など他の進捗フィールドはログに含めない
        else:
            log_lines.append(line)  # 全量を保持する。末尾20行への切り詰めは表示側(§11)で行う

    if is_cancelled():
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        set_process(None)
        return False, CANCELLED

    process.wait()
    set_process(None)

    if process.returncode != 0:
        return False, "\n".join(log_lines)
    return True, ""


class FFmpegSignals(QObject):
    progress = Signal(float)  # 0.0-1.0
    finished = Signal(bool, str)  # 成功可否, メッセージ(失敗時はエラー末尾/キャンセル理由)


class FFmpegJob(QRunnable):
    """ffmpeg を別スレッドで実行し、進捗をパースしてキャンセルにも対応するジョブ。

    抜き出し・冒頭削除・末尾削除など、単一区間の切り出しに使う。
    """

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
        def set_process(p: subprocess.Popen | None) -> None:
            self._process = p

        ok, message = _run_ffmpeg(
            self.cmd, self.total_duration_s,
            lambda: self._cancel_requested, set_process, self.signals.progress.emit,
        )
        if not ok:
            self._cleanup_partial_output()
            self.signals.finished.emit(False, "キャンセルされました" if message == CANCELLED else message)
            return
        self.signals.finished.emit(True, "")

    def _cleanup_partial_output(self) -> None:
        try:
            if self.dst.is_file():
                self.dst.unlink()
        except OSError:
            pass


class DeleteMiddleJob(QRunnable):
    """途中削除: 残す区間ごとに一時ファイルへ切り出し、concat demuxer で結合する。

    仕様書 §6/§8.5/§10 参照。2区間は同じ strategy で統一して切り出す(混在させると
    結合できないため)。進捗は各区間の長さで按分する。
    """

    def __init__(
        self,
        ffmpeg_path: Path,
        src: Path,
        dst: Path,
        ranges: list[tuple[float, float]],
        strategy: Strategy,
        vcodec: str,
        has_audio: bool,
    ):
        super().__init__()
        self.ffmpeg_path = ffmpeg_path
        self.src = src
        self.dst = dst
        self.ranges = ranges
        self.strategy = strategy
        self.vcodec = vcodec
        self.has_audio = has_audio
        self.signals = FFmpegSignals()
        self._process: subprocess.Popen | None = None
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True
        if self._process is not None:
            try:
                self._process.terminate()
            except OSError:
                pass

    def run(self) -> None:
        # 各区間切り出しに全体の80%、結合に20%を配分する
        CUT_WEIGHT = 0.8
        CONCAT_WEIGHT = 0.2

        range_durations = [end - start for start, end in self.ranges]
        total_range_duration = sum(range_durations) or 1.0

        with tempfile.TemporaryDirectory(prefix="videotrimmer_") as tmp_dir_name:
            tmp_dir = Path(tmp_dir_name)
            segment_paths: list[Path] = []
            progress_so_far = 0.0

            for i, (start, end) in enumerate(self.ranges):
                if self._cancel_requested:
                    self.signals.finished.emit(False, "キャンセルされました")
                    return

                segment_path = tmp_dir / f"segment_{i}.mp4"
                segment_paths.append(segment_path)
                cmd = build_cut_cmd(
                    self.ffmpeg_path, self.src, segment_path,
                    start=start, end=end, strategy=self.strategy,
                    vcodec=self.vcodec, has_audio=self.has_audio,
                )
                segment_weight = (range_durations[i] / total_range_duration) * CUT_WEIGHT
                base = progress_so_far

                def set_process(p: subprocess.Popen | None) -> None:
                    self._process = p

                def on_progress(fraction: float, base=base, weight=segment_weight) -> None:
                    self.signals.progress.emit(base + weight * fraction)

                ok, message = _run_ffmpeg(
                    cmd, range_durations[i], lambda: self._cancel_requested, set_process, on_progress,
                )
                if not ok:
                    self.signals.finished.emit(
                        False, "キャンセルされました" if message == CANCELLED else message
                    )
                    return
                progress_so_far += segment_weight

            list_path = tmp_dir / "concat_list.txt"
            with open(list_path, "w", encoding="utf-8") as f:
                for segment_path in segment_paths:
                    escaped = str(segment_path).replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")

            concat_cmd = [
                str(self.ffmpeg_path), "-f", "concat", "-safe", "0",
                "-i", str(list_path), "-c", "copy", "-y", str(self.dst),
            ]

            def set_process2(p: subprocess.Popen | None) -> None:
                self._process = p

            def on_concat_progress(fraction: float) -> None:
                self.signals.progress.emit(progress_so_far + CONCAT_WEIGHT * fraction)

            ok, message = _run_ffmpeg(
                concat_cmd, total_range_duration, lambda: self._cancel_requested,
                set_process2, on_concat_progress,
            )
            if not ok:
                self._cleanup_partial_output()
                self.signals.finished.emit(
                    False, "キャンセルされました" if message == CANCELLED else message
                )
                return

            self.signals.progress.emit(1.0)
            self.signals.finished.emit(True, "")

    def _cleanup_partial_output(self) -> None:
        try:
            if self.dst.is_file():
                self.dst.unlink()
        except OSError:
            pass
