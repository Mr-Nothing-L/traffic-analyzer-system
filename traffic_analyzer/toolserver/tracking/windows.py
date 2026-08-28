"""VLM sliding-window orchestration for suspect-target tracking.

[文件说明]
作用:track_suspects 的确定性编排核心。在可疑时段内默认 5fps 抽帧
    (首个传播窗发现高速运动目标自适应升 10fps)、滑窗 5 帧/stride 4 调
    VLM(复用 core/vlm_engine 的 failover + .vlm_cache.db;传播窗关
    thinking 换低延迟,re-anchor 窗保留服务端默认);窗 prompt 双
    模式:传播式(目标描述+上一框位+顺带框 2~3 辆参照车)与 re-anchor 式
    (每 REANCHOR_EVERY 窗按描述+外推预期位置重检测,检测结果与外推期望
    IoU < REANCHOR_MISMATCH_IOU 判跑飞);参照车位移中位数 → 环境流速;
    再经 stitch 后处理与渲染产出数值档案和产物文件。
上游:toolserver/server.py(端点调用);tests/test_track_windows.py(mock 引擎)。
下游:models/stitch/render 本包协作;core/vlm_engine(PromptTemplate 渲染入口);
    cv2(抽帧)。
"""

from __future__ import annotations

import base64
import bisect
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2

from traffic_analyzer.models.llm import PromptTemplate
from traffic_analyzer.toolserver.tracking import (
    STATIC_DISPLACEMENT_RATIO,
    SLOW_SPEED_RATIO,
    SuspectAnchor,
    Track,
    TrackPoint,
    box_diagonal,
    bbox_center,
    compute_profile,
    direction_verdict,
    infer_side_hint,
    is_consistent,
)
from traffic_analyzer.toolserver.tracking import stitch
from traffic_analyzer.toolserver.tracking.render import (
    best_frame_crops,
    export_csv,
    overlay_video,
    speed_colored_image,
)

logger = logging.getLogger(__name__)

# --- 编排参数(计划默认值) -----------------------------------------------

DEFAULT_SAMPLE_FPS = 5.0
FAST_SAMPLE_FPS = 10.0
WINDOW_FRAMES = 5
STRIDE = 4
REANCHOR_EVERY = 5          # 每 5 窗强制 re-anchor(第 0 窗本身即初始检测)
REANCHOR_MISMATCH_IOU = 0.3
SPAN_MARGIN_S = 2.0         # 锚点前后扩展的上下文秒数
DEFAULT_SPAN_S = 8.0        # 无 time_range 时锚点之后继续跟踪的时长
MAX_WINDOW_CALLS = 40       # 单次请求 VLM 调用硬上限(控成本/兜底超时)
_JPEG_QUALITY = 80
_SCALE_HINT_MAX = 2.0       # 坐标 <= 该值视为 0-1 输出,否则按 0-1000 解析

_TEMPLATE_ID = "track_suspects_window"


class TrackingFailure(Exception):
    """视频元信息不可读等致命失败:端点侧捕获并转成 failed:true 响应。"""


@dataclass
class _SuspectState:
    """单个可疑目标在编排过程中的可变状态。"""

    index: int
    anchor: SuspectAnchor
    letter: str
    active: bool = True
    points: List[Dict[str, Any]] = field(default_factory=list)  # {frame, box}
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def last_box(self) -> Optional[List[float]]:
        return self.points[-1]["box"] if self.points else None


# ---------------------------------------------------------------------------
# 抽帧与网格
# ---------------------------------------------------------------------------


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
# Prompt 构造与响应解析(JSON 容错参考旧 llm_client.py)
# ---------------------------------------------------------------------------


def _fmt_box(box: Sequence[float]) -> str:
    return "[" + ",".join(str(int(round(v * 1000))) for v in box[:4]) + "]"


