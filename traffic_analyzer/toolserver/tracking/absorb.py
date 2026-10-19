"""窗吸收状态与结果并档(自 windows.py 拆出)。

[文件说明]
作用:track_suspects 编排的可变状态与窗级吸收——_SuspectState(单目标
可变状态)、窗口机制常量(WINDOW_FRAMES/STRIDE/REANCHOR_EVERY/
REANCHOR_MISMATCH_IOU)、_expected_box(匀速外推)、_absorb_window_result
(传播窗追加点,re-anchor 窗校验偏移判跑飞)、_pick_candidate、
_dedupe_points、_write_run_snapshot(run.json 数值快照)。
上游:toolserver/tracking/windows.py(编排调用,常量与名字经其 re-export
保持既有引用面)。
下游:tracking/models.py(bbox_center)、tracking/stitch.py(iou/阈值)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from traffic_analyzer.toolserver.tracking import (
    SLOW_SPEED_RATIO,
    STATIC_DISPLACEMENT_RATIO,
    SuspectAnchor,
    Track,
    bbox_center,
)
from traffic_analyzer.toolserver.tracking import stitch

@dataclass
class _SuspectState:
    """单个可疑目标在编排过程中的可变状态。"""

    index: int
    anchor: SuspectAnchor
    letter: str
    active: bool = True
    points: List[Dict[str, Any]] = field(default_factory=list)  # {frame, box}
    events: List[Dict[str, Any]] = field(default_factory=list)
    # 失活记录:{"reason": reanchor_mismatch/reanchor_not_found, "window": 窗号}
    deactivated: Optional[Dict[str, Any]] = None
    # 锚点播种定位(时间戳缺省时保持 None → 旧式 t0 正向盲检)
    anchor_grid_index: Optional[int] = None  # 锚点时刻在采样网格中的序号
    anchor_frame: Optional[int] = None       # 对应原始帧号(锚框所在帧)
    anchor_group: Optional[int] = None       # 锚点所在窗组号(grid 序号 // STRIDE)
    anchor_validated: bool = False           # 锚点窗重检测校验已执行
    # 最终 side 来源(anchor/scene/infer/unknown)与依据文案
    final_side: str = "unknown"
    side_source: str = "unknown"
    side_rationale: Optional[str] = None

    @property
    def last_box(self) -> Optional[List[float]]:
        return self.points[-1]["box"] if self.points else None


# --- 窗口机制常量(编排与窗吸收共用) ---
WINDOW_FRAMES = 5
STRIDE = 4
REANCHOR_EVERY = 5          # 每 5 窗强制 re-anchor(第 0 窗本身即初始检测)
REANCHOR_MISMATCH_IOU = 0.3


def _dedupe_points(points: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一帧重复点保留先出现的(旧 track.py 口径),按帧排序返回。"""
    have: Dict[int, Dict[str, Any]] = {}
    for q in points:
        fi = int(q["frame"])
        if fi not in have:
            have[fi] = q
    return [have[k] for k in sorted(have)]


def _expected_box(s: _SuspectState, direction: str = "forward") -> Optional[List[float]]:
    """匀速外推下一采样步的 bbox;backward 沿时间负方向用最早两点外推。"""
    if not s.points:
        return None
    if direction == "backward":
        base = s.points[0]
        other = s.points[1] if len(s.points) >= 2 else None
        step = -1
    else:
        base = s.points[-1]
        other = s.points[-2] if len(s.points) >= 2 else None
        step = 1
    if other is None:
        return list(base["box"])
    d_f = int(other["frame"]) - int(base["frame"])
    if d_f == 0:
        return list(base["box"])
    c_b, c_o = bbox_center(base["box"]), bbox_center(other["box"])
    vx, vy = (c_o[0] - c_b[0]) / d_f, (c_o[1] - c_b[1]) / d_f
    cx_next, cy_next = c_b[0] + vx * step, c_b[1] + vy * step
    w2, h2 = base["box"][2] - base["box"][0], base["box"][3] - base["box"][1]
    return [cx_next - w2 / 2, cy_next - h2 / 2, cx_next + w2 / 2, cy_next + h2 / 2]


