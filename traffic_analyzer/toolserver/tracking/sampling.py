"""采样网格、抽帧与运动度量(自 windows.py 拆出,纯函数无 VLM)。

[文件说明]
作用:track_suspects 编排的采样层——read_video_meta(元信息)、
sampling_grid(时间区间 → 帧号网格)、extract_window_jpegs(按帧号抽
JPEG 字节);运动度量 disp_per_second / should_upgrade_fps(高速目标
升 10fps 判定)/ compute_env_flow(参照车环境流速中位数)。
上游:toolserver/tracking/windows.py(编排调用)。
下游:cv2(抽帧);tracking/models.py(bbox_center/box_diagonal/
SLOW_SPEED_RATIO);tracking/errors.py(TrackingFailure)。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import cv2

from traffic_analyzer.toolserver.tracking import (
    SLOW_SPEED_RATIO,
    bbox_center,
    box_diagonal,
)
from traffic_analyzer.toolserver.tracking.errors import TrackingFailure

_JPEG_QUALITY = 80


def read_video_meta(video_path: Path) -> Dict[str, Any]:
    """读取 fps/帧数/分辨率;打不开或元数据非法时抛 TrackingFailure。"""
    cap = cv2.VideoCapture(str(video_path))
    try:
        if not cap.isOpened():
            raise TrackingFailure(f"video unreadable: {video_path}")
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        cap.release()
    if fps <= 0 or total <= 0:
        raise TrackingFailure(f"invalid video metadata: {video_path}")
    return {"fps": fps, "total": total, "width": w, "height": h}


def sampling_grid(meta: Dict[str, Any], t0: float, t1: float, fps: float) -> List[int]:
    """时间区间内的采样帧号网格(round 时间戳去重升序,尾帧钳制)。"""
    total = meta["total"]
    src_fps = meta["fps"]
    if t1 <= t0:
        return []
    frames: List[int] = []
    for k in range(int((t1 - t0) * fps) + 1):
        ts = t0 + k / fps
        idx = min(max(int(round(ts * src_fps)), 0), max(total - 1, 0))
        if not frames or idx > frames[-1]:
            frames.append(idx)
    return frames


def extract_window_jpegs(video_path: Path, frames: Sequence[int]) -> List[bytes]:
    """按帧号顺序抽取 JPEG 字节序列(读不到的帧跳过)。"""
    cap = cv2.VideoCapture(str(video_path))
    out: List[bytes] = []
    try:
        for fi in frames:
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            ok, buf = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY]
            )
            if ok:
                out.append(buf.tobytes())
    finally:
        cap.release()
    return out


# ---------------------------------------------------------------------------
# 运动估计与环境流速
# ---------------------------------------------------------------------------


def disp_per_second(
    p1: Dict[str, Any], p2: Dict[str, Any], src_fps: float
) -> float:
    """相邻两点中心的位移速率(归一化单位/秒),由实际帧差换算时间。"""
    c1, c2 = bbox_center(p1["box"]), bbox_center(p2["box"])
    disp = math.hypot(c2[0] - c1[0], c2[1] - c1[1])
    dt = abs(int(p2["frame"]) - int(p1["frame"])) / src_fps if src_fps > 0 else 0.0
    return disp / dt if dt > 0 else 0.0


def should_upgrade_fps(pts: Sequence[Dict[str, Any]], src_fps: float) -> bool:
    """相邻点位移速率超过缓行上限(SLOW_SPEED_RATIO × 对角线/秒)
    即视为高速运动目标 → 升 10fps(帧号为原始帧号,按实际帧差换算时间)。"""
    for p1, p2 in zip(pts, pts[1:]):
        diag = max(box_diagonal(p2["box"]), 1e-6)
        speed = disp_per_second(p1, p2, src_fps)
        if speed > SLOW_SPEED_RATIO * diag:
            return True
    return False


def compute_env_flow(
    ref_tracks: List[List[Dict[str, Any]]], src_fps: float, sample_fps: float
) -> Optional[float]:
    """参照车速度中位数(归一化单位/秒);无有效连续位移返回 None。

    只统计相邻采样步内的速度样本(断裂跨段的距离不代表真实速率);
    帧号为原始帧号,名义采样步长 = src_fps / sample_fps。
    """
    step = max(1, round(src_fps / sample_fps))
    speeds: List[float] = []
    for pts in ref_tracks:
        for p1, p2 in zip(pts, pts[1:]):
            diff = abs(int(p2["frame"]) - int(p1["frame"]))  # type: ignore[arg-type]
            if diff <= 0 or diff > 2 * step:
                continue
            speeds.append(disp_per_second(p1, p2, src_fps))
    if not speeds:
        return None
    speeds.sort()
    n = len(speeds)
    mid = speeds[n // 2] if n % 2 else (speeds[n // 2 - 1] + speeds[n // 2]) / 2
    return round(mid, 6)
