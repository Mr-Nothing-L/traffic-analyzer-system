"""
PipelineStep module for the traffic analyzer framework.

Provides a pluggable step-based architecture for the analysis pipeline.
Each step encapsulates a discrete phase of analysis (expert agent layer,
adjudication) with built-in retry support.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.expert_agent import ExpertAgent
from traffic_analyzer.core.vlm_engine import FatalAPIError, VLMInferenceEngine
from traffic_analyzer.utils.annotation_spec_loader import AnnotationSpecLoader
from traffic_analyzer.utils.event_detection import select_event_images as _select_event_images_impl
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    AdjudicationResult,
    AuditEntry,
    EventCandidate,
    EventInstance,
    EventResult,
)

logger = logging.getLogger(__name__)

MAX_ADJUDICATION_RETRIES = 5

# JSON schema for adjudication VLM response (forces valid JSON output).
_ADJUDICATION_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["event_results"],
    "properties": {
        "event_results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["event_id", "event_name", "detected"],
                "properties": {
                    "event_id": {"type": "integer"},
                    "event_name": {"type": "string"},
                    "detected": {"type": "boolean"},
                    "summary": {"type": "string"},
                    "instances": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "start_time_sec": {"type": "number"},
                                "end_time_sec": {"type": "number"},
                                "evidence_frames": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                },
                                "description": {"type": "string"},
                                "reasoning": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "audit_log": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "integer"},
                    "event_name": {"type": "string"},
                    "action": {"type": "string"},
                    "reason": {"type": "string"},
                    "rule_id": {"type": ["string", "null"]},
                },
            },
        },
        "adjudication_reasoning": {"type": "string"},
        "reasoning_chain": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "integer"},
                    "event_name": {"type": "string"},
                    "decision": {"type": "string"},
                    "thought_process": {"type": "string"},
                    "basis": {"type": "string"},
                },
            },
        },
    },
}


class StepResult:
    """Result of a pipeline step execution."""

    def __init__(
        self,
        success: bool = True,
        data: Any = None,
        error: Optional[Exception] = None,
        duration_sec: float = 0.0,
        retry_count: int = 0,
    ) -> None:
        self.success = success
        self.data = data
        self.error = error
        self.duration_sec = duration_sec
        self.retry_count = retry_count


class PipelineStep(ABC):
    """Abstract base class for analysis pipeline steps.

    Each step encapsulates a discrete phase of the analysis pipeline
    with built-in retry and fallback support.
    """

    def __init__(
        self,
        name: str,
        max_retries: int = 0,
        fallback_enabled: bool = False,
    ) -> None:
        self.name = name
        self.max_retries = max_retries
        self.fallback_enabled = fallback_enabled

    @abstractmethod
    def _execute(self, context: AnalysisContext) -> Any:
        """Execute the step logic. Must be implemented by subclasses.

        Args:
            context: Shared analysis context.

        Returns:
            Step-specific output data.

        Raises:
            Exception: On step failure.
        """
        ...

    def execute(self, context: AnalysisContext) -> StepResult:
        """Execute the step with timing and fallback.

        Args:
            context: Shared analysis context.

        Returns:
            StepResult with success status, data, and timing.
        """
        start = time.perf_counter()
        try:
            data = self._execute(context)
            return StepResult(
                success=True,
                data=data,
                duration_sec=time.perf_counter() - start,
                retry_count=0,
            )
        except FatalAPIError:
            raise
        except Exception as exc:
            logger.warning("Step '%s' failed: %s", self.name, exc)
            if self.fallback_enabled:
                logger.info("Step '%s' running fallback", self.name)
                fallback_data = self._fallback(context, exc)
                return StepResult(
                    success=True,
                    data=fallback_data,
                    duration_sec=time.perf_counter() - start,
                    retry_count=0,
                )
            return StepResult(
                success=False,
                error=exc,
                duration_sec=time.perf_counter() - start,
                retry_count=0,
            )

    def _fallback(self, context: AnalysisContext, error: Optional[Exception]) -> Any:
        """Produce fallback output when the step fails.

        Subclasses may override to provide domain-specific defaults.
        """
        return None


class ExpertAgentLayer(PipelineStep):
    """Step 2: Parallel expert agents for each active event."""

    def __init__(self, config_manager, vlm_engine, max_workers=4, max_retries=0):
        super().__init__("expert_agent_layer", max_retries=max_retries)
        self.config_manager = config_manager
        self.vlm_engine = vlm_engine
        self.max_workers = max_workers

    def _execute(self, context: AnalysisContext) -> List[EventCandidate]:
        active_categories = self.config_manager.get_active_event_categories()
        expert_categories = [
            cat for cat in active_categories
            if cat.detection_mode == "expert_agent"
        ]

        if not expert_categories:
            logger.info("No active expert_agent categories found")
            return []

        candidates: List[EventCandidate] = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_category = {}
            for category in expert_categories:
                agent = ExpertAgent(
                    category=category,
                    vlm_engine=self.vlm_engine,
                    config_manager=self.config_manager,
                )
                future = executor.submit(agent.detect, context)
                future_to_category[future] = category

            for future in as_completed(future_to_category):
                category = future_to_category[future]
                try:
                    candidate = future.result()
                    candidates.append(candidate)
                    context.event_candidates[candidate.event_id] = candidate
                    logger.info(
                        "ExpertAgent[%s]: detected=%s",
                        category.name_zh,
                        candidate.detected,
                    )
                except FatalAPIError:
                    raise
                except Exception as exc:
                    logger.error(
                        "[pipeline_steps:ExpertAgentLayer] EXPERT_ERROR | event_id=%d event_name=%s | %s",
                        category.event_id,
                        category.name_zh,
                        exc,
                        exc_info=True,
                    )
                    error_candidate = EventCandidate(
                        event_id=category.event_id,
                        event_name=category.name_zh,
                        detected=False,
                        summary=f"ExpertAgent error: {exc}",
                    )
                    candidates.append(error_candidate)
                    context.event_candidates[category.event_id] = error_candidate

        return candidates


class AdjudicationStep(PipelineStep):
    """Step 3: Single VLM call to adjudicate all expert candidates."""

    def __init__(self, config_manager, vlm_engine, max_retries=0):
        super().__init__("adjudication", max_retries=max_retries, fallback_enabled=True)
        self.config_manager = config_manager
        self.vlm_engine = vlm_engine

    def _execute(self, context: AnalysisContext) -> AdjudicationResult:
        candidates = list(context.event_candidates.values())
        if not candidates:
            logger.info("No event candidates to adjudicate")
            return AdjudicationResult()

        # 1. Load adjudication rules
        try:
            rules = self.config_manager.get_adjudication_rules()
        except Exception as exc:
            logger.error(
                "[pipeline_steps:AdjudicationStep] RULE_LOAD_ERROR | %s",
                exc,
                exc_info=True,
            )
            rules = []

        # 2. Load prompt template
        try:
            template = self.config_manager.get_prompt_template("adjudication")
        except KeyError as exc:
            logger.error(
                "[pipeline_steps:AdjudicationStep] TEMPLATE_ERROR | template_id=adjudication | %s",
                exc,
                exc_info=True,
            )
            raise RuntimeError("Adjudication prompt template 'adjudication' not found")

        # 3. Select images
        vlm_max_frames = 6
        if context.config is not None:
            vlm_max_frames = context.config.vlm_max_frames
        images = _select_event_images_impl(context, vlm_max_frames=vlm_max_frames)

        # Load annotation spec (xlsx-derived business rules)
        annotation_spec_text = ""
        try:
            spec_path = self.config_manager.config_dir / "annotation_spec.yaml"
            if spec_path.exists():
                spec_loader = AnnotationSpecLoader(str(spec_path))
                annotation_spec_text = spec_loader.to_prompt_text()
            else:
                logger.warning("annotation_spec.yaml not found at %s", spec_path)
        except Exception as exc:
            logger.warning("Failed to load annotation_spec.yaml: %s", exc)

        business_rules = "\n".join(
            f"- [{r.rule_id}] {r.name}: "
            f"{r.description} (priority={r.priority})"
            for r in rules
        ) if rules else "No business rules configured."

        previously_missing_event_ids: List[int] = []
        last_response: Optional[Any] = None
        last_parsed_data: Dict[str, Any] = {}

        for attempt in range(1, MAX_ADJUDICATION_RETRIES + 1):
            candidates = list(context.event_candidates.values())
            candidates_json = json.dumps(
                [
                    {
                        "event_id": c.event_id,
                        "event_name": c.event_name,
                        "detected": c.detected,
                        "summary": c.summary,
                        "instances": [
                            {
                                "start_time_sec": i.start_time_sec,
                                "end_time_sec": i.end_time_sec,
                                "description": i.description,
                                "reasoning": i.reasoning,
                            }
                            for i in c.instances
                        ],
                    }
                    for c in candidates
                ],
                ensure_ascii=False,
                indent=2,
            )

            context_vars = {
                "candidates_json": candidates_json,
                "business_rules": business_rules,
                "annotation_spec": annotation_spec_text or "No annotation spec available.",
                "previously_missing_event_ids": previously_missing_event_ids,
            }

            try:
                response = self.vlm_engine.call(
                    template=template,
                    images=images,
                    context_vars=context_vars,
                    response_schema=_ADJUDICATION_RESPONSE_SCHEMA,
                )
            except FatalAPIError:
                raise
            except Exception as exc:
                logger.error(
                    "[pipeline_steps:AdjudicationStep] VLM_CALL_ERROR | attempt=%d candidates=%d | %s",
                    attempt,
                    len(candidates),
                    exc,
                    exc_info=True,
                )
                raise RuntimeError(f"Adjudication VLM call failed: {exc}") from exc

            last_response = response

            if not response.success or not isinstance(response.parsed_data, dict):
                logger.error(
                    "[pipeline_steps:AdjudicationStep] ADJUDICATION_ERROR | attempt=%d candidates=%d | response.success=%s parsed_data_type=%s | raw_text_len=%d",
                    attempt,
                    len(candidates),
                    response.success,
                    type(response.parsed_data).__name__ if response.parsed_data is not None else "None",
                    len(response.raw_text) if response.raw_text else 0,
                )
                raise RuntimeError(f"Adjudication VLM call failed: {response.raw_text[:500]}")

            data = response.parsed_data
            last_parsed_data = data

            event_results_raw = data.get("event_results", [])
            present_event_ids = {er.get("event_id") for er in event_results_raw if isinstance(er.get("event_id"), int)}
            expected_event_ids = set(context.event_candidates.keys())
            missing_event_ids = sorted(expected_event_ids - present_event_ids)

            if not missing_event_ids:
                logger.info(
                    "[pipeline_steps:AdjudicationStep] COMPLETE | attempt=%d event_results=%d",
                    attempt,
                    len(event_results_raw),
                )
                return self._build_adjudication_result(context, data, response)

            logger.warning(
                "[pipeline_steps:AdjudicationStep] MISSING_EVENTS | attempt=%d missing_event_ids=%s",
                attempt,
                missing_event_ids,
            )

            abnormal_event_ids = [
                eid for eid in missing_event_ids
                if self._is_abnormal_candidate(context.event_candidates.get(eid))
            ]

            if abnormal_event_ids:
                logger.info(
                    "[pipeline_steps:AdjudicationStep] RERUN_ABNORMAL_EXPERTS | attempt=%d event_ids=%s",
                    attempt,
                    abnormal_event_ids,
                )
                for eid in abnormal_event_ids:
                    category = self._get_category_for_event_id(eid)
                    if category is None:
                        logger.warning(
                            "[pipeline_steps:AdjudicationStep] CATEGORY_NOT_FOUND | event_id=%d",
                            eid,
                        )
                        continue
                    try:
                        agent = ExpertAgent(
                            category=category,
                            vlm_engine=self.vlm_engine,
                            config_manager=self.config_manager,
                        )
                        new_candidate = agent.detect(context)
                        context.event_candidates[eid] = new_candidate
                        logger.info(
                            "[pipeline_steps:AdjudicationStep] EXPERT_RERUN_SUCCESS | attempt=%d event_id=%d detected=%s",
                            attempt,
                            eid,
                            new_candidate.detected,
                        )
                    except FatalAPIError:
                        raise
                    except Exception as exc:
                        logger.error(
                            "[pipeline_steps:AdjudicationStep] EXPERT_RERUN_ERROR | attempt=%d event_id=%d | %s",
                            attempt,
                            eid,
                            exc,
                            exc_info=True,
                        )
                previously_missing_event_ids = []
                continue

            previously_missing_event_ids = missing_event_ids

        logger.warning(
            "[pipeline_steps:AdjudicationStep] MAX_RETRIES_REACHED | missing_event_ids=%s | filling from candidates",
            previously_missing_event_ids,
        )
        return self._build_adjudication_result_with_fallback(context, last_parsed_data, last_response)

    @staticmethod
    def _is_abnormal_candidate(candidate: Optional[EventCandidate]) -> bool:
        if candidate is None:
            return True
        if candidate.summary and candidate.summary.startswith("ExpertAgent error"):
            return True
        if not candidate.raw_vlm_response and not candidate.raw_vlm_text:
            return True
        if candidate.detected and not candidate.summary:
            return True
        if candidate.detected and not candidate.instances:
            return True
        return False

    def _get_category_for_event_id(self, event_id: int) -> Optional[Any]:
        try:
            for cat in self.config_manager.get_event_categories():
                if cat.event_id == event_id:
                    return cat
        except Exception as exc:
            logger.error(
                "[pipeline_steps:AdjudicationStep] GET_CATEGORY_ERROR | event_id=%d | %s",
                event_id,
                exc,
                exc_info=True,
            )
        return None

    def _build_adjudication_result(
        self,
        context: AnalysisContext,
        data: Dict[str, Any],
        response: Any,
    ) -> AdjudicationResult:
        # Determine active event IDs at execution time
        try:
            active_categories = self.config_manager.get_active_event_categories()
            active_event_ids = {cat.event_id for cat in active_categories}
        except Exception as exc:
            logger.error(
                "[pipeline_steps:AdjudicationStep] ACTIVE_CATEGORIES_ERROR | %s",
                exc,
                exc_info=True,
            )
            active_event_ids = set(context.event_candidates.keys())

        candidates = list(context.event_candidates.values())
        candidate_raw_map = {c.event_id: c.raw_vlm_text for c in candidates}
        candidate_cv_map = {c.event_id: c.cv_evidence for c in candidates}

        # Build event_results, filtering to active event IDs only
        event_results: List[EventResult] = []
        present_active_ids: set[int] = set()
        for er in data.get("event_results", []):
            eid = er.get("event_id", 0)
            if eid not in active_event_ids:
                continue
            present_active_ids.add(eid)
            instances = []
            for inst in er.get("instances", []):
                instances.append(
                    EventInstance(
                        event_id=eid,
                        event_name=er.get("event_name", ""),
                        event_name_en=er.get("event_name_en", ""),
                        start_time_sec=inst.get("start_time_sec", 0.0),
                        end_time_sec=inst.get("end_time_sec", 0.0),
                        description=inst.get("description", ""),
                        reasoning=inst.get("reasoning", ""),
                    )
                )
            event_results.append(
                EventResult(
                    event_id=eid,
                    event_name=er.get("event_name", ""),
                    event_name_en=er.get("event_name_en", ""),
                    detected=er.get("detected", False),
                    summary=er.get("summary", ""),
                    instances=instances,
                    reasoning=er.get("reasoning", ""),
                    expert_raw_description=candidate_raw_map.get(eid, ""),
                    cv_evidence=candidate_cv_map.get(eid, ""),
                )
            )

        # Fill any missing active event IDs from candidates
        missing_active_ids = sorted(active_event_ids - present_active_ids)
        for eid in missing_active_ids:
            candidate = context.event_candidates.get(eid)
            if candidate is not None:
                event_results.append(
                    EventResult(
                        event_id=candidate.event_id,
                        event_name=candidate.event_name,
                        detected=candidate.detected,
                        summary=candidate.summary,
                        instances=candidate.instances,
                        expert_raw_description=candidate.raw_vlm_text,
                        cv_evidence=candidate.cv_evidence,
                        tool_results=candidate.tool_results,
                    )
                )
                logger.info(
                    "[pipeline_steps:AdjudicationStep] FILLED_MISSING_ACTIVE | event_id=%d detected=%s",
                    eid,
                    candidate.detected,
                )
            else:
                # No candidate available — create a default undetected result
                event_results.append(
                    EventResult(
                        event_id=eid,
                        event_name="",
                        detected=False,
                        summary="",
                    )
                )
                logger.warning(
                    "[pipeline_steps:AdjudicationStep] FILLED_MISSING_ACTIVE_NO_CANDIDATE | event_id=%d",
                    eid,
                )

        # Filter audit_log to active event IDs only
        audit_log: List[AuditEntry] = []
        for entry in data.get("audit_log", []):
            eid = entry.get("event_id", 0)
            if eid not in active_event_ids:
                continue
            audit_log.append(
                AuditEntry(
                    event_id=eid,
                    event_name=entry.get("event_name", ""),
                    action=entry.get("action", "included"),
                    reason=entry.get("reason", ""),
                    rule_id=entry.get("rule_id"),
                )
            )

        for result in event_results:
            context.event_results[result.event_id] = result

        # Filter reasoning_chain to active event IDs only
        reasoning_chain: List[Dict[str, Any]] = []
        for rc in data.get("reasoning_chain", []):
            if isinstance(rc, dict):
                eid = rc.get("event_id", 0)
                if eid not in active_event_ids:
                    continue
                reasoning_chain.append({
                    "event_id": eid,
                    "event_name": rc.get("event_name", ""),
                    "decision": rc.get("decision", ""),
                    "thought_process": rc.get("thought_process", ""),
                    "basis": rc.get("basis", ""),
                })

        logger.info(
            "[pipeline_steps:AdjudicationStep] PARSED_RESULTS | event_results=%d detected=%d audit_log=%d active_ids=%s",
            len(event_results),
            sum(1 for r in event_results if r.detected),
            len(audit_log),
            sorted(active_event_ids),
        )

        return AdjudicationResult(
            event_results=event_results,
            audit_log=audit_log,
            adjudication_reasoning=data.get("adjudication_reasoning", ""),
            reasoning_chain=reasoning_chain,
            raw_vlm_text=response.raw_text if hasattr(response, "raw_text") else "",
        )

    def _build_adjudication_result_with_fallback(
        self,
        context: AnalysisContext,
        data: Dict[str, Any],
        response: Any,
    ) -> AdjudicationResult:
        result = self._build_adjudication_result(context, data, response)
        present_event_ids = {r.event_id for r in result.event_results}

        # Use active event IDs as the expected set
        try:
            active_categories = self.config_manager.get_active_event_categories()
            expected_event_ids = {cat.event_id for cat in active_categories}
        except Exception as exc:
            logger.error(
                "[pipeline_steps:AdjudicationStep] ACTIVE_CATEGORIES_FALLBACK_ERROR | %s",
                exc,
                exc_info=True,
            )
            expected_event_ids = set(context.event_candidates.keys())

        missing_event_ids = sorted(expected_event_ids - present_event_ids)

        for eid in missing_event_ids:
            candidate = context.event_candidates.get(eid)
            if candidate is None:
                continue
            fallback_result = EventResult(
                event_id=candidate.event_id,
                event_name=candidate.event_name,
                detected=candidate.detected,
                summary=candidate.summary,
                instances=candidate.instances,
                expert_raw_description=candidate.raw_vlm_text,
                cv_evidence=candidate.cv_evidence,
                tool_results=candidate.tool_results,
            )
            result.event_results.append(fallback_result)
            context.event_results[eid] = fallback_result
            logger.info(
                "[pipeline_steps:AdjudicationStep] FILLED_MISSING_FROM_CANDIDATE | event_id=%d detected=%s",
                eid,
                fallback_result.detected,
            )

        return result

    def _fallback(self, context: AnalysisContext, error: Optional[Exception]) -> AdjudicationResult:
        """Fallback: return raw expert candidates as EventResults (no filtering)."""
        reason = str(error) if error else "unknown"
        logger.error(
            "[pipeline_steps:AdjudicationStep] FALLBACK | reason=%s | returning %d raw candidates",
            reason,
            len(context.event_candidates),
        )
        event_results: List[EventResult] = []
        for candidate in context.event_candidates.values():
            event_results.append(
                EventResult(
                    event_id=candidate.event_id,
                    event_name=candidate.event_name,
                    detected=candidate.detected,
                    summary=candidate.summary,
                    instances=candidate.instances,
                    expert_raw_description=candidate.raw_vlm_text,
                    tool_results=candidate.tool_results,
                )
            )
            context.event_results[candidate.event_id] = event_results[-1]

        return AdjudicationResult(
            event_results=event_results,
            adjudication_reasoning=f"Fallback: adjudication failed ({error}). Raw expert candidates returned without filtering.",
        )
