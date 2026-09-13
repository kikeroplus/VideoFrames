"""秒 / タイムコード文字列(HH:MM:SS.mmm) / フレーム番号 の相互変換。"""

from __future__ import annotations


def seconds_to_timecode(seconds: float) -> str:
    """秒を "HH:MM:SS.mmm" 形式のタイムコード文字列に変換する。"""
    if seconds < 0:
        raise ValueError(f"seconds must be >= 0: {seconds}")

    total_ms = round(seconds * 1000)
    hours, rem_ms = divmod(total_ms, 3_600_000)
    minutes, rem_ms = divmod(rem_ms, 60_000)
    secs, ms = divmod(rem_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def timecode_to_seconds(timecode: str) -> float:
    """"HH:MM:SS.mmm" (または "MM:SS.mmm" / "SS.mmm") 形式の文字列を秒に変換する。"""
    timecode = timecode.strip()
    if not timecode:
        raise ValueError("timecode must not be empty")

    parts = timecode.split(":")
    if len(parts) > 3:
        raise ValueError(f"invalid timecode format: {timecode!r}")

    try:
        parts_f = [float(p) for p in parts]
    except ValueError as exc:
        raise ValueError(f"invalid timecode format: {timecode!r}") from exc

    seconds = 0.0
    for part in parts_f:
        seconds = seconds * 60 + part

    if seconds < 0:
        raise ValueError(f"invalid timecode format: {timecode!r}")

    return seconds


def seconds_to_frame(seconds: float, fps: float) -> int:
    """秒をフレーム番号(0始まり)に変換する。"""
    if fps <= 0:
        raise ValueError(f"fps must be > 0: {fps}")
    if seconds < 0:
        raise ValueError(f"seconds must be >= 0: {seconds}")
    return round(seconds * fps)


def frame_to_seconds(frame: int, fps: float) -> float:
    """フレーム番号(0始まり)を秒に変換する。"""
    if fps <= 0:
        raise ValueError(f"fps must be > 0: {fps}")
    if frame < 0:
        raise ValueError(f"frame must be >= 0: {frame}")
    return frame / fps