def build_window_prompt(
    mode: str,
    suspects: Sequence[Any],
    n_frames: int,
    expected_boxes: Dict[int, Optional[List[float]]],
) -> str:
    """构造单窗 prompt。mode 为 'propagate'(传播式)或 'reanchor'(重检测式)。

    suspects 需含 index/letter/anchor 属性(_SuspectState 满足)。
    """
    lines: List[str] = [
        f"以上是同一路口监控视频连续{n_frames}帧"
        f"(帧0到帧{n_frames - 1},按时间顺序)。"
    ]
    if mode == "reanchor":
        lines.append(
            "请重新检测下列目标车辆:按文字描述与预期位置在整幅画面中重新定位,"
            "不要假设给定框位仍然准确:"
        )
    else:
        lines.append("请在各帧中持续跟踪下列目标车辆:")
    for s in suspects:
        line = f"目标{s.letter}:{s.anchor.description}。"
        expected = expected_boxes.get(s.index)
        if expected is not None:
            if mode == "reanchor":
                line += f"预期位置 bbox={_fmt_box(expected)}(匀速外推,可能有偏差)。"
            else:
                line += f"上一位置 bbox={_fmt_box(expected)}(0-1000 归一化),就近接续。"
        lines.append(line)
    lines.append(
        "同时框出画面中 2~3 辆正常行驶的其他车辆作为参照车(参照车 id 从 1 开始)。"
    )
    lines.append(
        '以 JSON 对象输出:'
        '{"targets":[{"key":"A","found":true,"boxes":[{"frame":0,'
        '"bbox":[x1,y1,x2,y2]}]}],"references":[{"id":1,"boxes":[{"frame":0,'
        '"bbox":[x1,y1,x2,y2]}]}]}。'
        "bbox 为 0-1000 归一化;目标某帧不可见则跳过该帧。只输出 JSON。"
    )
    return "\n".join(lines)


_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_window_json(text: str) -> Dict[str, Any]:
    """从模型输出提取 JSON 对象(旧 llm_client.parse_json_object 的移植)。"""
    m = _JSON_RE.search(text or "")
    if not m:
        raise ValueError(f"output has no JSON object: {(text or '')[:200]}")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("top-level JSON is not an object")
    return data


def _norm_box(raw: Any) -> Optional[List[float]]:
    """把模型输出的单个 bbox 归一化到 0-1 并做合法性检查(自动识别量纲)。

    容错:模型偶尔把 bbox 输出为字符串(如 "[100,700,400,950]"),
    这里统一先提取数字再归一化。
    """
    vals: Optional[List[float]] = None
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                vals = [float(v) for v in decoded]
        except (ValueError, TypeError):
            nums = re.findall(r"-?\d+(?:\.\d+)?", raw)
            vals = [float(v) for v in nums] if len(nums) == 4 else None
        if vals is None:
            return None
    else:
        try:
            vals = [float(v) for v in raw]
        except (TypeError, ValueError):
            return None
    if len(vals) != 4:
        return None
    scale = 1.0 if max(vals) <= _SCALE_HINT_MAX else 1000.0
    x1, y1, x2, y2 = [min(max(v / scale, 0.0), 1.0) for v in vals]
    if x2 - x1 <= 1e-6 or y2 - y1 <= 1e-6:
        return None
    return [x1, y1, x2, y2]


def _collect_boxes(item: Dict[str, Any], n_frames: int) -> List[Dict[str, Any]]:
    raw_boxes = item.get("boxes") or item.get("track") or []
    boxes: List[Dict[str, Any]] = []
    if not isinstance(raw_boxes, list):
        return boxes
    for rb in raw_boxes:
        if not isinstance(rb, dict):
            continue
        try:
            fi = int(rb.get("frame"))
        except (TypeError, ValueError):
            continue
        if not (0 <= fi < n_frames):
            continue
        nb = _norm_box(rb.get("bbox"))
        if nb is not None:
            boxes.append({"frame": fi, "box": nb})
    return boxes


def parse_window_response(
    resp: Any, suspects: Sequence[Any], n_frames: int
) -> Tuple[Dict[int, List[Dict[str, Any]]], Dict[int, List[Dict[str, Any]]]]:
    """解析一次 VLM 窗响应为归一化结构。

    Returns:
        (suspect_boxes, reference_boxes):suspect_boxes[index] =
        [{frame_in_window, box}],reference_boxes[ref_id] 同构。
        引擎已解析(parsed_data)优先;否则对 raw_text 做容错正则提取。
    """
    data: Optional[Dict[str, Any]]
    parsed = getattr(resp, "parsed_data", None)
    if isinstance(parsed, dict) and ("targets" in parsed or "references" in parsed):
        data = parsed
    else:
        try:
            data = parse_window_json(getattr(resp, "raw_text", "") or "")
        except ValueError:
            data = None
    if data is None:
        return {}, {}

    key_to_idx = {s.letter.lower(): s.index for s in suspects}
    idx_set = {s.index for s in suspects}
    suspect_boxes: Dict[int, List[Dict[str, Any]]] = {}
    raw_targets = data.get("targets")
    if isinstance(raw_targets, list):
        for item in raw_targets:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or item.get("name") or item.get("id") or "").lower()
            idx = key_to_idx.get(key)
            if idx is None and key.lstrip("-").isdigit():
                cand = int(key)
                if cand in idx_set:
                    idx = cand
            if idx is None and len(idx_set) == 1:
                idx = next(iter(idx_set))
            if idx is None:
                continue
            boxes = _collect_boxes(item, n_frames)
            if boxes:
                suspect_boxes.setdefault(idx, []).extend(boxes)
    references: Dict[int, List[Dict[str, Any]]] = {}
    raw_refs = data.get("references")
    if isinstance(raw_refs, list):
        for item in raw_refs:
            if not isinstance(item, dict):
                continue
            try:
                rid = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            boxes = _collect_boxes(item, n_frames)
            if boxes:
                references.setdefault(rid, []).extend(boxes)
    return suspect_boxes, references


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


