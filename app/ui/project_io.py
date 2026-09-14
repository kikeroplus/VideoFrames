"""プロジェクトの保存/読み込み。

プロジェクトは「動画フォルダ + 動画ごとの抜き出しポイント一覧(+選択中の動画)」を
1つのJSONファイルにまとめたもの。サムネイル画像は保存しない(容量が大きくなる
ため。読込後にその動画を選び直せば、必要な情報(IN/OUT等)はそのまま復元される)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.core.edit_ops import CutDecision
from app.ui.extract_panel import ExtractRequest
from app.ui.extract_points_panel import ExtractPointEntry

PROJECT_FILE_FILTER = "VideoTrimmer プロジェクト (*.vtproj);;すべてのファイル (*)"
DEFAULT_PROJECT_SUFFIX = ".vtproj"

_FORMAT_VERSION = 1


class ProjectError(Exception):
    """プロジェクトファイルの保存・読み込みに失敗した場合に送出する。"""


@dataclass
class LoadedProject:
    folder: Path
    current_video: Path | None
    points_by_video: dict[Path, list[ExtractPointEntry]]


def _decision_to_dict(decision: CutDecision) -> dict:
    return {
        "strategy": decision.strategy,
        "start": decision.start,
        "snapped": decision.snapped,
        "exact_match": decision.exact_match,
        "nearest_prev": decision.nearest_prev,
        "nearest_next": decision.nearest_next,
    }


def _decision_from_dict(data: dict) -> CutDecision:
    return CutDecision(
        strategy=data["strategy"],
        start=float(data["start"]),
        snapped=bool(data["snapped"]),
        exact_match=bool(data["exact_match"]),
        nearest_prev=data.get("nearest_prev"),
        nearest_next=data.get("nearest_next"),
    )


def save_project(
    path: Path,
    folder: Path,
    current_video: Path | None,
    points_by_video: dict[Path, list[ExtractPointEntry]],
) -> None:
    """プロジェクトファイル(JSON)として保存する。"""
    data = {
        "format_version": _FORMAT_VERSION,
        "folder": str(folder),
        "current_video": str(current_video) if current_video is not None else None,
        "extract_points": {
            str(video_path): [
                {
                    "in_s": entry.request.in_s,
                    "out_s": entry.request.out_s,
                    "exclude_audio": entry.request.exclude_audio,
                    "decision": _decision_to_dict(entry.request.decision),
                }
                for entry in entries
            ]
            for video_path, entries in points_by_video.items()
            if entries  # 空リストは保存しない
        },
    }
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise ProjectError(f"プロジェクトの保存に失敗しました: {exc}") from exc


def load_project(path: Path) -> LoadedProject:
    """プロジェクトファイル(JSON)を読み込む。形式が不正な場合は ProjectError を送出する。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectError(f"プロジェクトファイルを読み込めません: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProjectError(f"プロジェクトファイルの形式が不正です: {exc}") from exc

    try:
        folder = Path(data["folder"])
        current_video_str = data.get("current_video")
        current_video = Path(current_video_str) if current_video_str else None

        points_by_video: dict[Path, list[ExtractPointEntry]] = {}
        for video_str, point_list in data.get("extract_points", {}).items():
            entries = []
            for p in point_list:
                decision = _decision_from_dict(p["decision"])
                request = ExtractRequest(
                    in_s=float(p["in_s"]),
                    out_s=float(p["out_s"]),
                    exclude_audio=bool(p["exclude_audio"]),
                    decision=decision,
                )
                entries.append(ExtractPointEntry(request=request, in_pixmap=None, out_pixmap=None))
            points_by_video[Path(video_str)] = entries
    except (KeyError, TypeError, ValueError) as exc:
        raise ProjectError(f"プロジェクトファイルの内容が不正です: {exc}") from exc

    return LoadedProject(folder=folder, current_video=current_video, points_by_video=points_by_video)
