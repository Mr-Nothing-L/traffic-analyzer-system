"""跟踪结果收尾:渲染产物、run.json 快照与端点载荷(自 windows.py 拆出)。

[文件说明]
作用:run_tracking 的收尾段——无有效轨迹的失败返回、渲染产物(叠加
视频/CSV/速度染色图,渲染异常不影响契约主体)、run.json 快照与
payload_tracks(best_frames)的构造。参数即编排末态,纯收尾无决策。
上游:toolserver/tracking/windows.py(编排调用)。
下游:render.py(overlay_video/export_csv/speed_colored_image/
best_frame_crops)、absorb.py(_write_run_snapshot)。
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from traffic_analyzer.toolserver.tracking import SuspectAnchor, Track
from traffic_analyzer.toolserver.tracking.absorb import _SuspectState, _write_run_snapshot
from traffic_analyzer.toolserver.tracking.render import (
    best_frame_crops,
    export_csv,
    overlay_video,
    speed_colored_image,
)

logger = logging.getLogger(__name__)


def finalize_tracking_result(
    *,
    video_path: Path,
    anchors: Sequence[SuspectAnchor],
    time_range: Optional[Sequence[float]],
    out_dir: Optional[Path],
    fps_used: float,
    env_flow: Optional[float],
    stop_reason: str,
    events_all: List[Dict[str, Any]],
    dropped: List[str],
    suspects: Sequence[_SuspectState],
    scene_side_result: Optional[Dict[str, Any]],
    scene_frame_index: Optional[int],
    scene_sample_frames: Optional[Sequence[int]],
    tracks: List[Track],
    grid: List[int],
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    """构造端点契约载荷(无有效轨迹 → failed;否则渲染产物 + 快照 + 载荷)。"""
    if not tracks:
        reason = "; ".join(dropped) or "no valid trajectories"
        if out_dir is not None:
            _write_run_snapshot(
                out_dir, video_path, anchors, time_range, fps_used, env_flow,
                tracks=[], events=events_all, stop_reason=stop_reason,
                suspects=suspects,
                scene_side_result=scene_side_result,
                scene_frame_index=scene_frame_index,
                scene_sample_frames=scene_sample_frames,
            )
        return {"failed": True, "failure_reason": f"all suspects lost: {reason}"}

    # --- 渲染产物(渲染异常不影响契约主体) ---
    annotated_b64: Optional[str] = None
    clip_name: Optional[str] = None
    csv_name: Optional[str] = None
    event_marks = {
        int(ev["frame"]): str(ev["label"]) for ev in events_all if "frame" in ev and "label" in ev
    }
    if out_dir is not None:
        try:
            clip_path = overlay_video(
                video_path,
                tracks,
                out_dir / "track_overlay.mp4",
                start_frame=grid[0],
                end_frame=min(grid[-1], meta["total"] - 1),
                fps_hint=meta["fps"],
                event_marks=event_marks,
            )
            csv_path = export_csv(tracks, out_dir / "tracks.csv", video_path.stem)
            colored_img, colored_jpeg = speed_colored_image(
                video_path, tracks, out_path=out_dir / "colored_overlay.jpg"
            )
            del colored_img
            if colored_jpeg:
                annotated_b64 = base64.b64encode(colored_jpeg).decode("ascii")
            clip_name = clip_path.name
            csv_name = csv_path.name
        except Exception as exc:
            logger.error("[track_suspects] artifact rendering failed: %s", exc)
        _write_run_snapshot(
            out_dir, video_path, anchors, time_range, fps_used, env_flow,
            tracks=tracks, events=events_all, stop_reason=stop_reason,
            suspects=suspects,
            scene_side_result=scene_side_result,
            scene_frame_index=scene_frame_index,
            scene_sample_frames=scene_sample_frames,
        )

    payload_tracks: List[Dict[str, Any]] = []
    for i, track in enumerate(tracks, 1):
        entry: Dict[str, Any] = {
            "id": i,
            "description": track.description,
            "profile": track.profile,
            "side_hint": track.side_hint,
            "direction_verdict": track.direction_verdict,
            "heading": track.heading,
            "best_frames": [],
        }
        try:
            entry["best_frames"] = best_frame_crops(video_path, track)
        except Exception as exc:
            logger.warning("[track_suspects] best-frame extraction failed: %s", exc)
        payload_tracks.append(entry)

    return {
        "failed": False,
        "failure_reason": None,
        "tracks": payload_tracks,
        "annotated_image": annotated_b64,
        "clip": clip_name,
        "csv": csv_name,
        "artifacts_dir": out_dir,
        "env_flow": env_flow,
        "fps_used": fps_used,
        "events": events_all,
        "dropped": dropped,
    }
