"""
SFT label rewrite step for the traffic analyzer framework.

Optional post-adjudication step (``sft_label`` mode). After adjudication, one
extra VLM call rewrites the adjudicated verdicts into ONE SFT training sample
(JSON) per video. The adjudicated verdicts act as privileged hints; the rewrite
call itself sees only the raw coarse keyframes and must ground its reasoning
solely in what the raw frames show. Samples whose positive events cannot be
grounded in the raw frames are written to a ``quarantine/`` subdirectory —
they would otherwise teach the student model to hallucinate.

present=true events additionally carry structured ``attributes`` (closed enums
from ``config/event_options.yaml``), a free-text ``detail`` and
``attr_mentions`` (exact substrings of detail bound to each attribute; for
multi-select groups the value is a nested ``{option_name: [substrings]}``
object instead of a flat array); the
sample gains top-level ``event_attributes`` / ``attr_mentions`` for the Web
editor's token mapping.

[文件说明]
作用:可选的裁决后 SFT 标签改写步骤(SftLabelRewriteStep,--sft-label 模式
启用)。以裁决结论为特权提示,让 VLM 仅基于原始粗关键帧重写出一条 SFT
训练样本(build_sample/build_description 组装 <think>/<answer> 格式),
写入 config 的 sft_label_output_dir;阳性事件无法在原始帧中锚定的样本
写入 quarantine/ 子目录。
上游:traffic_analyzer/orchestrator/analysis_orchestrator.py 的 [3.5/4]
SFT label rewrite 步骤;另被 core/grounding_verification.py 复用
_build_event_definitions_json。
下游:core/vlm_engine.py 的 VLMInferenceEngine.call;config/prompts/
sft_rewrite.yaml(template_id=sft_label_rewrite,经
ConfigManager.get_prompt_template 加载);utils/event_detection.py 的
select_event_images;core/pipeline_steps.py 的 PipelineStep 基类。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

import yaml

from traffic_analyzer.core.pipeline_steps import PipelineStep
from traffic_analyzer.core.vlm_engine import FatalAPIError
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCategory,
    EventResult,
    VideoMetadata,
)
from traffic_analyzer.utils.event_detection import select_event_images

logger = logging.getLogger(__name__)

# event_id 全局采用标注文档 v4.5 的 action 编号（9 = 正常占位，不对应任何事件），
# 因此 SFT 样本的 action / classN 直接等于 event_id，无需映射。

# JSON schema for the rewrite VLM response (forces valid JSON output).
# 注意:vlm_response_parser._validate_schema_basic 只校验顶层 required;
# event_thoughts 条目级约束(present=true 时 attributes 封闭枚举、
# attr_mentions 必须为 detail 的逐字子串)由本模块代码层校验并清洗。
_SFT_REWRITE_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": [
        "weather",
        "time_of_day",
        "scene",
        "event_thoughts",
        "ungrounded_event_ids",
    ],
    "properties": {
        "weather": {"type": "string"},
        "time_of_day": {"type": "string"},
        "scene": {"type": "string"},
        "event_thoughts": {
            "type": "array",
            "items": {
                "type": "object",
                # present=false 条目只带 thinking;present=true 条目带
                # attributes/detail/attr_mentions,条目级 required 仅约束
                # event_id/present,其余形状在代码层按 present 分支校验。
                "required": ["event_id", "present"],
                "properties": {
                    "event_id": {"type": "integer"},
                    "present": {"type": "boolean"},
                    "thinking": {"type": "string"},
                    "attributes": {
                        "type": "object",
                        "additionalProperties": {
                            "type": ["string", "array", "null"],
                            "items": {"type": "string"},
                        },
                    },
                    "detail": {"type": "string"},
                    "attr_mentions": {
                        "type": "object",
                        # 单选属性为字符串数组;多选属性(如施工要素 work_elements)
                        # 为「枚举选项名 → 字符串数组」的嵌套对象(兼容旧扁平数组,
                        # 严格校验在代码层 _validate_attr_mentions 完成)。
                        "additionalProperties": {
                            "anyOf": [
                                {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                {
                                    "type": "object",
                                    "additionalProperties": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                            ]
                        },
                    },
                },
            },
        },
        "ungrounded_event_ids": {
            "type": "array",
            "items": {"type": "integer"},
        },
    },
}


# ---------------------------------------------------------------------------
# 结构化属性:封闭枚举(event_options.yaml)、别名归一、骨架句、attr_mentions 校验
# ---------------------------------------------------------------------------

_EVENT_OPTIONS_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "event_options.yaml"
)


@lru_cache(maxsize=1)
def _event_options_index() -> Dict[int, List[Dict[str, Any]]]:
    """event_options.yaml 的封闭枚举定义:{event_id: [属性组, ...]}(保持声明顺序)。

    与 web/evidence_api._event_options_index 同口径,生成侧归一/校验复用同一
    事实源,保证产出的 event_attributes 一定能通过 PUT 的严格枚举校验。
    """
    data = yaml.safe_load(_EVENT_OPTIONS_PATH.read_text(encoding="utf-8")) or {}
    index: Dict[int, List[Dict[str, Any]]] = {}
    for ev in data.get("event_options") or []:
        groups = [
            {
                "key": str(g["key"]),
                "label": str(g.get("label") or g["key"]),
                "options": [str(o) for o in g.get("options") or []],
                "required": bool(g.get("required", False)),
                "multi": bool(g.get("multi", False)),
            }
            for g in ev.get("groups") or []
            if "key" in g
        ]
        if "event_id" in ev:
            index[int(ev["event_id"])] = groups
    return index


# 属性值别名表(与 web/static/js/sft.js 的 SFT_ATTR_ALIASES 保持一致):
# VLM 写出别名形态时归一到封闭枚举,枚举词本身总是合法书写形态。
_ATTR_ALIASES: Dict[str, List[str]] = {
    "行车道": ["主车道"],
    "来向": ["对向"],
    "小型车": ["小车", "轿车", "私家车"],
    "大客车": ["客车", "大巴"],
    "货车": ["卡车"],
    "工程车": ["工程作业车", "工程车辆", "施工车", "清障车", "救援车"],
    "施工人员": ["工人"],
    "滞留驾乘人员": ["驾乘人员", "滞留人员"],
    "摩托车": ["摩托"],
    "电动自行车": ["电动车", "电瓶车"],
    "单车道": ["一条车道"],
    "多车道": ["多条车道", "全部车道"],
    "施工车辆": ["工程车", "施工车"],
    "交通锥/隔离栏": ["交通锥", "锥桶", "路锥", "隔离栏", "锥形桶"],
    "施工标志牌": ["施工标志", "标志牌", "施工牌"],
    "车道封闭": ["封闭车道", "封路"],
    "塑料袋/纸张": ["塑料袋", "纸张", "塑料"],
    "水瓶/容器": ["水瓶", "瓶子"],
    "木板/构件": ["木板", "木条"],
    "泥土/散落物": ["泥土", "散落物", "碎石"],
    "三角警示牌": ["三角牌"],
}

# 骨架句模板(与 web/static/js/sft.js 的 SFT_SKELETON_TEMPLATES 保持一致):
# 字符串为固定文字;(slot, pre, post) 为该属性有值时输出的从句,空值整句省略。
_SKELETON_TEMPLATES: Dict[int, List[Any]] = {
    1: [("direction", "", "一侧"), ("lane_type", "", "内"), "停有一辆", ("vehicle_type", "", "")],
    2: [("direction", "", "一侧"), ("lane_type", "", "内"), ("vehicle_type", "有", "占用")],
    3: [("direction", "", "一侧"), ("lane_type", "", "内"), "发生交通事故", ("vehicle_type", ",涉及", "")],
    4: [("direction", "", "一侧"), "出现", ("person_type", "", "")],
    5: [("direction", "", "一侧"), "出现", ("non_motor_type", "", "")],
    6: [("direction", "", "一侧"), "出现", ("scope", "", ""), "拥堵"],
    7: [("direction", "", "一侧"), "道路施工", ("work_elements", ",现场有", "")],
    8: [("direction", "", "一侧"), ("lane_type", "", "内"), ("vehicle_type", "有", "逆行")],
}


def _match_option(group: Mapping[str, Any], text: str) -> Optional[str]:
    """精确匹配枚举词或别名,命中返回枚举值,否则返回 None。"""
    for option in group["options"]:
        if text == option or text in _ATTR_ALIASES.get(option, []):
            return option
    return None


def _normalize_attributes(event_id: int, raw_attrs: Any) -> Dict[str, Any]:
    """按 event_options 封闭枚举归一 attributes。

    未定义的键丢弃(告警);单选非法值置 null(告警),多选非法项丢弃(告警)、
    结果按 options 声明顺序排列;缺失/看不清的属性保持 null(多选为 [])。
    """
    groups = _event_options_index().get(event_id) or []
    raw = raw_attrs if isinstance(raw_attrs, dict) else {}
    known_keys = {g["key"] for g in groups}
    for key in raw:
        if key not in known_keys:
            logger.warning(
                "[sft_label_rewrite] ATTR_UNKNOWN_KEY | event_id=%s key=%r 已丢弃",
                event_id,
                key,
            )
    normalized: Dict[str, Any] = {}
    for group in groups:
        key = group["key"]
        value = raw.get(key)
        if group["multi"]:
            if isinstance(value, str):
                items: List[Any] = [value]
            elif isinstance(value, list):
                items = list(value)
            else:
                items = []
            picked: List[str] = []
            for item in items:
                if not isinstance(item, str) or not item.strip():
                    continue
                matched = _match_option(group, item.strip())
                if matched is None:
                    logger.warning(
                        "[sft_label_rewrite] ATTR_NORMALIZE | event_id=%s attr=%s "
                        "value=%r 不在封闭枚举内,已丢弃",
                        event_id,
                        key,
                        item,
                    )
                    continue
                if matched not in picked:
                    picked.append(matched)
            normalized[key] = [o for o in group["options"] if o in picked]
        else:
            if isinstance(value, str) and value.strip():
                matched = _match_option(group, value.strip())
                if matched is None:
                    logger.warning(
                        "[sft_label_rewrite] ATTR_NORMALIZE | event_id=%s attr=%s "
                        "value=%r 不在封闭枚举内,置 null",
                        event_id,
                        key,
                        value,
                    )
                normalized[key] = matched
            else:
                normalized[key] = None
    return normalized


def _filter_mention_strings(
    event_id: int, key: str, detail: str, values: Any
) -> List[str]:
    """逐字子串过滤:保留 detail 中逐字出现的非空字符串(去重),其余告警丢弃。"""
    if not isinstance(values, list):
        logger.warning(
            "[sft_label_rewrite] MENTION_BAD_SHAPE | event_id=%s key=%r 非数组,已丢弃",
            event_id,
            key,
        )
        return []
    kept: List[str] = []
    for value in values:
        if isinstance(value, str) and value and value in detail:
            if value not in kept:
                kept.append(value)
        else:
            logger.warning(
                "[sft_label_rewrite] MENTION_NOT_SUBSTRING | event_id=%s key=%r "
                "value=%r 不是 detail 的逐字子串,已丢弃",
                event_id,
                key,
                value,
            )
    return kept


def _validate_attr_mentions(
    event_id: int, detail: str, raw_mentions: Any
) -> Dict[str, Any]:
    """attr_mentions 校验:键必须为该事件的属性键,每个字符串必须是 detail 的
    逐字子串(exact substring);非法项丢弃并告警,Web 侧按精确字符串映射 token。

    单选属性的值为字符串数组;多选属性(如施工要素 work_elements)接受两种形态:
    - 新契约:{枚举选项名: [子串, ...]},选项名必须是该组 options 的原文
      (如「施工车辆」「交通锥/隔离栏」),无可见内容的选项省略;
    - 旧契约:扁平字符串数组(旧样本仍合法,原样保留)。
    """
    groups = _event_options_index().get(event_id) or []
    group_by_key = {g["key"]: g for g in groups}
    mentions: Dict[str, Any] = {}
    if not isinstance(raw_mentions, dict):
        return mentions
    for key, values in raw_mentions.items():
        group = group_by_key.get(key)
        if group is None:
            logger.warning(
                "[sft_label_rewrite] MENTION_UNKNOWN_KEY | event_id=%s key=%r 已丢弃",
                event_id,
                key,
            )
            continue
        if isinstance(values, dict):
            if not group["multi"]:
                logger.warning(
                    "[sft_label_rewrite] MENTION_BAD_SHAPE | event_id=%s key=%r "
                    "单选属性的值须为字符串数组,已丢弃",
                    event_id,
                    key,
                )
                continue
            kept_options: Dict[str, List[str]] = {}
            for option, opt_values in values.items():
                if option not in group["options"]:
                    logger.warning(
                        "[sft_label_rewrite] MENTION_UNKNOWN_OPTION | event_id=%s "
                        "key=%r option=%r 不在封闭枚举内,已丢弃",
                        event_id,
                        key,
                        option,
                    )
                    continue
                kept = _filter_mention_strings(
                    event_id, key, detail, opt_values
                )
                if kept:
                    kept_options[option] = kept
            if kept_options:
                mentions[key] = kept_options
            continue
        kept = _filter_mention_strings(event_id, key, detail, values)
        if kept:
            mentions[key] = kept
    return mentions


def _skeleton_sentence(event_id: int, attrs: Mapping[str, Any]) -> str:
    """按骨架模板把已选属性拼成一句,空值从句整体省略(与 JS skeleton 同语义)。"""
    parts = _SKELETON_TEMPLATES.get(event_id)
    if not parts:
        return ""
    out: List[str] = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
            continue
        slot, pre, post = part
        value = attrs.get(slot)
        if isinstance(value, list):
            if value:
                out.append(pre + "、".join(value) + post)
        elif value:
            out.append(pre + str(value) + post)
    return "".join(out)


def _positive_event_details(
    resp_data: Mapping[str, Any]
) -> Dict[int, Dict[str, Any]]:
    """present=true 条目的结构化数据:{event_id: {attributes, detail, attr_mentions}}。

    attributes 已经过封闭枚举归一,attr_mentions 已经过逐字子串校验;
    detail 缺失时回退 thinking(兼容旧形状响应)。
    """
    details: Dict[int, Dict[str, Any]] = {}
    raw_thoughts = resp_data.get("event_thoughts")
    if not isinstance(raw_thoughts, list):
        return details
    for item in raw_thoughts:
        # type(...) is int:JSON true 是 bool(True == 1),不得当作 event 1。
        if not isinstance(item, dict) or type(item.get("event_id")) is not int:
            continue
        if item.get("present") is not True:
            continue
        eid = item["event_id"]
        detail = str(item.get("detail") or item.get("thinking") or "").strip()
        details[eid] = {
            "attributes": _normalize_attributes(eid, item.get("attributes")),
            "detail": detail,
            "attr_mentions": _validate_attr_mentions(
                eid, detail, item.get("attr_mentions")
            ),
        }
    return details


def _detected_event_ids(event_results: Mapping[int, EventResult]) -> List[int]:
    """Return sorted event_ids whose adjudicated verdict is ``detected=True``."""
    return sorted(
        eid for eid, er in event_results.items() if getattr(er, "detected", False)
    )


def build_description(
    resp_data: Mapping[str, Any],
    event_results: Mapping[int, EventResult],
    categories: Sequence[EventCategory],
) -> str:
    """Assemble the ``<think>/<answer>`` description for one SFT sample.

    The format is fully code-assembled: ``<think>`` iterates the active
    categories in event_id order (``事件名：`` + the rewrite model's
    thinking), ``<answer>`` carries weather / time-of-day / scene first and
    ends with the conclusion (``classN: 事件名`` lines consistent with the
    ``action`` list).

    present=true 且裁决检出(detected)事件的 think 段由代码拼装:骨架句
    (结构化属性按 ``_SKELETON_TEMPLATES`` 拼成,空值从句省略) + detail;
    present=true 但未被裁决检出的事件按阴性处理(避免 think 段与最终结论
    自相矛盾);present=false 事件保持改写模型的 thinking 原文。
    """
    thoughts_by_id: Dict[int, Mapping[str, Any]] = {}
    raw_thoughts = resp_data.get("event_thoughts")
    if isinstance(raw_thoughts, list):
        for item in raw_thoughts:
            # type(...) is int:JSON true 是 bool(True == 1),不得当作 event 1。
            if isinstance(item, dict) and type(item.get("event_id")) is int:
                thoughts_by_id[item["event_id"]] = item

    details = _positive_event_details(resp_data)
    detected_ids = _detected_event_ids(event_results)
    detected_set = set(detected_ids)
    name_by_id = {c.event_id: c.name_zh for c in categories}

    think_lines: List[str] = []
    # 仅遍历激活类别(与 pipeline_steps 的 active_categories 口径一致):
    # 未激活事件不生成 think 段,SFT 样本与 md 报告保持相同事件集合。
    for cat in sorted(categories, key=lambda c: c.event_id):
        if not cat.is_active:
            continue
        thought = thoughts_by_id.get(cat.event_id, {})
        # 骨架+detail 仅用于「裁决检出」的 present 事件;present=true 但裁决
        # 未检出的事件按阴性处理,避免 think 段与最终结论自相矛盾。
        det = details.get(cat.event_id) if cat.event_id in detected_set else None
        if det is not None:
            skeleton = _skeleton_sentence(cat.event_id, det["attributes"])
            segments: List[str] = []
            if skeleton:
                segments.append(skeleton + "。")
            if det["detail"]:
                segments.append(det["detail"])
            thinking = "".join(segments) or str(
                thought.get("thinking") or ""
            ).strip()
        else:
            thinking = str(thought.get("thinking") or "").strip()
        if not thinking:
            thinking = (
                "（改写响应缺少该类思考）"
                if cat.event_id in detected_set
                else "未发现。"
            )
        think_lines.append(f"{cat.name_zh}：{thinking}")

    weather = str(resp_data.get("weather") or "").strip() or "未知"
    time_of_day = str(resp_data.get("time_of_day") or "").strip() or "未知"
    scene = str(resp_data.get("scene") or "").strip() or "未知"

    # Answer order: scene description elements first, conclusion last.
    answer_lines: List[str] = [
        f"天气：{weather}",
        f"时间：{time_of_day}",
        f"场景：{scene}",
    ]
    if detected_ids:
        answer_lines.append("最终结论：本视频块检出以下事件。")
        for eid in detected_ids:
            answer_lines.append(f"class{eid}: {name_by_id.get(eid, f'event_{eid}')}")
    else:
        answer_lines.append("最终结论：本视频块未检出任何事件，交通状况正常。")

    return (
        "<think>\n"
        + "\n\n".join(think_lines)
        + "\n</think>\n<answer>\n"
        + "\n".join(answer_lines)
        + "\n</answer>"
    )


def build_sample(
    resp_data: Mapping[str, Any],
    event_results: Mapping[int, EventResult],
    categories: Sequence[EventCategory],
    video_meta: Optional[VideoMetadata],
) -> Dict[str, Any]:
    """Build one SFT sample dict (keys exactly per the sft_label contract)."""
    # action 即排序后的 detected event_id（全局编号，无需映射）。
    action = _detected_event_ids(event_results)

    end_timestamp = 0.0
    chunk_name = ""
    if video_meta is not None:
        try:
            end_timestamp = float(video_meta.duration_sec)
        except (TypeError, ValueError):
            end_timestamp = 0.0
        chunk_name = video_meta.file_name or ""
        if not chunk_name and video_meta.file_path:
            chunk_name = Path(video_meta.file_path).name

    # 结构化属性契约(与 Web 编辑器对齐):
    # - event_attributes:{str(event_id): {attr_key: 枚举值/null(多选为列表)}},
    #   仅覆盖 detected 且 present=true 且在 event_options.yaml 中定义了属性组
    #   的事件(如实线变道(11) 无属性组,不出现);
    # - attr_mentions:{str(event_id): {attr_key: 标注}},同样仅覆盖上述事件且
    #   只保留非空标注;单选属性的标注为 [detail 的逐字子串, ...],多选属性
    #   (如施工要素 work_elements)为 {枚举选项名: [逐字子串, ...]} 的嵌套
    #   对象(旧样本的扁平数组形态原样保留)。
    details = _positive_event_details(resp_data)
    detected_set = set(action)
    options_index = _event_options_index()
    event_attributes: Dict[str, Any] = {}
    attr_mentions: Dict[str, Any] = {}
    for eid in sorted(details):
        if eid not in detected_set or not options_index.get(eid):
            continue
        event_attributes[str(eid)] = details[eid]["attributes"]
        if details[eid]["attr_mentions"]:
            attr_mentions[str(eid)] = details[eid]["attr_mentions"]

    return {
        "chunk": "chunk #1",
        "idx": 1,
        "action": action,
        "description": build_description(resp_data, event_results, categories),
        "start_timestamp": 0.0,
        "end_timestamp": end_timestamp,
        "chunk_name": chunk_name,
        "event_attributes": event_attributes,
        "attr_mentions": attr_mentions,
    }


def find_ungrounded_positive_event_ids(
    resp_data: Mapping[str, Any],
    event_results: Mapping[int, EventResult],
) -> List[int]:
    """Anchoring gate: ungrounded event_ids whose verdict is positive.

    Any overlap between the rewrite model's ``ungrounded_event_ids`` and the
    adjudicated ``detected=True`` events means the sample would teach
    hallucination and must be quarantined. Additionally, a ``present=false``
    thought for an adjudicated positive means the rewrite model could not
    ground the event but forgot to list it in ``ungrounded_event_ids`` — such
    samples are quarantined too.
    """
    detected_ids = {
        eid for eid, er in event_results.items() if getattr(er, "detected", False)
    }
    quarantine: set = set()
    ungrounded = resp_data.get("ungrounded_event_ids")
    if isinstance(ungrounded, list):
        for eid in ungrounded:
            # type(...) is int:JSON true 是 bool(True == 1),不得当作 event 1。
            if type(eid) is int and eid in detected_ids:
                quarantine.add(eid)
    # present=false ∧ detected=true:改写模型无法锚定该阳性事件但漏报了
    # ungrounded_event_ids,同样按幻觉样本隔离。
    raw_thoughts = resp_data.get("event_thoughts")
    if isinstance(raw_thoughts, list):
        for item in raw_thoughts:
            if not isinstance(item, dict) or type(item.get("event_id")) is not int:
                continue
            if item.get("present") is False and item["event_id"] in detected_ids:
                quarantine.add(item["event_id"])
    return sorted(quarantine)


def write_sample(
    sample: Mapping[str, Any],
    out_dir: Union[str, Path],
    video_stem: str,
) -> Path:
    """Write *sample* as ``<out_dir>/<video_stem>.json`` and return its path."""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    file_path = out_path / f"{video_stem}.json"
    file_path.write_text(
        json.dumps(sample, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return file_path


def _build_verdicts_json(
    event_results: Mapping[int, EventResult],
    categories: Sequence[EventCategory],
) -> str:
    """Serialize adjudicated verdicts (privileged hints) for the prompt."""
    verdicts: List[Dict[str, Any]] = []
    for cat in sorted(categories, key=lambda c: c.event_id):
        if not cat.is_active:
            continue  # 未激活类别不进入 prompt
        er = event_results.get(cat.event_id)
        verdicts.append(
            {
                "event_id": cat.event_id,
                "event_name": cat.name_zh,
                "detected": bool(er.detected) if er is not None else False,
                "summary": er.summary if er is not None else "",
                "instances": [
                    {
                        "start_time_sec": inst.start_time_sec,
                        "end_time_sec": inst.end_time_sec,
                        "description": inst.description,
                        "reasoning": inst.reasoning,
                    }
                    for inst in (er.instances if er is not None else [])
                ],
            }
        )
    return json.dumps(verdicts, ensure_ascii=False, indent=2)


def _build_event_definitions_json(categories: Sequence[EventCategory]) -> str:
    """Serialize event definitions for the prompt."""
    definitions = [
        {
            "event_id": cat.event_id,
            "event_name": cat.name_zh,
            "definition": cat.definition,
        }
        for cat in sorted(categories, key=lambda c: c.event_id)
        if cat.is_active
    ]
    return json.dumps(definitions, ensure_ascii=False, indent=2)


class SftLabelRewriteStep(PipelineStep):
    """Optional step 4: rewrite adjudicated verdicts into one SFT sample per video."""

    def __init__(self, config_manager, vlm_engine):
        super().__init__("sft_label_rewrite", max_retries=0)
        self.config_manager = config_manager
        self.vlm_engine = vlm_engine

    def _execute(self, context: AnalysisContext) -> Optional[Path]:
        # 1. Guards — fail-open: log and skip without writing anything.
        if not context.event_results:
            logger.info("[sft_label_rewrite] SKIP | no event_results to rewrite")
            return None
        if context.keyframes is None or not context.keyframes.coarse_frames:
            logger.warning("[sft_label_rewrite] SKIP | no keyframes available")
            return None
        output_dir = (
            getattr(context.config, "sft_label_output_dir", None)
            if context.config is not None
            else None
        )
        if not output_dir:
            logger.warning(
                "[sft_label_rewrite] SKIP | config missing sft_label_output_dir"
            )
            return None

        # 2. Student view: raw coarse frames only (no enhancement artifacts).
        vlm_max_frames = getattr(context.config, "vlm_max_frames", 6)
        images = select_event_images(context, vlm_max_frames)
        if not images:
            logger.warning("[sft_label_rewrite] SKIP | no raw frames selected")
            return None

        # 3. Prompt template + event definitions.
        try:
            template = self.config_manager.get_prompt_template("sft_label_rewrite")
        except Exception as exc:
            logger.warning(
                "[sft_label_rewrite] TEMPLATE_ERROR | template_id=sft_label_rewrite | %s",
                exc,
            )
            return None

        try:
            categories = self.config_manager.get_event_categories()
        except Exception as exc:
            logger.warning("[sft_label_rewrite] CATEGORY_ERROR | %s", exc)
            return None

        context_vars = {
            "verdicts_json": _build_verdicts_json(context.event_results, categories),
            "event_definitions_json": _build_event_definitions_json(categories),
        }

        # 4. Rewrite VLM call (fail-open except FatalAPIError, which must propagate).
        try:
            response = self.vlm_engine.call(
                template=template,
                images=images,
                context_vars=context_vars,
                response_schema=_SFT_REWRITE_RESPONSE_SCHEMA,
            )
        except FatalAPIError:
            raise
        except Exception as exc:
            logger.warning("[sft_label_rewrite] VLM_CALL_ERROR | %s", exc, exc_info=True)
            return None

        if not response.success or not isinstance(response.parsed_data, dict):
            logger.warning(
                "[sft_label_rewrite] PARSE_ERROR | success=%s raw_text=%s",
                getattr(response, "success", None),
                (getattr(response, "raw_text", "") or "")[:200],
            )
            return None

        resp_data = response.parsed_data

        # 5. Assemble the sample and write it (quarantine when ungroundable).
        sample = build_sample(
            resp_data, context.event_results, categories, context.video_meta
        )
        video_stem = (
            Path(sample["chunk_name"]).stem if sample["chunk_name"] else "unknown_video"
        )

        ungrounded_positive = find_ungrounded_positive_event_ids(
            resp_data, context.event_results
        )
        if ungrounded_positive:
            target_dir: Union[str, Path] = Path(output_dir) / "quarantine"
            logger.warning(
                "[sft_label_rewrite] QUARANTINE | video=%s ungrounded_event_ids=%s | "
                "positive event(s) not groundable in raw frames",
                sample["chunk_name"],
                ungrounded_positive,
            )
        else:
            target_dir = output_dir

        file_path = write_sample(sample, target_dir, video_stem)
        logger.info(
            "[sft_label_rewrite] SAMPLE_WRITTEN | path=%s actions=%s quarantine=%s",
            file_path,
            sample["action"],
            bool(ungrounded_positive),
        )
        return file_path
