"""场景级方位判定与车头朝向旁证(自 windows.py 拆出)。

[文件说明]
作用:track_suspects 的场景级 VLM 证据层——中央隔离带方位封闭提问
(build_scene_prompt / parse_scene_response)、多帧多数表决
(_vote_scene_side)、verdict 依据文案(_build_scene_rationale /
_build_fallback_rationale)、车头朝向旁证(_parse_heading /
_query_heading,仅补充证据不翻盘)。失败一律落 unknown,不抛异常
影响主流程。
上游:toolserver/tracking/windows.py(编排调用)。
下游:window_llm.parse_window_json(容错 JSON 提取);render.
best_frame_crops(朝向用 best-frame crop);models(llm.PromptTemplate)。
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from traffic_analyzer.models.llm import PromptTemplate
from traffic_analyzer.toolserver.tracking.models import Track
from traffic_analyzer.toolserver.tracking.render import best_frame_crops
from traffic_analyzer.toolserver.tracking.window_llm import parse_window_json

logger = logging.getLogger(__name__)

_SCENE_TEMPLATE_ID = "track_suspects_scene_side"
_HEADING_TEMPLATE_ID = "vehicle_heading"

# 车头朝向 closed question 的合法答案(中文→内部枚举)
_HEADING_ANSWERS = {
    "朝镜头": "toward",
    "背镜头": "away",
    "难以判断": "unknown",
}


# ---------------------------------------------------------------------------
# 中央隔离带方位判定(一次跟踪最多 3 帧 VLM 调用)
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
    suspects: Sequence[Any],
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
    engine: Any, frame_jpeg: bytes, suspects: Sequence[Any]
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
# 车头朝向旁证(仅补充证据,不翻盘)
# ---------------------------------------------------------------------------


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
