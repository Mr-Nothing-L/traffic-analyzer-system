"""
Grounding verification step for the traffic analyzer framework.

Optional post-adjudication step (``grounding_check_enable``). After
adjudication, one extra VLM call re-examines each positive (``detected=True``)
verdict against the raw coarse keyframes and tries to anchor its key visual
elements. Positives that cannot be anchored are treated as hallucinations and
overturned (``detected=False``, ``grounding_overturned=True``) before the
report and the SFT label rewrite see them.

[文件说明]
作用:可选的裁决后锚定核验步骤(GroundingVerificationStep,由
grounding_check_enable 控制)。对裁决阳性(detected=True)事件再发一次 VLM
调用,仅用原始粗关键帧验证其关键视觉元素是否可锚定;无法锚定的阳性事件
被判定为幻觉并就地推翻(改写 context.event_results 中对应 EventResult)。
上游:traffic_analyzer/orchestrator/analysis_orchestrator.py 的 [3.4/4]
Grounding verification 步骤(在 AdjudicationStep 之后执行)。
下游:core/vlm_engine.py 的 VLMInferenceEngine.call;config/prompts/
grounding_verification.yaml(经 ConfigManager.get_prompt_template 加载);
utils/event_detection.py 的 select_event_images;core/pipeline_steps.py 的
PipelineStep 基类;core/sft_label_rewrite.py 的 _build_event_definitions_json。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence

from traffic_analyzer.core.pipeline_steps import PipelineStep
from traffic_analyzer.core.sft_label_rewrite import _build_event_definitions_json
from traffic_analyzer.core.vlm_engine import FatalAPIError
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCategory,
    EventResult,
)
from traffic_analyzer.utils.event_detection import select_event_images

logger = logging.getLogger(__name__)

# JSON schema for the verification VLM response (forces valid JSON output).
_GROUNDING_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["grounding_results"],
    "properties": {
        "grounding_results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["event_id", "grounded", "analysis"],
                "properties": {
                    "event_id": {"type": "integer"},
                    "grounded": {"type": "boolean"},
                    "analysis": {"type": "string"},
                },
            },
        }
    },
}


def _build_verdicts_json(
    event_results: Mapping[int, EventResult],
    categories: Sequence[EventCategory],
) -> str:
    """Serialize the positive (detected=True) verdicts for the prompt.

    与 sft_label_rewrite._build_verdicts_json 口径一致（event_id / event_name /
    summary / instances），但只包含核验对象——裁决阳性事件。
    """
    active_ids = {c.event_id for c in categories if c.is_active}
    verdicts: List[Dict[str, Any]] = []
    for eid in sorted(event_results):
        er = event_results[eid]
        if not getattr(er, "detected", False) or eid not in active_ids:
            continue  # 未激活类别与非阳性事件不进入核验 prompt
        verdicts.append(
            {
                "event_id": eid,
                "event_name": er.event_name,
                "summary": er.summary,
                "instances": [
                    {
                        "start_time_sec": inst.start_time_sec,
                        "end_time_sec": inst.end_time_sec,
                        "description": inst.description,
                        "reasoning": inst.reasoning,
                    }
                    for inst in er.instances
                ],
            }
        )
    return json.dumps(verdicts, ensure_ascii=False, indent=2)


class GroundingVerificationStep(PipelineStep):
    """Optional step 3.4: overturn hallucinated positives via raw-frame anchoring."""

    def __init__(self, config_manager, vlm_engine):
        super().__init__("grounding_verification", max_retries=0)
        self.config_manager = config_manager
        self.vlm_engine = vlm_engine

    def _execute(self, context: AnalysisContext) -> Optional[List[Dict[str, Any]]]:
        # 1. Guards — fail-open: log and skip without touching event_results.
        enabled = (
            getattr(context.config, "grounding_check_enable", True)
            if context.config is not None
            else True
        )
        if not enabled:
            logger.info("[grounding_verification] SKIP | grounding_check_enable=false")
            return None
        positive_results = {
            eid: er
            for eid, er in (context.event_results or {}).items()
            if getattr(er, "detected", False)
        }
        if not positive_results:
            logger.info("[grounding_verification] SKIP | no detected events to verify")
            return None
        if context.keyframes is None or not context.keyframes.coarse_frames:
            logger.warning("[grounding_verification] SKIP | no keyframes available")
            return None

        # 2. Student view: raw coarse frames only (no enhancement artifacts).
        vlm_max_frames = getattr(context.config, "vlm_max_frames", 6)
        images = select_event_images(context, vlm_max_frames)
        if not images:
            logger.warning("[grounding_verification] SKIP | no raw frames selected")
            return None

        # 3. Prompt template + event definitions.
        try:
            template = self.config_manager.get_prompt_template("grounding_verification")
        except Exception as exc:
            logger.warning(
                "[grounding_verification] TEMPLATE_ERROR | template_id=grounding_verification | %s",
                exc,
            )
            return None

        try:
            categories = self.config_manager.get_event_categories()
        except Exception as exc:
            logger.warning("[grounding_verification] CATEGORY_ERROR | %s", exc)
            return None

        context_vars = {
            "verdicts_json": _build_verdicts_json(context.event_results, categories),
            "event_definitions_json": _build_event_definitions_json(categories),
        }

        # 4. Verification VLM call (fail-open except FatalAPIError, which must propagate).
        try:
            response = self.vlm_engine.call(
                template=template,
                images=images,
                context_vars=context_vars,
                response_schema=_GROUNDING_RESPONSE_SCHEMA,
            )
        except FatalAPIError:
            raise
        except Exception as exc:
            logger.warning(
                "[grounding_verification] VLM_CALL_ERROR | %s", exc, exc_info=True
            )
            return None

        if not response.success or not isinstance(response.parsed_data, dict):
            logger.warning(
                "[grounding_verification] PARSE_ERROR | success=%s raw_text=%s",
                getattr(response, "success", None),
                (getattr(response, "raw_text", "") or "")[:200],
            )
            return None

        raw_results = response.parsed_data.get("grounding_results")
        if not isinstance(raw_results, list):
            logger.warning("[grounding_verification] PARSE_ERROR | grounding_results not a list")
            return None

        # 5. Apply the verdicts: overturn ungroundable positives.
        grounding_by_id: Dict[int, Dict[str, Any]] = {}
        for item in raw_results:
            if isinstance(item, dict) and isinstance(item.get("event_id"), int):
                grounding_by_id[item["event_id"]] = item

        records: List[Dict[str, Any]] = []
        for eid in sorted(positive_results):
            er = positive_results[eid]
            item = grounding_by_id.get(eid)
            if item is None:
                # 响应缺失的阳性事件按 grounded=true 处理（不推翻）。
                logger.warning(
                    "[grounding_verification] MISSING_RESULT | event_id=%d treated as grounded",
                    eid,
                )
                records.append({"event_id": eid, "grounded": True, "analysis": ""})
                continue
            grounded = bool(item.get("grounded", True))
            analysis = str(item.get("analysis") or "").strip()
            records.append({"event_id": eid, "grounded": grounded, "analysis": analysis})
            if grounded:
                er.grounding_note = analysis
                logger.info(
                    "[grounding_verification] GROUNDED | event_id=%d event=%s",
                    eid,
                    er.event_name,
                )
            else:
                er.detected = False
                er.instances = []
                er.grounding_overturned = True
                er.grounding_note = analysis
                er.summary = f"[裁决检出，锚定核验推翻] {er.summary}"
                logger.warning(
                    "[grounding_verification] OVERTURNED | event_id=%d event=%s | %s",
                    eid,
                    er.event_name,
                    analysis[:100],
                )

        context.local_vars["grounding_verification"] = records
        overturned = sum(1 for r in records if not r["grounded"])
        logger.info(
            "[grounding_verification] DONE | positives=%d overturned=%d",
            len(records),
            overturned,
        )
        return records
