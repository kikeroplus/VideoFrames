"""キャッシュキー生成など、パス関連の共通ユーティリティ。"""

from __future__ import annotations

import hashlib
from pathlib import Path


def cache_key_for(path: Path) -> str:
    """絶対パス + mtime + サイズ から短いハッシュを作る（ファイル更新で自動的に変わる）。

    サムネイルキャッシュ(§5.2)とキーフレームキャッシュ(§8.2)で共通のキー生成方式。
    """
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_mtime}|{st.st_size}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def next_output_path(src: Path, output_dir: Path, suffix: str) -> Path:
    """出力ファイルパスを生成する。仕様書 §9 参照。

    `{元名}_{suffix}_{連番3桁}{元拡張子}` の形式。既存ファイルを走査して
    未使用の番号を採用する(既存ファイルは上書きしない)。
    """
    stem = src.stem
    ext = src.suffix
    n = 1
    while True:
        candidate = output_dir / f"{stem}_{suffix}_{n:03d}{ext}"
        if not candidate.exists():
            return candidate
        n += 1
