from pathlib import Path

from PySide6.QtCore import QSettings

from app.core.app_settings import (
    DEFAULT_EXTENSIONS,
    AppSettings,
    load_app_settings,
    save_app_settings,
)


def _ini_settings(tmp_path: Path) -> QSettings:
    return QSettings(str(tmp_path / "test.ini"), QSettings.Format.IniFormat)


class TestLoadAppSettings:
    def test_defaults_when_empty(self, tmp_path: Path):
        settings = _ini_settings(tmp_path)
        app_settings = load_app_settings(settings)
        assert app_settings.ffmpeg_path == ""
        assert app_settings.ffprobe_path == ""
        assert app_settings.output_dir == ""
        assert app_settings.extensions == DEFAULT_EXTENSIONS

    def test_roundtrip(self, tmp_path: Path):
        settings = _ini_settings(tmp_path)
        original = AppSettings(
            ffmpeg_path="C:/tools/ffmpeg.exe",
            ffprobe_path="C:/tools/ffprobe.exe",
            output_dir="D:/out",
            extensions=[".mp4", ".mov"],
        )
        save_app_settings(settings, original)

        settings2 = _ini_settings(tmp_path)
        loaded = load_app_settings(settings2)
        assert loaded == original

    def test_extensions_roundtrip_preserves_order(self, tmp_path: Path):
        settings = _ini_settings(tmp_path)
        original = AppSettings(extensions=[".webm", ".ts", ".mp4"])
        save_app_settings(settings, original)

        loaded = load_app_settings(_ini_settings(tmp_path))
        assert loaded.extensions == [".webm", ".ts", ".mp4"]
