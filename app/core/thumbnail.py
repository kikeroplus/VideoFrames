"""動画サムネイルの生成とキャッシュ。仕様書 §5.2 参照。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from app.utils.paths import cache_key_for
from app.utils.subprocess_flags import assign_to_job, hidden_subprocess_kwargs

CACHE_DIR = Path(os.environ["LOCALAPPDATA"]) / "VideoTrimmer" / "thumbs"


def cache_path_for(path: Path) -> Path:
    return CACHE_DIR / f"{cache_key_for(path)}.jpg"


def get_or_create_thumbnail(
    path: Path,
    duration: float,
    ffmpeg_path: Path,
    set_process: Callable[[subprocess.Popen | None], None] | None = None,
) -> Path | None:
    """サムネイルのキャッシュパスを返す。無ければ ffmpeg で生成してから返す。

    生成に失敗した場合は None を返す（呼び出し側は「読み込み不可」等で扱う）。
    set_process を渡すと、起動した Popen を呼び出し側(ワーカー)に通知する。呼び出し側は
    これを保持しておき、フォルダ切り替え等で不要になった際に terminate() でキャンセルできる。
    """
    cache_path = cache_path_for(path)
    if cache_path.is_file():
        return cache_path

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # 先頭は真っ暗なことが多いため duration*0.1 の位置（最低1秒）から1枚抽出
    pos = max(1.0, duration * 0.1)

    cmd = [
        str(ffmpeg_path),
        "-ss", f"{pos}",
        "-i", str(path),
        "-frames:v", "1",
        "-vf", "scale=320:-2",
        "-q:v", "4",
        "-y",
        str(cache_path),
    ]
    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **hidden_subprocess_kwargs(),
        )
    except OSError:
        return None

    assign_to_job(process)
    if set_process is not None:
        set_process(process)
    process.communicate()
    if set_process is not None:
        set_process(None)

    if process.returncode != 0 or not cache_path.is_file():
        return None
    return cache_path
