"""VideoTrimmer エントリポイント。

引数なしで実行すると GUI (MainWindow) を起動する。
引数に動画ファイルパスを1つ渡すと、Phase 0 から残している
CLI 動作確認モード（ffprobe でメタ情報を表示するだけ）で動く。
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.core.ffmpeg_runner import find_ffmpeg, find_ffprobe
from app.core.probe import ProbeError, probe_video


def run_gui() -> int:
    from PySide6.QtWidgets import QApplication

    from app.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


def probe_cli(video_path_str: str) -> int:
    video_path = Path(video_path_str)
    if not video_path.is_file():
        print(f"ファイルが見つかりません: {video_path}")
        return 1

    ffmpeg_path = find_ffmpeg()
    ffprobe_path = find_ffprobe()

    if ffmpeg_path is None or ffprobe_path is None:
        print("ffmpeg / ffprobe が見つかりません。")
        print("PATH に追加するか、プロジェクト直下の bin/ フォルダに配置してください。")
        return 1

    print(f"ffmpeg : {ffmpeg_path}")
    print(f"ffprobe: {ffprobe_path}")

    try:
        item = probe_video(video_path, ffprobe_path)
    except ProbeError as exc:
        print(f"メタ情報の取得に失敗しました: {exc}")
        return 1

    print()
    print(f"path       : {item.path}")
    print(f"duration   : {item.duration:.3f} 秒")
    print(f"fps        : {item.fps:.3f}")
    print(f"resolution : {item.width}x{item.height}")
    print(f"vcodec     : {item.vcodec}")
    print(f"acodec     : {item.acodec or '(なし)'}")
    print(f"size       : {item.size_bytes:,} bytes")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 1:
        return run_gui()
    if len(argv) == 2:
        return probe_cli(argv[1])

    print(f"使い方: python {Path(argv[0]).name} [動画ファイルパス]")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
