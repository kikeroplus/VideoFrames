"""ffmpeg / ffprobe 実行ファイルの探索と subprocess 実行。

Phase 0 時点では「探索」のみ実装する。進捗パース・キャンセルは Phase 3 で追加する。
"""

from __future__ import annotations

import shutil
from pathlib import Path

# app/core/ffmpeg_runner.py から見てプロジェクトルート（VideoTrimmer/ 相当）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


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
