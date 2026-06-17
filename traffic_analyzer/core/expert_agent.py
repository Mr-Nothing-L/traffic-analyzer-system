"""ExpertAgent — single-event detection agent.

Each ExpertAgent is responsible for detecting exactly one event category.
It reports what it sees (fact identification) without any filtering or
exclusion logic. Adjudication happens later in the pipeline.
"""

from __future__ import annotations

import logging
import json
import re
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
    compute_bbox_aspect_ratio,
    compute_roi_motion_score,
    create_composite,
    create_motion_comparison_composite,
    is_bbox_aspect_valid_for_non_motor,
    is_bbox_large_enough,
    load_image,
)

logger = logging.getLogger(__name__)

# Directory where far-distance non-motor vehicle composite images are saved.
# Kept relative to the project root so it works across local dev, CI and Docker.
_FAR_ENHANCEMENT_OUTPUT_DIR = Path("./output/tmp_img")

# Number of top-ranked ROI candidates to classify in the second pass.
_FAR_ENHANCEMENT_TOP_K = 2

# Motion-filtering parameters for the far-distance non-motor vehicle ROI
# collection stage.  A candidate whose adjacent-frame difference is too low
# receives a large scoring penalty so that static foreground objects (cameras,
# poles, brackets) are unlikely to survive into the top-K set.  No diff images
# are written to disk; only the scalar motion metrics are retained.
_FAR_MOTION_ENLARGE_SCALE = 3.0
_FAR_MOTION_GAUSSIAN_KERNEL = (3, 3)
_FAR_MOTION_PIXEL_THRESHOLD = 8.0
# A pixel is considered "changed" when its grayscale abs-diff exceeds the
# pixel threshold.  The combined motion_score = mean_diff + fraction * 100.
_FAR_MOTION_SCORE_THRESHOLD = 1.0
_FAR_MOTION_PENALTY = 5.0

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
        "occluded": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
}

