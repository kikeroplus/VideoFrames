from pathlib import Path

from app.core.edit_ops import build_cut_cmd, decide_cut_strategy

KEYFRAMES = [0.0, 2.0, 4.0, 6.0, 10.0]
FPS = 30.0


class TestDecideCutStrategyAuto:
    def test_exact_match_uses_copy(self):
        d = decide_cut_strategy(2.0, KEYFRAMES, FPS, "auto")
        assert d.strategy == "copy"
        assert d.start == 2.0
        assert d.snapped is False
        assert d.exact_match is True

    def test_mismatch_uses_encode_without_snapping(self):
        d = decide_cut_strategy(3.0, KEYFRAMES, FPS, "auto")
        assert d.strategy == "encode"
        assert d.start == 3.0  # autoはユーザーがスナップボタンを押すまで動かさない
        assert d.snapped is False
        assert d.exact_match is False
        assert d.nearest_prev == 2.0
        assert d.nearest_next == 4.0

    def test_no_keyframes_uses_encode(self):
        d = decide_cut_strategy(3.0, [], FPS, "auto")
        assert d.strategy == "encode"
        assert d.exact_match is False

    def test_keyframes_none_uses_encode(self):
        d = decide_cut_strategy(3.0, None, FPS, "auto")
        assert d.strategy == "encode"


class TestDecideCutStrategyCopyPriority:
    def test_snaps_to_prev_by_default(self):
        d = decide_cut_strategy(3.0, KEYFRAMES, FPS, "copy_priority")
        assert d.strategy == "copy"
        assert d.start == 2.0
        assert d.snapped is True

    def test_snaps_to_next_when_requested(self):
        d = decide_cut_strategy(3.0, KEYFRAMES, FPS, "copy_priority", snap_direction="next")
        assert d.strategy == "copy"
        assert d.start == 4.0
        assert d.snapped is True

    def test_no_keyframes_falls_back_to_encode(self):
        d = decide_cut_strategy(3.0, [], FPS, "copy_priority")
        assert d.strategy == "encode"

    def test_exact_match_does_not_snap(self):
        d = decide_cut_strategy(4.0, KEYFRAMES, FPS, "copy_priority")
        assert d.strategy == "copy"
        assert d.start == 4.0
        assert d.snapped is False


class TestDecideCutStrategyAlwaysEncode:
    def test_always_encode_ignores_keyframes(self):
        d = decide_cut_strategy(2.0, KEYFRAMES, FPS, "always_encode")
        assert d.strategy == "encode"
        assert d.start == 2.0
        assert d.snapped is False


class TestBuildCutCmd:
    def test_copy_command(self):
        cmd = build_cut_cmd(
            Path("ffmpeg.exe"), Path("src.mp4"), Path("dst.mp4"),
            start=1.5, end=10.0, strategy="copy", vcodec="h264", has_audio=True,
        )
        assert cmd[0] == "ffmpeg.exe"
        assert "-ss" in cmd and "1.5" in cmd
        assert "-to" in cmd and "10.0" in cmd
        assert "-c" in cmd and "copy" in cmd
        assert "-c:v" not in cmd
        assert "-an" not in cmd
        assert cmd[-1] == "dst.mp4"

    def test_copy_command_exclude_audio(self):
        cmd = build_cut_cmd(
            Path("ffmpeg.exe"), Path("src.mp4"), Path("dst.mp4"),
            start=0.0, end=5.0, strategy="copy", vcodec="h264", has_audio=True,
            exclude_audio=True,
        )
        assert "-an" in cmd

    def test_encode_command_h264(self):
        cmd = build_cut_cmd(
            Path("ffmpeg.exe"), Path("src.mp4"), Path("dst.mp4"),
            start=0.0, end=5.0, strategy="encode", vcodec="h264", has_audio=True,
        )
        assert "-c:v" in cmd
        idx = cmd.index("-c:v")
        assert cmd[idx + 1] == "libx264"
        assert "-c:a" in cmd and "aac" in cmd

    def test_encode_command_hevc(self):
        cmd = build_cut_cmd(
            Path("ffmpeg.exe"), Path("src.mp4"), Path("dst.mp4"),
            start=0.0, end=5.0, strategy="encode", vcodec="hevc", has_audio=True,
        )
        idx = cmd.index("-c:v")
        assert cmd[idx + 1] == "libx265"

    def test_encode_command_no_audio_stream(self):
        cmd = build_cut_cmd(
            Path("ffmpeg.exe"), Path("src.mp4"), Path("dst.mp4"),
            start=0.0, end=5.0, strategy="encode", vcodec="h264", has_audio=False,
        )
        assert "-c:a" not in cmd
        assert "-an" in cmd
