"""キーフレーム位置の取得・キャッシュ・コピー可否判定。仕様書 §8 参照。"""

from __future__ import annotations

import json
import os
import subprocess
from bisect import bisect_left, bisect_right
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from app.utils.paths import cache_key_for

CACHE_DIR = Path(os.environ["LOCALAPPDATA"]) / "VideoTrimmer" / "keyframes"


class KeyframeError(Exception):
    """キーフレーム位置の取得に失敗した場合に送出する。"""


def _cache_path_for(path: Path) -> Path:
    return CACHE_DIR / f"{cache_key_for(path)}.json"


def get_keyframes(path: Path, ffprobe_path: Path) -> list[float]:
    """動画のキーフレーム位置(秒, 昇順)を返す。結果はディスクにキャッシュする。

    取得に失敗した場合は KeyframeError を送出する（呼び出し側は §8.2 の通り
    「判定不能」として安全側＝再エンコードに倒すこと）。
    """
    cache_path = _cache_path_for(path)
    if cache_path.is_file():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return [float(v) for v in data]
        except (OSError, ValueError, TypeError):
            pass  # キャッシュ破損時は再取得する

    keyframes = _probe_keyframes(path, ffprobe_path)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        cache_path.write_text(json.dumps(keyframes), encoding="utf-8")
    except OSError:
        pass  # キャッシュ書き込み失敗は致命的ではない

    return keyframes


def _probe_keyframes(path: Path, ffprobe_path: Path) -> list[float]:
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
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    except OSError as exc:
        raise KeyframeError(f"ffprobe の実行に失敗しました: {exc}") from exc

    if result.returncode != 0:
        raise KeyframeError(f"ffprobe がエラー終了しました ({path}): {result.stderr.strip()}")

    keyframes: list[float] = []
    for line in result.stdout.splitlines():
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

    def run(self) -> None:
        try:
            keyframes = get_keyframes(self.path, self.ffprobe_path)
        except KeyframeError as exc:
            self.signals.failed.emit(self.path, str(exc))
            return
        self.signals.loaded.emit(self.path, keyframes)
