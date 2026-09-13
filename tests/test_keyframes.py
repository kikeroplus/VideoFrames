import pytest

from app.core.keyframes import can_copy, next_keyframe, prev_keyframe

KEYFRAMES = [0.0, 2.0, 4.0, 6.0, 10.0]
FPS = 30.0  # 1フレーム = 1/30秒 ≈ 0.0333秒, 許容差(0.5フレーム) ≈ 0.01667秒


class TestCanCopy:
    def test_exact_match(self):
        assert can_copy(2.0, KEYFRAMES, FPS) is True

    def test_within_tolerance(self):
        tol = 0.5 / FPS
        assert can_copy(2.0 + tol * 0.5, KEYFRAMES, FPS) is True

    def test_outside_tolerance(self):
        assert can_copy(2.1, KEYFRAMES, FPS) is False

    def test_between_keyframes(self):
        assert can_copy(3.0, KEYFRAMES, FPS) is False

    def test_empty_keyframes(self):
        assert can_copy(2.0, [], FPS) is False

    def test_zero_fps(self):
        assert can_copy(2.0, KEYFRAMES, 0) is False

    def test_last_keyframe(self):
        assert can_copy(10.0, KEYFRAMES, FPS) is True


class TestPrevKeyframe:
    def test_between_two(self):
        assert prev_keyframe(3.0, KEYFRAMES) == 2.0

    def test_exact_match_returns_earlier_one(self):
        # ちょうどキーフレーム上にいる場合、「前」は一つ手前を指す
        assert prev_keyframe(4.0, KEYFRAMES) == 2.0

    def test_before_first(self):
        assert prev_keyframe(-1.0, KEYFRAMES) is None

    def test_at_first(self):
        assert prev_keyframe(0.0, KEYFRAMES) is None

    def test_after_last(self):
        assert prev_keyframe(20.0, KEYFRAMES) == 10.0

    def test_empty_keyframes(self):
        assert prev_keyframe(3.0, []) is None


class TestNextKeyframe:
    def test_between_two(self):
        assert next_keyframe(3.0, KEYFRAMES) == 4.0

    def test_exact_match_returns_later_one(self):
        assert next_keyframe(4.0, KEYFRAMES) == 6.0

    def test_after_last(self):
        assert next_keyframe(20.0, KEYFRAMES) is None

    def test_at_last(self):
        assert next_keyframe(10.0, KEYFRAMES) is None

    def test_before_first(self):
        assert next_keyframe(-1.0, KEYFRAMES) == 0.0

    def test_empty_keyframes(self):
        assert next_keyframe(3.0, []) is None
