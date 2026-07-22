"""Far-distance object enhancement detector.

This module was split out of :mod:`traffic_analyzer.core.expert_agent`.
It contains the ROI-driven far-distance enhancement flow and the car-semantic
veto helpers used by that flow.  No new functionality was added.
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
from traffic_analyzer.utils.emergency_lane_occupancy import (
    build_occupancy_summary,
    compute_roi_zone_overlap,
    create_single_zooms,
    create_zoom_grid,
    draw_vehicle_rois,
    generate_masks_overlay,
)
from traffic_analyzer.utils.event_detection import (
    _parse_strict_bool,
    _safe_float,
    parse_expert_response,
)
from traffic_analyzer.utils.far_non_motor_enhancer import (
    compute_bbox_area_px,
    compute_bbox_aspect_ratio,
    compute_roi_motion_score,
    create_composite,
    create_motion_comparison_composite,
    create_multi_roi_gallery,
    is_bbox_aspect_valid,
    is_bbox_large_enough,
    load_image,
)

logger = logging.getLogger(__name__)


# Directory where far-distance object composite images are saved.
# Kept relative to the project root so it works across local dev, CI and Docker.
# Artifacts are grouped into a per-video subdirectory named after the video
# stem (see ``_detect_with_far_enhancement``).
_FAR_ENHANCEMENT_OUTPUT_DIR = Path("./output/tmp_img")

# Default far-enhancement parameters.  Most are overridden per-template via
# ``PromptTemplate.far_object_enhancement``; these constants act as fallback
# defaults and as fixed values that are not exposed in the config object.
_FAR_MOTION_ENLARGE_SCALE = 3.0
_FAR_MOTION_GAUSSIAN_KERNEL = (3, 3)
_FAR_MOTION_PIXEL_THRESHOLD = 8.0

# JSON schema expected from the VLM for expert-agent responses.
_EXPERT_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["detected"],
    "properties": {
        "detected": {"type": "boolean"},
        "is_target_explicitly_four_wheel_vehicle": {"type": "boolean"},
        "target_type": {"type": "string"},
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

# JSON schema for the far-distance per-frame ROI detection.
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

# JSON schema for the emergency lane / chevron polygon calibration used by event_id=1.
_EMERGENCY_LANE_CALIBRATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["emergency_polygon_rel", "chevron_polygon_rel"],
    "properties": {
        "emergency_polygon_rel": {
            "anyOf": [
                {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                },
                {"type": "null"},
            ]
        },
        "chevron_polygon_rel": {
            "anyOf": [
                {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                },
                {"type": "null"},
            ]
        },
        "summary": {"type": "string"},
    },
}

# JSON schema for the vehicle ROI detection inside calibrated emergency lane / chevron polygons.
_EMERGENCY_LANE_VEHICLE_ROI_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["rois", "summary"],
    "properties": {
        "rois": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "label", "zone", "rel_box"],
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "zone": {"type": "string"},
                    "reason": {"type": "string"},
                    "rel_box": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                },
            },
        },
        "summary": {"type": "string"},
    },
}

# JSON schema for the multi-evidence ROI detection used by event_id=6.
_MULTI_ROI_DETECTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["evidence_regions"],
    "properties": {
        "evidence_regions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["bbox_norm", "tag", "confidence"],
                "properties": {
                    "bbox_norm": {
                        "type": "array",
                        "items": {"type": "number"},
                    },
                    "tag": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "on_ground": {"type": "boolean"},
                },
            },
        },
        "summary": {"type": "string"},
    },
}

# JSON schema for the far-distance object final classifier.
# This is intentionally separate from the shared _EXPERT_RESPONSE_SCHEMA because
# the final classifier returns a minimal {detected, reason} object.
_FAR_ENHANCEMENT_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["detected"],
    "properties": {
        "detected": {"type": "boolean"},
        "is_target_explicitly_four_wheel_vehicle": {"type": "boolean"},
        "target_type": {"type": "string"},
        "reason": {"type": "string"},
    },
    "additionalProperties": False,
}




class FarEnhancementDetector:
    """Holder for far-distance enhancement dependencies."""

    def __init__(
        self,
        category: EventCategory,
        vlm_engine: VLMInferenceEngine,
        config_manager: ConfigManager,
    ) -> None:
        self.category = category
        self.vlm_engine = vlm_engine
        self.config_manager = config_manager
    _PEDESTRIAN_CAR_RE = re.compile(
        r"(是|为|疑似|像是|类似|看作|判定为|判断为|确认为)(?:一辆|一个|一尊)?(?:白色|黑色|红色|银色|灰色|蓝色|黄色|深色|浅色)?(?:小|大)?(?:轿车|suv|越野车|货车|卡车|厢式货车|面包车|客车|大巴|中巴|公交车|四轮车|四轮机动车|机动车|汽车)",
        re.IGNORECASE,
    )
    _PEDESTRIAN_CAR_OCCUPANT_RE = re.compile(
        r"(车内人员|车里的人|坐在车内|在车内|驾驶座|副驾驶|车内)",
        re.IGNORECASE,
    )
    _PEDESTRIAN_NOT_PEDESTRIAN_BUT_CAR_RE = re.compile(
        r"(不是行人|非行人|不是人|不是高速公路行人|不是滞留人员).*?(轿车|suv|货车|卡车|面包车|客车|四轮车|四轮机动车|汽车|机动车)",
        re.IGNORECASE,
    )
    _NON_MOTOR_POSITIVE_ANCHORS = [
        "摩托车",
        "电动车",
        "非机动车",
        "两轮车",
        "三轮车",
        "两轮",
        "三轮",
        "骑乘者",
        "骑乘姿态",
        "骑手",
        "头盔",
        "车把",
        "脚踏车",
        "自行车",
        "骑乘",
    ]
    _NON_MOTOR_CAR_NEGATION_RE = re.compile(
        r"(?:"
        r"被.*?轿车.*?取代|被.*?汽车.*?取代|"
        r"被.*?轿车.*?遮挡|被.*?汽车.*?遮挡|"
        r"而非轿车|而非汽车|而非机动车|"
        r"而不是轿车|而不是汽车|而不是机动车|"
        r"不是轿车|不是汽车|不是机动车|不是suv|不是面包车|不是货车|不是卡车|"
        r"不是一辆.*?轿车|不是一辆.*?汽车|不是一辆.*?机动车|"
        r"并未被.*?汽车|并未被.*?轿车|"
        r"没有被.*?汽车|没有被.*?轿车|"
        r"并非汽车|并非轿车|并非机动车|"
        r"没有.*?汽车|没有.*?轿车|没有.*?机动车"
        r")",
        re.IGNORECASE,
    )
    _NON_MOTOR_EXPLICIT_CAR_RE = re.compile(
        r"(?:"
        r"(?:红框内|框内|目标|该目标|主体|对象|这个物体|ROI内)"
        r".*?"
        r"(?:是|为|疑似|像是|类似|看作|判定为|判断为|确认为|属于|归类于)"
        r"(?:一辆|一台|一个|几辆)?"
        r"(?:白色|黑色|红色|银色|灰色|蓝色|黄色|深色|浅色|亮色|暗色)?"
        r"(?:小|大|小型|大型|中型|微型)?"
        r"(?:轿车|suv|越野车|货车|卡车|厢式货车|面包车|客车|大巴|中巴|公交车|四轮车|四轮机动车|机动车|汽车)"
        r")",
        re.IGNORECASE,
    )
    _NO_STRUCTURE_RE = re.compile(
        r"(?:无|没有)(?:清晰|明确|可辨识|具体|明显)?(?:的)?(?:车辆结构|非机动车结构|摩托车结构|结构|轮廓|特征|证据|"
        r"车轮|车把|车灯|车牌|车架|车身|车座|骑乘者|骑手|头盔|车辆|非机动车|摩托车|两轮|三轮)|"
        r"轮廓不清|无明显轮廓|无明显车辆结构|无明显结构|缺乏(?:清晰|明确|可辨识|具体|明显)?(?:的)?(?:结构|轮廓|特征|证据)|"
        r"无法(?:辨识|识别|辨认|看清|看出)(?:的)?(?:车辆结构|非机动车结构|摩托车结构|结构|轮廓|特征|"
        r"车轮|车把|车灯|车牌|车架|车身|车座|骑乘者|骑手|头盔|车辆|非机动车|摩托车|两轮|三轮)|"
        r"(?:仅|只|仅为)(?:是|为|能看到|是一个|一个|一团|一块)(?:暗斑|黑块|阴影|模糊|色块|点|亮点|反光点|轮廓|黑影)|"
        r"(?:暗斑|黑块|阴影|模糊色块|一团模糊|一个黑点|仅一个亮点|仅一个反光点|模糊轮廓|模糊目标|色块|反光点)",
        re.IGNORECASE,
    )
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
    def _is_explicitly_car_reasoning_for_pedestrian(self, reason: str) -> bool:
        """判断 reason 文本是否明确说明红框内目标是汽车/四轮车或车内人员。"""
        if not reason:
            return False
        return bool(
            self._PEDESTRIAN_CAR_RE.search(reason)
            or self._PEDESTRIAN_CAR_OCCUPANT_RE.search(reason)
            or self._PEDESTRIAN_NOT_PEDESTRIAN_BUT_CAR_RE.search(reason)
        )
    def _is_explicitly_car_reasoning_for_non_motor(self, reason: str) -> bool:
        """判断 reason 是否明确说红框内目标是汽车/四轮车（非机动车事件专用）。

        对 event_id=4 做更精细的语义判断：
        - 若出现否定/对比/替代语境（“而非汽车”“被轿车取代”），返回 False；
        - 若明确断言“红框内是汽车/轿车/...”，返回 True；
        - 若出现非机动车/摩托车强锚定词且无明确汽车断言，返回 False。
        """
        if not reason:
            return False
        lower = reason.lower()

        # 1. Negation / comparison / replacement contexts mentioning cars.
        if self._NON_MOTOR_CAR_NEGATION_RE.search(reason):
            return False

        # 2. Explicit assertion that the target is a car.
        explicit_car_match = self._NON_MOTOR_EXPLICIT_CAR_RE.search(reason)
        if explicit_car_match:
            return True

        # 3. Strong positive anchors for non-motor vehicles: never veto.
        if any(anchor in lower for anchor in self._NON_MOTOR_POSITIVE_ANCHORS):
            return False

        return False
    def _select_car_veto_check(self, event_id: int):
        """Return the appropriate car-semantic veto function for an event."""
        if event_id == 3:
            return self._is_explicitly_car_reasoning_for_pedestrian
        if event_id == 4:
            return self._is_explicitly_car_reasoning_for_non_motor
        return self._is_explicitly_car_reasoning
    def _build_minimal_final_classifier_template(
        self,
        template: PromptTemplate,
    ) -> PromptTemplate:
        """Build a concise retry prompt when the first classifier response is unparseable."""
        if self.category.event_id == 4:
            example_json = (
                '{\n'
                '  "detected": <true|false>,\n'
                '  "is_target_explicitly_four_wheel_vehicle": <true|false>,\n'
                '  "target_type": "<汽车|摩托车|电动车|非机动车|施工元素|行人|无法确定>",\n'
                '  "reason": "<一句话判断理由>"\n'
                '}'
            )
        else:
            # Pedestrian and construction use the full expert response shape.
            example_json = (
                '{\n'
                '  "detected": <true|false>,\n'
                '  "is_target_explicitly_four_wheel_vehicle": <true|false>,\n'
                '  "target_type": "<汽车|摩托车|电动车|非机动车|施工元素|行人|无法确定>",\n'
                '  "instances": [...],\n'
                '  "summary": "<总体评估>"\n'
                '}'
            )
        minimal_user = (
            "你刚才的输出格式不正确，无法按 JSON schema 解析。请仅根据图像重新输出合法 JSON，"
            "不要包含 markdown 代码块、解释或其他任何文字。\n\n"
            "必须包含以下字段：\n"
            f"{example_json}\n\n"
            "重要提示：\n"
            "- is_target_explicitly_four_wheel_vehicle 只回答红色方框内的目标本身是否是四轮机动车（汽车/SUV/货车/面包车）。\n"
            "- 如果目标是行人、摩托车、电动车、非机动车、施工元素，必须填 false。\n"
            "- 如果只是背景中提到汽车、被汽车取代、与汽车对比，必须填 false。"
        )
        return PromptTemplate(
            template_id=template.template_id,
            name=template.name,
            version=template.version,
            system_prompt=template.system_prompt,
            user_prompt=minimal_user,
            output_format_hint="JSON",
            example_input=None,
            example_output=None,
            traffic_percentage=template.traffic_percentage,
            available_tools=[],
            far_object_enhancement=template.far_object_enhancement,
        )
    def _should_veto_as_car(
        self,
        parsed: Dict[str, Any],
        text: str,
        event_id: int,
    ) -> bool:
        """Decide whether to veto the detection because the target is a car.

        Primary path: structured ``is_target_explicitly_four_wheel_vehicle``.
        Fallback path: regex on the free-text reason when the field is missing.
        """
        structured = parsed.get("is_target_explicitly_four_wheel_vehicle")
        if structured is True:
            return True
        if structured is False:
            return False
        # Structured field absent/null: fall back to regex reasoning checks.
        car_check = self._select_car_veto_check(event_id)
        return bool(car_check(text))
    def _apply_structured_veto_to_candidate(
        self,
        candidate: EventCandidate,
    ) -> EventCandidate:
        """Force candidate.detected=False when the classifier says it is a car."""
        parsed = candidate.raw_vlm_response
        if not isinstance(parsed, dict):
            return candidate
        is_car = parsed.get("is_target_explicitly_four_wheel_vehicle")
        if is_car is True and candidate.detected:
            logger.info(
                "[expert_agent:_apply_structured_veto_to_candidate] CAR_OVERRIDDEN | "
                "event_id=%d target_type=%s",
                candidate.event_id,
                parsed.get("target_type", ""),
            )
            candidate.detected = False
        candidate.is_target_explicitly_four_wheel_vehicle = is_car
        candidate.target_type = str(parsed.get("target_type", ""))
        return candidate
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
            summary=f"检测到{self.category.name_zh}：{reason}",
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
        """Run the final far-distance classifier on a candidate's composites.

        The expected response format depends on the event category:
        - event_id=3 (pedestrian) uses the full expert response schema with
          ``detected`` / ``instances`` / ``summary``.
        - event_id=4 (non-motor vehicle) and other categories use the minimal
          ``{detected, reason}`` classifier schema.

        The classifier is now expected to emit a structured veto field
        ``is_target_explicitly_four_wheel_vehicle``. When the field is missing
        or the response cannot be parsed, we fall back to the legacy regex
        checks and, as a last resort, retry once with a shorter prompt.
        """
        global_index = frame_info["global_index"]

        # Pedestrian final classifier returns a full expert response so that
        # the adjudication layer receives the same structured instances as
        # other expert agents.
        if self.category.event_id == 3:
            response_schema = _EXPERT_RESPONSE_SCHEMA
        else:
            response_schema = _FAR_ENHANCEMENT_RESPONSE_SCHEMA

        images = [
            frame_info["composite_path"],
            frame_info["motion_composite_path"],
        ]

        def _call_classifier(
            prompt_template: PromptTemplate,
        ) -> Any:
            return self.vlm_engine.call(
                template=prompt_template,
                images=images,
                context_vars=context_vars,
                response_schema=response_schema,
            )

        try:
            response = _call_classifier(template)
        except FatalAPIError:
            raise
        except Exception as exc:
            logger.error(
                "[expert_agent:_run_final_classifier] FINAL_CALL_ERROR | event_id=%d frame=%d | %s",
                self.category.event_id,
                global_index,
                exc,
                exc_info=True,
            )
            return None

        # Retry once with a minimal prompt if the first response is unparseable.
        if not response.success or not isinstance(response.parsed_data, dict):
            logger.warning(
                "[expert_agent:_run_final_classifier] PARSE_RETRY | event_id=%d frame=%d success=%s error=%s",
                self.category.event_id,
                global_index,
                response.success,
                getattr(response, "raw_text", "")[:200],
            )
            retry_template = self._build_minimal_final_classifier_template(template)
            try:
                retry_response = _call_classifier(retry_template)
            except Exception as exc:
                logger.error(
                    "[expert_agent:_run_final_classifier] RETRY_CALL_ERROR | event_id=%d frame=%d | %s",
                    self.category.event_id,
                    global_index,
                    exc,
                    exc_info=True,
                )
                retry_response = None

            if (
                retry_response is not None
                and retry_response.success
                and isinstance(retry_response.parsed_data, dict)
            ):
                logger.info(
                    "[expert_agent:_run_final_classifier] RETRY_SUCCESS | event_id=%d frame=%d",
                    self.category.event_id,
                    global_index,
                )
                response = retry_response
            else:
                logger.warning(
                    "[expert_agent:_run_final_classifier] RETRY_FAILED | event_id=%d frame=%d",
                    self.category.event_id,
                    global_index,
                )
                # Preserve the raw text so fallback can still apply regex.
                frame_info["negative_final_reason"] = str(
                    getattr(response, "raw_text", "")
                )[:2000]
                return None

        parsed = response.parsed_data
        detected = _parse_strict_bool(parsed.get("detected", False))

        # Preserve the classifier's raw output before any car-semantic override.
        # This lets fallback distinguish "classifier was negative" from
        # "classifier was positive but vetoed because of a car keyword".
        frame_info["raw_final_detected"] = detected
        frame_info["is_target_explicitly_four_wheel_vehicle"] = parsed.get(
            "is_target_explicitly_four_wheel_vehicle"
        )
        frame_info["target_type"] = parsed.get("target_type", "")

        # ------------------------------------------------------------------
        # Pedestrian branch: keep instances/summary from the classifier.
        # ------------------------------------------------------------------
        if self.category.event_id == 3:
            final_summary = str(parsed.get("summary", ""))
            frame_info["raw_final_reason"] = final_summary
            final_instances = parsed.get("instances") or []
            if not isinstance(final_instances, list):
                final_instances = []
            final_instances = [
                inst for inst in final_instances if isinstance(inst, dict)
            ]

            # Structured car veto: if the classifier explicitly says the boxed
            # target is a four-wheel vehicle, override detected=false. Fallback
            # to regex checks only when the structured field is missing.
            text_for_veto = " ".join(
                [
                    final_summary,
                    *(
                        str(inst.get("description", ""))
                        + " "
                        + str(inst.get("reasoning", ""))
                        for inst in final_instances
                    ),
                ]
            )
            if detected and self._should_veto_as_car(
                parsed, text_for_veto, self.category.event_id
            ):
                logger.info(
                    "[expert_agent:_run_final_classifier] CAR_OVERRIDDEN | event_id=%d frame=%d summary=%s",
                    self.category.event_id,
                    global_index,
                    final_summary,
                )
                detected = False

            if detected:
                normalized_instances: List[EventInstance] = []
                for inst in final_instances:
                    evidence_frames = inst.get("evidence_frames")
                    if not isinstance(evidence_frames, list):
                        evidence_frames = []
                    normalized_instances.append(
                        EventInstance(
                            event_id=self.category.event_id,
                            event_name=self.category.name_zh,
                            start_time_sec=_safe_float(inst.get("start_time_sec", 0.0)),
                            end_time_sec=_safe_float(inst.get("end_time_sec", 0.0)),
                            evidence_frames=[
                                int(f) for f in evidence_frames if isinstance(f, (int, float))
                            ]
                            or [global_index, frame_info["adjacent_index"]],
                            description=str(inst.get("description", ""))
                            or final_summary,
                            reasoning=str(inst.get("reasoning", "")) or final_summary,
                        )
                    )
                if not normalized_instances:
                    normalized_instances = [
                        EventInstance(
                            event_id=self.category.event_id,
                            event_name=self.category.name_zh,
                            evidence_frames=[global_index, frame_info["adjacent_index"]],
                            description=final_summary,
                            reasoning=final_summary,
                        )
                    ]

                far_enhancement: Dict[str, Any] = {
                    "selected_frame_index": global_index,
                    "bbox_norm": frame_info["bbox_norm"],
                    "reason": final_summary,
                    "frame_analysis_log": frame_info.get("frame_analysis_log", []),
                }
                return EventCandidate(
                    event_id=self.category.event_id,
                    event_name=self.category.name_zh,
                    detected=True,
                    summary=final_summary
                    or f"检测到{self.category.name_zh}",
                    instances=normalized_instances,
                    raw_vlm_response={
                        "composite_image_path": frame_info["composite_ref"],
                        "motion_composite_image_path": frame_info["motion_composite_ref"],
                        "far_enhancement": far_enhancement,
                    },
                    raw_vlm_text=response.raw_text,
                    is_target_explicitly_four_wheel_vehicle=False,
                    target_type=parsed.get("target_type", "行人"),
                )

            negative_reason = final_summary or "未检测到高速公路行人。"
            frame_info["negative_final_reason"] = negative_reason
            return None

        # ------------------------------------------------------------------
        # Non-motor / minimal branch: {detected, reason}.
        # ------------------------------------------------------------------
        final_reason = str(parsed.get("reason", ""))
        frame_info["raw_final_reason"] = final_reason

        if detected and self._should_veto_as_car(
            parsed, final_reason, self.category.event_id
        ):
            logger.info(
                "[expert_agent:_run_final_classifier] CAR_OVERRIDDEN | event_id=%d frame=%d reason=%s",
                self.category.event_id,
                global_index,
                final_reason,
            )
            detected = False

        if detected:
            candidate = self._build_far_candidate(
                frame_info,
                final_reason,
                frame_info.get("frame_analysis_log", []),
                raw_text=response.raw_text,
            )
            candidate.is_target_explicitly_four_wheel_vehicle = False
            candidate.target_type = parsed.get("target_type", "非机动车")
            return candidate
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

        # Pedestrians require a higher confidence bar before we override the
        # final classifier, because the ROI detector is intentionally permissive.
        confidence_threshold = 0.7 if self.category.event_id == 3 else 0.5
        confidence = self._parse_roi_confidence(frame_info.get("confidence", 0.0))
        if confidence < confidence_threshold:
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] FALLBACK_REJECT_CONFIDENCE | event_id=%d frame=%d confidence=%s threshold=%s",
                self.category.event_id,
                frame_info["global_index"],
                confidence,
                confidence_threshold,
            )
            return None

        # For pedestrians, re-validate that the ROI itself passed the configured
        # size/aspect filters. These checks already happened during candidate
        # collection, but repeating them here makes the fallback self-contained.
        if self.category.event_id == 3:
            if not frame_info.get("area_px"):
                logger.info(
                    "[expert_agent:_detect_with_far_enhancement] FALLBACK_REJECT_NO_AREA | event_id=%d frame=%d",
                    self.category.event_id,
                    frame_info["global_index"],
                )
                return None
            aspect_ratio = float(frame_info.get("aspect_ratio", 0.0))
            if aspect_ratio <= 0.0:
                logger.info(
                    "[expert_agent:_detect_with_far_enhancement] FALLBACK_REJECT_ASPECT | event_id=%d frame=%d aspect=%s",
                    self.category.event_id,
                    frame_info["global_index"],
                    aspect_ratio,
                )
                return None

        # Apply car-semantic veto to the classifier's negative reasoning. The
        # ROI reason may legitimately mention surrounding cars for comparison,
        # so only the final classifier's explicit car description is used here.
        # Pedestrians use a stricter veto because a pedestrian near a vehicle is
        # still a pedestrian.
        negative_reason = str(frame_info.get("negative_final_reason", ""))
        structured_is_car = frame_info.get("is_target_explicitly_four_wheel_vehicle")
        if structured_is_car is True:
            logger.info(
                "[expert_agent:_accept_fallback] FALLBACK_REJECT_CAR_STRUCTURED | event_id=%d frame=%d",
                self.category.event_id,
                frame_info["global_index"],
            )
            return None
        if structured_is_car is None:
            # Structured field missing: fall back to regex reasoning checks.
            car_veto_check = self._select_car_veto_check(self.category.event_id)
            if car_veto_check(negative_reason):
                logger.info(
                    "[expert_agent:_accept_fallback] FALLBACK_REJECT_CAR_REGEX | event_id=%d frame=%d negative_reason=%s",
                    self.category.event_id,
                    frame_info["global_index"],
                    negative_reason,
                )
                return None
        # The "no structure" veto is specific to non-motor vehicles (event_id=4):
        # it blocks fallback when the classifier says the box lacks identifiable
        # vehicle-structure evidence (wheels, handlebars, etc.). For pedestrians
        # the ROI detector already verified an upright human silhouette, so a
        # vague "cannot confirm" classifier reason should not block fallback.
        if self.category.event_id == 4 and self._is_no_structure_reasoning(negative_reason):
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] FALLBACK_REJECT_NO_STRUCTURE | event_id=%d frame=%d negative_reason=%s",
                self.category.event_id,
                frame_info["global_index"],
                negative_reason,
            )
            return None

        # Build a self-consistent summary. For pedestrians, anchor the summary
        # in the ROI detector's own reason so the report's "expert raw output"
        # matches the per-frame ROI evidence table.
        if self.category.event_id == 3:
            roi_reason = str(frame_info.get("reason", ""))
            fallback_reason = (
                f"检测到高速公路行人。第{frame_info['global_index']}帧红色方框内"
                f"{'，' + roi_reason if roi_reason else '目标位于道路区域，直立人形轮廓'}"
            )
        else:
            fallback_reason = (
                f"检测到远距离{self.category.name_zh}。第{frame_info['global_index']}帧红色方框内目标位于道路区域，"
                f"尺寸与宽高比符合{self.category.name_zh}特征。"
            )
        candidate = self._build_far_candidate(
            frame_info,
            fallback_reason,
            frame_analysis_log,
            raw_text=fallback_reason,
            fallback=True,
        )
        candidate.is_target_explicitly_four_wheel_vehicle = False
        candidate.target_type = "行人" if self.category.event_id == 3 else "非机动车"
        return candidate
    def _has_construction_evidence(self, regions: List[Dict[str, Any]]) -> bool:
        """Check if ROI evidence regions satisfy the construction work-zone definition.

        A valid construction scene requires at least one of the following:
        - at least one grounded cone plus at least one worker or vehicle;
        - at least three cones (continuous or grouped arrangement);
        - at least two barriers forming a clear lane closure;
        - at least one sign plus at least one worker or vehicle.

        Worker + vehicle alone is NOT sufficient; ground-based construction
        elements (cone, barrier, sign) must be present.

        Only regions with confidence >= 0.5 are counted.
        """
        tags = [
            str(r.get("tag", "")).lower()
            for r in regions
            if self._parse_roi_confidence(r.get("confidence", 0.0)) >= 0.5
        ]
        cone_count = tags.count("cone")
        worker_count = tags.count("worker")
        vehicle_count = tags.count("vehicle")
        barrier_count = tags.count("barrier")
        sign_count = tags.count("sign")

        if cone_count >= 1 and (worker_count + vehicle_count) >= 1:
            return True
        if cone_count >= 3:
            return True
        if barrier_count >= 2:
            return True
        if sign_count >= 1 and (worker_count + vehicle_count) >= 1:
            return True
        return False
    def _build_construction_fallback_candidate(
        self,
        candidate: EventCandidate,
        display_regions: List[Dict[str, Any]],
        valid_regions: List[Dict[str, Any]],
        selected_index: int,
        gallery_ref: str,
        roi_summary: str,
    ) -> EventCandidate:
        """Promote a negative construction candidate when evidence clearly supports it."""
        tag_counts: Dict[str, int] = {}
        for region in valid_regions:
            tag = str(region.get("tag", "unknown")).lower()
            if self._parse_roi_confidence(region.get("confidence", 0.0)) >= 0.5:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

        present_tags: List[str] = []
        if tag_counts.get("cone", 0) > 0:
            present_tags.append(f"锥桶×{tag_counts['cone']}")
        if tag_counts.get("worker", 0) > 0:
            present_tags.append(f"施工人员×{tag_counts['worker']}")
        if tag_counts.get("vehicle", 0) > 0:
            present_tags.append(f"施工车辆×{tag_counts['vehicle']}")
        if tag_counts.get("barrier", 0) > 0:
            present_tags.append(f"隔离栏/围挡×{tag_counts['barrier']}")
        if tag_counts.get("sign", 0) > 0:
            present_tags.append(f"施工标志牌×{tag_counts['sign']}")

        tags_str = "、".join(present_tags) if present_tags else "施工元素"
        summary = (
            f"检测到道路施工。证据合成图中出现 {tags_str} 等施工元素，"
            f"满足施工作业区定义。"
        )

        candidate.detected = True
        candidate.summary = summary
        candidate.instances = [
            EventInstance(
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                evidence_frames=[selected_index],
                description=summary,
                reasoning=summary,
            )
        ]

        candidate.raw_vlm_response["gallery_image_path"] = gallery_ref
        candidate.raw_vlm_response.setdefault("far_enhancement", {})
        candidate.raw_vlm_response["far_enhancement"].update(
            {
                "selected_frame_index": selected_index,
                "evidence_regions": display_regions,
                "summary": roi_summary,
                "fallback": True,
            }
        )
        return candidate
    @staticmethod
    def _parse_roi_confidence(value: Any) -> float:
        """Normalize ROI confidence to a 0-1 float, handling string legacy values."""
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            legacy_map = {"high": 0.85, "medium": 0.55, "low": 0.15}
            return legacy_map.get(value.lower(), 0.0)
        return 0.0

    def _filter_grounded_construction_regions(
        self,
        regions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Remove cone regions that are not resting on the ground/road surface.

        Cones placed on vehicle roofs or in truck beds are not valid road-
        construction evidence. When the VLM does not provide ``on_ground``,
        fall back to a positional check: the bottom of the cone bbox should be
        in the lower half of the image (y2 > 0.5).
        """
        filtered: List[Dict[str, Any]] = []
        for region in regions:
            tag = str(region.get("tag", "")).lower()
            if tag != "cone":
                filtered.append(region)
                continue

            on_ground = region.get("on_ground")
            if on_ground is False:
                logger.info(
                    "[expert_agent:_filter_grounded_construction_regions] CONE_NOT_ON_GROUND | "
                    "event_id=%d on_ground=false",
                    self.category.event_id,
                )
                continue

            if on_ground is None:
                bbox_norm = region.get("bbox_norm")
                if bbox_norm and len(bbox_norm) >= 4 and bbox_norm[3] <= 0.5:
                    logger.info(
                        "[expert_agent:_filter_grounded_construction_regions] CONE_POSITION_REJECT | "
                        "event_id=%d y2=%.2f",
                        self.category.event_id,
                        bbox_norm[3],
                    )
                    continue

            filtered.append(region)
        return filtered

    def _score_far_candidates(
        self,
        candidates: List[Dict[str, Any]],
        motion_score_threshold: float = 1.0,
        motion_penalty: float = 5.0,
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
            applied_motion_penalty = (
                motion_penalty
                if motion_score < motion_score_threshold
                else 0.0
            )
            score = (
                conf
                + area_score
                - aspect_penalty
                - occlusion_penalty
                - applied_motion_penalty
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

    def _generate_low_confidence_evidence(
        self,
        candidates: List[Dict[str, Any]],
        images: List[Any],
        output_dir: Path,
        image_ref_prefix: str,
        video_stem: str,
        frame_analysis_log: List[Dict[str, Any]],
        motion_score_threshold: float,
        motion_penalty: float,
    ) -> Dict[str, Any]:
        """Generate evidence composites from the best candidate below a gate.

        Even when no candidate passes the confidence gate, we still want to show
        the best available ROI in the report so users can see what was analysed
        and rejected. If ``candidates`` is empty, only the frame analysis log is
        returned.
        """
        raw_response: Dict[str, Any] = {
            "far_enhancement": {
                "frame_analysis_log": frame_analysis_log,
            }
        }
        if not candidates:
            return raw_response

        scored = self._score_far_candidates(
            candidates,
            motion_score_threshold=motion_score_threshold,
            motion_penalty=motion_penalty,
        )
        best = scored[0]
        global_index = best["global_index"]
        adjacent_index = best["adjacent_index"]
        composite_filename = (
            f"{video_stem}_event_{self.category.event_id}_frame_{global_index}_composite.jpg"
        )
        composite_path = str(output_dir / composite_filename)
        composite_ref = f"{image_ref_prefix}/{composite_filename}"
        motion_composite_filename = (
            f"{video_stem}_event_{self.category.event_id}_frame_{global_index}_motion_{adjacent_index}.jpg"
        )
        motion_composite_path = str(output_dir / motion_composite_filename)
        motion_composite_ref = f"{image_ref_prefix}/{motion_composite_filename}"

        try:
            create_composite(
                best["frame"], best["bbox_norm"], output_path=composite_path
            )
            create_motion_comparison_composite(
                best["frame"],
                images[adjacent_index],
                best["bbox_norm"],
                scale=3.0,
                output_path=motion_composite_path,
            )
            raw_response["composite_image_path"] = composite_ref
            raw_response["motion_composite_image_path"] = motion_composite_ref
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] LOW_CONFIDENCE_EVIDENCE | "
                "event_id=%d frame=%d composite=%s motion=%s",
                self.category.event_id,
                global_index,
                composite_path,
                motion_composite_path,
            )
        except Exception as exc:
            logger.error(
                "[expert_agent:_detect_with_far_enhancement] LOW_CONFIDENCE_EVIDENCE_ERROR | "
                "event_id=%d frame=%d | %s",
                self.category.event_id,
                global_index,
                exc,
                exc_info=True,
            )
        return raw_response

    def _detect_emergency_lane_occupancy(
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
        """Far-distance enhancement branch for emergency lane occupancy (event_id=1).

        1. Select the middle frame.
        2. Run the ``emergency_lane_calibration`` template to obtain the
           emergency lane / chevron polygons.
        3. Run the ``emergency_lane_vehicle_roi`` template to detect vehicles
           inside the calibrated polygons.
        4. Generate mask overlay, red-box vehicle annotation, zoom grid and
           per-vehicle zoom crops.
        5. Compute each ROI's overlap with its declared zone polygon.
        6. Run the final ``emergency_lane_occupancy_detection`` classifier on
           the annotated vehicle image and zoom grid.
        7. Return an EventCandidate that always includes the occupancy evidence
           paths, even when the classifier is negative.

        ``roi_template`` is kept in the signature for caller compatibility but
        the two helper templates are loaded explicitly by ID.
        """
        if not images:
            return None

        selected_index = len(images) // 2
        frame = images[selected_index]

        logger.info(
            "[expert_agent:_detect_emergency_lane_occupancy] START | event_id=%d event_name=%s frame=%d",
            self.category.event_id,
            self.category.name_zh,
            selected_index,
        )

        # --- Load the two helper templates explicitly ----------------------
        try:
            calibration_template = self.config_manager.get_prompt_template(
                "emergency_lane_calibration"
            )
            vehicle_roi_template = self.config_manager.get_prompt_template(
                "emergency_lane_vehicle_roi"
            )
        except (KeyError, RuntimeError) as exc:
            logger.warning(
                "[expert_agent:_detect_emergency_lane_occupancy] TEMPLATE_LOAD_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
            )
            return None

        # --- Step A: polygon calibration on the middle frame ---------------
        try:
            calibration_response = self.vlm_engine.call(
                template=calibration_template,
                images=[frame],
                context_vars=context_vars,
                response_schema=_EMERGENCY_LANE_CALIBRATION_SCHEMA,
            )
        except FatalAPIError:
            raise
        except Exception as exc:
            logger.error(
                "[expert_agent:_detect_emergency_lane_occupancy] CALIBRATION_CALL_ERROR | event_id=%d frame=%d | %s",
                self.category.event_id,
                selected_index,
                exc,
                exc_info=True,
            )
            return None

        if not calibration_response.success or not isinstance(
            calibration_response.parsed_data, dict
        ):
            logger.warning(
                "[expert_agent:_detect_emergency_lane_occupancy] CALIBRATION_PARSE_ERROR | event_id=%d frame=%d",
                self.category.event_id,
                selected_index,
            )
            return None

        calibration_parsed = calibration_response.parsed_data
        emergency_polygon_rel = calibration_parsed.get("emergency_polygon_rel") or None
        chevron_polygon_rel = calibration_parsed.get("chevron_polygon_rel") or None
        calibration_summary = str(calibration_parsed.get("summary", ""))

        occupancy_detection: Dict[str, Any] = {
            "selected_frame_index": selected_index,
            "emergency_polygon_rel": emergency_polygon_rel,
            "chevron_polygon_rel": chevron_polygon_rel,
            "calibration_summary": calibration_summary,
            "rois": [],
            "calibration_reasoning": calibration_summary,
        }

        # --- Step B: early negative when no zone is calibrated -------------
        if not emergency_polygon_rel and not chevron_polygon_rel:
            logger.info(
                "[expert_agent:_detect_emergency_lane_occupancy] NO_ZONES | event_id=%d frame=%d",
                self.category.event_id,
                selected_index,
            )
            return EventCandidate(
                detected=False,
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                summary=f"未检测到{self.category.name_zh}。",
                raw_vlm_response={"occupancy_detection": occupancy_detection},
                raw_vlm_text=getattr(calibration_response, "raw_text", "") or "",
            )

        # --- Step C: vehicle ROI detection inside calibrated polygons ------
        roi_context_vars = {
            **context_vars,
            "emergency_polygon_rel": emergency_polygon_rel,
            "chevron_polygon_rel": chevron_polygon_rel,
        }
        try:
            roi_response = self.vlm_engine.call(
                template=vehicle_roi_template,
                images=[frame],
                context_vars=roi_context_vars,
                response_schema=_EMERGENCY_LANE_VEHICLE_ROI_SCHEMA,
            )
        except FatalAPIError:
            raise
        except Exception as exc:
            logger.error(
                "[expert_agent:_detect_emergency_lane_occupancy] VEHICLE_ROI_CALL_ERROR | event_id=%d frame=%d | %s",
                self.category.event_id,
                selected_index,
                exc,
                exc_info=True,
            )
            return None

        if not roi_response.success or not isinstance(roi_response.parsed_data, dict):
            logger.warning(
                "[expert_agent:_detect_emergency_lane_occupancy] VEHICLE_ROI_PARSE_ERROR | event_id=%d frame=%d",
                self.category.event_id,
                selected_index,
            )
            return None

        roi_parsed = roi_response.parsed_data
        rois = roi_parsed.get("rois", []) or []
        vehicle_roi_summary = str(roi_parsed.get("summary", ""))

        occupancy_detection["rois"] = rois
        occupancy_detection["vehicle_roi_summary"] = vehicle_roi_summary
        occupancy_detection["calibration_reasoning"] = (
            f"{calibration_summary}；{vehicle_roi_summary}".strip("；")
        )

        # --- No ROI: return negative candidate with calibration data -------
        if not rois:
            logger.info(
                "[expert_agent:_detect_emergency_lane_occupancy] NO_ROIS | event_id=%d frame=%d",
                self.category.event_id,
                selected_index,
            )
            return EventCandidate(
                detected=False,
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                summary=f"未检测到{self.category.name_zh}。",
                raw_vlm_response={"occupancy_detection": occupancy_detection},
                raw_vlm_text=getattr(roi_response, "raw_text", "") or "",
            )

        # --- Prepare output directory --------------------------------------
        occupancy_dir = output_dir / f"{video_stem}_event_1_occupancy"
        try:
            occupancy_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.error(
                "[expert_agent:_detect_emergency_lane_occupancy] OUTPUT_DIR_ERROR | event_id=%d path=%s | %s",
                self.category.event_id,
                occupancy_dir,
                exc,
                exc_info=True,
            )
            return None

        try:
            frame_pil = load_image(frame)
            img_width, img_height = frame_pil.size
        except Exception as exc:
            logger.warning(
                "[expert_agent:_detect_emergency_lane_occupancy] LOAD_FRAME_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
            )
            return None

        # --- Generate visual evidence --------------------------------------
        masks_filename = "02_masks_overlay.jpg"
        vehicles_filename = "03_vehicles_red_boxes.jpg"
        zoom_grid_filename = "04_zoom_grid.jpg"

        masks_path = str(occupancy_dir / masks_filename)
        vehicles_path = str(occupancy_dir / vehicles_filename)
        zoom_grid_path = str(occupancy_dir / zoom_grid_filename)

        masks_ref = f"{image_ref_prefix}/{video_stem}_event_1_occupancy/{masks_filename}"
        vehicles_ref = f"{image_ref_prefix}/{video_stem}_event_1_occupancy/{vehicles_filename}"
        zoom_grid_ref = f"{image_ref_prefix}/{video_stem}_event_1_occupancy/{zoom_grid_filename}"

        try:
            generate_masks_overlay(
                frame,
                emergency_polygon_rel=emergency_polygon_rel,
                chevron_polygon_rel=chevron_polygon_rel,
                output_path=masks_path,
            )
            draw_vehicle_rois(frame, rois, output_path=vehicles_path)
            create_zoom_grid(frame, rois, scale=4, output_path=zoom_grid_path)
            single_zoom_results = create_single_zooms(
                frame, rois, scale=4, output_dir=str(occupancy_dir)
            )
        except Exception as exc:
            logger.error(
                "[expert_agent:_detect_emergency_lane_occupancy] VISUAL_EVIDENCE_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
                exc_info=True,
            )
            return None

        single_zoom_refs: List[Tuple[str, str]] = [
            (
                roi_id,
                f"{image_ref_prefix}/{video_stem}_event_1_occupancy/{rel_path}",
            )
            for roi_id, rel_path in single_zoom_results
        ]

        logger.info(
            "[expert_agent:_detect_emergency_lane_occupancy] EVIDENCE_CREATED | event_id=%d rois=%d dir=%s",
            self.category.event_id,
            len(rois),
            occupancy_dir,
        )

        # --- Compute per-ROI zone overlap ----------------------------------
        vehicle_overlaps: Dict[str, float] = {}
        for roi in rois:
            roi_id = roi.get("id")
            zone = str(roi.get("zone", ""))
            rel_box = roi.get("rel_box")
            if not roi_id or not rel_box or len(rel_box) != 4:
                continue

            zone_polygon = None
            if zone == "emergency_lane":
                zone_polygon = emergency_polygon_rel
            elif zone == "chevron":
                zone_polygon = chevron_polygon_rel

            if not zone_polygon:
                vehicle_overlaps[roi_id] = 0.0
                continue

            try:
                overlap = compute_roi_zone_overlap(
                    rel_box, zone_polygon, img_width, img_height
                )
                vehicle_overlaps[roi_id] = overlap
            except Exception as exc:
                logger.warning(
                    "[expert_agent:_detect_emergency_lane_occupancy] OVERLAP_ERROR | event_id=%d roi=%s | %s",
                    self.category.event_id,
                    roi_id,
                    exc,
                )
                vehicle_overlaps[roi_id] = 0.0

        occupancy_detection["vehicle_overlaps"] = vehicle_overlaps
        occupancy_detection["summary"] = build_occupancy_summary(
            video_stem, rois, vehicle_overlaps
        )

        # --- Final classifier on annotated vehicles + zoom grid ------------
        try:
            response = self.vlm_engine.call(
                template=template,
                images=[masks_path, vehicles_path, zoom_grid_path],
                context_vars=context_vars,
                response_schema=_EXPERT_RESPONSE_SCHEMA,
            )
        except FatalAPIError:
            raise
        except Exception as exc:
            logger.error(
                "[expert_agent:_detect_emergency_lane_occupancy] FINAL_CALL_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
                exc_info=True,
            )
            return EventCandidate(
                detected=False,
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                summary=f"{self.category.name_zh}增强分类失败。",
                raw_vlm_response={
                    "mask_overlay_image_path": masks_ref,
                    "vehicle_boxes_image_path": vehicles_ref,
                    "zoom_grid_image_path": zoom_grid_ref,
                    "single_zoom_image_paths": single_zoom_refs,
                    "occupancy_detection": occupancy_detection,
                },
                raw_vlm_text="",
            )

        occupancy_detection["final_classifier_raw_text"] = getattr(
            response, "raw_text", ""
        )

        candidate = parse_expert_response(response, self.category)
        # Ensure the selected frame is recorded as evidence.
        if candidate.instances:
            for inst in candidate.instances:
                if selected_index not in (inst.evidence_frames or []):
                    inst.evidence_frames = (inst.evidence_frames or []) + [selected_index]
        else:
            candidate.instances = [
                EventInstance(
                    event_id=self.category.event_id,
                    event_name=self.category.name_zh,
                    evidence_frames=[selected_index],
                    description=candidate.summary,
                    reasoning=candidate.summary,
                )
            ]

        # Merge occupancy evidence into the candidate's raw response.
        candidate.raw_vlm_response["mask_overlay_image_path"] = masks_ref
        candidate.raw_vlm_response["vehicle_boxes_image_path"] = vehicles_ref
        candidate.raw_vlm_response["zoom_grid_image_path"] = zoom_grid_ref
        candidate.raw_vlm_response["single_zoom_image_paths"] = single_zoom_refs
        candidate.raw_vlm_response["occupancy_detection"] = occupancy_detection
        return candidate

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
        """Multi-ROI gallery branch for static, evidence-rich events (e.g. construction).

        1. Select the middle frame.
        2. Run the multi-ROI template to get evidence_regions.
        3. Filter ROIs by area/aspect and keep the top ``max_regions`` by confidence.
        4. Build a gallery composite (annotated original + zoom grid).
        5. Run the final classifier on the gallery.
        6. Return an EventCandidate that always includes the gallery path, even
           when the classifier is negative.
        """
        if not images:
            return None

        selected_index = len(images) // 2
        frame = images[selected_index]

        logger.info(
            "[expert_agent:_detect_with_far_enhancement_gallery] START | event_id=%d event_name=%s frame=%d",
            self.category.event_id,
            self.category.name_zh,
            selected_index,
        )

        # --- ROI detection on the middle frame ------------------------------
        try:
            roi_response = self.vlm_engine.call(
                template=roi_template,
                images=[frame],
                context_vars=context_vars,
                response_schema=_MULTI_ROI_DETECTION_SCHEMA,
            )
        except FatalAPIError:
            raise
        except Exception as exc:
            logger.error(
                "[expert_agent:_detect_with_far_enhancement_gallery] ROI_CALL_ERROR | event_id=%d frame=%d | %s",
                self.category.event_id,
                selected_index,
                exc,
                exc_info=True,
            )
            return None

        if not roi_response.success or not isinstance(roi_response.parsed_data, dict):
            logger.warning(
                "[expert_agent:_detect_with_far_enhancement_gallery] ROI_PARSE_ERROR | event_id=%d frame=%d",
                self.category.event_id,
                selected_index,
            )
            return None

        parsed = roi_response.parsed_data
        evidence_regions = parsed.get("evidence_regions", []) or []
        roi_summary = str(parsed.get("summary", ""))

        # --- Validate/filter regions ----------------------------------------
        min_area_px = far_cfg.min_area_px
        max_aspect_ratio = far_cfg.max_aspect_ratio
        valid_regions: List[Dict[str, Any]] = []
        try:
            frame_pil = load_image(frame)
            img_width, img_height = frame_pil.size
        except Exception as exc:
            logger.warning(
                "[expert_agent:_detect_with_far_enhancement_gallery] LOAD_FRAME_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
            )
            return None

        for region in evidence_regions:
            bbox_norm = region.get("bbox_norm")
            tag = str(region.get("tag", "unknown"))
            confidence = self._parse_roi_confidence(region.get("confidence", 0.0))
            if not bbox_norm or len(bbox_norm) != 4:
                logger.info(
                    "[expert_agent:_detect_with_far_enhancement_gallery] INVALID_BBOX | event_id=%d bbox=%s",
                    self.category.event_id,
                    bbox_norm,
                )
                continue
            try:
                area_px = compute_bbox_area_px(bbox_norm, img_width, img_height)
                aspect_ratio = compute_bbox_aspect_ratio(bbox_norm)
                if not is_bbox_large_enough(
                    bbox_norm, img_width, img_height, min_area_px=min_area_px
                ):
                    logger.info(
                        "[expert_agent:_detect_with_far_enhancement_gallery] ROI_TOO_SMALL | event_id=%d area_px=%d < %d",
                        self.category.event_id,
                        area_px,
                        min_area_px,
                    )
                    continue
                if not is_bbox_aspect_valid(bbox_norm, max_ratio=max_aspect_ratio):
                    logger.info(
                        "[expert_agent:_detect_with_far_enhancement_gallery] ASPECT_REJECT | event_id=%d ratio=%.2f",
                        self.category.event_id,
                        aspect_ratio,
                    )
                    continue
            except Exception as exc:
                logger.warning(
                    "[expert_agent:_detect_with_far_enhancement_gallery] SIZE_CHECK_ERROR | event_id=%d | %s",
                    self.category.event_id,
                    exc,
                )
                continue

            valid_regions.append(
                {
                    "bbox_norm": bbox_norm,
                    "tag": tag,
                    "confidence": confidence,
                    "area_px": area_px,
                    "aspect_ratio": aspect_ratio,
                    "on_ground": region.get("on_ground"),
                }
            )

        # Construction-specific: cones must be on the ground.
        valid_regions = self._filter_grounded_construction_regions(valid_regions)

        if not valid_regions:
            logger.info(
                "[expert_agent:_detect_with_far_enhancement_gallery] NO_VALID_REGIONS | event_id=%d",
                self.category.event_id,
            )
            return EventCandidate(
                detected=False,
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                summary=f"未检测到{self.category.name_zh}。",
                raw_vlm_response={
                    "far_enhancement": {
                        "selected_frame_index": selected_index,
                        "evidence_regions": [],
                        "summary": roi_summary,
                    }
                },
            )

        # Keep highest-confidence regions for the gallery.
        valid_regions.sort(key=lambda r: r["confidence"], reverse=True)
        display_regions = valid_regions[:4]

        # --- Build gallery composite ----------------------------------------
        gallery_filename = f"{video_stem}_event_{self.category.event_id}_frame_{selected_index}_gallery.jpg"
        gallery_path = str(output_dir / gallery_filename)
        gallery_ref = f"{image_ref_prefix}/{gallery_filename}"

        try:
            create_multi_roi_gallery(
                frame, display_regions, output_path=gallery_path, max_regions=4
            )
        except Exception as exc:
            logger.error(
                "[expert_agent:_detect_with_far_enhancement_gallery] GALLERY_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
                exc_info=True,
            )
            return None

        logger.info(
            "[expert_agent:_detect_with_far_enhancement_gallery] GALLERY_CREATED | event_id=%d path=%s regions=%d",
            self.category.event_id,
            gallery_path,
            len(display_regions),
        )

        # --- Final classifier on the gallery --------------------------------
        try:
            response = self.vlm_engine.call(
                template=template,
                images=[gallery_path],
                context_vars=context_vars,
                response_schema=_EXPERT_RESPONSE_SCHEMA,
            )
        except FatalAPIError:
            raise
        except Exception as exc:
            logger.error(
                "[expert_agent:_detect_with_far_enhancement_gallery] FINAL_CALL_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
                exc_info=True,
            )
            return EventCandidate(
                detected=False,
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                summary=f"{self.category.name_zh}增强分类失败。",
                raw_vlm_response={
                    "gallery_image_path": gallery_ref,
                    "far_enhancement": {
                        "selected_frame_index": selected_index,
                        "evidence_regions": display_regions,
                        "summary": roi_summary,
                    },
                },
            )

        candidate = parse_expert_response(response, self.category)
        # Ensure the selected frame is recorded as evidence.
        if candidate.instances:
            for inst in candidate.instances:
                if not inst.evidence_frames:
                    inst.evidence_frames = [selected_index]
        else:
            candidate.instances = [
                EventInstance(
                    event_id=self.category.event_id,
                    event_name=self.category.name_zh,
                    evidence_frames=[selected_index],
                    description=candidate.summary,
                    reasoning=candidate.summary,
                )
            ]

        # Apply structured car veto (and capture target_type) for the gallery
        # classifier, just like the per-frame far-enhancement path.
        candidate = self._apply_structured_veto_to_candidate(candidate)

        # ------------------------------------------------------------------
        # Construction fallback: if the final classifier rejected the scene
        # but the ROI evidence clearly satisfies the work-zone definition,
        # promote the candidate to detected=True. This keeps the expert
        # output consistent with the evidence table and gallery image.
        # ------------------------------------------------------------------
        if (
            self.category.event_id == 6
            and not candidate.detected
            and self._has_construction_evidence(valid_regions)
        ):
            logger.info(
                "[expert_agent:_detect_with_far_enhancement_gallery] CONSTRUCTION_FALLBACK | "
                "event_id=%d frame=%d regions=%d",
                self.category.event_id,
                selected_index,
                len(valid_regions),
            )
            candidate = self._build_construction_fallback_candidate(
                candidate=candidate,
                display_regions=display_regions,
                valid_regions=valid_regions,
                selected_index=selected_index,
                gallery_ref=gallery_ref,
                roi_summary=roi_summary,
            )

        # Merge the gallery metadata into the candidate's raw response.
        candidate.raw_vlm_response["gallery_image_path"] = gallery_ref
        candidate.raw_vlm_response.setdefault("far_enhancement", {})
        candidate.raw_vlm_response["far_enhancement"].update(
            {
                "selected_frame_index": selected_index,
                "evidence_regions": display_regions,
                "summary": roi_summary,
            }
        )
        return candidate
    def _detect_with_far_enhancement(
        self,
        context: AnalysisContext,
        images: List[Any],
        template: PromptTemplate,
        context_vars: Dict[str, Any],
        default_output_dir: Path = _FAR_ENHANCEMENT_OUTPUT_DIR,
    ) -> Optional[EventCandidate]:
        """Run the far-distance object enhancement flow.

        Two-pass design:
        1. Collect ROI candidates from every input frame. Valid ROIs must pass
           the configured minimum-area check and aspect-ratio filter.
        2. Score all candidates and keep the top ``far_object_enhancement.top_k``.
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

        far_cfg = template.far_object_enhancement
        roi_template_id = far_cfg.roi_template_id
        min_area_px = far_cfg.min_area_px
        max_aspect_ratio = far_cfg.max_aspect_ratio
        enable_motion_filter = far_cfg.enable_motion_filter
        motion_score_threshold = far_cfg.motion_score_threshold
        motion_penalty = far_cfg.motion_penalty
        top_k = far_cfg.top_k

        try:
            roi_template = self.config_manager.get_prompt_template(roi_template_id)
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
        # project-root default for backward compatibility. Artifacts are always
        # grouped into a per-video subdirectory named after the video stem.
        report_output_dir = getattr(context, "output_dir", None)
        if report_output_dir:
            output_dir = Path(report_output_dir) / "tmp_img" / video_stem
            image_ref_prefix = f"tmp_img/{video_stem}"
        else:
            output_dir = default_output_dir / video_stem
            image_ref_prefix = str(default_output_dir / video_stem)
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
        # Emergency lane occupancy branch (event_id=1).
        # Uses a single middle frame and polygon/ROI-based evidence generation.
        # Must be checked before the generic "middle" gallery branch so that
        # event_id=6 keeps using the construction gallery path.
        # ------------------------------------------------------------------
        if self.category.event_id == 1:
            return self._detect_emergency_lane_occupancy(
                context=context,
                images=images,
                template=template,
                context_vars=context_vars,
                roi_template=roi_template,
                output_dir=output_dir,
                image_ref_prefix=image_ref_prefix,
                video_stem=video_stem,
                far_cfg=far_cfg,
            )

        # ------------------------------------------------------------------
        # Multi-ROI gallery branch (e.g. event_id=6 road construction).
        # Uses a single middle frame and a gallery of evidence ROIs.
        # ------------------------------------------------------------------
        if far_cfg.frame_selection == "middle":
            return self._detect_with_far_enhancement_gallery(
                context=context,
                images=images,
                template=template,
                context_vars=context_vars,
                roi_template=roi_template,
                output_dir=output_dir,
                image_ref_prefix=image_ref_prefix,
                video_stem=video_stem,
                far_cfg=far_cfg,
            )

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
                    bbox_norm, img_width, img_height, min_area_px=min_area_px
                ):
                    self._log_frame(
                        frame_analysis_log,
                        frame_log,
                        reason=f"ROI candidate too small: area_px={bbox_area} < {min_area_px}",
                    )
                    logger.info(
                        "[expert_agent:_detect_with_far_enhancement] ROI_TOO_SMALL | event_id=%d frame=%d area_px=%d < %d",
                        self.category.event_id,
                        global_index,
                        bbox_area,
                        min_area_px,
                    )
                    continue
                if not is_bbox_aspect_valid(
                    bbox_norm, max_ratio=max_aspect_ratio
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

            # Compute adjacent-frame motion inside the enlarged ROI when
            # configured.  This is used to penalise static foreground objects
            # (camera brackets, poles, wires) that the VLM occasionally returns
            # as false ROIs.
            adjacent_index = (
                global_index - 1
                if global_index == len(images) - 1
                else global_index + 1
            )
            if enable_motion_filter:
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
            else:
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
                summary=f"未检测到{self.category.name_zh}。",
                raw_vlm_response={
                    "far_enhancement": {
                        "frame_analysis_log": frame_analysis_log,
                    }
                },
            )

        # ------------------------------------------------------------------
        # Confidence gate for pedestrians (event_id=3) and non-motor vehicles
        # (event_id=4): only ROIs with confidence >= 0.6 enter the final
        # classifier. This reduces false positives from distant, low-confidence
        # enhancements. When the gate drops every candidate, keep the best ROI
        # as evidence so the report shows what was analysed and rejected.
        # ------------------------------------------------------------------
        if self.category.event_id in (3, 4):
            total_candidates = len(candidates)
            gated_candidates = [c for c in candidates if c.get("confidence", 0.0) >= 0.6]
            if not gated_candidates:
                entity = "高速公路行人" if self.category.event_id == 3 else "非机动车"
                logger.info(
                    "[expert_agent:_detect_with_far_enhancement] LOW_CONFIDENCE_FILTER | "
                    "event_id=%d kept=0 total=%d",
                    self.category.event_id,
                    total_candidates,
                )
                negative_raw_response = self._generate_low_confidence_evidence(
                    candidates,
                    images,
                    output_dir,
                    image_ref_prefix,
                    video_stem,
                    frame_analysis_log,
                    motion_score_threshold,
                    motion_penalty,
                )
                return EventCandidate(
                    detected=False,
                    event_id=self.category.event_id,
                    event_name=self.category.name_zh,
                    summary=f"未检测到{entity}。所有远距离候选ROI置信度均低于0.6。",
                    raw_vlm_response=negative_raw_response,
                )
            candidates = gated_candidates

        # ------------------------------------------------------------------
        # Rank candidates and keep the top K.
        # ------------------------------------------------------------------
        ranked_candidates = self._score_far_candidates(
            candidates,
            motion_score_threshold=motion_score_threshold,
            motion_penalty=motion_penalty,
        )
        top_candidates = ranked_candidates[:top_k]
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
            composite_filename = f"{video_stem}_event_{self.category.event_id}_frame_{global_index}_composite.jpg"
            composite_path = str(output_dir / composite_filename)
            composite_ref = f"{image_ref_prefix}/{composite_filename}"
            adjacent_index = candidate["adjacent_index"]
            motion_composite_filename = (
                f"{video_stem}_event_{self.category.event_id}_frame_{global_index}_motion_{adjacent_index}.jpg"
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
        # Fallback is used for far-distance object categories (event_id=3
        # pedestrians and event_id=4 non-motor vehicles). When the final
        # classifier is over-conservative but the ROI detector produced a
        # high-confidence, unoccluded, well-shaped candidate, promote it to
        # detected=True so the expert raw output stays consistent with the
        # per-frame ROI evidence table.
        # ------------------------------------------------------------------
        if not top_candidates:
            # top_k <= 0 (or no candidates survived scoring): nothing to
            # classify, return the standard negative candidate.
            logger.info(
                "[expert_agent:_detect_with_far_enhancement] NO_POSITIVE_CANDIDATES | event_id=%d",
                self.category.event_id,
            )
            return EventCandidate(
                detected=False,
                event_id=self.category.event_id,
                event_name=self.category.name_zh,
                summary=f"未检测到{self.category.name_zh}。",
                raw_vlm_response={
                    "far_enhancement": {
                        "frame_analysis_log": frame_analysis_log,
                    }
                },
            )
        best_candidate = top_candidates[0]
        if "composite_path" in best_candidate and self.category.event_id in (3, 4):
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
            summary=f"未检测到{self.category.name_zh}。",
            raw_vlm_response=negative_raw_response,
            is_target_explicitly_four_wheel_vehicle=best_candidate.get(
                "is_target_explicitly_four_wheel_vehicle"
            ),
            target_type=str(best_candidate.get("target_type", "")),
        )

