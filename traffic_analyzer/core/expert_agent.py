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
    PromptTemplate,
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

        # -- 3. Inject prior knowledge (scene_understanding rules) -----------
        # scene_understanding prompt contains universal rules (direction,
        # emergency lane identification, camera perspective) that all experts
        # should know.  It is treated as fixed prior knowledge, not a VLM call.
        prior_knowledge = ""
        try:
            prior_template = self.config_manager.get_prompt_template(
                "scene_understanding"
            )
            if prior_template.user_prompt:
                prior_knowledge = prior_template.user_prompt
        except (KeyError, RuntimeError):
            logger.debug(
                "ExpertAgent[%s]: scene_understanding template not found, "
                "skipping prior knowledge injection",
                self.category.name_zh,
            )

        if prior_knowledge:
            # Build enhanced template with prior knowledge appended to system_prompt
            enhanced_system = template.system_prompt
            if enhanced_system and not enhanced_system.endswith("\n"):
                enhanced_system += "\n"
            enhanced_system += (
                "\n============================================================\n"
                "先验知识（高速公路监控场景通用规则，直接应用，无需重新推断）\n"
                "============================================================\n"
                + prior_knowledge
            )
            template = PromptTemplate(
                template_id=template.template_id,
                name=template.name,
                version=template.version,
                system_prompt=enhanced_system,
                user_prompt=template.user_prompt,
                output_format_hint=template.output_format_hint,
                example_input=template.example_input,
                example_output=template.example_output,
                traffic_percentage=template.traffic_percentage,
            )

        # -- 4. Context variables ----------------------------------------------
        context_vars: Dict[str, Any] = {
            "event_definition": self.category.definition,
            "event_name": self.category.name_zh,
            "event_id": self.category.event_id,
        }
        if context.video_meta is not None:
            context_vars["video_meta"] = context.video_meta.model_dump()

        # -- Vehicle tracking supplement for reversing detection (event_id=7) --
        cv_evidence = ""
        tracking_evidence = ""
        tracking_enabled = False
        if self.category.event_id == 7 and context.config is not None:
            tracking_enabled = getattr(context.config, "tracking_enabled", False)

        if self.category.event_id == 7 and tracking_enabled:
            tracker_result = None
            try:
                from traffic_analyzer.core.vehicle_tracker import YOLOVehicleTracker

                cache_key = "vehicle_tracking_evidence"
                cached = context.get_local(cache_key)
                if cached is not None:
                    tracker_result = cached
                else:
                    model_path = self.category.yolo_model_path if hasattr(self.category, 'yolo_model_path') else "traffic_analyzer/models/yolo/yolov8n.pt"
                    target_fps = 5.0
                    device = "cpu"
                    conf_thresh = 0.3
                    if context.config is not None:
                        model_path = getattr(context.config, 'yolo_model_path', model_path)
                        target_fps = getattr(context.config, 'tracking_target_fps', target_fps)
                        device = getattr(context.config, 'tracking_device', device)
                        conf_thresh = getattr(context.config, 'tracking_confidence_threshold', conf_thresh)

                    tracker = YOLOVehicleTracker(
                        model_path=model_path,
                        target_fps=target_fps,
                        device=device,
                        confidence_threshold=conf_thresh,
                    )
                    tracker_result = tracker.detect(context)
                    context.set_local(cache_key, tracker_result)

                tracking_evidence = tracker_result.evidence_text or tracker_result.to_prompt_text()
                cv_evidence = tracking_evidence  # backward compat

            except Exception as exc:
                logger.warning("YOLO tracker failed (%s), falling back to CV detector", exc)
                from traffic_analyzer.core.reversing_cv_detector import ReversingCVDetector
                cv_detector = ReversingCVDetector()
                cv_result = cv_detector.detect(context)
                cv_evidence = cv_result.evidence
                tracking_evidence = cv_evidence

        context_vars["cv_evidence"] = cv_evidence
        context_vars["tracking_evidence"] = tracking_evidence

        # -- 5. VLM call -------------------------------------------------------
        response = self.vlm_engine.call(
            template=template,
            images=images,
            context_vars=context_vars,
            response_schema=_EXPERT_RESPONSE_SCHEMA,
        )

        # -- 5. Parse response -------------------------------------------------
        candidate = parse_expert_response(response, self.category)
        candidate.cv_evidence = cv_evidence
        candidate.tracking_evidence = tracking_evidence
        logger.debug(
            "ExpertAgent[%s]: detected=%s confidence=%.2f instances=%d",
            self.category.name_zh,
            candidate.detected,
            candidate.confidence,
            len(candidate.instances),
        )
        return candidate
