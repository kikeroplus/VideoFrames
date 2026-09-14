"""キーフレーム位置の取得・キャッシュ・コピー可否判定。仕様書 §8 参照。"""

from __future__ import annotations

import json
import os
import subprocess
from bisect import bisect_left, bisect_right
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, Signal

from app.utils.paths import cache_key_for
from app.utils.subprocess_flags import assign_to_job, hidden_subprocess_kwargs

CACHE_DIR = Path(os.environ["LOCALAPPDATA"]) / "VideoTrimmer" / "keyframes"


class KeyframeError(Exception):
    """キーフレーム位置の取得に失敗した場合に送出する。"""


def _cache_path_for(path: Path) -> Path:
    return CACHE_DIR / f"{cache_key_for(path)}.json"


def get_keyframes(
    path: Path,
    ffprobe_path: Path,
    set_process: Callable[[subprocess.Popen | None], None] | None = None,
) -> list[float]:
    """動画のキーフレーム位置(秒, 昇順)を返す。結果はディスクにキャッシュする。

    取得に失敗した場合は KeyframeError を送出する（呼び出し側は §8.2 の通り
    「判定不能」として安全側＝再エンコードに倒すこと）。
    set_process を渡すと、起動した Popen を呼び出し側(ワーカー)に通知する。呼び出し側は
    これを保持しておき、不要になった際に terminate() でキャンセルできる。
    """
    cache_path = _cache_path_for(path)
    if cache_path.is_file():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return [float(v) for v in data]
        except (OSError, ValueError, TypeError):
            pass  # キャッシュ破損時は再取得する

    keyframes = _probe_keyframes(path, ffprobe_path, set_process=set_process)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        cache_path.write_text(json.dumps(keyframes), encoding="utf-8")
    except OSError:
        pass  # キャッシュ書き込み失敗は致命的ではない

    return keyframes


def _probe_keyframes(
    path: Path,
    ffprobe_path: Path,
    set_process: Callable[[subprocess.Popen | None], None] | None = None,
) -> list[float]:
    cmd = [
        str(ffprobe_path),
        "-v", "error",
        "-select_streams", "v:0",
        "-show_packets",
        "-show_entries", "packet=pts_time,flags",
        "-of", "csv=p=0",
        str(path),
    ]
    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            **hidden_subprocess_kwargs(),
        )
    except OSError as exc:
        raise KeyframeError(f"ffprobe の実行に失敗しました: {exc}") from exc

    assign_to_job(process)
    if set_process is not None:
        set_process(process)
    stdout, stderr = process.communicate()
    if set_process is not None:
        set_process(None)

    if process.returncode != 0:
        raise KeyframeError(f"ffprobe がエラー終了しました ({path}): {stderr.strip()}")

    keyframes: list[float] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        pts_time_str, flags = parts[0], parts[1]
        if "K" not in flags:
            continue
        try:
            keyframes.append(float(pts_time_str))
        except ValueError:
            continue

    keyframes.sort()
    return keyframes


def can_copy(t: float, keyframes: list[float], fps: float) -> bool:
    """t がキーフレーム位置と一致するか（許容差=1フレーム時間の半分）。仕様書 §8.3 参照。"""
    if fps <= 0 or not keyframes:
        return False
    tol = 0.5 / fps
    i = bisect_left(keyframes, t - tol)
    return i < len(keyframes) and abs(keyframes[i] - t) <= tol


def prev_keyframe(t: float, keyframes: list[float]) -> float | None:
    """現在位置 t より前にある最も近いキーフレームを返す(無ければ None)。"""
    if not keyframes:
        return None
    i = bisect_left(keyframes, t)
    if i > 0:
        return keyframes[i - 1]
    return None


def next_keyframe(t: float, keyframes: list[float]) -> float | None:
    """現在位置 t より後にある最も近いキーフレームを返す(無ければ None)。"""
    if not keyframes:
        return None
    i = bisect_right(keyframes, t)
    if i < len(keyframes):
        return keyframes[i]
    return None


def nearest_keyframe(t: float, keyframes: list[float]) -> float | None:
    """t に最も近いキーフレームを返す(前後どちらか。無ければ None)。

    IN/OUT設定を押した時点でキーフレームへ強制的に揃えるモード用。t が既に
    キーフレーム上にある場合はそのまま t を返す(prev_keyframe/next_keyframe は
    どちらも「厳密に前/後」を返すため、ここでは別に判定する)。
    """
    if not keyframes:
        return None
    i = bisect_left(keyframes, t)
    if i < len(keyframes) and keyframes[i] == t:
        return t
    prev_kf = keyframes[i - 1] if i > 0 else None
    next_kf = keyframes[i] if i < len(keyframes) else None
    if prev_kf is None:
        return next_kf
    if next_kf is None:
        return prev_kf
    return prev_kf if (t - prev_kf) <= (next_kf - t) else next_kf


class KeyframeSignals(QObject):
    loaded = Signal(Path, list)  # path, keyframes
    failed = Signal(Path, str)  # path, エラーメッセージ


class KeyframeWorker(QRunnable):
    """1本の動画についてキーフレーム位置を取得するバックグラウンドタスク。"""

    def __init__(self, path: Path, ffprobe_path: Path):
        super().__init__()
        self.path = path
        self.ffprobe_path = ffprobe_path
        self.signals = KeyframeSignals()
        self._process: subprocess.Popen | None = None
        self._cancelled = False

    def cancel(self) -> None:
        """呼び出し側(GUIスレッド)から呼ぶ。実行中の ffprobe を直ちに終了させる。"""
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
            keyframes = get_keyframes(self.path, self.ffprobe_path, set_process=set_process)
        except KeyframeError as exc:
            if not self._cancelled:
                self.signals.failed.emit(self.path, str(exc))
            return
        if self._cancelled:
            return
        self.signals.loaded.emit(self.path, keyframes)
