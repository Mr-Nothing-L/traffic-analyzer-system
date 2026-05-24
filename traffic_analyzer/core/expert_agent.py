"""ExpertAgent — single-event detection agent.

Each ExpertAgent is responsible for detecting exactly one event category.
It reports what it sees (fact identification) without any filtering or
exclusion logic. Adjudication happens later in the pipeline.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.vlm_engine import VLMInferenceEngine
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCandidate,
    EventCategory,
    EventInstance,
)
from traffic_analyzer.utils.event_detection import parse_expert_response, select_event_images

logger = logging.getLogger(__name__)

# JSON schema expected from the VLM for expert-agent responses.
_EXPERT_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["detected"],
    "properties": {
        "detected": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
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
}


class ExpertAgent:
    """Single-event detection agent. Only responsible for fact identification."""

    def __init__(
        self,
        category: EventCategory,
        vlm_engine: VLMInferenceEngine,
        config_manager: ConfigManager,
    ) -> None:
        self.category = category
        self.vlm_engine = vlm_engine
        self.config_manager = config_manager

    def detect(self, context: AnalysisContext) -> EventCandidate:
        """Run VLM detection for this single event.

        Steps:
        1. Select images from context.keyframes.
        2. Load and render the prompt template.
        3. Call the VLM engine.
        4. Parse the response into an EventCandidate.

        If the VLM call fails, returns an EventCandidate with detected=False
        and a summary containing the error message.
        """
        # -- 1. Image selection ------------------------------------------------
        vlm_max_frames = 6
        if context.config is not None:
            vlm_max_frames = context.config.vlm_max_frames
        images = select_event_images(context, vlm_max_frames)

        if not images:
            logger.warning(
                "ExpertAgent[%s]: no images available for detection",
                self.category.name_zh,
            )
            return EventCandidate(
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                detected=False,
                summary="No images available for detection",
            )

        # -- 2. Prompt template ------------------------------------------------
        if not self.category.prompt_template_id:
            logger.warning(
                "ExpertAgent[%s]: no prompt_template_id configured",
                self.category.name_zh,
            )
            return EventCandidate(
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                detected=False,
                summary="No prompt template configured for this event",
            )

        try:
            template = self.config_manager.get_prompt_template(
                self.category.prompt_template_id
            )
        except (KeyError, RuntimeError) as exc:
            logger.warning(
                "ExpertAgent[%s]: failed to load prompt template: %s",
                self.category.name_zh,
                exc,
            )
            return EventCandidate(
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                detected=False,
                summary=f"Failed to load prompt template: {exc}",
            )

        # -- 3. Context variables ----------------------------------------------
        context_vars: Dict[str, Any] = {
            "event_definition": self.category.definition,
            "event_name": self.category.name_zh,
            "event_id": self.category.event_id,
        }
        if context.video_meta is not None:
            context_vars["video_meta"] = context.video_meta.model_dump()
        if context.scene_understanding is not None:
            context_vars["scene_understanding"] = context.scene_understanding.model_dump()

        # -- 4. VLM call -------------------------------------------------------
        response = self.vlm_engine.call(
            template=template,
            images=images,
            context_vars=context_vars,
            response_schema=_EXPERT_RESPONSE_SCHEMA,
        )

        # -- 5. Parse response -------------------------------------------------
        candidate = parse_expert_response(response, self.category)
        logger.debug(
            "ExpertAgent[%s]: detected=%s confidence=%.2f instances=%d",
            self.category.name_zh,
            candidate.detected,
            candidate.confidence,
            len(candidate.instances),
        )
        return candidate
