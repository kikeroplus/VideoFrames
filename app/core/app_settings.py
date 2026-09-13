"""アプリ設定(QSettings)の永続化。キー名をこのモジュールに集約する。仕様書 §2/§9 参照。"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QSettings

ORG_NAME = "VideoTrimmer"
APP_NAME = "VideoTrimmer"

# 仕様書 §5.1
DEFAULT_EXTENSIONS = [".mp4", ".mov", ".mkv", ".avi", ".wmv", ".m4v", ".ts", ".webm"]

KEY_LAST_FOLDER = "last_folder"
KEY_FFMPEG_PATH = "ffmpeg_path"
KEY_FFPROBE_PATH = "ffprobe_path"
KEY_OUTPUT_DIR = "output_dir"
KEY_EXTENSIONS = "target_extensions"
KEY_THUMB_SIZE = "thumb_size"
KEY_EXTRACT_CUT_MODE = "extract_cut_mode"
KEY_DELETE_CUT_MODE = "delete_cut_mode"


def make_settings() -> QSettings:
    return QSettings(ORG_NAME, APP_NAME)


@dataclass
class AppSettings:
    ffmpeg_path: str = ""  # 空なら自動検出を使う
    ffprobe_path: str = ""
    output_dir: str = ""  # 空なら既定(元動画フォルダ直下の output/)
    extensions: list[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))


def load_app_settings(settings: QSettings) -> AppSettings:
    ffmpeg_path = settings.value(KEY_FFMPEG_PATH, "", str)
    ffprobe_path = settings.value(KEY_FFPROBE_PATH, "", str)
    output_dir = settings.value(KEY_OUTPUT_DIR, "", str)
    ext_str = settings.value(KEY_EXTENSIONS, "", str)
    extensions = (
        [e.strip() for e in ext_str.split(",") if e.strip()]
        if ext_str else list(DEFAULT_EXTENSIONS)
    )
    return AppSettings(
        ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path,
        output_dir=output_dir, extensions=extensions,
    )


def save_app_settings(settings: QSettings, app_settings: AppSettings) -> None:
    settings.setValue(KEY_FFMPEG_PATH, app_settings.ffmpeg_path)
    settings.setValue(KEY_FFPROBE_PATH, app_settings.ffprobe_path)
    settings.setValue(KEY_OUTPUT_DIR, app_settings.output_dir)
    settings.setValue(KEY_EXTENSIONS, ",".join(app_settings.extensions))
