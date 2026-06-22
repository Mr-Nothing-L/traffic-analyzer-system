"""ExpertAgent — single-event detection agent.

Each ExpertAgent is responsible for detecting exactly one event category.
It reports what it sees (fact identification) without any filtering or
exclusion logic. Adjudication happens later in the pipeline.

This file is now a thin compatibility layer.  The far-distance enhancement
and tool-call execution implementations live in
:mod:`traffic_analyzer.core.expert_agent_far_enhancement` and
:mod:`traffic_analyzer.core.expert_agent_tools` respectively.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.expert_agent_far_enhancement import (
    FarEnhancementDetector,
    _EXPERT_RESPONSE_SCHEMA,
    _FAR_ENHANCEMENT_OUTPUT_DIR,
)
from traffic_analyzer.core.expert_agent_tools import ToolCallExecutor
from traffic_analyzer.core.vlm_engine import FatalAPIError, VLMInferenceEngine
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCandidate,
    EventCategory,
    EventInstance,
    PromptTemplate,
)
from traffic_analyzer.utils.event_detection import parse_expert_response, select_event_images

logger = logging.getLogger(__name__)



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
            logger.error(
                "[expert_agent:detect] NO_IMAGES | event_id=%d event_name=%s",
                self.category.event_id,
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
            logger.error(
                "[expert_agent:detect] NO_TEMPLATE | event_id=%d event_name=%s",
                self.category.event_id,
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
            logger.error(
                "[expert_agent:detect] TEMPLATE_LOAD_ERROR | event_id=%d event_name=%s template_id=%s | %s",
                self.category.event_id,
                self.category.name_zh,
                self.category.prompt_template_id,
                exc,
                exc_info=True,
            )
            return EventCandidate(
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                detected=False,
                summary=f"Failed to load prompt template: {exc}",
            )

        # Decide whether to use the far-distance object enhancement path before
        # the template is possibly mutated by prior-knowledge injection.
        enable_far_enhancement = template.far_object_enhancement.enabled

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
            logger.info(
                "[expert_agent:detect] PRIOR_KNOWLEDGE | event_id=%d event_name=%s loaded=True length=%d",
                self.category.event_id,
                self.category.name_zh,
                len(prior_knowledge),
            )
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
                available_tools=template.available_tools,
                far_object_enhancement=template.far_object_enhancement,
            )

        # -- 4. Context variables ----------------------------------------------
        context_vars: Dict[str, Any] = {
            "event_definition": self.category.definition,
            "event_name": self.category.name_zh,
            "event_id": self.category.event_id,
        }
        if context.video_meta is not None:
            context_vars["video_meta"] = context.video_meta.model_dump()

        # -- 5. Far-distance object enhancement (template-driven) ---------------
        if enable_far_enhancement:
            enhanced_candidate = self._detect_with_far_enhancement(
                context=context,
                images=images,
                template=template,
                context_vars=context_vars,
            )
            if enhanced_candidate is not None:
                return enhanced_candidate
            logger.info(
                "[expert_agent:detect] FAR_ENHANCEMENT_FALLBACK | event_id=%d event_name=%s",
                self.category.event_id,
                self.category.name_zh,
            )

        # -- 6. VLM call -------------------------------------------------------
        try:
            response = self.vlm_engine.call(
                template=template,
                images=images,
                context_vars=context_vars,
                response_schema=_EXPERT_RESPONSE_SCHEMA,
            )
        except FatalAPIError:
            # Propagate fatal API errors (quota/auth) to stop batch processing
            raise
        except Exception as exc:
            logger.error(
                "[expert_agent:detect] VLM_ERROR | event_id=%d event_name=%s | %s",
                self.category.event_id,
                self.category.name_zh,
                exc,
                exc_info=True,
            )
            return EventCandidate(
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                detected=False,
                summary=f"VLM call failed: {exc}",
            )

        candidate = parse_expert_response(response, self.category)
        logger.debug(
            "ExpertAgent[%s]: detected=%s instances=%d",
            self.category.name_zh,
            candidate.detected,
            len(candidate.instances),
        )
        return candidate

    @property
    def _far_detector(self) -> FarEnhancementDetector:
        return FarEnhancementDetector(
            self.category, self.vlm_engine, self.config_manager
        )

    @property
    def _tool_executor(self) -> ToolCallExecutor:
        return ToolCallExecutor(self.category, self.vlm_engine)
    def _is_explicitly_car_reasoning(self, reason: str) -> bool:
        return self._far_detector._is_explicitly_car_reasoning(reason)

    def _is_explicitly_car_reasoning_for_pedestrian(self, reason: str) -> bool:
        return self._far_detector._is_explicitly_car_reasoning_for_pedestrian(reason)

    def _is_explicitly_car_reasoning_for_non_motor(self, reason: str) -> bool:
        return self._far_detector._is_explicitly_car_reasoning_for_non_motor(reason)

    def _select_car_veto_check(self, event_id: int):
        return self._far_detector._select_car_veto_check(event_id)

    def _build_minimal_final_classifier_template(
        self,
        template: PromptTemplate,
    ) -> PromptTemplate:
        return self._far_detector._build_minimal_final_classifier_template(template)

    def _should_veto_as_car(
        self,
        parsed: Dict[str, Any],
        text: str,
        event_id: int,
    ) -> bool:
        return self._far_detector._should_veto_as_car(parsed, text, event_id)

    def _apply_structured_veto_to_candidate(
        self,
        candidate: EventCandidate,
    ) -> EventCandidate:
        return self._far_detector._apply_structured_veto_to_candidate(candidate)

    def _is_no_structure_reasoning(self, reason: str) -> bool:
        return self._far_detector._is_no_structure_reasoning(reason)

    def _build_far_candidate(
        self,
        frame_info: Dict[str, Any],
        reason: str,
        frame_analysis_log: List[Dict[str, Any]],
        raw_text: Optional[str] = None,
        fallback: bool = False,
    ) -> EventCandidate:
        return self._far_detector._build_far_candidate(frame_info, reason, frame_analysis_log, raw_text, fallback)

    def _run_final_classifier(
        self,
        frame_info: Dict[str, Any],
        template: PromptTemplate,
        context_vars: Dict[str, Any],
    ) -> Optional[EventCandidate]:
        return self._far_detector._run_final_classifier(frame_info, template, context_vars)

    def _accept_fallback(
        self,
        frame_info: Dict[str, Any],
        frame_analysis_log: List[Dict[str, Any]],
    ) -> Optional[EventCandidate]:
        return self._far_detector._accept_fallback(frame_info, frame_analysis_log)

    def _has_construction_evidence(self, regions: List[Dict[str, Any]]) -> bool:
        return self._far_detector._has_construction_evidence(regions)

    def _build_construction_fallback_candidate(
        self,
        candidate: EventCandidate,
        display_regions: List[Dict[str, Any]],
        valid_regions: List[Dict[str, Any]],
        selected_index: int,
        gallery_ref: str,
        roi_summary: str,
    ) -> EventCandidate:
        return self._far_detector._build_construction_fallback_candidate(candidate, display_regions, valid_regions, selected_index, gallery_ref, roi_summary)

    @staticmethod
    def _parse_roi_confidence(value: Any) -> float:
        return FarEnhancementDetector._parse_roi_confidence(value)

    def _score_far_candidates(
        self,
        candidates: List[Dict[str, Any]],
        motion_score_threshold: float = 1.0,
        motion_penalty: float = 5.0,
    ) -> List[Dict[str, Any]]:
        return self._far_detector._score_far_candidates(candidates, motion_score_threshold, motion_penalty)

    def _log_frame(
        self,
        frame_analysis_log: List[Dict[str, Any]],
        frame_log: Dict[str, Any],
        reason: Optional[str] = None,
    ) -> None:
        return self._far_detector._log_frame(frame_analysis_log, frame_log, reason)

    def _detect_with_far_enhancement_gallery(
        self,
        context: AnalysisContext,
        images: List[Any],
        template: PromptTemplate,
        context_vars: Dict[str, Any],
        roi_template: PromptTemplate,
        output_dir: Path,
        image_ref_prefix: str,
        video_stem: str,
        far_cfg: Any,
    ) -> Optional[EventCandidate]:
        return self._far_detector._detect_with_far_enhancement_gallery(context, images, template, context_vars, roi_template, output_dir, image_ref_prefix, video_stem, far_cfg)

    def _detect_with_far_enhancement(
        self,
        context: AnalysisContext,
        images: List[Any],
        template: PromptTemplate,
        context_vars: Dict[str, Any],
    ) -> Optional[EventCandidate]:
        return self._far_detector._detect_with_far_enhancement(context, images, template, context_vars, default_output_dir=_FAR_ENHANCEMENT_OUTPUT_DIR)

    def _execute_tool_calls(
        self,
        response: Any,
        context: AnalysisContext,
        images: List[Any],
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        return self._tool_executor._execute_tool_calls(response, context, images)

    def _execute_native_tool_calls(
        self,
        template: Any,
        images: List[Any],
        context_vars: Dict[str, Any],
        context: AnalysisContext,
    ) -> Optional[Tuple[Dict[str, Any], Optional[str]]]:
        return self._tool_executor._execute_native_tool_calls(template, images, context_vars, context)

    def _execute_anthropic_native_tools(
        self,
        template: Any,
        images: List[Any],
        context_vars: Dict[str, Any],
        context: AnalysisContext,
    ) -> Optional[Tuple[Dict[str, Any], Optional[str]]]:
        return self._tool_executor._execute_anthropic_native_tools(template, images, context_vars, context)

    def _format_tracking_result(self, result_data: Dict[str, Any]) -> str:
        return self._tool_executor._format_tracking_result(result_data)

    def _second_vlm_call(
        self,
        template: PromptTemplate,
        first_response: Any,
        tool_result: Dict[str, Any],
        annotated_image: Optional[str],
        images: List[Any],
        context_vars: Dict[str, Any],
    ) -> EventCandidate:
        return self._tool_executor._second_vlm_call(template, first_response, tool_result, annotated_image, images, context_vars)