def _absorb_window_result(
    suspects: Sequence[_SuspectState],
    mode: str,
    win_frames: Sequence[int],
    first_frame: int,
    suspect_boxes: Dict[int, List[Dict[str, Any]]],
    expected: Dict[int, Optional[List[float]]],
    events_all: List[Dict[str, Any]],
    direction: str = "forward",
    anchor_validations: Optional[Dict[int, List[float]]] = None,
    window: Optional[int] = None,
) -> None:
    """把一窗解析结果并入状态:传播窗追加点,re-anchor 窗校验偏移判跑飞。

    帧号换算:窗内局部 frame 经 win_frames 查表 = 原始帧号(采样网格跳号,
    不能用 offset+local 线性换算)。re-anchor 与外推期望 IoU
    < REANCHOR_MISMATCH_IOU(或未检出)→ 目标标记跑飞、停止跟踪并记录
    deactivated {reason, window}。锚点窗校验(anchor_validations)不同:
    重检与锚框严重不符(或未检出)只记事件(kept_anchor),锚框保留、
    不失活——锚点是唯一直接可靠的定位;不符时本窗重检整体不吸收。
    direction="backward" 时按最早点向时间负方向外推与选候选。
    """
    validations = anchor_validations or {}
    for s in suspects:
        if not s.active:
            continue
        boxes_local = suspect_boxes.get(s.index, [])
        boxes_global = [
            {
                "frame": int(win_frames[int(q["frame"])])
                if 0 <= int(q["frame"]) < len(win_frames)
                else int(win_frames[-1]),
                "box": q["box"],
            }
            for q in boxes_local
        ]
        if s.index in validations:
            a_box = validations[s.index]
            if not boxes_global:
                s.events.append(
                    {"type": "reanchor_not_found", "frame": first_frame, "kept_anchor": True}
                )
                events_all.append(
                    {
                        "type": "reanchor_not_found",
                        "frame": first_frame,
                        "label": "keep-anchor",
                        "kept_anchor": True,
                    }
                )
                continue
            score = stitch.iou(
                _pick_candidate(boxes_global, s.points, direction), a_box
            )
            if score < REANCHOR_MISMATCH_IOU:
                s.events.append(
                    {
                        "type": "reanchor_mismatch",
                        "frame": first_frame,
                        "iou": round(score, 3),
                        "kept_anchor": True,
                    }
                )
                events_all.append(
                    {
                        "type": "reanchor_mismatch",
                        "frame": first_frame,
                        "label": "keep-anchor",
                        "iou": round(score, 3),
                        "kept_anchor": True,
                    }
                )
                continue
            # 校验通过:吸收本窗重检框(锚框所在帧已被种子占据,去重保留锚框)
            have = {int(q["frame"]) for q in s.points}
            s.points.extend(q for q in boxes_global if int(q["frame"]) not in have)
            continue
        if mode != "reanchor":
            have = {int(q["frame"]) for q in s.points}
            s.points.extend(q for q in boxes_global if int(q["frame"]) not in have)
            continue
        exp_box = expected.get(s.index)
        if not boxes_global:
            s.events.append({"type": "reanchor_not_found", "frame": first_frame})
            events_all.append(
                {"type": "reanchor_not_found", "frame": first_frame, "label": "lost?"}
            )
            s.active = False
            s.deactivated = {"reason": "reanchor_not_found", "window": window}
            continue
        candidate = _pick_candidate(boxes_global, s.points, direction)
        if exp_box is None:
            # 初始 re-anchor 窗:无外推基准,检出即接续
            have = {int(q["frame"]) for q in s.points}
            s.points.extend(q for q in boxes_global if int(q["frame"]) not in have)
            continue
        score = stitch.iou(candidate, exp_box)
        if score < REANCHOR_MISMATCH_IOU:
            s.events.append(
                {
                    "type": "reanchor_mismatch",
                    "frame": first_frame,
                    "iou": round(score, 3),
                }
            )
            events_all.append(
                {"type": "reanchor_mismatch", "frame": first_frame, "label": "re-anchor!"}
            )
            s.active = False
            s.deactivated = {
                "reason": "reanchor_mismatch",
                "window": window,
                "iou": round(score, 3),
            }
            continue
        have = {int(q["frame"]) for q in s.points}
        s.points.extend(q for q in boxes_global if int(q["frame"]) not in have)