# ---------------------------------------------------------------------------
# 主编排
# ---------------------------------------------------------------------------


def run_tracking(
    engine: Any,
    video_path: Path,
    anchors: Sequence[SuspectAnchor],
    time_range: Optional[Sequence[float]] = None,
    out_dir: Optional[Path] = None,
    deadline: Optional[float] = None,
) -> Dict[str, Any]:
    """执行定向跟踪,返回端点契约所需的载荷(server 补 artifacts 相对路径)。

    运行级问题不抛异常,返回 {"failed": True, "failure_reason": ...};
    只有视频不可读这类致命问题抛 TrackingFailure。
    """
    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
    meta = read_video_meta(video_path)
    duration = meta["total"] / meta["fps"]

    # --- 可疑时段 ---
    if time_range and len(time_range) == 2:
        t0 = max(0.0, float(min(time_range)))
        t1 = min(duration, float(max(time_range)))
    else:
        anchor_ts = [a.timestamp for a in anchors]
        t0 = max(0.0, min(anchor_ts) - SPAN_MARGIN_S)
        t1 = min(duration, max(anchor_ts) + SPAN_MARGIN_S + DEFAULT_SPAN_S)
    if t1 <= t0:
        return {
            "failed": True,
            "failure_reason": f"empty time range [{t0:.2f},{t1:.2f}]s",
        }

    grid = sampling_grid(meta, t0, t1, DEFAULT_SAMPLE_FPS)
    if len(grid) < 2:
        return {"failed": True, "failure_reason": "fewer than 2 sampled frames"}

    suspects = [
        _SuspectState(index=i, anchor=a, letter=chr(ord("A") + i))
        for i, a in enumerate(anchors)
    ]
    events_all: List[Dict[str, Any]] = []
    ref_windows: List[Tuple[int, List[Dict[str, Any]]]] = []  # (ref_id, 局部帧点)
    windows_log: List[Dict[str, Any]] = []

    fps_used = DEFAULT_SAMPLE_FPS
    fps_upgraded = False
    pos = 0
    win_calls = 0
    n_ok_calls = 0

    # --- 滑窗主循环 ---
    while pos < len(grid) and win_calls < MAX_WINDOW_CALLS:
        if deadline is not None and deadline - _now() <= 0:
            return {"failed": True, "failure_reason": "tracking timed out"}
        win_frames = grid[pos : pos + WINDOW_FRAMES]
        if not win_frames:
            break
        wi = win_calls
        mode = "reanchor" if wi % REANCHOR_EVERY == 0 else "propagate"
        active = [s for s in suspects if s.active]
        if not active:
            break

        expected = {s.index: _expected_box(s) for s in active}
        prompt = build_window_prompt(mode, active, len(win_frames), expected)
        images = extract_window_jpegs(video_path, win_frames)

        suspect_parsed: Dict[int, List[Dict[str, Any]]] = {}
        refs_parsed: Dict[int, List[Dict[str, Any]]] = {}
        err_msg: Optional[str] = None
        response_ok = False
        if len(images) < 2:
            err_msg = f"only {len(images)} frames readable in window {wi}"
        else:
            template = PromptTemplate(
                template_id=_TEMPLATE_ID,
                name="track_suspects window",
                user_prompt=prompt,
            )
            try:
                win_calls += 1
                call_kwargs: Dict[str, Any] = {"template": template, "images": images}
                if mode == "propagate":
                    # 传播窗是封闭感知任务(接框),关 thinking 换低延迟;
                    # re-anchor 窗不传,保留服务端默认(qwen3 默认开)。
                    call_kwargs["enable_thinking"] = False
                resp = engine.call(**call_kwargs)
                if getattr(resp, "success", False):
                    response_ok = True
                    n_ok_calls += 1
                    suspect_parsed, refs_parsed = parse_window_response(
                        resp, active, len(win_frames)
                    )
                else:
                    err_msg = (
                        getattr(resp, "error_message", None) or "vlm call failed"
                    )
            except Exception as exc:  # 网络/配额等:该窗按无响应处理
                logger.warning("[track_suspects] window %d vlm failed: %s", wi, exc)
                err_msg = f"vlm call error: {exc}"

        record = {
            "window": wi,
            "mode": mode,
            "frames": list(win_frames),
            "timestamps": [round(f / meta["fps"], 3) for f in win_frames],
            "request_prompt": prompt,
            "response": {
                "targets": {str(k): v for k, v in suspect_parsed.items()},
                "references": {str(k): v for k, v in refs_parsed.items()},
            },
            "error": err_msg,
            "ok": response_ok,
        }
        windows_log.append(record)
        if out_dir is not None:
            with open(out_dir / "windows.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        if response_ok:
            _absorb_window_result(
                suspects=suspects,
                mode=mode,
                win_frames=win_frames,
                first_frame=int(win_frames[0]),
                suspect_boxes=suspect_parsed,
                expected=expected,
                events_all=events_all,
            )
            for rid, boxes in refs_parsed.items():
                if boxes:
                    # 参照框同样按 win_frames 查表换算到原始帧号
                    # (否则各窗局部 0-4 帧互相污染)
                    ref_windows.append(
                        (
                            rid,
                            [
                                {
                                    "frame": int(win_frames[int(q["frame"])])
                                    if 0 <= int(q["frame"]) < len(win_frames)
                                    else int(win_frames[-1]),
                                    "box": q["box"],
                                }
                                for q in boxes
                            ],
                        )
                    )

            # 自适应升帧率:首个传播窗发现高速目标即整段改用 10fps 接续
            if not fps_upgraded and mode == "propagate":
                upgraded = False
                for s in suspects:
                    recent = s.points[-WINDOW_FRAMES:]
                    if len(recent) >= 2 and should_upgrade_fps(recent, meta["fps"]):
                        upgraded = True
                        break
                if upgraded:
                    fps_upgraded = True
                    fps_used = FAST_SAMPLE_FPS
                    new_grid = sampling_grid(meta, t0, t1, FAST_SAMPLE_FPS)
                    boundary = win_frames[-1]
                    # 从边界帧继续(保留 1 帧重叠供段间 IoU 缝合)
                    pos = max(bisect.bisect_left(new_grid, boundary) - 1, 0)
                    grid = new_grid
                    events_all.append(
                        {
                            "type": "fps_upgrade",
                            "frame": int(boundary),
                            "label": f"{fps_used:g}fps",
                        }
                    )
                    continue

        pos += STRIDE

    # --- 全部窗口失败 ---
    if n_ok_calls == 0:
        reason = windows_log[-1].get("error") if windows_log else "vlm unavailable"
        return {"failed": True, "failure_reason": f"all VLM window calls failed ({reason})"}

    # --- 参照车缝合与环境流速 ---
    ref_tracks = (
        stitch.stitch_overlapping([list(seg) for _, seg in ref_windows])
        if ref_windows
        else []
    )
    env_flow = compute_env_flow(ref_tracks, meta["fps"], fps_used)

    # --- 轨迹装配:去重 → 瞬移断裂 → 取主链 → 平滑 → 档案/互证 ---
    tracks: List[Track] = []
    dropped: List[str] = []
    for s in suspects:
        ordered = _dedupe_points(s.points)
        if not ordered:
            dropped.append(f"suspect {s.letter}({s.anchor.description}): never located")
            continue
        chains = stitch.teleport_break(ordered)
        if len(chains) > 1:
            split_at = int(chains[1][0]["frame"])
            s.events.append({"type": "teleport_split", "frame": split_at})
            events_all.append({"type": "teleport_split", "frame": split_at, "label": "split"})
        main = stitch.longest_chain(chains)
        smoothed = stitch.smooth_chain(main)
        track = Track(
            id=len(tracks) + 1,
            description=s.anchor.description,
            points=[
                TrackPoint(
                    frame_idx=int(q["frame"]),
                    timestamp=round(int(q["frame"]) / meta["fps"], 3),
                    box=q["box"],
                )
                for q in smoothed
            ],
        )
        track.profile = compute_profile(track, fps=fps_used, env_flow=env_flow)
        track.side_hint = infer_side_hint(track.description)
        track.direction_verdict = direction_verdict(track)
        ok, why = is_consistent(track)
        if not ok:
            dropped.append(f"suspect {s.letter}({s.anchor.description}): {why}")
            events_all.append(
                {"type": "mutual_check_fail", "frame": ordered[0]["frame"], "label": "drift"}
            )
            continue
        tracks.append(track)

    if not tracks:
        reason = "; ".join(dropped) or "no valid trajectories"
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
            out_dir, video_path, anchors, time_range, fps_used, env_flow, tracks
        )

    payload_tracks: List[Dict[str, Any]] = []
    for i, track in enumerate(tracks, 1):
        entry: Dict[str, Any] = {
            "id": i,
            "description": track.description,
            "profile": track.profile,
            "side_hint": track.side_hint,
            "direction_verdict": track.direction_verdict,
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


def _dedupe_points(points: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一帧重复点保留先出现的(旧 track.py 口径),按帧排序返回。"""
    have: Dict[int, Dict[str, Any]] = {}
    for q in points:
        fi = int(q["frame"])
        if fi not in have:
            have[fi] = q
    return [have[k] for k in sorted(have)]


def _expected_box(s: _SuspectState) -> Optional[List[float]]:
    """匀速外推下一时刻的 bbox(以最后两点速度平移最后一个框)。"""
    if not s.points:
        return None
    if len(s.points) < 2:
        return list(s.points[-1]["box"])
    p1, p2 = s.points[-2], s.points[-1]
    d_f = int(p2["frame"]) - int(p1["frame"])
    if d_f <= 0:
        return list(p2["box"])
    c1, c2 = bbox_center(p1["box"]), bbox_center(p2["box"])
    vx, vy = (c2[0] - c1[0]) / d_f, (c2[1] - c1[1]) / d_f
    cx_next, cy_next = c2[0] + vx, c2[1] + vy
    w2, h2 = p2["box"][2] - p2["box"][0], p2["box"][3] - p2["box"][1]
    return [cx_next - w2 / 2, cy_next - h2 / 2, cx_next + w2 / 2, cy_next + h2 / 2]


def _absorb_window_result(
    suspects: Sequence[_SuspectState],
    mode: str,
    win_frames: Sequence[int],
    first_frame: int,
    suspect_boxes: Dict[int, List[Dict[str, Any]]],
    expected: Dict[int, Optional[List[float]]],
    events_all: List[Dict[str, Any]],
) -> None:
    """把一窗解析结果并入状态:传播窗追加点,re-anchor 窗校验偏移判跑飞。

    帧号换算:窗内局部 frame 经 win_frames 查表 = 原始帧号(采样网格跳号,
    不能用 offset+local 线性换算)。re-anchor 与外推期望 IoU
    < REANCHOR_MISMATCH_IOU(或未检出)→ 目标标记跑飞、停止跟踪。
    """
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
            continue
        candidate = _pick_candidate(boxes_global, s.points)
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
            continue
        have = {int(q["frame"]) for q in s.points}
        s.points.extend(q for q in boxes_global if int(q["frame"]) not in have)


def _pick_candidate(
    boxes_global: Sequence[Dict[str, Any]],
    points: Sequence[Dict[str, Any]],
) -> List[float]:
    """选与外推目标帧(最后已知点 +1)最接近的候选框;无历史点取首框。"""
    if not points:
        return boxes_global[0]["box"]
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
) -> None:
    """运行参数+数值档案快照(run.json):复现/回放一次跟踪所需全部数值。"""
    snapshot = {
        "video": str(video_path),
        "time_range": list(time_range) if time_range else None,
        "sample_fps": fps_used,
        "window_frames": WINDOW_FRAMES,
        "stride": STRIDE,
        "reanchor_every": REANCHOR_EVERY,
        "reanchor_mismatch_iou": REANCHOR_MISMATCH_IOU,
        # thinking 口径:propagate 窗关 thinking(低延迟);reanchor 窗不传参
        # (None = 服务端默认,vLLM qwen3 默认开启)。
        "enable_thinking": {"propagate": False, "reanchor": None},
        "env_flow": env_flow,
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
        "tracks": [
            {
                "id": t.id,
                "description": t.description,
                "profile": t.profile,
                "side_hint": t.side_hint,
                "direction_verdict": t.direction_verdict,
            }
            for t in tracks
        ],
    }
    with open(out_dir / "run.json", "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1)


def _now() -> float:
    """与 server 侧 time.monotonic() 同一单调时钟(deadline 用)。"""
    return time.monotonic()
