"""ExpertAgent — single-event detection agent.

Each ExpertAgent is responsible for detecting exactly one event category.
It reports what it sees (fact identification) without any filtering or
exclusion logic. Adjudication happens later in the pipeline.

This file is now a thin compatibility layer.  The far-distance enhancement
implementation lives in
:mod:`traffic_analyzer.core.expert_agent_far_enhancement`.

[文件说明]
作用:单事件检测代理 ExpertAgent。每个实例只负责一个事件类别的事实识别
(选帧→加载 prompt 模板并注入 scene_understanding 先验→VLM 调用→解析为
EventCandidate,可选自我反思),不做任何过滤/排除,裁决留给后续步骤。
本文件为兼容层,远距离增强实现拆分到同目录另一模块。
上游:core/pipeline_steps.py 的 ExpertAgentLayer(并行调度)与
AdjudicationStep(缺失事件时重跑专家)。
下游:core/vlm_engine.py 的 VLMInferenceEngine.call;core/config_manager.py
加载 config/prompts/ 下的 event_N.yaml 事件模板及 scene_understanding、
expert_response_reflection 模板;core/expert_agent_far_enhancement.py 的
FarEnhancementDetector;
utils/event_detection.py 的 select_event_images / parse_expert_response /
reflect_expert_candidate。
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
from traffic_analyzer.core.vlm_engine import FatalAPIError, VLMInferenceEngine
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCandidate,
    EventCategory,
    EventInstance,
    PromptTemplate,
)
from traffic_analyzer.utils.event_detection import (
    parse_expert_response,
    reflect_expert_candidate,
    select_event_images,
)
from traffic_analyzer.utils.progress import get_reporter as _get_progress_reporter

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

        _get_progress_reporter().phase("prepare")

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
        candidate: Optional[EventCandidate] = None
        if enable_far_enhancement:
            _get_progress_reporter().phase("main_detect")
            candidate = self._detect_with_far_enhancement(
                context=context,
                images=images,
                template=template,
                context_vars=context_vars,
            )
            # Templates with far_object_enhancement enabled expect exactly the
            # generated composites (vehicle boxes + zoom grid), not the original
            # raw frames. Feeding raw frames to such a template leads to a
            # prompt/image mismatch and repeated JSON parse failures, so when
            # the enhanced flow fails to produce a candidate, return a negative
            # candidate directly instead of falling back to raw images.
            if candidate is None:
                logger.warning(
                    "[expert_agent:detect] FAR_ENHANCEMENT_FAILED_NEGATIVE | "
                    "event_id=%d event_name=%s reason=增强检测失败，无法生成有效证据",
                    self.category.event_id,
                    self.category.name_zh,
                )
                return EventCandidate(
                    event_id=self.category.event_id,
                    event_name=self.category.name_zh,
                    detected=False,
                    summary=f"{self.category.name_zh}增强检测失败，无法生成有效证据",
                )

        # -- 6. Direct VLM call (used when far enhancement is disabled) --------
        if candidate is None:
            try:
                _get_progress_reporter().phase("main_detect")
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

        # -- 7. Optional self-consistency reflection ---------------------------
        _get_progress_reporter().phase("parse")
        reflection_enabled = (
            context.config.expert_enable_reflection
            if context.config is not None
            else True
        )
        if reflection_enabled:
            try:
                reflection_template = self.config_manager.get_prompt_template(
                    "expert_response_reflection"
                )
            except (KeyError, RuntimeError) as exc:
                logger.warning(
                    "[expert_agent:detect] REFLECTION_TEMPLATE_MISSING | event_id=%d event_name=%s | %s",
                    self.category.event_id,
                    self.category.name_zh,
                    exc,
                )
                return candidate

            _get_progress_reporter().phase("reflect")
            candidate = reflect_expert_candidate(
                candidate=candidate,
                category=self.category,
                vlm_engine=self.vlm_engine,
                reflection_template=reflection_template,
            )
            _get_progress_reporter().phase("finish")

        return candidate

    @property
    def _far_detector(self) -> FarEnhancementDetector:
        return FarEnhancementDetector(
            self.category, self.vlm_engine, self.config_manager
        )

    def _detect_with_far_enhancement(
        self,
        context: AnalysisContext,
        images: List[Any],
        template: PromptTemplate,
        context_vars: Dict[str, Any],
    ) -> Optional[EventCandidate]:
        return self._far_detector._detect_with_far_enhancement(context, images, template, context_vars, default_output_dir=_FAR_ENHANCEMENT_OUTPUT_DIR)

