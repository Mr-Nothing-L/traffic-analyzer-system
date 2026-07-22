"""
SFT label rewrite step for the traffic analyzer framework.

Optional post-adjudication step (``sft_label`` mode). After adjudication, one
extra VLM call rewrites the adjudicated verdicts into ONE SFT training sample
(JSON) per video. The adjudicated verdicts act as privileged hints; the rewrite
call itself sees only the raw coarse keyframes and must ground its reasoning
solely in what the raw frames show. Samples whose positive events cannot be
grounded in the raw frames are written to a ``quarantine/`` subdirectory —
they would otherwise teach the student model to hallucinate.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

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

# event_id → 标注文档 v4.5 的 action 编号（action 9 = 正常占位，跳过）。
EVENT_ID_TO_ACTION: Dict[int, int] = {
    0: 1,
    1: 2,
    2: 3,
    3: 4,
    4: 5,
    5: 6,
    6: 7,
    7: 8,
    8: 10,
    9: 11,
}

# JSON schema for the rewrite VLM response (forces valid JSON output).
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
                "required": ["event_id", "present", "thinking"],
                "properties": {
                    "event_id": {"type": "integer"},
                    "present": {"type": "boolean"},
                    "thinking": {"type": "string"},
                },
            },
        },
        "ungrounded_event_ids": {
            "type": "array",
            "items": {"type": "integer"},
        },
    },
}


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
    """
    thoughts_by_id: Dict[int, Mapping[str, Any]] = {}
    raw_thoughts = resp_data.get("event_thoughts")
    if isinstance(raw_thoughts, list):
        for item in raw_thoughts:
            if isinstance(item, dict) and isinstance(item.get("event_id"), int):
                thoughts_by_id[item["event_id"]] = item

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
            action_id = EVENT_ID_TO_ACTION.get(eid)
            if action_id is None:
                continue
            answer_lines.append(f"class{action_id}: {name_by_id.get(eid, f'event_{eid}')}")
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
    action = [
        EVENT_ID_TO_ACTION[eid]
        for eid in _detected_event_ids(event_results)
        if eid in EVENT_ID_TO_ACTION
    ]

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

    return {
        "chunk": "chunk #1",
        "idx": 1,
        "action": action,
        "description": build_description(resp_data, event_results, categories),
        "start_timestamp": 0.0,
        "end_timestamp": end_timestamp,
        "chunk_name": chunk_name,
    }


def find_ungrounded_positive_event_ids(
    resp_data: Mapping[str, Any],
    event_results: Mapping[int, EventResult],
) -> List[int]:
    """Anchoring gate: ungrounded event_ids whose verdict is positive.

    Any overlap between the rewrite model's ``ungrounded_event_ids`` and the
    adjudicated ``detected=True`` events means the sample would teach
    hallucination and must be quarantined.
    """
    ungrounded = resp_data.get("ungrounded_event_ids")
    if not isinstance(ungrounded, list):
        return []
    positive: List[int] = []
    for eid in ungrounded:
        if not isinstance(eid, int):
            continue
        er = event_results.get(eid)
        if er is not None and getattr(er, "detected", False):
            positive.append(eid)
    return sorted(positive)


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
