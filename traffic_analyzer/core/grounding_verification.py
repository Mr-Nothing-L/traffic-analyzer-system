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
PipelineStep 基类;core/verdict_format.py 的 build_event_definitions_json /
build_verdicts_json(only_positive=True)。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from traffic_analyzer.core.pipeline_steps import PipelineStep
from traffic_analyzer.core.verdict_format import (
    build_event_definitions_json,
    build_verdicts_json,
)
from traffic_analyzer.core.vlm_engine import FatalAPIError
from traffic_analyzer.models.schemas import AnalysisContext
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
            "verdicts_json": build_verdicts_json(
                context.event_results, categories, only_positive=True
            ),
            "event_definitions_json": build_event_definitions_json(categories),
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
            # type(...) is int:JSON true 是 bool(True == 1),不得当作 event 1。
            if isinstance(item, dict) and type(item.get("event_id")) is int:
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
            # 仅在显式 false 时推翻:null/缺失按可锚定处理,避免误杀真实阳性。
            grounded_value = item.get("grounded")
            grounded = True if grounded_value is None else bool(grounded_value)
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

        overturned = sum(1 for r in records if not r["grounded"])
        logger.info(
            "[grounding_verification] DONE | positives=%d overturned=%d",
            len(records),
            overturned,
        )
        return records
