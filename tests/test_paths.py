from pathlib import Path

from app.utils.paths import next_output_path


class TestNextOutputPath:
    def test_first_number(self, tmp_path: Path):
        src = tmp_path / "video.mp4"
        result = next_output_path(src, tmp_path, "clip")
        assert result == tmp_path / "video_clip_001.mp4"

    def test_skips_existing_numbers(self, tmp_path: Path):
        src = tmp_path / "video.mp4"
        (tmp_path / "video_clip_001.mp4").touch()
        (tmp_path / "video_clip_002.mp4").touch()
        result = next_output_path(src, tmp_path, "clip")
        assert result == tmp_path / "video_clip_003.mp4"

    def test_fills_gap_after_first_free_number(self, tmp_path: Path):
        # 001が使用中で002が空いていても、連番は先頭から探す(002は使わない)
        src = tmp_path / "video.mp4"
        (tmp_path / "video_clip_001.mp4").touch()
        (tmp_path / "video_clip_003.mp4").touch()
        result = next_output_path(src, tmp_path, "clip")
        assert result == tmp_path / "video_clip_002.mp4"

    def test_different_suffix_independent(self, tmp_path: Path):
        src = tmp_path / "video.mp4"
        (tmp_path / "video_clip_001.mp4").touch()
        result = next_output_path(src, tmp_path, "cut")
        assert result == tmp_path / "video_cut_001.mp4"

    def test_preserves_extension(self, tmp_path: Path):
        src = tmp_path / "video.mov"
        result = next_output_path(src, tmp_path, "clip")
        assert result.suffix == ".mov"