# JSON schema for the far-distance non-motor vehicle final classifier.
# This is intentionally separate from the shared _EXPERT_RESPONSE_SCHEMA because
# the final non-motor classifier returns a minimal {detected, reason} object.
_NON_MOTOR_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["detected"],
    "properties": {
        "detected": {"type": "boolean"},
        "reason": {"type": "string"},
    },
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

    def _is_explicitly_car_reasoning(self, reason: str) -> bool:
        """判断 reason 文本是否明确说明目标是汽车/四轮车。"""
        if not reason:
            return False
        lower = reason.lower()
        car_keywords = [
            "汽车",
            "轿车",
            "suv",
            "货车",
            "客车",
            "面包车",
            "四轮车",
            "四轮机动车",
            "已驶离",
            "vehicle has left",
        ]
        return any(keyword in lower for keyword in car_keywords)

    # Pattern matching phrases that indicate the ROI lacks identifiable
    # vehicle-structure evidence (no wheels, handlebars, rider, lights, etc.).
    _NO_STRUCTURE_RE = re.compile(
        r"(无|没有)(结构|明确|可辨识|清晰|具体|车轮|车把|车灯|车牌|骑乘|骑手|头盔|车身|车辆|非机动车|摩托车|两轮|三轮|轮廓|明显)|"
        r"轮廓不清|无明显|"
        r"无法(确认|辨认|识别|判断|确定|提供)|"
        r"不能(确认|辨认|识别|判断|确定)|"
        r"看不清|看不见|看不到|看不出|"
        r"(仅|只|仅为)(是|为|能看到|是一个|一个|一团|一块)|"
        r"暗斑|黑块|阴影|模糊色块",
        re.IGNORECASE,
    )

    def _is_no_structure_reasoning(self, reason: str) -> bool:
        """判断 reason 文本是否说明框内没有可辨识的车辆结构证据。"""
        return bool(reason) and bool(self._NO_STRUCTURE_RE.search(reason))

    def _build_far_candidate(
        self,
        frame_info: Dict[str, Any],
        reason: str,
        frame_analysis_log: List[Dict[str, Any]],
        raw_text: Optional[str] = None,
        fallback: bool = False,
    ) -> EventCandidate:
        """Build a positive EventCandidate from a far-enhancement frame_info dict."""
        global_index = frame_info["global_index"]
        adjacent_index = frame_info["adjacent_index"]
        bbox_norm = frame_info["bbox_norm"]
        composite_ref = frame_info["composite_ref"]
        motion_composite_ref = frame_info["motion_composite_ref"]

        far_enhancement: Dict[str, Any] = {
            "selected_frame_index": global_index,
            "bbox_norm": bbox_norm,
            "reason": reason,
            "frame_analysis_log": frame_analysis_log,
        }
        if fallback:
            far_enhancement["fallback"] = True

        raw_text = raw_text if raw_text is not None else reason
        return EventCandidate(
            event_id=self.category.event_id,
            event_name=self.category.name_zh,
            detected=True,
            summary=f"检测到摩托车/非机动车：{reason}",
            instances=[
                EventInstance(
                    event_id=self.category.event_id,
                    event_name=self.category.name_zh,
                    start_time_sec=0.0,
                    end_time_sec=0.0,
                    evidence_frames=[global_index, adjacent_index],
                    description=reason,
                    reasoning=reason,
                )
            ],
            raw_vlm_response={
                "composite_image_path": composite_ref,
                "motion_composite_image_path": motion_composite_ref,
                "far_enhancement": far_enhancement,
            },
            raw_vlm_text=raw_text,
        )

    def _run_final_classifier(
        self,
        frame_info: Dict[str, Any],
        template: PromptTemplate,
        context_vars: Dict[str, Any],
    ) -> Optional[EventCandidate]:
        """Run the final non-motor classifier on a candidate's composites."""
        try:
            response = self.vlm_engine.call(
                template=template,
                images=[
                    frame_info["composite_path"],
                    frame_info["motion_composite_path"],
                ],
                context_vars=context_vars,
                response_schema=_NON_MOTOR_RESPONSE_SCHEMA,
            )
        except FatalAPIError:
            raise
        except Exception as exc:
            logger.error(
                "[expert_agent:_detect_with_far_enhancement] FINAL_CALL_ERROR | event_id=%d frame=%d | %s",
                self.category.event_id,
                frame_info["global_index"],
                exc,
                exc_info=True,
            )
            return None

        if not response.success or not isinstance(response.parsed_data, dict):
            logger.warning(
                "[expert_agent:_detect_with_far_enhancement] FINAL_PARSE_ERROR | event_id=%d frame=%d success=%s",
                self.category.event_id,
                frame_info["global_index"],
                response.success,
            )
            return None

        detected = bool(response.parsed_data.get("detected", False))
        final_reason = str(response.parsed_data.get("reason", ""))

        if detected and self._is_explicitly_car_reasoning(final_reason):
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] CAR_OVERRIDDEN | event_id=%d frame=%d reason=%s",
                self.category.event_id,
                frame_info["global_index"],
                final_reason,
            )
            detected = False

        if detected:
            return self._build_far_candidate(
                frame_info,
                final_reason,
                frame_info.get("frame_analysis_log", []),
                raw_text=response.raw_text,
            )
        # Preserve the negative classifier reason so fallback logic can still
        # apply the car-semantic veto.
        frame_info["negative_final_reason"] = final_reason
        return None

    def _accept_fallback(
        self,
        frame_info: Dict[str, Any],
        frame_analysis_log: List[Dict[str, Any]],
    ) -> Optional[EventCandidate]:
        """Promote a previously negative candidate to detected=True if safe."""
        if frame_info.get("occluded"):
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] FALLBACK_REJECT_OCCLUDED | event_id=%d frame=%d",
                self.category.event_id,
                frame_info["global_index"],
            )
            return None
        confidence = self._parse_roi_confidence(frame_info.get("confidence", 0.0))
        if confidence < 0.5:
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] FALLBACK_REJECT_CONFIDENCE | event_id=%d frame=%d confidence=%s",
                self.category.event_id,
                frame_info["global_index"],
                confidence,
            )
            return None
        # Apply car-semantic veto to the classifier's negative reasoning. The
        # ROI reason may legitimately mention surrounding cars for comparison,
        # so only the final classifier's explicit car description is used here.
        negative_reason = str(frame_info.get("negative_final_reason", ""))
        if self._is_explicitly_car_reasoning(negative_reason):
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] FALLBACK_REJECT_CAR | event_id=%d frame=%d negative_reason=%s",
                self.category.event_id,
                frame_info["global_index"],
                negative_reason,
            )
            return None
        if self._is_no_structure_reasoning(negative_reason):
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] FALLBACK_REJECT_NO_STRUCTURE | event_id=%d frame=%d negative_reason=%s",
                self.category.event_id,
                frame_info["global_index"],
                negative_reason,
            )
            return None
        # Use a confident, self-consistent summary so the adjudication layer's
        # self-consistency check does not downgrade a fallback positive.
        fallback_reason = (
            f"检测到远距离非机动车。第{frame_info['global_index']}帧红色方框内目标位于道路区域，"
            f"尺寸与宽高比符合摩托车、电动车、自行车或三轮车特征。"
        )
        return self._build_far_candidate(
            frame_info,
            fallback_reason,
            frame_analysis_log,
            raw_text=fallback_reason,
            fallback=True,
        )

    @staticmethod
    def _parse_roi_confidence(value: Any) -> float:
        """Normalize ROI confidence to a 0-1 float, handling string legacy values."""
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            legacy_map = {"high": 0.85, "medium": 0.55, "low": 0.15}
            return legacy_map.get(value.lower(), 0.0)
        return 0.0

    def _score_far_candidates(
        self,
        candidates: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Rank ROI candidates by confidence, area, aspect, occlusion and motion."""
        if not candidates:
            return []
        max_area = max(c["area_px"] for c in candidates)
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for candidate in candidates:
            conf = 3.0 * self._parse_roi_confidence(candidate.get("confidence", 0.0))
            area_score = (
                candidate["area_px"] / max_area if max_area > 0 else 0.0
            )
            aspect_penalty = max(0.0, candidate["aspect_ratio"] - 1.0)
            occlusion_penalty = 2.0 if candidate.get("occluded") else 0.0
            motion_score = float(
                candidate.get("motion_score", {}).get("motion_score", 0.0)
            )
            motion_penalty = (
                _FAR_MOTION_PENALTY
                if motion_score < _FAR_MOTION_SCORE_THRESHOLD
                else 0.0
            )
            score = (
                conf
                + area_score
                - aspect_penalty
                - occlusion_penalty
                - motion_penalty
            )
            candidate["score"] = score
            scored.append((score, candidate))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [candidate for _, candidate in scored]

    def _log_frame(
        self,
        frame_analysis_log: List[Dict[str, Any]],
        frame_log: Dict[str, Any],
        reason: Optional[str] = None,
    ) -> None:
        """Append a frame log, optionally setting its reason first."""
        if reason is not None:
            frame_log["reason"] = reason
        frame_analysis_log.append(frame_log)

    def _detect_with_far_enhancement(
        self,
        context: AnalysisContext,
        images: List[Any],
        template: PromptTemplate,
        context_vars: Dict[str, Any],
    ) -> Optional[EventCandidate]:
        """Run the far-distance non-motor vehicle enhancement flow.

        Two-pass design:
        1. Collect ROI candidates from every input frame. Valid ROIs must pass
           the existing minimum-area check (>=80 px) and aspect-ratio filter
           (width/height < 1.2).
        2. Score all candidates and keep the top ``_FAR_ENHANCEMENT_TOP_K``.
        3. For each top candidate generate the dual composites
           (single-frame + motion-comparison) and run the final classifier.
        4. Return the highest-scoring positive result. If none of the top-K
           candidates is positive, return detected=False. An optional fallback
           promotion is applied to the highest-scored candidate when it is safe
           (not occluded, high/medium confidence, not explicitly a car).

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

        # When the orchestrator knows where the report will be written, place
        # composites next to the report and reference them with a relative path
        # so markdown viewers can resolve the image. Otherwise fall back to the
        # project-root default for backward compatibility.
        report_output_dir = getattr(context, "output_dir", None)
        if report_output_dir:
            output_dir = Path(report_output_dir) / "tmp_img"
            image_ref_prefix = "tmp_img"
        else:
            output_dir = _FAR_ENHANCEMENT_OUTPUT_DIR
            image_ref_prefix = str(_FAR_ENHANCEMENT_OUTPUT_DIR)
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

        # Per-frame ROI analysis log, attached to every EventCandidate produced by
        # this flow so the report can render a frame-by-frame ROI summary.
        frame_analysis_log: List[Dict[str, Any]] = []

        # ------------------------------------------------------------------
        # First pass: collect all valid ROI candidates and a per-frame log.
        # ------------------------------------------------------------------
        candidates: List[Dict[str, Any]] = []
        for global_index, frame in enumerate(images):
            frame_log: Dict[str, Any] = {
                "frame": global_index,
                "has_candidate": False,
                "bbox_norm": None,
                "area_px": None,
                "aspect_ratio": None,
                "confidence": None,
                "motion_score": None,
                "reason": "",
            }

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
                self._log_frame(
                    frame_analysis_log,
                    frame_log,
                    reason=f"ROI detection failed: {exc}",
                )
                logger.warning(
                    "[expert_agent:_detect_with_far_enhancement] ROI_CALL_ERROR | event_id=%d frame=%d | %s",
                    self.category.event_id,
                    global_index,
                    exc,
                )
                continue

            if not roi_response.success or not isinstance(
                roi_response.parsed_data, dict
            ):
                self._log_frame(
                    frame_analysis_log,
                    frame_log,
                    reason="ROI response parsing failed",
                )
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
            occluded = bool(parsed.get("occluded", False))
            confidence = self._parse_roi_confidence(parsed.get("confidence", 0.0))

            if bbox_norm is None:
                self._log_frame(
                    frame_analysis_log,
                    frame_log,
                    reason=reason or "ROI returned no candidate",
                )
                logger.info(
                    "[expert_agent:_detect_with_far_enhancement] NO_CANDIDATE | event_id=%d frame=%d reason=%s",
                    self.category.event_id,
                    global_index,
                    reason,
                )
                continue

            if not isinstance(bbox_norm, list) or len(bbox_norm) != 4:
                self._log_frame(
                    frame_analysis_log,
                    frame_log,
                    reason=f"invalid bbox from ROI: {bbox_norm}",
                )
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
                aspect_ratio = compute_bbox_aspect_ratio(bbox_norm)
                if not is_bbox_large_enough(
                    bbox_norm, img_width, img_height, min_area_px=80
                ):
                    self._log_frame(
                        frame_analysis_log,
                        frame_log,
                        reason=f"ROI candidate too small: area_px={bbox_area} < 80",
                    )
                    logger.info(
                        "[expert_agent:_detect_with_far_enhancement] ROI_TOO_SMALL | event_id=%d frame=%d area_px=%d < 80",
                        self.category.event_id,
                        global_index,
                        bbox_area,
                    )
                    continue
                if not is_bbox_aspect_valid_for_non_motor(
                    bbox_norm, max_ratio=1.2
                ):
                    self._log_frame(
                        frame_analysis_log,
                        frame_log,
                        reason=f"ROI candidate aspect ratio rejected: {aspect_ratio:.2f}",
                    )
                    logger.info(
                        "[expert_agent:_detect_with_far_enhancement] ASPECT_REJECT | event_id=%d frame=%d bbox=%s ratio=%.2f",
                        self.category.event_id,
                        global_index,
                        bbox_norm,
                        aspect_ratio,
                    )
                    continue
            except Exception as exc:
                self._log_frame(
                    frame_analysis_log,
                    frame_log,
                    reason=f"ROI candidate size check failed: {exc}",
                )
                logger.warning(
                    "[expert_agent:_detect_with_far_enhancement] SIZE_CHECK_ERROR | event_id=%d frame=%d | %s",
                    self.category.event_id,
                    global_index,
                    exc,
                )
                continue

            # Compute adjacent-frame motion inside the enlarged ROI.  This is
            # used to penalise static foreground objects (camera brackets,
            # poles, wires) that the VLM occasionally returns as false ROIs.
            adjacent_index = (
                global_index - 1
                if global_index == len(images) - 1
                else global_index + 1
            )
            try:
                motion_score = compute_roi_motion_score(
                    frame,
                    images[adjacent_index],
                    bbox_norm,
                    scale=_FAR_MOTION_ENLARGE_SCALE,
                    gaussian_kernel=_FAR_MOTION_GAUSSIAN_KERNEL,
                    pixel_threshold=_FAR_MOTION_PIXEL_THRESHOLD,
                )
            except Exception as exc:
                logger.warning(
                    "[expert_agent:_detect_with_far_enhancement] MOTION_SCORE_ERROR | event_id=%d frame=%d | %s",
                    self.category.event_id,
                    global_index,
                    exc,
                )
                motion_score = {
                    "mean_diff": 0.0,
                    "fraction_above_threshold": 0.0,
                    "motion_score": 0.0,
                }

            motion_score_value = motion_score.get("motion_score", 0.0)
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] FRAME_CANDIDATE | event_id=%d frame=%d area_px=%d aspect=%.2f confidence=%s motion_score=%.3f",
                self.category.event_id,
                global_index,
                bbox_area,
                aspect_ratio,
                confidence,
                motion_score_value,
            )

            frame_log.update(
                {
                    "has_candidate": True,
                    "bbox_norm": bbox_norm,
                    "area_px": bbox_area,
                    "aspect_ratio": aspect_ratio,
                    "confidence": confidence,
                    "motion_score": motion_score_value,
                    "reason": reason,
                }
            )
            frame_analysis_log.append(frame_log)

            candidates.append(
                {
                    "global_index": global_index,
                    "frame": frame,
                    "bbox_norm": bbox_norm,
                    "area_px": bbox_area,
                    "aspect_ratio": aspect_ratio,
                    "occluded": occluded,
                    "confidence": confidence,
                    "reason": reason,
                    "motion_score": motion_score,
                    "adjacent_index": adjacent_index,
                    "frame_analysis_log": frame_analysis_log,
                }
            )

        if not candidates:
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] NO_VALID_CANDIDATES | event_id=%d",
                self.category.event_id,
            )
            return EventCandidate(
                detected=False,
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                summary="未检测到非机动车及两轮/三轮异常车辆。",
                raw_vlm_response={
                    "far_enhancement": {
                        "frame_analysis_log": frame_analysis_log,
                    }
                },
            )

        # ------------------------------------------------------------------
        # Rank candidates and keep the top K.
        # ------------------------------------------------------------------
        ranked_candidates = self._score_far_candidates(candidates)
        top_candidates = ranked_candidates[:_FAR_ENHANCEMENT_TOP_K]
        logger.info(
            "[expert_agent:_detect_with_far_enhancement] TOP_CANDIDATES | event_id=%d total=%d selected=%d",
            self.category.event_id,
            len(ranked_candidates),
            len(top_candidates),
        )

        # ------------------------------------------------------------------
        # Second pass: classify the top-K candidates.
        # ------------------------------------------------------------------
        for candidate in top_candidates:
            global_index = candidate["global_index"]
            composite_filename = f"{video_stem}_frame_{global_index}_composite.jpg"
            composite_path = str(output_dir / composite_filename)
            composite_ref = f"{image_ref_prefix}/{composite_filename}"
            adjacent_index = candidate["adjacent_index"]
            motion_composite_filename = (
                f"{video_stem}_frame_{global_index}_motion_{adjacent_index}.jpg"
            )
            motion_composite_path = str(output_dir / motion_composite_filename)
            motion_composite_ref = (
                f"{image_ref_prefix}/{motion_composite_filename}"
            )

            logger.info(
                "[expert_agent:_detect_with_far_enhancement] DUAL_COMPOSITE | event_id=%d frame=%d adjacent=%d composite=%s motion=%s",
                self.category.event_id,
                global_index,
                adjacent_index,
                composite_path,
                motion_composite_path,
            )

            try:
                create_composite(
                    candidate["frame"], candidate["bbox_norm"], output_path=composite_path
                )
                create_motion_comparison_composite(
                    candidate["frame"],
                    images[adjacent_index],
                    candidate["bbox_norm"],
                    scale=3.0,
                    output_path=motion_composite_path,
                )
            except Exception as exc:
                logger.error(
                    "[expert_agent:_detect_with_far_enhancement] COMPOSITE_ERROR | event_id=%d frame=%d | %s",
                    self.category.event_id,
                    global_index,
                    exc,
                    exc_info=True,
                )
                continue

            candidate.update(
                {
                    "composite_path": composite_path,
                    "motion_composite_path": motion_composite_path,
                    "composite_ref": composite_ref,
                    "motion_composite_ref": motion_composite_ref,
                }
            )

            final_candidate = self._run_final_classifier(
                candidate, template=template, context_vars=context_vars
            )
            if final_candidate is not None:
                logger.info(
                    "[expert_agent:_detect_with_far_enhancement] COMPLETE | event_id=%d detected=True frame=%d composite=%s motion=%s",
                    self.category.event_id,
                    global_index,
                    composite_path,
                    motion_composite_path,
                )
                return final_candidate

            logger.info(
                "[expert_agent:_detect_with_far_enhancement] CLASSIFIER_NEGATIVE | event_id=%d frame=%d score=%.2f reason=%s",
                self.category.event_id,
                global_index,
                candidate.get("score", 0.0),
                candidate.get("negative_final_reason", ""),
            )

        # ------------------------------------------------------------------
        # Optional fallback on the highest-scored candidate.
        # ------------------------------------------------------------------
        best_candidate = top_candidates[0]
        if "composite_path" in best_candidate:
            fallback_candidate = self._accept_fallback(
                best_candidate, frame_analysis_log
            )
            if fallback_candidate is not None:
                logger.info(
                    "[expert_agent:_detect_with_far_enhancement] FALLBACK_ACCEPT | event_id=%d frame=%d",
                    self.category.event_id,
                    best_candidate["global_index"],
                )
                return fallback_candidate

        logger.info(
            "[expert_agent:_detect_with_far_enhancement] NO_POSITIVE_CANDIDATES | event_id=%d",
            self.category.event_id,
        )
        # Preserve the best candidate's composite paths so the report can still
        # show what was analyzed and rejected.
        negative_raw_response: Dict[str, Any] = {
            "far_enhancement": {
                "frame_analysis_log": frame_analysis_log,
            }
        }
        if best_candidate.get("composite_ref"):
            negative_raw_response["composite_image_path"] = best_candidate["composite_ref"]
        if best_candidate.get("motion_composite_ref"):
            negative_raw_response["motion_composite_image_path"] = best_candidate["motion_composite_ref"]
        return EventCandidate(
            detected=False,
            event_id=self.category.event_id,
            event_name=self.category.name_zh,
            summary="未检测到非机动车及两轮/三轮异常车辆。",
            raw_vlm_response=negative_raw_response,
        )

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
