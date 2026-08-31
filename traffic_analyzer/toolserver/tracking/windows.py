"""VLM sliding-window orchestration for suspect-target tracking.

[文件说明]
作用:track_suspects 的确定性编排核心。在可疑时段内默认 5fps 抽帧
    (首个传播窗发现高速运动目标自适应升 10fps)、滑窗 5 帧/stride 4 调
    VLM(复用 core/vlm_engine 的 failover + .vlm_cache.db;传播窗关
    thinking 换低延迟,re-anchor 窗保留服务端默认);锚点 {box, timestamp}
    是唯一直接可靠的定位:锚框直接作为传播链初始框,以锚点所在采样窗为
    界先向后(未来)再向前(过去)双向传播,锚点窗做一次重检测校验
    (锚框 vs 重检 IoU,严重不符时记事件但保留锚框;时间戳缺省回退旧式
    t0 正向盲检);窗 prompt 双模式:传播式(目标描述+上一框位+顺带框
    2~3 辆参照车)与 re-anchor 式(每 REANCHOR_EVERY 窗按描述+外推预期
    位置重检测,检测结果与外推期望 IoU < REANCHOR_MISMATCH_IOU 判跑飞);
    参照车位移中位数 → 环境流速;再经 stitch 后处理与渲染产出数值档案
    (含 covered_s/coverage 覆盖率)和产物文件,run.json 记录 stop_reason、
    逐目标 deactivated 与全部 events。
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
from traffic_analyzer.toolserver.tracking.models import _direction_verdict_state
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
# re-anchor 窗的思考软预算(qwen3 thinking_budget):保留重检测所需推理,
# 但防止犹豫循环烧穿 max_tokens;传播窗关 thinking,不受影响。
REANCHOR_THINKING_BUDGET = 1024
REANCHOR_MISMATCH_IOU = 0.3
SPAN_MARGIN_S = 2.0         # 锚点前后扩展的上下文秒数
DEFAULT_SPAN_S = 8.0        # 无 time_range 时锚点之后继续跟踪的时长
MAX_WINDOW_CALLS = 40       # 单次请求 VLM 调用硬上限(控成本/兜底超时)
_JPEG_QUALITY = 80
_SCALE_HINT_MAX = 2.0       # 坐标 <= 该值视为 0-1 输出,否则按 0-1000 解析

_TEMPLATE_ID = "track_suspects_window"
_SCENE_TEMPLATE_ID = "track_suspects_scene_side"
_HEADING_TEMPLATE_ID = "vehicle_heading"

# 车头朝向 closed question 的合法答案(中文→内部枚举)
_HEADING_ANSWERS = {
    "朝镜头": "toward",
    "背镜头": "away",
    "难以判断": "unknown",
}


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
# 场景级中央隔离带方位判定(一次跟踪一次 VLM 调用)
# ---------------------------------------------------------------------------


def build_scene_prompt(suspects: Sequence[Any]) -> str:
    """构造中央隔离带方位封闭提问 prompt。

    用首窗首帧做单图提问,要求模型输出 JSON:
    {"median_side": "left|right|unknown",
     "per_target": [{"index": 0, "side": "coming|going|unknown", "rationale": "..."}]}
    """
    lines: List[str] = [
        "以上是同一路口监控视频的一帧画面。请回答一个关于画面中央隔离带的封闭问题:",
        "",
        "画面中的**中央隔离带**(分隔双向车道的中央护栏/绿化带,**不是**道路两侧的护栏)"
        "在画面哪一侧?如果中央隔离带不可见或无法判断,请直接回答 unknown。",
        "",
        "画面中标注的疑似目标:",
    ]
    for s in suspects:
        lines.append(f"目标{s.letter}: {s.anchor.description}。")
    lines.extend(
        [
            "",
            "对每个目标回答两个问题:①它相对于中央隔离带位于哪一侧;②**该侧车道(与目标"
            "同侧的全部车道)的法定/正常行驶方向**是朝镜头(coming)还是背镜头(going)。",
            "第②问只允许依据道路结构(中央隔离带位置、车道布局)与**其他车辆**的流向"
            "判断,**禁止**用目标车辆自身的朝向或运动方向作答——目标可能是违章车,"
            "它的朝向不能代表车道方向。",
            "",
            "如果中央隔离带不可见(画面只拍到单向车道):不要直接答 unknown——"
            "改为判断**画面内其他车辆的统一流向**:若其他车辆一致朝镜头驶来,整幅画面即"
            "来向场景(coming);一致背向镜头驶离即去向场景(going);流向不一致或无法"
            "判断时才回答 unknown,并在 rationale 注明依据(道路结构/其他车辆流向/不可判)。",
            "",
            "以 JSON 对象输出,不要任何解释:",
            '{"median_side": "left|right|unknown", "per_target": ['
            '{"index": 0, "side": "coming|going|unknown", "rationale": "...(须说明依据的是道路结构还是其他车辆流向)"}, '
            '{"index": 1, ...}]}。',
        ]
    )
    return "\n".join(lines)


def _as_unknown_scene(
    suspects: Sequence[Any], error: Optional[str] = None
) -> Dict[str, Any]:
    """构造统一 unknown 场景结果(解析失败/中央隔离带不可见/调用失败时使用)。"""
    return {
        "median_side": "unknown",
        "per_target": {
            s.index: {"side": "unknown", "rationale": None} for s in suspects
        },
        "parse_error": error,
    }


def parse_scene_response(
    resp: Any, suspect_indices: Sequence[int]
) -> Optional[Dict[str, Any]]:
    """解析场景级中央隔离带响应,失败返回 None(调用方落 unknown)。"""
    data: Optional[Dict[str, Any]] = getattr(resp, "parsed_data", None)
    if not isinstance(data, dict):
        try:
            data = parse_window_json(getattr(resp, "raw_text", "") or "")
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None

    raw_text = getattr(resp, "raw_text", "") or ""
    median_side = str(data.get("median_side", "unknown")).lower()
    if median_side not in ("left", "right"):
        median_side = "unknown"

    idx_set = set(suspect_indices)
    per_target: Dict[int, Dict[str, Any]] = {}
    raw_targets = data.get("per_target") or data.get("targets") or []
    if isinstance(raw_targets, list):
        for item in raw_targets:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            if idx not in idx_set:
                continue
            side = str(item.get("side", "unknown")).lower()
            if side not in ("coming", "going"):
                side = "unknown"
            per_target[idx] = {
                "side": side,
                "rationale": (item.get("rationale") or None),
            }

    # 中央隔离带不可见时,仅当模型也没给出任何流向判定才强制全部 unknown;
    # 单向机位下模型可仅凭其他车辆统一流向给出 coming/going(新兜底路径),
    # 此时保留其判定(rationale 可审计)。
    if median_side == "unknown" and all(
        per_target.get(idx, {}).get("side", "unknown") == "unknown" for idx in idx_set
    ):
        per_target = {idx: {"side": "unknown", "rationale": None} for idx in idx_set}

    return {
        "median_side": median_side,
        "per_target": per_target,
        "raw_text": raw_text,
    }


def _scene_sample_frames(
    grid: List[int], meta: Dict[str, Any], t0: float, t1: float
) -> List[int]:
    """在跟踪时段内均匀取 3 个采样帧(首/中/尾),复用现有采样网格。"""
    if not grid:
        return []
    src_fps = meta["fps"]
    targets = [t0, (t0 + t1) / 2.0, t1]
    seen: set[int] = set()
    chosen: List[int] = []
    for ts in targets:
        best = min(grid, key=lambda f: abs(f / src_fps - ts))
        if best not in seen:
            seen.add(best)
            chosen.append(best)
    return chosen


def _vote_scene_side(
    frame_results: List[Dict[str, Any]],
    suspects: Sequence[_SuspectState],
    events_all: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """对多帧场景判定结果做多数表决。

    - median_side: 仅 left/right 出现 ≥2 次时采用,否则 unknown。
    - 每个目标的 side: coming/going 仅出现 ≥2 次时采用,否则 unknown。
    - 出现 coming 与 going 同时存在且未形成多数(如 1+1+1)时记
      {type: "scene_side_split"} 事件。
    """
    total = len(frame_results)
    median_sides = [
        fr["result"].get("median_side", "unknown") for fr in frame_results
    ]
    median_counts: Dict[str, int] = {"left": 0, "right": 0, "unknown": 0}
    for s in median_sides:
        median_counts[s if s in median_counts else "unknown"] += 1

    ordered = [("left", median_counts["left"]), ("right", median_counts["right"])]
    median_side, n_median = max(ordered, key=lambda x: x[1])
    if n_median < 2:
        median_side = "unknown"

    per_target: Dict[int, Dict[str, Any]] = {}
    per_target_votes: Dict[int, Dict[str, int]] = {}
    for s in suspects:
        idx = s.index
        counts: Dict[str, int] = {"coming": 0, "going": 0, "unknown": 0}
        rationales: List[Optional[str]] = []
        sides: List[str] = []
        for fr in frame_results:
            target = fr["result"].get("per_target", {}).get(idx, {})
            side = target.get("side", "unknown")
            if side not in counts:
                side = "unknown"
            counts[side] += 1
            sides.append(side)
            rationales.append(target.get("rationale"))

        counts["total"] = total
        per_target_votes[idx] = counts
        ordered_s = [("coming", counts["coming"]), ("going", counts["going"])]
        side, n_side = max(ordered_s, key=lambda x: x[1])
        if n_side < 2:
            side = "unknown"

        non_unknown = {x for x in sides if x != "unknown"}
        if side == "unknown" and len(non_unknown) > 1:
            events_all.append(
                {
                    "type": "scene_side_split",
                    "frames": [fr["frame"] for fr in frame_results],
                    "index": idx,
                    "votes": counts.copy(),
                }
            )

        chosen_rationale: Optional[str] = None
        if side != "unknown":
            for fr, r in zip(frame_results, rationales):
                if (
                    r
                    and fr["result"].get("per_target", {}).get(idx, {}).get("side")
                    == side
                ):
                    chosen_rationale = r
                    break
        per_target[idx] = {"side": side, "rationale": chosen_rationale}

    frames_record: List[Dict[str, Any]] = []
    for fr in frame_results:
        res = fr["result"]
        frames_record.append(
            {
                "frame": fr["frame"],
                "median_side": res.get("median_side", "unknown"),
                "per_target": {
                    idx: res.get("per_target", {}).get(idx, {}).get("side", "unknown")
                    for idx in [s.index for s in suspects]
                },
                "raw_response": res.get("raw_text")
                or res.get("parse_error")
                or None,
            }
        )

    return {
        "median_side": median_side,
        "per_target": per_target,
        "votes": {
            "median": {**median_counts, "total": total},
            "per_target": per_target_votes,
        },
        "frames": frames_record,
    }


def _query_scene_side(
    engine: Any, frame_jpeg: bytes, suspects: Sequence[_SuspectState]
) -> Dict[str, Any]:
    """调用 VLM 对单帧做场景级中央隔离带方位判定。

    失败/解析失败均返回统一 unknown 结构,不抛异常影响主流程。
    """
    prompt = build_scene_prompt(suspects)
    template = PromptTemplate(
        template_id=_SCENE_TEMPLATE_ID,
        name="track_suspects scene side",
        user_prompt=prompt,
    )
    try:
        resp = engine.call(
            template=template, images=[frame_jpeg], enable_thinking=False
        )
        if not getattr(resp, "success", False):
            return _as_unknown_scene(
                suspects,
                error=getattr(resp, "error_message", None) or "vlm call failed",
            )
        result = parse_scene_response(resp, [s.index for s in suspects])
        if result is None:
            return _as_unknown_scene(suspects, error="scene side parse failed")
        return result
    except Exception as exc:
        logger.warning("[track_suspects] scene side vlm call failed: %s", exc)
        return _as_unknown_scene(suspects, error=str(exc))


def _scene_vote_text(counts: Dict[str, int], side: str) -> str:
    """生成表决口径文案,如「3/3 帧一致」或「2/3 帧」。"""
    total = counts.get("total", 0)
    n = counts.get(side, 0) if side != "unknown" else 0
    if total == 0:
        return "无有效帧"
    if n == total and n > 0:
        return f"{n}/{total}帧一致"
    return f"{n}/{total}帧"


def _build_scene_rationale(
    median_side: str, final_side: str, vlm_rationale: Optional[str]
) -> str:
    """由中央隔离带侧别与目标车道生成 verdict 的括号内依据文案。"""
    if median_side not in ("left", "right") or final_side not in (
        "coming",
        "going",
    ):
        return vlm_rationale or ""
    side_text = {"left": "左", "right": "右"}
    # 目标车道必在中央隔离带另一侧
    target_relative = "right" if median_side == "left" else "left"
    direction_text = "来向" if final_side == "coming" else "去向"
    camera_text = "应靠近镜头" if final_side == "coming" else "应远离镜头"
    return (
        f"中央隔离带在画面{side_text[median_side]}侧,目标位于其{side_text[target_relative]}侧,"
        f"该侧为{direction_text}车道,{camera_text}"
    )


def _build_fallback_rationale(final_side: str, source: str) -> str:
    """anchor / infer / unknown 等非 scene 来源的车道依据文案。"""
    direction_text = "来向" if final_side == "coming" else "去向"
    camera_text = "应靠近镜头" if final_side == "coming" else "应远离镜头"
    if source == "anchor":
        return f"由锚点 side 指定为{direction_text}车道,{camera_text}"
    if source == "infer":
        return f"由描述关键词推断为{direction_text}车道,{camera_text}"
    return camera_text


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
# 车头朝向旁证(仅补充证据,不翻盘)
# ---------------------------------------------------------------------------


def _needs_heading(state: Dict[str, Any]) -> bool:
    """一致性初判为「相反」或「不明」时才请求车头朝向旁证。"""
    if state.get("empty"):
        return False
    if state["consistent"] is False:
        return True
    if state["side"] == "unknown" and state["moving"]:
        return True
    # 运动但 bbox 趋势无法区分靠近/远离 → 方向不明
    if state["actual"] == "stable" and state["moving"]:
        return True
    return False


def _parse_heading(raw_text: str) -> str:
    """从模型输出提取车头朝向:朝镜头/背镜头/难以判断。"""
    text = (raw_text or "").strip()
    # 简单关键词匹配,兼容 JSON 包装
    for cn, value in _HEADING_ANSWERS.items():
        if cn in text:
            return value
    # 容错:只看首尾关键字
    if "朝" in text or "镜头" in text and "背" not in text:
        return "toward"
    if "背" in text:
        return "away"
    return "unknown"


def _query_heading(
    engine: Any, video_path: Path, track: Track, max_frames: int = 2
) -> Optional[Dict[str, Any]]:
    """对单条轨迹请求 1-2 张 best-frame 的车头朝向封闭问题。

    返回 {"raw_answers": [...], "accepted": "toward"/"away"/"unknown",
          "n_total": int, "n_consistent": int}。
    调用失败或答案不一致均记为 unknown,不抛异常影响主流程。
    """
    try:
        crops = best_frame_crops(video_path, track, max_frames=max_frames)
    except Exception as exc:
        logger.warning("[track_suspects] best-frame crop failed for heading: %s", exc)
        return None
    if not crops:
        return None

    raw_answers: List[str] = []
    for crop in crops:
        jpeg_bytes = base64.b64decode(crop["jpeg_base64"])
        template = PromptTemplate(
            template_id=_HEADING_TEMPLATE_ID,
            name="vehicle heading",
            user_prompt="图中目标车辆车头朝向?只答:朝镜头/背镜头/难以判断",
        )
        try:
            resp = engine.call(
                template=template, images=[jpeg_bytes], enable_thinking=False
            )
            ans = _parse_heading(getattr(resp, "raw_text", "") or "")
        except Exception as exc:
            logger.warning("[track_suspects] heading vlm call failed: %s", exc)
            ans = "unknown"
        raw_answers.append(ans)

    non_unknown = [a for a in raw_answers if a != "unknown"]
    if non_unknown and all(a == non_unknown[0] for a in non_unknown):
        accepted = non_unknown[0]
        n_consistent = len(non_unknown)
    else:
        accepted = "unknown"
        n_consistent = 0

    return {
        "raw_answers": raw_answers,
        "accepted": accepted,
        "n_total": len(raw_answers),
        "n_consistent": n_consistent,
    }


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
    win_calls = 0
    n_ok_calls = 0
    stop_reason = "completed"

    # --- 锚点播种:锚框直接入链,锚点时刻映射到采样网格 ---
    # (时间戳缺省 → 不播种,回退旧式 t0 正向盲检路径)
    for s in suspects:
        ts = getattr(s.anchor, "timestamp", None)
        if ts is None:
            continue
        s.anchor_grid_index = min(
            range(len(grid)), key=lambda i: abs(grid[i] / meta["fps"] - ts)
        )
        s.anchor_frame = grid[s.anchor_grid_index]
        s.anchor_group = s.anchor_grid_index // STRIDE
        s.points.append({"frame": s.anchor_frame, "box": list(s.anchor.box)})
    anchored_groups = [s.anchor_group for s in suspects if s.anchor_group is not None]
    first_group = min(anchored_groups) if anchored_groups else 0

    # --- 场景级中央隔离带方位判定(首/中/尾 3 帧多数表决) ---
    scene_sample_frames = _scene_sample_frames(grid, meta, t0, t1)
    scene_frame_index = scene_sample_frames[0] if scene_sample_frames else None
    scene_side_result: Optional[Dict[str, Any]] = None
    scene_frame_results: List[Dict[str, Any]] = []
    if scene_sample_frames:
        scene_jpegs = extract_window_jpegs(video_path, scene_sample_frames)
        for jpeg, frame in zip(scene_jpegs, scene_sample_frames):
            scene_frame_results.append(
                {"frame": frame, "result": _query_scene_side(engine, jpeg, suspects)}
            )
    if scene_frame_results:
        scene_side_result = _vote_scene_side(
            scene_frame_results, suspects, events_all
        )
    if scene_side_result is None:
        scene_side_result = _as_unknown_scene(
            suspects, error="sample frames unavailable"
        )

    def _reindex_anchors(new_grid: List[int]) -> int:
        """fps 升档后把锚点定位迁移到新网格,返回新的最早锚点窗组号。"""
        for s in suspects:
            if s.anchor_frame is None:
                continue
            s.anchor_grid_index = min(
                range(len(new_grid)), key=lambda i: abs(new_grid[i] - s.anchor_frame)  # type: ignore[arg-type]
            )
            s.anchor_frame = new_grid[s.anchor_grid_index]
            s.anchor_group = s.anchor_grid_index // STRIDE
        groups_now = [s.anchor_group for s in suspects if s.anchor_group is not None]
        return min(groups_now) if groups_now else 0

    def _participants(group: Optional[int], direction: str) -> List[_SuspectState]:
        """本窗参与目标:剔除失活;前向未到锚点窗的锚定目标不参与
        (防止目标进场前盲检锁错对象)。"""
        out: List[_SuspectState] = []
        for s in suspects:
            if not s.active:
                continue
            if (
                direction == "forward"
                and group is not None
                and s.anchor_group is not None
                and s.anchor_group > group
            ):
                continue
            out.append(s)
        return out

    def _deadline_hit() -> bool:
        return deadline is not None and deadline - _now() <= 0

    def _execute_window(
        win_frames: List[int], direction: str, group: Optional[int] = None
    ) -> Dict[str, Any]:
        """跑一个窗:prompt → VLM → 吸收。

        返回 {"status": "ok"/"skip"/"inactive", "response_ok": bool,
        "mode": 窗模式};skip = 有存活目标但都不属于本窗(静默跳过);
        inactive = 全部目标已失活。窗号/日志/事件/参照框/目标状态经闭包
        变量累积。
        """
        nonlocal win_calls, n_ok_calls
        wi = win_calls
        participants = _participants(group, direction)
        if not participants:
            if not any(s.active for s in suspects):
                return {"status": "inactive", "response_ok": False, "mode": "propagate"}
            # 有存活目标但都不属于本窗(如锚点窗在更后面):静默跳过
            return {"status": "skip", "response_ok": False, "mode": "propagate"}
        validations = {
            s.index: list(s.anchor.box)
            for s in participants
            if direction == "forward"
            and s.anchor_group == group
            and not s.anchor_validated
        }
        mode = (
            "reanchor"
            if (validations or wi % REANCHOR_EVERY == 0)
            else "propagate"
        )
        expected = {s.index: _expected_box(s, direction) for s in participants}
        prompt = build_window_prompt(mode, participants, len(win_frames), expected)
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
                    # re-anchor 窗保留思考但给 1024 软预算,防止犹豫循环
                    # 烧穿 max_tokens(qwen3 thinking_budget,实测有效)。
                    call_kwargs["enable_thinking"] = False
                else:
                    call_kwargs["thinking_budget"] = REANCHOR_THINKING_BUDGET
                resp = engine.call(**call_kwargs)
                if getattr(resp, "success", False):
                    response_ok = True
                    n_ok_calls += 1
                    suspect_parsed, refs_parsed = parse_window_response(
                        resp, participants, len(win_frames)
                    )
                else:
                    err_msg = (
                        getattr(resp, "error_message", None) or "vlm call failed"
                    )
            except Exception as exc:  # 网络/配额等:该窗按无响应处理
                logger.warning("[track_suspects] window %d vlm failed: %s", wi, exc)
                err_msg = f"vlm call error: {exc}"

        deactivated_now: List[Dict[str, Any]] = []
        if response_ok:
            before = {s.index: s.deactivated for s in participants}
            _absorb_window_result(
                suspects=participants,
                mode=mode,
                win_frames=win_frames,
                first_frame=int(win_frames[0]),
                suspect_boxes=suspect_parsed,
                expected=expected,
                events_all=events_all,
                direction=direction,
                anchor_validations=validations or None,
                window=wi,
            )
            deactivated_now = [
                {"index": s.index, "letter": s.letter, **(s.deactivated or {})}
                for s in participants
                if s.deactivated is not None and s.deactivated is not before[s.index]
            ]
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
        # 锚点窗执行过即消耗校验机会(该窗不重跑,失败窗不重试)
        for s in participants:
            if s.index in validations:
                s.anchor_validated = True

        record = {
            "window": wi,
            "mode": mode,
            "direction": direction,
            "frames": list(win_frames),
            "timestamps": [round(f / meta["fps"], 3) for f in win_frames],
            "request_prompt": prompt,
            "response": {
                "targets": {str(k): v for k, v in suspect_parsed.items()},
                "references": {str(k): v for k, v in refs_parsed.items()},
            },
            "error": err_msg,
            "ok": response_ok,
            "deactivated": deactivated_now,
        }
        windows_log.append(record)
        if out_dir is not None:
            with open(out_dir / "windows.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {"status": "ok", "response_ok": response_ok, "mode": mode}

    # --- 正向传播:锚点窗 → 时段末端 ---
    pos = first_group * STRIDE
    while pos < len(grid):
        if _deadline_hit():
            stop_reason = "timeout"
            break
        if win_calls >= MAX_WINDOW_CALLS:
            stop_reason = "max_calls"
            break
        if not any(s.active for s in suspects):
            stop_reason = "all_inactive"
            break
        win_frames = grid[pos : pos + WINDOW_FRAMES]
        if not win_frames:
            break
        res = _execute_window(win_frames, "forward", group=pos // STRIDE)
        if res["status"] == "skip":
            pos += STRIDE
            continue
        if res["status"] != "ok":
            stop_reason = "all_inactive" if res["status"] == "inactive" else "timeout"
            break

        # 自适应升帧率:首个传播窗发现高速目标即整段改用 10fps 接续
        if res["response_ok"] and not fps_upgraded and res["mode"] == "propagate":
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
                bi = min(bisect.bisect_left(new_grid, boundary), len(new_grid) - 1)
                grid = new_grid
                first_group = _reindex_anchors(grid)
                # 对齐到组边界继续:重叠帧吸收时去重,不产生覆盖空洞
                pos = max((bi // STRIDE) * STRIDE, 0)
                events_all.append(
                    {
                        "type": "fps_upgrade",
                        "frame": int(boundary),
                        "label": f"{fps_used:g}fps",
                    }
                )
                continue

        pos += STRIDE

    # --- 反向传播:锚点窗之前 → 时段起点(无锚点时间则无反向窗) ---
    end_idx = first_group * STRIDE
    while end_idx > 0:
        if _deadline_hit():
            stop_reason = "timeout"
            break
        if win_calls >= MAX_WINDOW_CALLS:
            stop_reason = "max_calls"
            break
        if not any(s.active for s in suspects):
            stop_reason = "all_inactive"
            break
        lo = max(end_idx - (WINDOW_FRAMES - 1), 0)
        res = _execute_window(grid[lo : end_idx + 1], "backward")
        if res["status"] == "skip":
            end_idx -= STRIDE
            continue
        if res["status"] != "ok":
            stop_reason = "all_inactive" if res["status"] == "inactive" else "timeout"
            break
        end_idx -= STRIDE

    # --- 超时:契约保持 failed:true,但落 run.json 便于观测 ---
    if stop_reason == "timeout":
        if out_dir is not None:
            _write_run_snapshot(
                out_dir, video_path, anchors, time_range, fps_used, None,
                tracks=[], events=events_all, stop_reason=stop_reason,
                suspects=suspects,
                scene_side_result=scene_side_result,
                scene_frame_index=scene_frame_index,
                scene_sample_frames=scene_sample_frames,
            )
        return {"failed": True, "failure_reason": "tracking timed out"}

    # --- 全部窗口失败 ---
    if n_ok_calls == 0:
        reason = windows_log[-1].get("error") if windows_log else "vlm unavailable"
        if out_dir is not None:
            _write_run_snapshot(
                out_dir, video_path, anchors, time_range, fps_used, None,
                tracks=[], events=events_all, stop_reason=stop_reason,
                suspects=suspects,
                scene_side_result=scene_side_result,
                scene_frame_index=scene_frame_index,
                scene_sample_frames=scene_sample_frames,
            )
        return {"failed": True, "failure_reason": f"all VLM window calls failed ({reason})"}

    # --- 参照车缝合与环境流速 ---
    ref_tracks = (
        stitch.stitch_overlapping([list(seg) for _, seg in ref_windows])
        if ref_windows
        else []
    )
    env_flow = compute_env_flow(ref_tracks, meta["fps"], fps_used)

    # --- 轨迹装配:去重 → 瞬移断裂 → 取主链 → 平滑 → 档案/互证 ---
    span_s = max(t1 - t0, 0.0)  # 目标时段总长(coverage 分母)
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
        track.profile = compute_profile(
            track, fps=fps_used, env_flow=env_flow, span_s=span_s
        )
        # side 来源优先级:场景判定 > 锚点 side > 描述关键词嗅探
        anchor_side = getattr(s.anchor, "side", None) or "unknown"
        scene_target = (scene_side_result or {}).get("per_target", {}).get(
            s.index, {}
        )
        scene_side = scene_target.get("side", "unknown")
        scene_median = (scene_side_result or {}).get("median_side", "unknown")

        if scene_side != "unknown":
            track.side_hint = scene_side
            track.side_source = "scene"
            vote_text = _scene_vote_text(
                (scene_side_result or {})
                .get("votes", {})
                .get("per_target", {})
                .get(s.index, {}),
                scene_side,
            )
            base_rationale = _build_scene_rationale(
                scene_median, scene_side, scene_target.get("rationale")
            )
            track.side_rationale = f"场景方位{vote_text}。" + (base_rationale or "")
            if anchor_side != "unknown" and anchor_side != scene_side:
                events_all.append(
                    {
                        "type": "side_conflict",
                        "frame": scene_frame_index,
                        "index": s.index,
                        "anchor_side": anchor_side,
                        "scene_side": scene_side,
                    }
                )
        elif anchor_side != "unknown":
            track.side_hint = anchor_side
            track.side_source = "anchor"
            track.side_rationale = _build_fallback_rationale(anchor_side, "anchor")
        else:
            inferred = infer_side_hint(track.description)
            track.side_hint = inferred
            track.side_source = "infer" if inferred != "unknown" else "unknown"
            track.side_rationale = (
                _build_fallback_rationale(inferred, "infer")
                if inferred != "unknown"
                else None
            )

        # 回写 suspect 状态,便于 run.json 记录每个目标最终 side 与来源
        s.final_side = track.side_hint
        s.side_source = track.side_source
        s.side_rationale = track.side_rationale

        # 方向初判:一致性相反/不明时请求车头朝向旁证(不翻盘)
        state = _direction_verdict_state(track)
        heading_result: Optional[Dict[str, Any]] = None
        if _needs_heading(state):
            heading_result = _query_heading(engine, video_path, track)
        track.heading = heading_result
        track.direction_verdict = direction_verdict(track, heading=heading_result)
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
        # (None = 服务端默认,vLLM qwen3 默认开启);场景判定 also False。
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
            }
            for t in tracks
        ],
    }
    with open(out_dir / "run.json", "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1)


def _now() -> float:
    """与 server 侧 time.monotonic() 同一单调时钟(deadline 用)。"""
    return time.monotonic()
