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
from traffic_analyzer.core.vlm_engine import VLMInferenceEngine
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
        """Execute the step with retry and timing.

        Args:
            context: Shared analysis context.

        Returns:
            StepResult with success status, data, and timing.
        """
        start = time.perf_counter()
        last_error: Optional[Exception] = None
        retries = 0

        for attempt in range(self.max_retries + 1):
            try:
                data = self._execute(context)
                return StepResult(
                    success=True,
                    data=data,
                    duration_sec=time.perf_counter() - start,
                    retry_count=retries,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Step '%s' failed (attempt %d/%d): %s",
                    self.name,
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                )
                if attempt < self.max_retries:
                    retries += 1
                    wait = min(2 ** retries, 30)
                    time.sleep(wait)
                else:
                    break

        # All retries exhausted
        if self.fallback_enabled:
            logger.info("Step '%s' running fallback", self.name)
            fallback_data = self._fallback(context, last_error)
            return StepResult(
                success=True,
                data=fallback_data,
                duration_sec=time.perf_counter() - start,
                retry_count=retries,
            )

        return StepResult(
            success=False,
            error=last_error,
            duration_sec=time.perf_counter() - start,
            retry_count=retries,
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
        event_categories = self.config_manager.get_event_categories()
        expert_categories = [
            cat for cat in event_categories
            if cat.is_active and cat.detection_mode == "expert_agent"
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
                        "ExpertAgent[%s]: detected=%s confidence=%.2f",
                        category.name_zh,
                        candidate.detected,
                        candidate.confidence,
                    )
                except Exception as exc:
                    logger.error(
                        "ExpertAgent[%s] failed: %s",
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
            logger.warning("Failed to load adjudication rules: %s", exc)
            rules = []

        # 2. Load prompt template
        try:
            template = self.config_manager.get_prompt_template("adjudication")
        except KeyError:
            logger.error("Adjudication prompt template not found")
            raise RuntimeError("Adjudication prompt template 'adjudication' not found")

        # 3. Select images
        images = _select_event_images_impl(context)

        # 4. Build context variables
        candidates_json = json.dumps(
            [
                {
                    "event_id": c.event_id,
                    "event_name": c.event_name,
                    "detected": c.detected,
                    "confidence": c.confidence,
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

        business_rules = "\n".join(
            f"- [{r.get('rule_id', 'N/A')}] {r.get('name', 'Unnamed')}: "
            f"{r.get('description', '')} (priority={r.get('priority', 0)})"
            for r in rules
        ) if rules else "No business rules configured."

        context_vars = {
            "candidates_json": candidates_json,
            "business_rules": business_rules,
        }

        # 5. Call VLM
        response = self.vlm_engine.call(
            template=template,
            images=images,
            context_vars=context_vars,
        )

        if not response.success or not isinstance(response.parsed_data, dict):
            logger.error("Adjudication VLM call failed: %s", response.raw_text)
            raise RuntimeError(f"Adjudication VLM call failed: {response.raw_text}")

        data = response.parsed_data

        # 6. Parse event_results
        event_results: List[EventResult] = []
        for er in data.get("event_results", []):
            instances = []
            for inst in er.get("instances", []):
                instances.append(
                    EventInstance(
                        event_id=er.get("event_id", 0),
                        event_name=er.get("event_name", ""),
                        event_name_en=er.get("event_name_en", ""),
                        start_time_sec=inst.get("start_time_sec", 0.0),
                        end_time_sec=inst.get("end_time_sec", 0.0),
                        confidence=inst.get("confidence", 0.0),
                        description=inst.get("description", ""),
                        reasoning=inst.get("reasoning", ""),
                    )
                )
            event_results.append(
                EventResult(
                    event_id=er.get("event_id", 0),
                    event_name=er.get("event_name", ""),
                    event_name_en=er.get("event_name_en", ""),
                    detected=er.get("detected", False),
                    confidence=er.get("confidence", 0.0),
                    summary=er.get("summary", ""),
                    instances=instances,
                    reasoning=er.get("reasoning", ""),
                )
            )

        # 7. Parse audit_log
        audit_log: List[AuditEntry] = []
        for entry in data.get("audit_log", []):
            audit_log.append(
                AuditEntry(
                    event_id=entry.get("event_id", 0),
                    event_name=entry.get("event_name", ""),
                    action=entry.get("action", "included"),
                    reason=entry.get("reason", ""),
                    rule_id=entry.get("rule_id"),
                )
            )

        # 8. Store results in context
        for result in event_results:
            context.event_results[result.event_id] = result

        adjudication_result = AdjudicationResult(
            event_results=event_results,
            audit_log=audit_log,
            adjudication_reasoning=data.get("adjudication_reasoning", ""),
        )

        logger.info(
            "Adjudication complete: %d event results, %d audit entries",
            len(event_results),
            len(audit_log),
        )
        return adjudication_result

    def _fallback(self, context: AnalysisContext, error: Optional[Exception]) -> AdjudicationResult:
        """Fallback: return raw expert candidates as EventResults (no filtering)."""
        logger.warning(
            "Adjudication fallback: returning raw expert candidates as EventResults (%s)",
            error,
        )
        event_results: List[EventResult] = []
        for candidate in context.event_candidates.values():
            event_results.append(
                EventResult(
                    event_id=candidate.event_id,
                    event_name=candidate.event_name,
                    detected=candidate.detected,
                    confidence=candidate.confidence,
                    summary=candidate.summary,
                    instances=candidate.instances,
                )
            )
            context.event_results[candidate.event_id] = event_results[-1]

        return AdjudicationResult(
            event_results=event_results,
            adjudication_reasoning=f"Fallback: adjudication failed ({error}). Raw expert candidates returned without filtering.",
        )
