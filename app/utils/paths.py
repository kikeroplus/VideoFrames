"""キャッシュキー生成など、パス関連の共通ユーティリティ。"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

# app/utils/paths.py から見てプロジェクトルート（VideoTrimmer/ 相当）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def resource_path(relative: str) -> Path:
    """アイコン等の同梱リソースへのパスを解決する。

    開発時はプロジェクトルート基準、PyInstaller でパッケージ化された
    実行ファイルでは展開先(sys._MEIPASS)基準になる。
    """
    base = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
    return base / relative


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
