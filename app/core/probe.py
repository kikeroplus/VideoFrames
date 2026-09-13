"""ffprobe を使った動画メタ情報の取得。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.core.models import VideoItem


class ProbeError(Exception):
    """ffprobe の実行または結果の解析に失敗した場合に送出する。"""


def _parse_frame_rate(rate_str: str) -> float:
    """"30000/1001" のような分数文字列を float に変換する。"""
    if "/" in rate_str:
        num_str, den_str = rate_str.split("/", 1)
        num, den = float(num_str), float(den_str)
        if den == 0:
            return 0.0
        return num / den
    return float(rate_str)


def probe_video(path: Path, ffprobe_path: Path) -> VideoItem:
    """ffprobe を実行し、動画1本のメタ情報を VideoItem として返す。

    映像ストリームが見つからない場合や ffprobe が失敗した場合は ProbeError を送出する。
    """
    cmd = [
        str(ffprobe_path),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        # ffprobe の出力は常に UTF-8。Windows のロケール(cp932等)に引きずられないよう明示する。
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    except OSError as exc:
        raise ProbeError(f"ffprobe の実行に失敗しました: {exc}") from exc

    if result.returncode != 0:
        raise ProbeError(f"ffprobe がエラー終了しました ({path}): {result.stderr.strip()}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe の出力を解析できませんでした ({path}): {exc}") from exc

    fmt = data.get("format", {})
    streams = data.get("streams", [])

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_stream is None:
        raise ProbeError(f"映像ストリームが見つかりません: {path}")

    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    try:
        duration = float(fmt.get("duration", video_stream.get("duration", 0.0)))
    except (TypeError, ValueError):
        duration = 0.0

    try:
        size_bytes = int(fmt.get("size", 0))
    except (TypeError, ValueError):
        size_bytes = 0

    r_frame_rate = _parse_frame_rate(video_stream.get("r_frame_rate", "0/1"))
    avg_frame_rate = _parse_frame_rate(video_stream.get("avg_frame_rate", "0/1"))
    # r_frame_rate と avg_frame_rate が大きくずれている場合はVFR(可変フレームレート)とみなす
    is_vfr = avg_frame_rate > 0 and abs(r_frame_rate - avg_frame_rate) > 0.01

    return VideoItem(
        path=path,
        duration=duration,
        fps=r_frame_rate,
        width=int(video_stream.get("width", 0)),
        height=int(video_stream.get("height", 0)),
        vcodec=video_stream.get("codec_name", ""),
        acodec=audio_stream.get("codec_name") if audio_stream else None,
        size_bytes=size_bytes,
        is_vfr=is_vfr,
    )
