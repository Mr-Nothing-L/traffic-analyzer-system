"""
PipelineStep module for the traffic analyzer framework.

Provides a pluggable step-based architecture for the analysis pipeline.
Each step encapsulates a discrete phase of analysis (scene understanding,
event detection, post-processing) with built-in retry support.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.external_adapter import ExternalAdapter
from traffic_analyzer.core.logic_engine import LogicEngine, _parse_scene_tags
from traffic_analyzer.core.vlm_engine import VLMInferenceEngine
from traffic_analyzer.utils.tool_call_logger import tool_call
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    CrossEventInferenceRule,
    DirectionAnalysis,
    DirectionConclusion,
    ConsistencyCheck,
    HeadOrientation,
    EventCategory,
    EventInstance,
    EventResult,
    KeyframeSequence,
    LogicChain,
    PromptTemplate,
    SceneInfo,
    VehicleMotion,
    VideoMetadata,
)

logger = logging.getLogger(__name__)


def _get_system_font_path() -> Optional[str]:
    """Return a path to a usable bold system font, or None if none found."""
    import os
    import platform

    system = platform.system()
    candidates: List[str] = []
    if system == "Windows":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        candidates = [
            os.path.join(windir, r"Fonts\arialbd.ttf"),
            os.path.join(windir, r"Fonts\arial.ttf"),
            os.path.join(windir, r"Fonts\msyhbd.ttc"),
            os.path.join(windir, r"Fonts\simhei.ttf"),
        ]
    elif system == "Darwin":
        candidates = [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Arial.ttf",
            "/Library/Fonts/Arial Bold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _annotate_frame(image: Any, label: str) -> bytes:
    """Overlay *label* on the top-left corner of *image* and return as JPEG bytes.

    The label is drawn with a dark semi-transparent background so it remains
    readable regardless of image content.
    """
    import io

    if isinstance(image, bytes):
        img = Image.open(io.BytesIO(image))
    elif isinstance(image, str):
        img = Image.open(image)
    elif isinstance(image, Image.Image):
        img = image.copy()
    else:
        raise TypeError(f"Unsupported image type: {type(image)}")

    img = img.convert("RGB")

    # Resize to 1080p max for better VLM detail recognition
    img.thumbnail((1920, 1080), Image.LANCZOS)

    # Try to load a TrueType font; fall back to default bitmap font
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    font_path = _get_system_font_path()
    if font_path:
        try:
            font = ImageFont.truetype(font_path, 28)
        except Exception:
            font = ImageFont.load_default()
    else:
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(img)

    # Measure text size
    bbox = draw.textbbox((0, 0), label, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 10
    rect = [4, 4, 8 + text_w + pad * 2, 8 + text_h + pad * 2]

    # Draw semi-transparent black background for label
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rectangle(rect, fill=(0, 0, 0, 180))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    # Draw text on top of the background
    draw = ImageDraw.Draw(img)
    draw.text((8 + pad, 8 + pad), label, fill=(255, 255, 255), font=font)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


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


class SceneUnderstandingStep(PipelineStep):
    """Step 1: Global scene understanding via VLM."""

    def __init__(
        self,
        config_manager: ConfigManager,
        vlm_engine: VLMInferenceEngine,
        max_retries: int = 1,
    ) -> None:
        super().__init__("scene_understanding", max_retries=max_retries, fallback_enabled=True)
        self.config_manager = config_manager
        self.vlm_engine = vlm_engine

    def _execute(self, context: AnalysisContext) -> SceneInfo:
        keyframes = context.keyframes
        if not keyframes:
            raise ValueError("No keyframes available for scene understanding")

        try:
            template = self.config_manager.get_prompt_template("scene_understanding")
        except KeyError:
            logger.warning("scene_understanding template not found, using default")
            template = PromptTemplate(
                template_id="scene_understanding",
                name="Scene Understanding",
                system_prompt="You are a traffic surveillance analyst.",
                user_prompt="Analyze these video frames and describe the road structure, traffic flow, and conditions.",
            )

        coarse_frames = keyframes.coarse_frames
        total_coarse = len(coarse_frames)
        target_count = 20

        if total_coarse >= target_count:
            front_count = min(5, total_coarse)
            front_frames = coarse_frames[:front_count]
            remaining = coarse_frames[front_count:]
            back_count = target_count - front_count
            if remaining and back_count > 0:
                back_indices = [
                    front_count + int(i * (len(remaining) - 1) / max(back_count - 1, 1))
                    for i in range(back_count)
                ]
                back_frames = [coarse_frames[i] for i in back_indices]
            else:
                back_frames = []
            raw_frames = (front_frames + back_frames)[:target_count]
        else:
            raw_frames = coarse_frames[:]
            logger.info(
                "Coarse frames insufficient (%d < %d), using all available",
                total_coarse,
                target_count,
            )

        total = len(raw_frames)
        logger.info(
            "Scene understanding: using %d frames (first %d dense + %d uniform tail)",
            total,
            min(5, total),
            max(0, total - 5),
        )
        images: List[Any] = []
        for idx, kf in enumerate(raw_frames):
            img = kf.image_data or kf.image_path
            if img is None:
                continue
            if idx == 0:
                label = f"第1帧(最早) | Frame 1/{total} | t={kf.timestamp_sec:.1f}s | [VIDEO START]"
            elif idx == total - 1:
                label = f"第{total}帧(最新) | Frame {total}/{total} | t={kf.timestamp_sec:.1f}s | [VIDEO END]"
            else:
                label = f"第{idx + 1}帧 | Frame {idx + 1}/{total} | t={kf.timestamp_sec:.1f}s"
            annotated = _annotate_frame(img, label)
            images.append(annotated)

        response = self.vlm_engine.call(
            template=template,
            images=images,
            context_vars={},
        )

        if not response.success or not isinstance(response.parsed_data, dict):
            logger.warning("Scene understanding failed: %s", response.raw_text)
            raise ResponseParseError("Scene understanding VLM call failed")

        data = response.parsed_data
        roads = data.get("roads", [])
        scene_info = SceneInfo(
            road_count=len(roads),
            roads=roads,
            weather=data.get("weather", "unknown"),
            lighting=data.get("lighting", "unknown"),
            traffic_density=data.get("traffic_density", "unknown"),
            total_vehicles_estimate=data.get("total_vehicles_estimate", 0),
            scene_description=data.get("scene_description", ""),
            confidence=data.get("confidence", 0.0),
            pedestrian_present=data.get("pedestrian_present"),
            non_motor_vehicle_present=data.get("non_motor_vehicle_present"),
            thrown_object_present=data.get("thrown_object_present"),
        )

        # Parse direction_analysis if present
        dir_data = data.get("direction_analysis", {})
        if dir_data:
            scene_info = self._parse_direction_analysis(scene_info, dir_data)

        return scene_info

    def _parse_direction_analysis(self, scene_info: SceneInfo, dir_data: Dict[str, Any]) -> SceneInfo:
        conclusions = dir_data.get("conclusions", [])
        vehicle_motions = dir_data.get("vehicle_motions", [])
        updated_count = 0

        for conclusion in conclusions:
            road_id = conclusion.get("road_id", 0)
            for road in scene_info.roads:
                if road.road_id == road_id:
                    new_dir = conclusion.get("normal_direction", "")
                    if new_dir and new_dir != "unknown":
                        road.normal_direction = new_dir
                        road.direction_confidence = conclusion.get("confidence", 0.0)
                        updated_count += 1
                    for motion in vehicle_motions:
                        if motion.get("road_id") == road_id:
                            from traffic_analyzer.models.schemas import DirectionEvidence
                            road.direction_evidence.append(
                                DirectionEvidence(
                                    vehicle=motion.get("description", ""),
                                    movement=motion.get("movement_direction", "unknown"),
                                    location_earlier=motion.get("displacement", ""),
                                )
                            )

        from traffic_analyzer.models.schemas import (
            DirectionConclusion,
            VehicleMotion,
            HeadOrientation,
            ConsistencyCheck,
            PerspectiveCheck,
        )
        scene_info.direction_analysis = DirectionAnalysis(
            anchor_points=dir_data.get("anchor_points", []),
            vehicle_motions=[
                VehicleMotion(
                    vehicle_id=m.get("vehicle_id", ""),
                    description=m.get("description", ""),
                    displacement=m.get("displacement", ""),
                    movement_direction=m.get("movement_direction", "unknown"),
                    road_id=m.get("road_id", 0),
                )
                for m in vehicle_motions
            ],
            head_orientations=[
                HeadOrientation(
                    vehicle_id=h.get("vehicle_id", ""),
                    head_orientation=h.get("head_orientation", "unknown"),
                    evidence=h.get("evidence", ""),
                )
                for h in dir_data.get("head_orientations", [])
            ],
            consistency_check=[
                ConsistencyCheck(
                    vehicle_id=c.get("vehicle_id", ""),
                    movement=c.get("movement", "unknown"),
                    head_orientation=c.get("head_orientation", "unknown"),
                    consistent=c.get("consistent", True),
                    anomaly=c.get("anomaly", False),
                )
                for c in dir_data.get("consistency_check", [])
            ],
            perspective_check=[
                PerspectiveCheck(
                    vehicle_id=p.get("vehicle_id", ""),
                    size_change=p.get("size_change", ""),
                    matches_direction=p.get("matches_direction", True),
                    trajectory_parallel_to_lanes=p.get("trajectory_parallel_to_lanes", True),
                )
                for p in dir_data.get("perspective_check", [])
            ],
            conclusions=[
                DirectionConclusion(
                    road_id=c.get("road_id", 0),
                    name=c.get("name", ""),
                    normal_direction=c.get("normal_direction", "unknown"),
                    confidence=c.get("confidence", 0.0),
                    evidence_summary=c.get("evidence_summary", ""),
                )
                for c in conclusions
            ],
        )
        logger.info(
            "Direction analysis parsed: updated %d/%d roads",
            updated_count,
            len(scene_info.roads),
        )
        return scene_info

    def _fallback(self, context: AnalysisContext, error: Optional[Exception]) -> SceneInfo:
        logger.warning("Scene understanding fallback: returning empty SceneInfo")
        return SceneInfo()


from traffic_analyzer.core.vlm_engine import ResponseParseError


class EventDetectionStep(PipelineStep):
    """Step 2: Detect all configured events (direct_vlm, logic_chain, scene_tag)."""

    def __init__(
        self,
        config_manager: ConfigManager,
        vlm_engine: VLMInferenceEngine,
        logic_engine: LogicEngine,
        max_retries: int = 0,
    ) -> None:
        super().__init__("event_detection", max_retries=max_retries)
        self.config_manager = config_manager
        self.vlm_engine = vlm_engine
        self.logic_engine = logic_engine

    def _execute(self, context: AnalysisContext) -> List[EventResult]:
        event_categories = self.config_manager.get_event_categories()
        results: List[EventResult] = []

        direct_vlm_categories: List[EventCategory] = []
        sequential_categories: List[EventCategory] = []

        for category in event_categories:
            if not category.is_active:
                continue
            if category.detection_mode == "direct_vlm":
                direct_vlm_categories.append(category)
            else:
                sequential_categories.append(category)

        # Parallel direct_vlm batch
        if direct_vlm_categories:
            results.extend(self._detect_parallel_direct_vlm(direct_vlm_categories, context))

        # Sequential logic_chain / scene_tag
        for category in sequential_categories:
            try:
                if category.detection_mode == "logic_chain":
                    result = self._detect_logic_chain(category, context)
                elif category.detection_mode == "scene_tag":
                    with tool_call(
                        "event_detector.detect",
                        event=category.event_id,
                        mode="scene_tag",
                    ) as _tc:
                        result = EventResult(
                            event_id=category.event_id,
                            event_name=category.name_zh,
                            detected=False,
                            summary="等待场景标签后处理",
                        )
                        _tc.result(
                            "stub created, real detection in post_process"
                        )
                else:
                    logger.warning("Unknown detection mode for %s: %s", category.name_zh, category.detection_mode)
                    result = EventResult(
                        event_id=category.event_id,
                        event_name=category.name_zh,
                        detected=False,
                        summary=f"Unknown detection mode: {category.detection_mode}",
                    )
                results.append(result)
                context.event_results[result.event_id] = result
            except Exception as exc:
                logger.error("Event detection failed for %s: %s", category.name_zh, exc, exc_info=True)
                error_result = EventResult(
                    event_id=category.event_id,
                    event_name=category.name_zh,
                    detected=False,
                    summary=f"Detection error: {exc}",
                )
                results.append(error_result)
                context.event_results[category.event_id] = error_result

        return results

    def _detect_parallel_direct_vlm(
        self,
        categories: List[EventCategory],
        context: AnalysisContext,
    ) -> List[EventResult]:
        logger.info("[并行检测] 准备并行检测 %d 个 direct_vlm 事件", len(categories))
        batch_requests: List[Dict[str, Any]] = []
        batched_categories: List[EventCategory] = []
        shared_images = self._get_event_images(context)

        for category in categories:
            template_id = category.prompt_template_id
            if not template_id:
                logger.error("Event %s has no prompt_template_id", category.name_zh)
                error_result = EventResult(
                    event_id=category.event_id,
                    event_name=category.name_zh,
                    detected=False,
                    summary="Configuration error: no prompt_template_id",
                )
                results.append(error_result)
                context.event_results[category.event_id] = error_result
                continue
            try:
                template = self.config_manager.get_prompt_template(template_id)
            except KeyError:
                template = PromptTemplate(
                    template_id=template_id,
                    name="Direct Event Detection",
                    system_prompt="You are a traffic surveillance analyst.",
                    user_prompt=f"Detect {category.name_zh}: {category.description}",
                )

            ctx_vars: Dict[str, Any] = {
                "event_name": category.name_zh,
                "event_definition": category.definition,
                "visual_indicators": category.visual_indicators,
            }
            if context.scene_understanding:
                ctx_vars["scene_understanding"] = context.scene_understanding

            batch_requests.append({
                "template": template,
                "images": shared_images,
                "context_vars": ctx_vars,
            })
            batched_categories.append(category)

        if not batch_requests:
            return []

        batch_start = time.perf_counter()
        try:
            batch_responses = self.vlm_engine.batch_call(
                batch_requests,
                parallel=True,
                max_workers=min(4, len(batched_categories)),
            )
        except Exception as exc:
            logger.error("Batch call failed entirely: %s", exc, exc_info=True)
            # Return error results for all batched categories so the pipeline continues
            error_results: List[EventResult] = []
            for category in batched_categories:
                error_result = EventResult(
                    event_id=category.event_id,
                    event_name=category.name_zh,
                    detected=False,
                    summary=f"Batch detection error: {exc}",
                )
                error_results.append(error_result)
                context.event_results[category.event_id] = error_result
            return error_results
        batch_duration = time.perf_counter() - batch_start

        if len(batch_responses) != len(batched_categories):
            logger.error(
                "Batch response count mismatch: expected %d, got %d",
                len(batched_categories),
                len(batch_responses),
            )

        valid_count = 0
        results: List[EventResult] = []
        for idx, category in enumerate(batched_categories):
            if idx >= len(batch_responses):
                logger.error("[parallel] Missing response for %s", category.name_zh)
                error_result = EventResult(
                    event_id=category.event_id,
                    event_name=category.name_zh,
                    detected=False,
                    summary="Batch response missing",
                )
                results.append(error_result)
                context.event_results[category.event_id] = error_result
                continue

            resp = batch_responses[idx]
            detected = (
                resp.parsed_data.get("detected", False)
                if resp.success and isinstance(resp.parsed_data, dict)
                else "N/A"
            )
            logger.info("[parallel] done: %s, detected=%s", category.name_zh, detected)
            result = self._parse_direct_vlm_response(resp, category)
            with tool_call(
                "event_detector.detect",
                event=category.event_id,
                mode="direct_vlm",
            ) as _tc:
                _tc.result(
                    f"detected={result.detected}, "
                    f"confidence={result.confidence:.2f}"
                )
            results.append(result)
            context.event_results[result.event_id] = result
            if resp.success and isinstance(resp.parsed_data, dict):
                valid_count += 1

        logger.info(
            "[parallel] all done: %d/%d events (%.2fs)",
            valid_count,
            len(batched_categories),
            batch_duration,
        )
        return results

    def _get_event_images(self, context: AnalysisContext) -> List[Any]:
        images: List[Any] = []
        if not context.keyframes:
            return images

        max_frames = 6
        if (
            self.config_manager._system_config is not None
            and self.config_manager._system_config.vlm_max_frames > 0
        ):
            max_frames = self.config_manager._system_config.vlm_max_frames

        coarse = context.keyframes.coarse_frames
        if len(coarse) > max_frames:
            indices = [int(i * (len(coarse) - 1) / (max_frames - 1)) for i in range(max_frames)]
            selected = [coarse[i] for i in indices]
        else:
            selected = coarse

        images = [kf.image_data or kf.image_path for kf in selected]
        images = [img for img in images if img is not None]
        return images

    def _parse_direct_vlm_response(self, response: Any, category: EventCategory) -> EventResult:
        if response.success and isinstance(response.parsed_data, dict):
            data = response.parsed_data
            detected = bool(data.get("detected", False))
            instances_data = data.get("instances", [])
            instances = []
            if isinstance(instances_data, list):
                for inst in instances_data:
                    if isinstance(inst, dict):
                        instances.append(
                            EventInstance(
                                event_id=category.event_id,
                                event_name=category.name_zh,
                                start_time_sec=float(inst.get("start_time_sec", 0.0)),
                                end_time_sec=float(inst.get("end_time_sec", 0.0)),
                                confidence=float(inst.get("confidence", 0.0)),
                                evidence_frames=inst.get("evidence_frames", []),
                                description=str(inst.get("description", "")),
                                reasoning=str(inst.get("reasoning", "")),
                            )
                        )
            return EventResult(
                event_id=category.event_id,
                event_name=category.name_zh,
                detected=detected,
                instances=instances,
                summary=str(data.get("summary", "")),
                confidence=float(data.get("confidence", 0.0)),
            )

        return EventResult(
            event_id=category.event_id,
            event_name=category.name_zh,
            detected=False,
            summary=f"VLM call failed or returned invalid data: {response.raw_text[:200]}",
        )

    def _detect_logic_chain(self, category: EventCategory, context: AnalysisContext) -> EventResult:
        if not category.logic_chain_id:
            return EventResult(
                event_id=category.event_id,
                event_name=category.name_zh,
                detected=False,
                summary="Logic chain ID not configured",
            )

        logic_chain = self.config_manager.get_logic_chain(category.logic_chain_id)
        if not logic_chain:
            return EventResult(
                event_id=category.event_id,
                event_name=category.name_zh,
                detected=False,
                summary=f"Logic chain '{category.logic_chain_id}' not found",
            )

        return self.logic_engine.execute(logic_chain, context)


class PostProcessStep(PipelineStep):
    """Step 3: Post-process inferred events (cross-event inference, boolean fields, tags)."""

    def __init__(
        self,
        config_manager: ConfigManager,
        max_retries: int = 0,
    ) -> None:
        super().__init__("post_processing", max_retries=max_retries, fallback_enabled=True)
        self.config_manager = config_manager

    def _execute(self, context: AnalysisContext) -> List[EventResult]:
        event_results = list(context.event_results.values())
        scene_info = context.scene_understanding
        results_by_id = {r.event_id: r for r in event_results}

        # Phase 1: Cross-event inference
        event_categories = {c.event_id: c for c in self.config_manager.get_event_categories()}
        for rule in self.config_manager.get_inference_rules():
            self._apply_cross_event_inference(rule, results_by_id, event_categories)

        # Phase 2: Boolean field inference
        if scene_info:
            for cat in self.config_manager.get_event_categories():
                if cat.detection_mode.value != "scene_tag" or not cat.scene_boolean_field:
                    continue
                result = results_by_id.get(cat.event_id)
                if not result:
                    continue

                present = getattr(scene_info, cat.scene_boolean_field, None)
                if present is None:
                    continue

                if present is False:
                    result.detected = False
                    result.confidence = 0.0
                    result.instances = []
                    result.summary = f"{cat.name_zh}未检测到（场景理解结构化字段为 False）"
                    logger.info("%s explicitly FALSE from scene boolean field", cat.name_zh)
                    continue

                if present is True and not result.detected:
                    inferred_confidence = 0.65
                    result.detected = True
                    result.confidence = max(result.confidence, inferred_confidence)
                    result.instances = [
                        EventInstance(
                            event_id=cat.event_id,
                            event_name=result.event_name,
                            event_name_en=result.event_name_en,
                            start_time_sec=0.0,
                            end_time_sec=0.0,
                            confidence=inferred_confidence,
                            description=f"{cat.name_zh}（由场景理解结构化字段推断）",
                            reasoning=f"场景理解判断{cat.name_zh}存在。",
                        )
                    ]
                    result.summary = f"{cat.name_zh}（由场景理解推断）"
                    logger.info("Inferred %s from scene boolean field", cat.name_zh)

        # Phase 3: Structured tag inference
        if scene_info and scene_info.scene_description:
            tag_map = _parse_scene_tags(scene_info.scene_description)
            for cat in self.config_manager.get_event_categories():
                if cat.detection_mode.value != "scene_tag" or not cat.scene_tag_key:
                    continue
                result = results_by_id.get(cat.event_id)
                if not result:
                    continue

                if cat.scene_boolean_field and getattr(scene_info, cat.scene_boolean_field, None) is not None:
                    continue

                tag_value = tag_map.get(cat.scene_tag_key, "")

                if tag_value.startswith("无"):
                    result.detected = False
                    result.confidence = 0.0
                    result.instances = []
                    result.summary = f"{cat.name_zh}未检测到（scene_description 结构化标签为 无）"
                    logger.info("%s tag is '%s' — marking not detected", cat.name_zh, tag_value)
                elif tag_value.startswith("有") and not result.detected:
                    inferred_confidence = 0.65
                    result.detected = True
                    result.confidence = max(result.confidence, inferred_confidence)
                    result.instances = [
                        EventInstance(
                            event_id=cat.event_id,
                            event_name=result.event_name,
                            event_name_en=result.event_name_en,
                            start_time_sec=0.0,
                            end_time_sec=0.0,
                            confidence=inferred_confidence,
                            description=f"{cat.name_zh}（由场景描述推断：{tag_value}）",
                            reasoning=f"场景描述中 {{{cat.scene_tag_key}：...}} 标签显示存在{cat.name_zh}。",
                        )
                    ]
                    result.summary = f"{cat.name_zh}（由场景描述推断：{tag_value}）"
                    logger.info("Inferred %s from structured tag: %s", cat.name_zh, tag_value)

        return list(results_by_id.values())

    def _apply_cross_event_inference(
        self,
        rule: CrossEventInferenceRule,
        results_by_id: Dict[int, EventResult],
        event_categories: Dict[int, EventCategory],
    ) -> None:

        source_result = results_by_id.get(rule.source_event_id)
        target_result = results_by_id.get(rule.target_event_id)
        if not source_result or not target_result:
            return
        if not source_result.detected:
            return

        target_cat = event_categories.get(rule.target_event_id)
        target_name = target_cat.name_zh if target_cat else target_result.event_name

        inferred_instances: List[EventInstance] = []
        for inst in source_result.instances:
            desc = (inst.description or "").lower()
            if any(kw.lower() in desc for kw in rule.source_description_keywords):
                inferred_instances.append(
                    EventInstance(
                        event_id=rule.target_event_id,
                        event_name=target_name,
                        event_name_en=target_result.event_name_en,
                        start_time_sec=inst.start_time_sec,
                        end_time_sec=inst.end_time_sec,
                        confidence=inst.confidence * rule.confidence_multiplier,
                        description=f"{rule.description_prefix}（源自{inst.description}）",
                        reasoning=rule.reasoning,
                    )
                )

        if inferred_instances:
            target_result.detected = True
            target_result.confidence = max(
                target_result.confidence,
                max(i.confidence for i in inferred_instances),
            )
            target_result.instances.extend(inferred_instances)
            source_cat = event_categories.get(rule.source_event_id)
            source_name = source_cat.name_zh if source_cat else str(rule.source_event_id)
            target_result.summary = (
                f"{target_name}（由{source_name}结果推断，"
                f"共 {len(inferred_instances)} 个实例）"
            )
            logger.info(
                "Inferred %s from event %d (%d instance(s))",
                target_name,
                rule.source_event_id,
                len(inferred_instances),
            )

    def _fallback(self, context: AnalysisContext, error: Optional[Exception]) -> List[EventResult]:
        logger.warning("Post-processing fallback: returning unmodified event results")
        return list(context.event_results.values())
