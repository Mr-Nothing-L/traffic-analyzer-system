"""ExpertAgent — single-event detection agent.

Each ExpertAgent is responsible for detecting exactly one event category.
It reports what it sees (fact identification) without any filtering or
exclusion logic. Adjudication happens later in the pipeline.
"""

from __future__ import annotations

import logging
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.vlm_engine import FatalAPIError, VLMInferenceEngine
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCandidate,
    EventCategory,
    EventInstance,
    PromptTemplate,
)
from traffic_analyzer.utils.event_detection import parse_expert_response, select_event_images
from traffic_analyzer.utils.far_non_motor_enhancer import (
    compute_bbox_area_px,
    create_composite,
    create_motion_comparison_composite,
    is_bbox_large_enough,
    load_image,
)

logger = logging.getLogger(__name__)

# Directory where far-distance non-motor vehicle composite images are saved.
_FAR_ENHANCEMENT_OUTPUT_DIR = Path(
    "/media/wanji/Elements/大模型应用/高速交通事件Agent测试视频V4/output/tmp_img"
)

# JSON schema expected from the VLM for expert-agent responses.
_EXPERT_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["detected"],
    "properties": {
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
}

# JSON schema for the far-distance non-motor vehicle per-frame ROI detection.
_ROI_DETECTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["bbox_norm"],
    "properties": {
        "bbox_norm": {
            "anyOf": [
                {"type": "array", "items": {"type": "number"}},
                {"type": "null"},
            ]
        },
        "reason": {"type": "string"},
    },
}