def _pick_candidate(
    boxes_global: Sequence[Dict[str, Any]],
    points: Sequence[Dict[str, Any]],
    direction: str = "forward",
) -> List[float]:
    """选与外推目标帧最接近的候选框;无历史点取首框。

    forward 目标帧 = 最后已知点 +1;backward 目标帧 = 最早已知点 -1。
    """
    if not points:
        return boxes_global[0]["box"]
    if direction == "backward":
        target = int(points[0]["frame"]) - 1
    else:
        target = int(points[-1]["frame"]) + 1
    best_q = min(boxes_global, key=lambda q: abs(int(q["frame"]) - target))
    return best_q["box"]


def _write_run_snapshot(
    out_dir: Path,
    video_path: Path,
    anchors: Sequence[SuspectAnchor],
    time_range: Optional[Sequence[float]],
    fps_used: float,
    env_flow: Optional[float],
    tracks: Sequence[Track] = (),
    events: Optional[Sequence[Dict[str, Any]]] = None,
    stop_reason: str = "completed",
    suspects: Sequence[_SuspectState] = (),
    scene_side_result: Optional[Dict[str, Any]] = None,
    scene_frame_index: Optional[int] = None,
    scene_sample_frames: Optional[Sequence[int]] = None,
) -> None:
    """运行参数+数值档案快照(run.json):复现/回放一次跟踪所需全部数值。

    stop_reason: completed/max_calls/timeout/all_inactive;suspects 记录
    每个目标的失活状态(deactivated);events 为全量编排事件(含 re-anchor)。
    """
    # 每个目标最终采用的 side 及来源(anchor/scene/infer/unknown)
    per_target_side_summary = [
        {
            "index": s.index,
            "side": s.final_side,
            "source": s.side_source,
            "anchor_side": getattr(s.anchor, "side", None) or "unknown",
            "scene_side": (scene_side_result or {})
            .get("per_target", {})
            .get(s.index, {})
            .get("side", "unknown"),
        }
        for s in suspects
    ]

    snapshot = {
        "video": str(video_path),
        "time_range": list(time_range) if time_range else None,
        "sample_fps": fps_used,
        "window_frames": WINDOW_FRAMES,
        "stride": STRIDE,
        "reanchor_every": REANCHOR_EVERY,
        "reanchor_mismatch_iou": REANCHOR_MISMATCH_IOU,
        # thinking 口径:propagate 窗关 thinking(低延迟);reanchor 窗不传参
        # (使用引擎默认 reasoning_effort=medium);场景判定 also False。
        "enable_thinking": {"propagate": False, "reanchor": None, "scene_side": False},
        "env_flow": env_flow,
        "stop_reason": stop_reason,
        "scene_side": {
            "frame": scene_frame_index,
            "frames": list(scene_sample_frames) if scene_sample_frames else None,
            "median_side": (scene_side_result or {}).get("median_side", "unknown"),
            "per_target": per_target_side_summary,
            "votes": (scene_side_result or {}).get("votes"),
            "raw_response": scene_side_result,
        },
        "thresholds": {
            "static_displacement_ratio": STATIC_DISPLACEMENT_RATIO,
            "slow_speed_ratio": SLOW_SPEED_RATIO,
            "iou_th": stitch.IOU_TH,
            "max_gap_frames": stitch.MAX_GAP_FRAMES,
            "teleport_ratio": stitch.TELEPORT_RATIO,
        },
        "anchors": [
            {"box": a.box, "timestamp": a.timestamp, "description": a.description}
            for a in anchors
        ],
        "suspects": [
            {
                "index": s.index,
                "letter": s.letter,
                "description": s.anchor.description,
                "anchor_frame": s.anchor_frame,
                "active": s.active,
                "deactivated": s.deactivated,
            }
            for s in suspects
        ],
        "events": list(events or []),
        "tracks": [
            {
                "id": t.id,
                "description": t.description,
                "profile": t.profile,
                "side_hint": t.side_hint,
                "side_source": t.side_source,
                "side_rationale": t.side_rationale,
                "direction_verdict": t.direction_verdict,
                "heading": t.heading,
                "clipped_count": t.profile.get("clipped_count", 0),
                "clipped_frames": t.profile.get("clipped_frames", []),
            }
            for t in tracks
        ],
    }
    with open(out_dir / "run.json", "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1)
