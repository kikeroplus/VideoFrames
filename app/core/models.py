"""アプリ内で使うデータクラス群。仕様書 §4 参照。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass
class VideoItem:
    path: Path
    duration: float  # 秒
    fps: float  # r_frame_rate を float 化
    width: int
    height: int
    vcodec: str
    acodec: str | None
    size_bytes: int
    thumb_path: Path | None = None  # キャッシュ済みサムネ
    keyframes: list[float] | None = None  # 遅延取得。§8参照
    is_vfr: bool = False  # 可変フレームレート(VFR)かどうか。§6のフレーム単位入力の警告に使う


@dataclass
class EditJob:
    src: Path
    dst: Path
    mode: Literal["extract", "delete_head", "delete_tail", "delete_middle"]
    ranges: list[tuple[float, float]]  # 残す区間（秒）。deleteも「残す区間」に正規化してから処理
    strategy: Literal["copy", "encode"]  # 区間ごとではなくジョブ全体で統一する
    snapped: bool = False  # スナップでIN点を動かしたか（ログ・結果表示用）
