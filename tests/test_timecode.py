import pytest

from app.utils.timecode import (
    frame_to_seconds,
    seconds_to_frame,
    seconds_to_timecode,
    timecode_to_seconds,
)


class TestSecondsToTimecode:
    def test_zero(self):
        assert seconds_to_timecode(0) == "00:00:00.000"

    def test_sub_second(self):
        assert seconds_to_timecode(0.4) == "00:00:00.400"

    def test_minutes_and_seconds(self):
        assert seconds_to_timecode(83.4) == "00:01:23.400"

    def test_hours(self):
        assert seconds_to_timecode(3661.5) == "01:01:01.500"

    def test_rounds_to_millisecond(self):
        # 浮動小数点誤差でミリ秒がずれないこと
        assert seconds_to_timecode(1 / 3) == "00:00:00.333"

    def test_negative_raises(self):
        with pytest.raises(ValueError):
            seconds_to_timecode(-1)


class TestTimecodeToSeconds:
    def test_full_format(self):
        assert timecode_to_seconds("00:01:23.400") == pytest.approx(83.4)

    def test_hours(self):
        assert timecode_to_seconds("01:01:01.500") == pytest.approx(3661.5)

    def test_minutes_seconds_only(self):
        assert timecode_to_seconds("01:23.400") == pytest.approx(83.4)

    def test_seconds_only(self):
        assert timecode_to_seconds("23.4") == pytest.approx(23.4)

    def test_roundtrip(self):
        original = 3725.125
        assert timecode_to_seconds(seconds_to_timecode(original)) == pytest.approx(
            original, abs=0.001
        )

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            timecode_to_seconds("")

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError):
            timecode_to_seconds("not-a-timecode")

    def test_too_many_parts_raises(self):
        with pytest.raises(ValueError):
            timecode_to_seconds("00:00:00:00")


class TestSecondsToFrame:
    def test_basic(self):
        assert seconds_to_frame(1.0, 30.0) == 30

    def test_rounds_nearest(self):
        assert seconds_to_frame(1.0166, 30.0) == 30  # 30.5フレーム -> 丸め

    def test_zero(self):
        assert seconds_to_frame(0.0, 29.97) == 0

    def test_negative_fps_raises(self):
        with pytest.raises(ValueError):
            seconds_to_frame(1.0, 0)

    def test_negative_seconds_raises(self):
        with pytest.raises(ValueError):
            seconds_to_frame(-1.0, 30.0)


class TestFrameToSeconds:
    def test_basic(self):
        assert frame_to_seconds(30, 30.0) == pytest.approx(1.0)

    def test_ntsc_fps(self):
        assert frame_to_seconds(30, 29.97) == pytest.approx(1.001, abs=1e-3)

    def test_roundtrip(self):
        fps = 24.0
        frame = 120
        seconds = frame_to_seconds(frame, fps)
        assert seconds_to_frame(seconds, fps) == frame

    def test_negative_frame_raises(self):
        with pytest.raises(ValueError):
            frame_to_seconds(-1, 30.0)
