"""Windows でサブプロセス起動時にコンソールウィンドウを出さないための設定。

--windowed でパッケージ化した EXE には親コンソールが無いため、フラグを
指定せずに ffmpeg/ffprobe (コンソールアプリ) を起動すると、実行の度に
新しいコンソールウィンドウや conhost.exe プロセスが残ってしまう。
creationflags=CREATE_NO_WINDOW だけでは PyInstaller の --windowed ビルドから
起動した場合に conhost.exe が残留することがあるため、STARTUPINFO による
明示的な非表示指定もあわせて使う。全ての ffmpeg/ffprobe 起動箇所で
hidden_subprocess_kwargs() の戻り値を渡すこと。
"""

from __future__ import annotations

import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def hidden_subprocess_kwargs() -> dict:
    """subprocess.run()/Popen() にそのまま展開して渡す追加キーワード引数。"""
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": startupinfo}
