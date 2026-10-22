"""滑窗 prompt 构造与应答解析(自 windows.py 拆出)。

[文件说明]
作用:track_suspects 单窗 VLM 交互的纯文本层——build_window_prompt
(传播式/重检测式双模式)、parse_window_json(容错 JSON 提取,旧
llm_client.parse_json_object 的移植)、parse_window_response(响应 →
归一化 suspect/reference 框结构,自动识别 0-1/0-1000 量纲)。
上游:toolserver/tracking/windows.py(编排调用)。
下游:tracking/scene_vote.py(复用 parse_window_json)。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

_SCALE_HINT_MAX = 2.0       # 坐标 <= 该值视为 0-1 输出,否则按 0-1000 解析


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
