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
