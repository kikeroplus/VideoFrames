"""トリム/抽出のカット方式判定と ffmpeg コマンド組み立て。仕様書 §8 参照。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.core.keyframes import can_copy, next_keyframe, prev_keyframe
from app.utils.timecode import seconds_to_timecode

CutMode = Literal["auto", "copy_priority", "always_encode"]
Strategy = Literal["copy", "encode"]
DeleteMode = Literal["head", "tail", "middle"]


@dataclass
class CutDecision:
    strategy: Strategy
    start: float  # 実際に使用する開始点(秒)。copy_priorityでスナップされた場合は元と異なる
    snapped: bool  # 開始点をキーフレームへスナップしたか
    exact_match: bool  # 元の開始点がそのままキーフレーム上だったか
    nearest_prev: float | None  # 元の開始点から見た直前のキーフレーム
    nearest_next: float | None  # 元の開始点から見た直後のキーフレーム


def decide_cut_strategy(
    start: float,
    keyframes: list[float] | None,
    fps: float,
    cut_mode: CutMode,
    snap_direction: Literal["prev", "next"] = "prev",
) -> CutDecision:
    """§8.3/§8.4 の判定ロジック。開始点(start)だけでコピー可否が決まる。"""
    kfs = keyframes or []
    exact = fps > 0 and can_copy(start, kfs, fps)
    nearest_prev = prev_keyframe(start, kfs)
    nearest_next = next_keyframe(start, kfs)

    if cut_mode == "always_encode":
        return CutDecision(
            strategy="encode", start=start, snapped=False, exact_match=exact,
            nearest_prev=nearest_prev, nearest_next=nearest_next,
        )

    if exact:
        return CutDecision(
            strategy="copy", start=start, snapped=False, exact_match=True,
            nearest_prev=nearest_prev, nearest_next=nearest_next,
        )

    if cut_mode == "copy_priority":
        primary = nearest_prev if snap_direction == "prev" else nearest_next
        fallback = nearest_next if snap_direction == "prev" else nearest_prev
        target = primary if primary is not None else fallback
        if target is None:
            # キーフレームが一つも無い(または判定不能) -> 安全側で再エンコード
            return CutDecision(
                strategy="encode", start=start, snapped=False, exact_match=False,
                nearest_prev=nearest_prev, nearest_next=nearest_next,
            )
        return CutDecision(
            strategy="copy", start=target, snapped=True, exact_match=False,
            nearest_prev=nearest_prev, nearest_next=nearest_next,
        )

    # auto: 一致しない場合は再エンコード。スナップはUI側のボタン操作に委ねる。
    return CutDecision(
        strategy="encode", start=start, snapped=False, exact_match=False,
        nearest_prev=nearest_prev, nearest_next=nearest_next,
    )


def build_cut_cmd(
    ffmpeg_path: Path,
    src: Path,
    dst: Path,
    start: float,
    end: float,
    strategy: Strategy,
    vcodec: str,
    has_audio: bool,
    exclude_audio: bool = False,
) -> list[str]:
    """単一区間切り出しの ffmpeg コマンドを組み立てる。仕様書 §8.5/§8.6 参照。"""
    cmd = [str(ffmpeg_path), "-ss", f"{start}", "-to", f"{end}", "-i", str(src)]

    include_audio = has_audio and not exclude_audio

    if strategy == "copy":
        cmd += ["-c", "copy"]
    else:
        if vcodec.lower() in ("hevc", "h265"):
            cmd += ["-c:v", "libx265", "-crf", "23"]
        else:
            cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium"]
        if include_audio:
            cmd += ["-c:a", "aac", "-b:a", "192k"]

    if not include_audio:
        cmd += ["-an"]

    cmd += ["-avoid_negative_ts", "make_zero", "-y", str(dst)]
    return cmd


def residual_ranges_head(n: float, duration: float) -> list[tuple[float, float]]:
    """冒頭からN秒削除した場合の残す区間。仕様書 §6 参照。"""
    return [(n, duration)]


def residual_ranges_tail(n: float, duration: float) -> list[tuple[float, float]]:
    """末尾からN秒削除した場合の残す区間。仕様書 §6 参照。"""
    return [(0.0, duration - n)]


def residual_ranges_middle(a: float, b: float, duration: float) -> list[tuple[float, float]]:
    """A〜Bを削除した場合の残す区間(2区間)。仕様書 §6 参照。"""
    return [(0.0, a), (b, duration)]


def format_cut_status(
    decision: CutDecision, cut_mode: CutMode, keyframe_state: str, can_snap: bool = True,
) -> tuple[str, bool, bool]:
    """§8.4 共通UIのステータス文言とスナップボタンの表示要否を組み立てる。

    抜き出し/削除タブで共通利用する。戻り値は (表示テキスト, 手前ボタン表示, 奥ボタン表示)。
    """
    unavailable_note = (
        "(キーフレーム判定不能のため安全側で再エンコードします) " if keyframe_state == "unavailable" else ""
    )

    if decision.strategy == "copy":
        if decision.snapped:
            text = (
                f"✅ {unavailable_note}コピー優先: 開始点を "
                f"{seconds_to_timecode(decision.start)} へスナップして処理します(高速)。"
            )
        else:
            text = f"✅ {unavailable_note}再エンコードなしで処理できます(高速)。"
        return text, False, False

    if cut_mode == "always_encode":
        return "常に再エンコードで処理します。", False, False

    lines = [f"⚠ {unavailable_note}開始点がキーフレーム上にありません。このまま実行すると再エンコードします。"]
    nearest_parts = []
    if decision.nearest_prev is not None:
        diff = decision.start - decision.nearest_prev
        nearest_parts.append(f"-{diff:.2f}秒 ({seconds_to_timecode(decision.nearest_prev)})")
    if decision.nearest_next is not None:
        diff = decision.nearest_next - decision.start
        nearest_parts.append(f"+{diff:.2f}秒 ({seconds_to_timecode(decision.nearest_next)})")
    if nearest_parts:
        lines.append("最寄り: " + " / ".join(nearest_parts))
    text = "\n".join(lines)

    show_snap = can_snap and cut_mode == "auto"
    show_prev = show_snap and decision.nearest_prev is not None
    show_next = show_snap and decision.nearest_next is not None
    return text, show_prev, show_next