# JSON schema for the adjacent-frame motion reflection verification.
_MOTION_VERIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_moving": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["is_moving", "reason"],
    "additionalProperties": False,
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

        # Decide whether to use the far-distance non-motor vehicle enhancement
        # path before the template is possibly mutated by prior-knowledge injection.
        enable_far_enhancement = (
            self.category.event_id == 4
            and getattr(template, "enable_far_object_enhancement", False)
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
                enable_far_object_enhancement=template.enable_far_object_enhancement,
            )

        # -- 4. Context variables ----------------------------------------------
        context_vars: Dict[str, Any] = {
            "event_definition": self.category.definition,
            "event_name": self.category.name_zh,
            "event_id": self.category.event_id,
        }
        if context.video_meta is not None:
            context_vars["video_meta"] = context.video_meta.model_dump()

        # -- 5. Far-distance non-motor vehicle enhancement (event_id=4 only) -----
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

    def _detect_with_far_enhancement(
        self,
        context: AnalysisContext,
        images: List[Any],
        template: PromptTemplate,
        context_vars: Dict[str, Any],
    ) -> Optional[EventCandidate]:
        """Run the per-frame far-distance non-motor vehicle enhancement flow.

        Each input keyframe is processed individually. The VLM returns a distant
        non-motor vehicle bbox for that frame. If the bbox is valid (area >= 80 px),
        a composite is generated immediately and passed to the final classifier.
        On the first detected composite the function returns early with the
        candidate and far-enhancement metadata.

        If no valid candidate is found after all frames are exhausted, a
        detected=False EventCandidate is returned. Fatal API errors are re-raised.
        """
        if context.video_meta is None:
            logger.warning(
                "[expert_agent:_detect_with_far_enhancement] NO_VIDEO_META | event_id=%d",
                self.category.event_id,
            )
            return None

        logger.info(
            "[expert_agent:_detect_with_far_enhancement] START | event_id=%d event_name=%s frames=%d",
            self.category.event_id,
            self.category.name_zh,
            len(images),
        )

        try:
            roi_template = self.config_manager.get_prompt_template(
                "far_non_motor_roi_detection"
            )
        except (KeyError, RuntimeError) as exc:
            logger.warning(
                "[expert_agent:_detect_with_far_enhancement] ROI_TEMPLATE_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
            )
            return None

        video_stem = Path(context.video_meta.file_path).stem
        output_dir = _FAR_ENHANCEMENT_OUTPUT_DIR
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.error(
                "[expert_agent:_detect_with_far_enhancement] OUTPUT_DIR_ERROR | event_id=%d path=%s | %s",
                self.category.event_id,
                output_dir,
                exc,
                exc_info=True,
            )
            return None

        attempted_final = False

        for global_index, frame in enumerate(images):
            try:
                roi_response = self.vlm_engine.call(
                    template=roi_template,
                    images=[frame],
                    context_vars=context_vars,
                    response_schema=_ROI_DETECTION_SCHEMA,
                )
            except FatalAPIError:
                raise
            except Exception as exc:
                logger.warning(
                    "[expert_agent:_detect_with_far_enhancement] ROI_CALL_ERROR | event_id=%d frame=%d | %s",
                    self.category.event_id,
                    global_index,
                    exc,
                )
                continue

            if not roi_response.success or not isinstance(roi_response.parsed_data, dict):
                logger.warning(
                    "[expert_agent:_detect_with_far_enhancement] ROI_PARSE_ERROR | event_id=%d frame=%d success=%s",
                    self.category.event_id,
                    global_index,
                    roi_response.success,
                )
                continue

            parsed = roi_response.parsed_data
            bbox_norm = parsed.get("bbox_norm")
            reason = parsed.get("reason", "")

            if bbox_norm is None:
                logger.info(
                    "[expert_agent:_detect_with_far_enhancement] NO_CANDIDATE | event_id=%d frame=%d reason=%s",
                    self.category.event_id,
                    global_index,
                    reason,
                )
                continue

            if not isinstance(bbox_norm, list) or len(bbox_norm) != 4:
                logger.warning(
                    "[expert_agent:_detect_with_far_enhancement] INVALID_BBOX | event_id=%d frame=%d bbox=%s",
                    self.category.event_id,
                    global_index,
                    bbox_norm,
                )
                continue

            try:
                frame_pil = load_image(frame)
                img_width, img_height = frame_pil.size
                bbox_area = compute_bbox_area_px(bbox_norm, img_width, img_height)
                if not is_bbox_large_enough(bbox_norm, img_width, img_height, min_area_px=80):
                    logger.info(
                        "[expert_agent:_detect_with_far_enhancement] ROI_TOO_SMALL | event_id=%d frame=%d area_px=%d < 80",
                        self.category.event_id,
                        global_index,
                        bbox_area,
                    )
                    continue
            except Exception as exc:
                logger.warning(
                    "[expert_agent:_detect_with_far_enhancement] SIZE_CHECK_ERROR | event_id=%d frame=%d | %s",
                    self.category.event_id,
                    global_index,
                    exc,
                )
                continue

            logger.info(
                "[expert_agent:_detect_with_far_enhancement] FRAME_CANDIDATE | event_id=%d frame=%d area_px=%d reason=%s",
                self.category.event_id,
                global_index,
                bbox_area,
                reason,
            )

            output_path = str(
                output_dir / f"{video_stem}_frame_{global_index}_composite.jpg"
            )
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] COMPOSITE | event_id=%d frame=%d output_path=%s",
                self.category.event_id,
                global_index,
                output_path,
            )

            try:
                create_composite(frame, bbox_norm, output_path=output_path)
            except Exception as exc:
                logger.error(
                    "[expert_agent:_detect_with_far_enhancement] COMPOSITE_ERROR | event_id=%d frame=%d | %s",
                    self.category.event_id,
                    global_index,
                    exc,
                    exc_info=True,
                )
                continue

            try:
                final_response = self.vlm_engine.call(
                    template=template,
                    images=[output_path],
                    context_vars=context_vars,
                    response_schema=_EXPERT_RESPONSE_SCHEMA,
                )
                attempted_final = True
            except FatalAPIError:
                raise
            except Exception as exc:
                logger.error(
                    "[expert_agent:_detect_with_far_enhancement] FINAL_CALL_ERROR | event_id=%d frame=%d | %s",
                    self.category.event_id,
                    global_index,
                    exc,
                    exc_info=True,
                )
                continue

            candidate = parse_expert_response(final_response, self.category)
            if candidate.detected:
                candidate.raw_vlm_response["composite_image_path"] = output_path
                candidate.raw_vlm_response["far_enhancement"] = {
                    "selected_frame_index": global_index,
                    "bbox_norm": bbox_norm,
                    "reason": reason,
                }
                logger.info(
                    "[expert_agent:_detect_with_far_enhancement] COMPLETE | event_id=%d detected=True frame=%d composite=%s",
                    self.category.event_id,
                    global_index,
                    output_path,
                )
                return candidate

            logger.info(
                "[expert_agent:_detect_with_far_enhancement] CLASSIFIER_NEGATIVE | event_id=%d frame=%d composite=%s",
                self.category.event_id,
                global_index,
                output_path,
            )

            # ---- Adjacent-frame motion reflection verification -------------------
            if global_index == len(images) - 1:
                adjacent_index = global_index - 1
            else:
                adjacent_index = global_index + 1
            adjacent_frame = images[adjacent_index]

            motion_composite_path = str(
                output_dir / f"{video_stem}_frame_{global_index}_motion_{adjacent_index}.jpg"
            )
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] MOTION_COMPOSITE | event_id=%d frame=%d adjacent=%d output_path=%s",
                self.category.event_id,
                global_index,
                adjacent_index,
                motion_composite_path,
            )

            try:
                create_motion_comparison_composite(
                    frame, adjacent_frame, bbox_norm, output_path=motion_composite_path
                )
            except Exception as exc:
                logger.error(
                    "[expert_agent:_detect_with_far_enhancement] MOTION_COMPOSITE_ERROR | event_id=%d frame=%d adjacent=%d | %s",
                    self.category.event_id,
                    global_index,
                    adjacent_index,
                    exc,
                    exc_info=True,
                )
                continue

            try:
                motion_template = self.config_manager.get_prompt_template(
                    "far_non_motor_motion_verification"
                )
            except (KeyError, RuntimeError) as exc:
                logger.warning(
                    "[expert_agent:_detect_with_far_enhancement] MOTION_TEMPLATE_ERROR | event_id=%d frame=%d adjacent=%d | %s",
                    self.category.event_id,
                    global_index,
                    adjacent_index,
                    exc,
                )
                continue

            try:
                motion_response = self.vlm_engine.call(
                    template=motion_template,
                    images=[motion_composite_path],
                    context_vars=context_vars,
                    response_schema=_MOTION_VERIFICATION_SCHEMA,
                )
            except FatalAPIError:
                raise
            except Exception as exc:
                logger.error(
                    "[expert_agent:_detect_with_far_enhancement] MOTION_CALL_ERROR | event_id=%d frame=%d adjacent=%d | %s",
                    self.category.event_id,
                    global_index,
                    adjacent_index,
                    exc,
                    exc_info=True,
                )
                continue

            if not motion_response.success or not isinstance(motion_response.parsed_data, dict):
                logger.warning(
                    "[expert_agent:_detect_with_far_enhancement] MOTION_PARSE_ERROR | event_id=%d frame=%d adjacent=%d success=%s",
                    self.category.event_id,
                    global_index,
                    adjacent_index,
                    motion_response.success,
                )
                continue

            motion_data = motion_response.parsed_data
            is_moving = bool(motion_data.get("is_moving", False))
            motion_reason = str(motion_data.get("reason", ""))

            if not is_moving:
                logger.info(
                    "[expert_agent:_detect_with_far_enhancement] MOTION_REJECT | event_id=%d frame=%d adjacent=%d reason=%s",
                    self.category.event_id,
                    global_index,
                    adjacent_index,
                    motion_reason,
                )
                continue

            raw_response = dict(candidate.raw_vlm_response)
            raw_response["composite_image_path"] = output_path
            raw_response["far_enhancement"] = {
                "selected_frame_index": global_index,
                "bbox_norm": bbox_norm,
                "reason": reason,
            }
            raw_response["motion_verification"] = {
                "is_moving": True,
                "reason": motion_reason,
            }
            raw_response["motion_composite_image_path"] = motion_composite_path

            # Build a concrete instance so that adjudication sees supporting evidence
            # rather than an empty instance list.
            instance = EventInstance(
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                start_time_sec=0.0,
                end_time_sec=0.0,
                evidence_frames=[global_index, adjacent_index],
                description=f"第 {global_index} 帧红色方框内发现远距离小型移动目标，经与第 {adjacent_index} 帧同一位置对比，目标发生位移：{motion_reason}",
                reasoning=f"ROI 检测在 frame={global_index} 发现疑似非机动车候选；最终分类器因目标模糊 initially 返回负例。经相邻帧运动反射验证，目标在两帧间发生移动（{motion_reason}），排除静态路面设施/反光点/护栏阴影等误报源，判定为移动中的摩托车/非机动车。",
                confidence_level="low",
            )
            motion_candidate = EventCandidate(
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                detected=True,
                summary=f"检测到摩托车/非机动车：经相邻帧运动反射验证，frame={global_index} 与 frame={adjacent_index} 中红色方框内目标发生移动，疑似远距离两轮/三轮车辆，位于道路区域。",
                instances=[instance],
                raw_vlm_response=raw_response,
                raw_vlm_text=motion_response.raw_text,
            )
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] MOTION_ACCEPT | event_id=%d frame=%d adjacent=%d reason=%s",
                self.category.event_id,
                global_index,
                adjacent_index,
                motion_reason,
            )
            return motion_candidate

        logger.info(
            "[expert_agent:_detect_with_far_enhancement] NO_VALID_CANDIDATES | event_id=%d",
            self.category.event_id,
        )
        # If at least one composite was generated and classified as negative, we have
        # exhausted the enhancement path and should report detected=False. Otherwise,
        # no valid bbox candidate was ever found, so fall back to the original path.
        if attempted_final:
            return EventCandidate(
                detected=False,
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                summary="未检测到非机动车及两轮/三轮异常车辆。",
            )
        return None

    def _execute_tool_calls(
        self,
        response: Any,
        context: AnalysisContext,
        images: List[Any],
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Parse tool calls from VLM response and execute them.

        Returns:
            (tool_result_dict, annotated_image_path) or (None, None) if no tool call.
        """
        if not response or not hasattr(response, "raw_text"):
            return None, None

        raw_text = response.raw_text or ""
        if "<tool_call>" not in raw_text:
            return None, None

        # Try to import tool router
        try:
            from traffic_analyzer.tools.tool_router import ToolRouter, ToolRequest, ToolResponse
            from traffic_analyzer.tools.tool_registry import get_default_router
        except ImportError as exc:
            logger.warning(
                "[expert_agent:_execute_tool_calls] TOOL_ROUTER_NOT_AVAILABLE | %s",
                exc,
            )
            return None, None

        router = get_default_router()
        if router is None:
            logger.warning(
                "[expert_agent:_execute_tool_calls] DEFAULT_ROUTER_NOT_INITIALIZED"
            )
            return None, None

        # Parse tool request from raw_text
        try:
            tool_request = ToolRequest.from_json(raw_text)
        except Exception as exc:
            logger.warning(
                "[expert_agent:_execute_tool_calls] PARSE_FAILED | %s",
                exc,
            )
            return None, None

        # Check if the requested tool is in allowed tools list
        if tool_request.tool_name not in self.category.tools:
            logger.warning(
                "[expert_agent:_execute_tool_calls] TOOL_NOT_ALLOWED | "
                "requested=%s allowed=%s",
                tool_request.tool_name,
                self.category.tools,
            )
            return None, None

        # Auto-fill video_path — always use the actual video being analyzed
        if "video_path" in tool_request.arguments and context.video_meta is not None:
            tool_request.arguments["video_path"] = context.video_meta.file_path
            logger.debug(
                "[expert_agent:_execute_tool_calls] AUTO_FILL_VIDEO_PATH | %s",
                context.video_meta.file_path,
            )

        # Execute tool
        try:
            tool_response = router.route(tool_request)
        except Exception as exc:
            logger.error(
                "[expert_agent:_execute_tool_calls] ROUTE_FAILED | tool=%s | %s",
                tool_request.tool_name,
                exc,
                exc_info=True,
            )
            return None, None

        if not tool_response.success:
            logger.warning(
                "[expert_agent:_execute_tool_calls] TOOL_FAILED | tool=%s error=%s",
                tool_request.tool_name,
                tool_response.error,
            )
            return None, None

        # Extract annotated image path and tracking data
        result_data = tool_response.data or {}
        annotated_image = result_data.get("annotated_image_path")

        # Build tracking text for prompt injection
        tracking_text = self._format_tracking_result(result_data)

        logger.info(
            "[expert_agent:_execute_tool_calls] SUCCESS | tool=%s | "
            "annotated_image=%s displacements=%d",
            tool_request.tool_name,
            annotated_image,
            len(result_data.get("displacements", [])),
        )

        return {
            "tool_name": tool_request.tool_name,
            "result": result_data,
            "tracking_text": tracking_text,
        }, annotated_image

    def _execute_native_tool_calls(
        self,
        template: Any,
        images: List[Any],
        context_vars: Dict[str, Any],
        context: AnalysisContext,
    ) -> Optional[Tuple[Dict[str, Any], Optional[str]]]:
        """
        Execute tool calls using Anthropic Native API.
        
        Returns:
            (tool_result_dict, annotated_image_path) or None if failed/no tool call.
        """
        try:
            from traffic_analyzer.tools.tool_registry import get_default_router
            from traffic_analyzer.tools.tool_schema import ToolDefinition
        except ImportError as exc:
            logger.debug(
                "[expert_agent:_execute_native_tool_calls] IMPORT_FAILED | %s",
                exc,
            )
            return None
        
        # Get tool definitions for configured tools
        router = get_default_router()
        if router is None:
            logger.debug(
                "[expert_agent:_execute_native_tool_calls] NO_ROUTER"
            )
            return None
        
        tool_definitions = []
        for tool_name in self.category.tools:
            tool_def = router.get_tool(tool_name)
            if tool_def is None:
                logger.warning(
                    "[expert_agent:_execute_native_tool_calls] TOOL_NOT_FOUND | tool=%s",
                    tool_name,
                )
                continue
            tool_definitions.append(tool_def.to_anthropic())
        
        if not tool_definitions:
            logger.debug(
                "[expert_agent:_execute_native_tool_calls] NO_TOOL_DEFS"
            )
            return None
        
        # First call with tools
        try:
            first_response, tool_uses = self.vlm_engine.call_with_tools(
                template=template,
                images=images,
                tool_definitions=tool_definitions,
                context_vars=context_vars,
                response_schema=_EXPERT_RESPONSE_SCHEMA,
            )
        except Exception as exc:
            logger.error(
                "[expert_agent:_execute_native_tool_calls] FIRST_CALL_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
                exc_info=True,
            )
            return None
        
        if not tool_uses:
            logger.debug(
                "[expert_agent:_execute_native_tool_calls] NO_TOOL_USES"
            )
            return None
        
        # Track the last successful tool name for fallback
        last_tool_name = ""
        
        # Execute each tool
        tool_results = []
        all_result_data = {}
        annotated_image = None
        
        for tool_use in tool_uses:
            tool_name = tool_use.get("name", "")
            tool_id = tool_use.get("id", "")
            tool_input = tool_use.get("input", {})
            
            if tool_name not in self.category.tools:
                logger.warning(
                    "[expert_agent:_execute_native_tool_calls] TOOL_NOT_ALLOWED | "
                    "requested=%s allowed=%s",
                    tool_name,
                    self.category.tools,
                )
                continue
            
            last_tool_name = tool_name

            # Auto-fill video_path — always use the actual video being analyzed
            if "video_path" in tool_input and context.video_meta is not None:
                tool_input["video_path"] = context.video_meta.file_path

            # Execute tool
            try:
                from traffic_analyzer.tools.tool_router import ToolRequest
                tool_request = ToolRequest(
                    tool_name=tool_name,
                    arguments=tool_input,
                )
                tool_response = router.route(tool_request)
            except Exception as exc:
                logger.error(
                    "[expert_agent:_execute_native_tool_calls] EXECUTE_FAILED | tool=%s | %s",
                    tool_name,
                    exc,
                    exc_info=True,
                )
                continue
            
            if not tool_response.success:
                logger.warning(
                    "[expert_agent:_execute_native_tool_calls] TOOL_FAILED | tool=%s error=%s",
                    tool_name,
                    tool_response.error,
                )
                continue
            
            result_data = tool_response.data or {}
            all_result_data = result_data
            annotated_image = result_data.get("annotated_image_path")
            
            # Format result for VLM
            tracking_text = self._format_tracking_result(result_data)
            tool_results.append({
                "tool_use_id": tool_id,
                "content": tracking_text,
            })
            
            logger.info(
                "[expert_agent:_execute_native_tool_calls] TOOL_SUCCESS | tool=%s | "
                "annotated_image=%s displacements=%d",
                tool_name,
                annotated_image,
                len(result_data.get("displacements", [])),
            )
        
        if not tool_results:
            logger.debug(
                "[expert_agent:_execute_native_tool_calls] NO_SUCCESSFUL_TOOLS"
            )
            return None
        
        # Second call with tool results
        try:
            # Build previous messages from first call
            system_prompt, user_prompt = self.vlm_engine.render_prompt(template, context_vars)
            
            # Import the build function from vlm_engine module
            from traffic_analyzer.core.vlm_engine import _build_anthropic_payload
            _, kwargs = _build_anthropic_payload(
                system_prompt,
                user_prompt,
                images,
                self.vlm_engine.config.model,
                self.vlm_engine.config.max_tokens,
                self.vlm_engine.config.temperature,
            )
            kwargs["tools"] = tool_definitions
            kwargs["tool_choice"] = {"type": "auto"}
            
            # Get messages from first call
            previous_messages = kwargs.get("messages", [])
            
            second_response = self.vlm_engine.call_with_tool_results(
                template=template,
                images=images,
                previous_messages=previous_messages,
                tool_results=tool_results,
                context_vars=context_vars,
                response_schema=_EXPERT_RESPONSE_SCHEMA,
            )
        except Exception as exc:
            logger.error(
                "[expert_agent:_execute_native_tool_calls] SECOND_CALL_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
                exc_info=True,
            )
            # Fallback: return first tool result without second call
            tracking_text = self._format_tracking_result(all_result_data)
            return {
                "tool_name": last_tool_name,
                "result": all_result_data,
                "tracking_text": tracking_text,
            }, annotated_image
        
        # Parse second response
        if second_response.success:
            logger.info(
                "[expert_agent:_execute_native_tool_calls] SECOND_CALL_SUCCESS | event_id=%d",
                self.category.event_id,
            )
        
        tracking_text = self._format_tracking_result(all_result_data)
        return {
            "tool_name": last_tool_name,
            "result": all_result_data,
            "tracking_text": tracking_text,
        }, annotated_image

    def _execute_anthropic_native_tools(
        self,
        template: Any,
        images: List[Any],
        context_vars: Dict[str, Any],
        context: AnalysisContext,
    ) -> Optional[Tuple[Dict[str, Any], Optional[str]]]:
        """
        Execute tool calls using Anthropic Native API directly.
        
        Returns:
            (tool_result_dict, annotated_image_path) or None if failed/no tool call.
        """
        import anthropic
        from traffic_analyzer.tools.tool_registry import get_default_router
        from traffic_analyzer.tools.tool_router import ToolRequest
        
        # Get tool definitions
        router = get_default_router()
        if router is None:
            logger.debug("[expert_agent:_execute_anthropic_native_tools] NO_ROUTER")
            return None
        
        tool_definitions = []
        for tool_name in self.category.tools:
            tool_def = router.get_tool(tool_name)
            if tool_def is None:
                logger.warning("[expert_agent:_execute_anthropic_native_tools] TOOL_NOT_FOUND | tool=%s", tool_name)
                continue
            tool_definitions.append(tool_def.to_anthropic())
        
        if not tool_definitions:
            logger.debug("[expert_agent:_execute_anthropic_native_tools] NO_TOOL_DEFS")
            return None
        
        # Render prompt
        system_prompt, user_prompt = self.vlm_engine.render_prompt(template, context_vars)
        
        # Build messages with images
        messages = []
        content = []
        if user_prompt:
            content.append({"type": "text", "text": user_prompt})
        for img in images:
            # Encode image to base64
            from traffic_analyzer.core.vlm_engine import _encode_image_to_base64
            b64_uri = _encode_image_to_base64(img)
            b64_data = b64_uri.split(",", 1)[1]
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": b64_data,
                },
            })
        messages.append({"role": "user", "content": content})
        
        # First call with tools
        client = self.vlm_engine._client
        if client is None:
            logger.error("[expert_agent:_execute_anthropic_native_tools] NO_CLIENT")
            return None
        
        try:
            response = client.messages.create(
                model=self.vlm_engine.config.model,
                max_tokens=self.vlm_engine.config.max_tokens,
                temperature=self.vlm_engine.config.temperature,
                system=system_prompt,
                messages=messages,
                tools=tool_definitions,
                tool_choice={"type": "auto"},
            )
        except Exception as exc:
            logger.error(
                "[expert_agent:_execute_anthropic_native_tools] FIRST_CALL_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
            )
            return None
        
        # Extract tool uses (standard Anthropic API)
        tool_uses = []
        raw_text = ""
        
        # Check stop_reason first (standard Anthropic pattern)
        if getattr(response, "stop_reason", None) == "tool_use":
            for block in response.content:
                if getattr(block, "type", None) == "tool_use":
                    tool_uses.append({
                        "name": getattr(block, "name", ""),
                        "id": getattr(block, "id", ""),
                        "input": getattr(block, "input", {}),
                    })
                elif getattr(block, "type", None) == "text":
                    raw_text += block.text
        else:
            # No native tool_use — collect text for fallback parsing
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    raw_text += block.text
        
        # Fallback: parse <tool_call> from text if native tool_use not available
        if not tool_uses and "<tool_call>" in raw_text:
            import re
            tool_call_match = re.search(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', raw_text, re.DOTALL)
            if tool_call_match:
                try:
                    tool_json = json.loads(tool_call_match.group(1))
                    tool_uses.append({
                        "name": tool_json.get("tool_name", ""),
                        "id": "tool_call_from_text",
                        "input": tool_json.get("arguments", {}),
                    })
                    logger.info(
                        "[expert_agent:_execute_anthropic_native_tools] PARSED_FROM_TEXT | tool=%s",
                        tool_json.get("tool_name"),
                    )
                except Exception as exc:
                    logger.warning("JSON_PARSE_FAILED | %s", exc)
        
        if not tool_uses:
            logger.debug(
                "[expert_agent:_execute_anthropic_native_tools] NO_TOOL_USES | stop_reason=%s",
                getattr(response, "stop_reason", "unknown"),
            )
            return None
        
        logger.info(
            "[expert_agent:_execute_anthropic_native_tools] TOOL_USES_FOUND | count=%d",
            len(tool_uses),
        )
        
        # Execute tools
        tool_results_for_vlm = []
        all_result_data = {}
        annotated_image = None
        last_tool_name = ""
        
        for tool_use in tool_uses:
            tool_name = tool_use["name"]
            tool_id = tool_use["id"]
            tool_input = tool_use["input"]
            
            if tool_name not in self.category.tools:
                logger.warning("TOOL_NOT_ALLOWED | requested=%s allowed=%s", tool_name, self.category.tools)
                continue
            
            last_tool_name = tool_name

            # Auto-fill video_path — always use the actual video being analyzed
            if "video_path" in tool_input and context.video_meta is not None:
                tool_input["video_path"] = context.video_meta.file_path

            # Execute
            try:
                tool_request = ToolRequest(tool_name=tool_name, arguments=tool_input)
                tool_response = router.route(tool_request)
            except Exception as exc:
                logger.error("EXECUTE_FAILED | tool=%s | %s", tool_name, exc)
                continue
            
            if not tool_response.success:
                logger.warning("TOOL_FAILED | tool=%s error=%s", tool_name, tool_response.error)
                continue
            
            result_data = tool_response.data or {}
            all_result_data = result_data
            annotated_image = result_data.get("annotated_image_path")
            
            tracking_text = self._format_tracking_result(result_data)
            tool_results_for_vlm.append({
                "tool_use_id": tool_id,
                "content": tracking_text,
            })
            
            logger.info(
                "[expert_agent:_execute_anthropic_native_tools] TOOL_SUCCESS | tool=%s displacements=%d",
                tool_name,
                len(result_data.get("displacements", [])),
            )
        
        if not tool_results_for_vlm:
            return None
        
        # Second call with tool results
        # Build message history: user -> assistant (tool_use) -> user (tool_result)
        second_messages = list(messages)
        
        # Add assistant message with tool_use
        assistant_content = []
        for tu in tool_uses:
            assistant_content.append({
                "type": "tool_use",
                "id": tu["id"],
                "name": tu["name"],
                "input": tu["input"],
            })
        second_messages.append({"role": "assistant", "content": assistant_content})
        
        # Add user message with tool_result
        user_tool_content = []
        for tr in tool_results_for_vlm:
            user_tool_content.append({
                "type": "tool_result",
                "tool_use_id": tr["tool_use_id"],
                "content": tr["content"],
            })
        second_messages.append({"role": "user", "content": user_tool_content})
        
        try:
            second_response = client.messages.create(
                model=self.vlm_engine.config.model,
                max_tokens=self.vlm_engine.config.max_tokens,
                temperature=self.vlm_engine.config.temperature,
                system=system_prompt,
                messages=second_messages,
                tools=tool_definitions,
            )
        except Exception as exc:
            logger.error(
                "[expert_agent:_execute_anthropic_native_tools] SECOND_CALL_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
            )
            tracking_text = self._format_tracking_result(all_result_data)
            return {
                "tool_name": last_tool_name,
                "result": all_result_data,
                "tracking_text": tracking_text,
            }, annotated_image
        
        # Parse second response
        second_text = ""
        for block in second_response.content:
            if getattr(block, "type", None) == "text":
                second_text += block.text
        
        logger.info(
            "[expert_agent:_execute_anthropic_native_tools] SECOND_CALL_SUCCESS | event_id=%d text_len=%d",
            self.category.event_id,
            len(second_text),
        )
        
        # Try to parse JSON from second response
        from traffic_analyzer.core.vlm_engine import _extract_json_from_text
        try:
            parsed = _extract_json_from_text(second_text)
            logger.debug("JSON_PARSED | detected=%s", parsed.get("detected", "unknown"))
        except Exception as exc:
            logger.debug("JSON_PARSE_FAILED | %s", exc)
        
        tracking_text = self._format_tracking_result(all_result_data)
        return {
            "tool_name": last_tool_name,
            "result": all_result_data,
            "tracking_text": tracking_text,
        }, annotated_image

    def _format_tracking_result(self, result_data: Dict[str, Any]) -> str:
        """Format tracking result into human-readable text for prompt injection."""
        lines = ["=== 跟踪结果 ===", ""]

        displacements = result_data.get("displacements", [])
        if not displacements:
            lines.append("未检测到车辆跟踪数据。")
            return "\n".join(lines)

        lines.append(f"共跟踪到 {len(displacements)} 辆车：")
        lines.append("")

        for disp in displacements:
            track_id = disp.get("track_id", "?")
            distance = disp.get("distance_pixels", 0)
            is_stationary = disp.get("is_stationary", False)
            stationary_str = "静止" if is_stationary else "移动"

            lines.append(
                f"  track_id={track_id}: {stationary_str}, 总位移={distance:.1f}px"
            )

        lines.append("")
        lines.append("附带的标注图可直接对照图上信息判断。")

        return "\n".join(lines)

    def _second_vlm_call(
        self,
        template: PromptTemplate,
        first_response: Any,
        tool_result: Dict[str, Any],
        annotated_image: Optional[str],
        images: List[Any],
        context_vars: Dict[str, Any],
    ) -> EventCandidate:
        """Perform second VLM call with tool results injected.

        Light-weight context: includes first response summary + tool results.
        """
        # Build enhanced prompt with tool results
        enhanced_user = template.user_prompt or ""

        # Add first response context (light-weight)
        first_text = first_response.raw_text if hasattr(first_response, "raw_text") else ""
        context_section = (
            "\n\n============================================================\n"
            "上下文 — 第一次分析结论\n"
            "============================================================\n"
            f"{first_text[:500]}...\n"
            "\n【任务】将上述描述的车辆与标注图上的信息匹配，判断是否为逆行。"
        )

        # Add tool results
        tool_section = (
            "\n\n============================================================\n"
            "工具调用结果 — 跟踪数据\n"
            "============================================================\n"
            f"{tool_result['tracking_text']}\n"
            "\n【重要】基于以上跟踪数据，重新判断并输出 JSON。必须包含 detected 字段。"
        )

        enhanced_user += context_section + tool_section

        # Build second template
        second_template = PromptTemplate(
            template_id=template.template_id,
            name=template.name,
            version=template.version,
            system_prompt=template.system_prompt,
            user_prompt=enhanced_user,
            output_format_hint=template.output_format_hint,
            example_input=template.example_input,
            example_output=template.example_output,
            traffic_percentage=template.traffic_percentage,
            available_tools=[],  # Don't show tools again in second call
        )

        # Build image list: annotated image + first/last original frames for context
        second_images = []
        if images:
            # Add first and last frame for temporal context
            second_images.append(images[0])
            if len(images) > 1:
                second_images.append(images[-1])
        if annotated_image:
            second_images.append(annotated_image)
            logger.debug(
                "[expert_agent:_second_vlm_call] ADDED_ANNOTATED_IMAGE | path=%s",
                annotated_image,
            )

        # Second VLM call
        try:
            second_response = self.vlm_engine.call(
                template=second_template,
                images=second_images,
                context_vars=context_vars,
                response_schema=_EXPERT_RESPONSE_SCHEMA,
            )
        except Exception as exc:
            logger.error(
                "[expert_agent:_second_vlm_call] SECOND_VLM_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
                exc_info=True,
            )
            # Fallback to first response
            return parse_expert_response(first_response, self.category)

        # Parse second response
        candidate = parse_expert_response(second_response, self.category)
        
        # Store tool results for report generation
        candidate.tool_results = [tool_result] if tool_result else []
        
        # Build raw_vlm_text (handle None first_response for Native API path)
        first_text = getattr(first_response, 'raw_text', '[Native API tool call]') if first_response else '[Native API tool call]'
        candidate.raw_vlm_text = (
            f"[First call]\n{first_text}\n\n"
            f"[Second call with tool results]\n{second_response.raw_text}"
        )
        logger.info(
            "[expert_agent:_second_vlm_call] COMPLETE | event_id=%d detected=%s",
            self.category.event_id,
            candidate.detected,
        )
        return candidate
