"""
AnalysisOrchestrator module for the traffic analyzer framework.

Wires all modules together into a cohesive analysis pipeline:
1. Load configuration
2. Preprocess video (coarse + precision sampling)
3. Global scene understanding via VLM
4. Iterate events (direct VLM or logic chain)
5. Cross-validate with CV tracks if provided
6. Generate final report
"""

from __future__ import annotations

import io
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw, ImageFont

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.external_adapter import ExternalAdapter
from traffic_analyzer.core.logic_engine import LogicEngine, _parse_scene_tags
from traffic_analyzer.core.report_generator import ReportGenerator
from traffic_analyzer.core.video_preprocessor import VideoPreprocessor
from traffic_analyzer.core.vlm_engine import VLMInferenceEngine
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    DirectionEvidence,
    EventCategory,
    EventInstance,
    EventResult,
    KeyframeSequence,
    PromptTemplate,
    Report,
    SceneInfo,
    VideoMetadata,
)

logger = logging.getLogger(__name__)


def _annotate_frame(image: Any, label: str, grid_cols: int = 6, grid_rows: int = 6) -> bytes:
    """Overlay *label* on the top-left corner of *image* and return as JPEG bytes.

    The label is drawn with a dark semi-transparent background so it remains
    readable regardless of image content.

    Additionally, a semi-transparent reference grid is drawn on the image to help
    the VLM describe vehicle positions and movements using grid coordinates.
    """
    if isinstance(image, bytes):
        img = Image.open(io.BytesIO(image))
    elif isinstance(image, str):
        img = Image.open(image)
    elif isinstance(image, Image.Image):
        img = image.copy()
    else:
        raise TypeError(f"Unsupported image type: {type(image)}")

    img = img.convert("RGB")
    draw = ImageDraw.Draw(img)

    # Try to load a TrueType font; fall back to default bitmap font
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
    except Exception:
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 28)
        except Exception:
            font = ImageFont.load_default()

    # Try to load a smaller font for grid labels
    grid_font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    try:
        grid_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    except Exception:
        try:
            grid_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
        except Exception:
            grid_font = font

    width, height = img.size

    # ------------------------------------------------------------------
    # Draw semi-transparent reference grid
    # ------------------------------------------------------------------
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    # Grid line styling
    grid_color = (255, 255, 255, 60)  # white, very low alpha
    grid_label_color = (255, 255, 255, 120)

    col_step = width / grid_cols
    row_step = height / grid_rows

    # Vertical lines + column labels
    for c in range(grid_cols + 1):
        x = int(c * col_step)
        overlay_draw.line([(x, 0), (x, height)], fill=grid_color, width=1)
        if c < grid_cols:
            label_x = int(x + col_step / 2)
            overlay_draw.text(
                (label_x, 4),
                str(c + 1),
                fill=grid_label_color,
                font=grid_font,
                anchor="mm",
            )

    # Horizontal lines + row labels
    for r in range(grid_rows + 1):
        y = int(r * row_step)
        overlay_draw.line([(0, y), (width, y)], fill=grid_color, width=1)
        if r < grid_rows:
            label_y = int(y + row_step / 2)
            overlay_draw.text(
                (4, label_y),
                str(r + 1),
                fill=grid_label_color,
                font=grid_font,
                anchor="lm",
            )

    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    # ------------------------------------------------------------------
    # Draw label background + text
    # ------------------------------------------------------------------
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

    # Draw text
    draw = ImageDraw.Draw(img)
    draw.text((8 + pad, 8 + pad), label, fill=(255, 255, 255), font=font)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


import re

class OrchestratorError(Exception):
    """Base exception for orchestration errors."""


class AnalysisOrchestrator:
    """Orchestrates the full traffic analysis pipeline."""

    def __init__(
        self,
        config_manager: ConfigManager,
        video_preprocessor: VideoPreprocessor,
        vlm_engine: VLMInferenceEngine,
        logic_engine: LogicEngine,
        report_generator: ReportGenerator,
        external_adapter: Optional[ExternalAdapter] = None,
    ) -> None:
        """Initialize the orchestrator with all required modules.

        Args:
            config_manager: Loaded configuration manager.
            video_preprocessor: Video preprocessing module.
            vlm_engine: VLM inference engine.
            logic_engine: Logic chain execution engine.
            report_generator: Report generation module.
            external_adapter: Optional external CV data adapter.
        """
        self.config_manager = config_manager
        self.video_preprocessor = video_preprocessor
        self.vlm_engine = vlm_engine
        self.logic_engine = logic_engine
        self.report_generator = report_generator
        self.external_adapter = external_adapter

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config_dir(cls, config_dir: str) -> "AnalysisOrchestrator":
        """Create an orchestrator from a configuration directory.

        Args:
            config_dir: Path to the configuration directory.

        Returns:
            Configured AnalysisOrchestrator instance.
        """
        config_manager = ConfigManager(config_dir)
        system_config = config_manager.load_all()

        video_preprocessor = VideoPreprocessor(
            config=system_config.sampling,
            output_dir=system_config.output_dir if system_config.save_debug_frames else None,
            save_debug_frames=system_config.save_debug_frames,
        )

        vlm_engine = VLMInferenceEngine(system_config.llm_provider)
        logic_engine = LogicEngine(vlm_engine, config_manager)
        report_generator = ReportGenerator()
        external_adapter = ExternalAdapter()

        return cls(
            config_manager=config_manager,
            video_preprocessor=video_preprocessor,
            vlm_engine=vlm_engine,
            logic_engine=logic_engine,
            report_generator=report_generator,
            external_adapter=external_adapter,
        )

    # ------------------------------------------------------------------
    # Main analysis flow
    # ------------------------------------------------------------------

    def analyze(
        self,
        video_path: str,
        cv_tracks_path: Optional[str] = None,
    ) -> Report:
        """Run the full analysis pipeline on a video.

        Args:
            video_path: Path to the video file.
            cv_tracks_path: Optional path to external CV track data JSON.

        Returns:
            Complete analysis report.
        """
        logger.info("=" * 60)
        logger.info("Starting traffic analysis for: %s", video_path)
        logger.info("=" * 60)

        analysis_start = time.perf_counter()
        context = AnalysisContext(config=self.config_manager.load_all())

        # Step 1: Video preprocessing
        logger.info("[1/7] Preprocessing video...")
        keyframes = self.video_preprocessor.process(video_path)
        context.keyframes = keyframes
        logger.info("  Coarse frames: %d", len(keyframes.coarse_frames))
        logger.info("  Precision frames: %d", len(keyframes.precision_frames))

        # Step 2: Global scene understanding
        logger.info("[2/7] Scene understanding...")
        scene_info = self._scene_understanding(keyframes)
        context.scene_understanding = scene_info
        logger.info("  Roads detected: %d", scene_info.road_count)
        logger.info("  Traffic density: %s", scene_info.traffic_density)

        # Step 3: Load CV tracks if provided
        if cv_tracks_path and self.external_adapter:
            logger.info("[3/7] Loading CV tracks from %s...", cv_tracks_path)
            tracks = self.external_adapter.load_cv_tracks(cv_tracks_path)
            context.cv_tracks = tracks
            logger.info("  Tracks loaded: %d", len(tracks))
        else:
            logger.info("[3/7] No CV tracks provided, skipping external validation")

        # Step 4: Event detection
        logger.info("[4/7] Detecting events...")
        event_results = self._detect_events(context)
        context.event_results = {r.event_id: r for r in event_results}
        detected_count = sum(1 for r in event_results if r.detected)
        logger.info("  Events detected: %d / %d", detected_count, len(event_results))

        # Step 5: Post-process inferred events
        logger.info("[5/7] Post-processing inferred events...")
        event_results = self._post_process_events(event_results, context.scene_understanding)
        context.event_results = {r.event_id: r for r in event_results}

        # Step 6: Cross-validation with CV tracks
        if context.cv_tracks and self.external_adapter:
            logger.info("[6/7] Cross-validating with CV tracks...")
            event_results = self._cross_validate(event_results, context)
            context.event_results = {r.event_id: r for r in event_results}

        # Step 7: Report generation
        logger.info("[7/7] Generating report...")
        video_meta = self._extract_video_meta(video_path)
        context.video_meta = video_meta
        usage_stats = self.vlm_engine.get_usage_stats()
        analysis_duration_sec = time.perf_counter() - analysis_start

        report = self.report_generator.generate(
            event_results=event_results,
            scene_info=scene_info,
            video_meta=video_meta,
            usage_stats=usage_stats,
            analysis_duration_sec=round(analysis_duration_sec, 2),
        )
        context.final_report = report

        logger.info("=" * 60)
        logger.info("Analysis complete in %.2f s. Binary encoding: %s", analysis_duration_sec, report.binary_encoding.encoding_string)
        logger.info("=" * 60)

        return report

    # ------------------------------------------------------------------
    # Pipeline steps
    # ------------------------------------------------------------------

    def _scene_understanding(self, keyframes: KeyframeSequence) -> SceneInfo:
        """Perform global scene understanding via VLM."""
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

        # Use only the first 4 coarse frames for scene understanding.
        # Fewer frames = smaller time gap = the same vehicles are more likely
        # to appear across consecutive frames, making direction tracking reliable.
        raw_frames = keyframes.coarse_frames[:4]
        images: List[Any] = []
        for idx, kf in enumerate(raw_frames):
            img = kf.image_data or kf.image_path
            if img is None:
                continue
            label = f"FRAME {idx + 1}/{len(raw_frames)}  t={kf.timestamp_sec:.2f}s  EARLIEST→LATEST"
            if idx == 0:
                label += "  [OLDEST]"
            if idx == len(raw_frames) - 1:
                label += "  [NEWEST]"
            annotated = _annotate_frame(img, label)
            images.append(annotated)

        response = self.vlm_engine.call(
            template=template,
            images=images,
            context_vars={},
        )

        if not response.success or not isinstance(response.parsed_data, dict):
            logger.warning("Scene understanding failed: %s", response.raw_text)
            return SceneInfo()

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

        # ------------------------------------------------------------------
        # Direction Analysis: use dedicated 6-step prompt for reliable direction
        # ------------------------------------------------------------------
        try:
            dir_template = self.config_manager.get_prompt_template("direction_analysis")
            dir_response = self.vlm_engine.call(
                template=dir_template,
                images=images,
                context_vars={
                    "road_count": scene_info.road_count,
                    "pre_directions": [
                        {
                            "road_id": r.road_id,
                            "normal_direction": r.normal_direction,
                        }
                        for r in scene_info.roads
                    ],
                },
            )
            if dir_response.success and isinstance(dir_response.parsed_data, dict):
                dir_data = dir_response.parsed_data.get("direction_analysis", {})
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
                            # Add direction evidence from vehicle_motions
                            for motion in vehicle_motions:
                                if motion.get("road_id") == road_id:
                                    road.direction_evidence.append(
                                        DirectionEvidence(
                                            vehicle=motion.get("description", ""),
                                            movement=motion.get("movement_direction", "unknown"),
                                            location_earlier=motion.get("displacement", ""),
                                        )
                                    )
                logger.info(
                    "Direction analysis updated %d/%d roads",
                    updated_count,
                    len(scene_info.roads),
                )
        except Exception as exc:
            logger.warning("Direction analysis failed: %s", exc)

        # ------------------------------------------------------------------
        # Direction verification: if confidence is still low, re-verify with more frames
        # ------------------------------------------------------------------
        low_confidence_roads = [
            r for r in scene_info.roads
            if r.direction_confidence < 0.8
        ]
        if scene_info.confidence < 0.8 or low_confidence_roads:
            logger.info(
                "Direction confidence low (scene=%.2f, roads=%s), triggering re-verification",
                scene_info.confidence,
                [(r.road_id, r.direction_confidence) for r in low_confidence_roads],
            )
            scene_info = self._verify_directions(scene_info, keyframes)

        return scene_info

    def _verify_directions(
        self,
        scene_info: SceneInfo,
        keyframes: KeyframeSequence,
    ) -> SceneInfo:
        """Re-verify road directions using the road_direction_analysis template."""
        try:
            template = self.config_manager.get_prompt_template("road_direction_analysis")
        except KeyError:
            logger.warning("road_direction_analysis template not found, skipping verification")
            return scene_info

        # Use more frames for direction verification (all coarse frames)
        raw_frames = keyframes.coarse_frames
        images: List[Any] = []
        for idx, kf in enumerate(raw_frames):
            img = kf.image_data or kf.image_path
            if img is None:
                continue
            label = f"FRAME {idx + 1}/{len(raw_frames)}  t={kf.timestamp_sec:.2f}s  EARLIEST→LATEST"
            if idx == 0:
                label += "  [OLDEST]"
            if idx == len(raw_frames) - 1:
                label += "  [NEWEST]"
            annotated = _annotate_frame(img, label)
            images.append(annotated)

        response = self.vlm_engine.call(
            template=template,
            images=images,
            context_vars={
                "road_count": scene_info.road_count,
                "road_directions": scene_info.roads,
            },
        )

        if not response.success or not isinstance(response.parsed_data, dict):
            logger.warning("Direction verification failed: %s", response.raw_text)
            return scene_info

        verified_roads = response.parsed_data.get("roads", [])
        if not verified_roads:
            return scene_info

        # Build a lookup of verified directions
        verified_map = {
            r.get("road_id"): r for r in verified_roads if "road_id" in r
        }

        for road in scene_info.roads:
            verified = verified_map.get(road.road_id)
            if not verified:
                continue
            v_dir = verified.get("normal_direction")
            v_conf = verified.get("confidence", 0.0)
            if v_dir and v_conf >= road.direction_confidence:
                logger.info(
                    "Road %d direction updated: %s (conf %.2f) -> %s (conf %.2f)",
                    road.road_id,
                    road.normal_direction,
                    road.direction_confidence,
                    v_dir,
                    v_conf,
                )
                road.normal_direction = v_dir
                road.direction_confidence = v_conf

        return scene_info

    def _detect_events(self, context: AnalysisContext) -> List[EventResult]:
        """Detect all configured events."""
        event_categories = self.config_manager.get_event_categories()
        results: List[EventResult] = []

        for category in event_categories:
            if not category.is_active:
                continue

            try:
                if category.detection_mode == "direct_vlm":
                    result = self._detect_direct_vlm(category, context)
                elif category.detection_mode == "logic_chain":
                    result = self._detect_logic_chain(category, context)
                else:
                    logger.warning("Unknown detection mode for %s: %s", category.name_zh, category.detection_mode)
                    result = EventResult(
                        event_id=category.event_id,
                        event_name=category.name_zh,
                        detected=False,
                        summary=f"Unknown detection mode: {category.detection_mode}",
                    )
                results.append(result)
                # Update context immediately so later logic chains can see
                # prerequisite events (e.g. reversing depends on parking/emergency).
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

    def _detect_direct_vlm(self, category: EventCategory, context: AnalysisContext) -> EventResult:
        """Detect an event using a single direct VLM call."""
        try:
            template = self.config_manager.get_prompt_template("direct_event_detection")
        except KeyError:
            template = PromptTemplate(
                template_id="direct_event_detection",
                name="Direct Event Detection",
                system_prompt="You are a traffic surveillance analyst.",
                user_prompt=f"Detect {category.name_zh}: {category.description}",
            )

        images = []
        if context.keyframes:
            images = [kf.image_data or kf.image_path for kf in context.keyframes.coarse_frames[:6]]
            images = [img for img in images if img is not None]

        response = self.vlm_engine.call(
            template=template,
            images=images,
            context_vars={
                "event_name": category.name_zh,
                "event_definition": category.definition,
                "visual_indicators": category.visual_indicators,
            },
        )

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
        """Detect an event using a configured logic chain."""
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

    @staticmethod
    def _post_process_events(
        event_results: List[EventResult],
        scene_info: Optional[SceneInfo] = None,
    ) -> List[EventResult]:
        """Infer events that were missed by direct detection but implied by other events.

        Two inference sources:
        1. Other detected events (e.g. Illegal parking on shoulder -> Emergency Lane Occupancy).
        2. Scene description keywords (e.g. "工程车" in scene_description -> Road Construction).
        """
        results_by_id = {r.event_id: r for r in event_results}
        scene_desc = (scene_info.scene_description or "").lower() if scene_info else ""

        # ------------------------------------------------------------------
        # Rule 1: Illegal parking on shoulder/emergency lane -> Emergency Lane Occupancy
        # ------------------------------------------------------------------
        illegal_parking = results_by_id.get(0)
        emergency_occupancy = results_by_id.get(1)

        if (
            illegal_parking
            and illegal_parking.detected
            and emergency_occupancy
            and not emergency_occupancy.detected
        ):
            shoulder_keywords = ["shoulder", "emergency", "路肩", "应急"]
            inferred_instances: List[EventInstance] = []
            for inst in illegal_parking.instances:
                desc = (inst.description or "").lower()
                if any(kw in desc for kw in shoulder_keywords):
                    inferred_instances.append(
                        EventInstance(
                            event_id=1,
                            event_name=emergency_occupancy.event_name,
                            event_name_en=emergency_occupancy.event_name_en,
                            start_time_sec=inst.start_time_sec,
                            end_time_sec=inst.end_time_sec,
                            confidence=inst.confidence * 0.9,
                            description=f"车辆在应急车道/路肩区域停放（源自{inst.description}）",
                            reasoning="违法停车事件检测到车辆在应急车道/路肩区域停放，符合应急车道占用定义。",
                        )
                    )
            if inferred_instances:
                emergency_occupancy.detected = True
                emergency_occupancy.confidence = max(
                    emergency_occupancy.confidence,
                    max(i.confidence for i in inferred_instances),
                )
                emergency_occupancy.instances = inferred_instances
                emergency_occupancy.summary = (
                    f"应急车道占用（由违法停车结果推断，共 {len(inferred_instances)} 个实例）"
                )
                logger.info(
                    "Inferred Emergency Lane Occupancy from Illegal Parking (%d instance(s))",
                    len(inferred_instances),
                )

        # ------------------------------------------------------------------
        # Rule 2: Infer from structured boolean fields (simple presence events)
        # event_id 3=pedestrian, 4=motorcycle, 8=thrown_objects
        # These are unambiguous presence checks — VLM already gave us true/false.
        # We do NOT use keyword matching on scene_description because negated
        # mentions (e.g. "无行人") would produce false positives.
        # ------------------------------------------------------------------
        if scene_info:
            _BOOL_INFERENCE = [
                (3, scene_info.pedestrian_present, "高速公路行人出现", "场景理解判断有行人存在。"),
                (4, scene_info.non_motor_vehicle_present, "摩托车出现", "场景理解判断有非机动车（摩托车等）存在。"),
                (8, scene_info.thrown_object_present, "抛洒物", "场景理解判断有抛洒物存在。"),
            ]
            for eid, present, name, reasoning in _BOOL_INFERENCE:
                result = results_by_id.get(eid)
                if not result:
                    continue

                # Boolean field explicitly says FALSE → force not detected
                if present is False:
                    result.detected = False
                    result.confidence = 0.0
                    result.instances = []
                    result.summary = f"{name}未检测到（场景理解结构化字段为 False）"
                    logger.info("%s explicitly FALSE from scene boolean field — marking not detected", name)
                    continue

                # Boolean field explicitly says TRUE → force detected
                if present is True and not result.detected:
                    inferred_confidence = 0.65
                    result.detected = True
                    result.confidence = max(result.confidence, inferred_confidence)
                    result.instances = [
                        EventInstance(
                            event_id=eid,
                            event_name=result.event_name,
                            event_name_en=result.event_name_en,
                            start_time_sec=0.0,
                            end_time_sec=0.0,
                            confidence=inferred_confidence,
                            description=f"{name}（由场景理解结构化字段推断）",
                            reasoning=reasoning,
                        )
                    ]
                    result.summary = f"{name}（由场景理解推断）"
                    logger.info("Inferred %s from scene boolean field", name)

        # ------------------------------------------------------------------
        # Rule 3: Infer from structured tags in scene_description
        # The scene_description now uses {类别：内容} format.
        # We parse these tags explicitly instead of keyword matching.
        # ------------------------------------------------------------------
        if scene_info and scene_info.scene_description:
            tag_map = _parse_scene_tags(scene_info.scene_description)

            # --- Accident (event_id=2) ---
            # Scene tags are fallback only — they do NOT override a positive
            # direct-detection result (logic-chain / direct_vlm is more reliable).
            accident_tag = tag_map.get("交通事故", "")
            accident_result = results_by_id.get(2)
            if accident_result and not accident_result.detected:
                if accident_tag.startswith("无"):
                    accident_result.detected = False
                    accident_result.confidence = 0.0
                    accident_result.instances = []
                    accident_result.summary = "交通事故未检测到（scene_description 结构化标签为 无）"
                    logger.info("Accident tag is '%s' — marking not detected", accident_tag)
                elif accident_tag.startswith("有"):
                    inferred_confidence = 0.65
                    accident_result.detected = True
                    accident_result.confidence = max(accident_result.confidence, inferred_confidence)
                    accident_result.instances = [
                        EventInstance(
                            event_id=2,
                            event_name=accident_result.event_name,
                            event_name_en=accident_result.event_name_en,
                            start_time_sec=0.0,
                            end_time_sec=0.0,
                            confidence=inferred_confidence,
                            description=f"交通事故（由场景描述推断：{accident_tag}）",
                            reasoning="场景描述中 {交通事故：...} 标签显示存在交通事故。",
                        )
                    ]
                    accident_result.summary = f"交通事故（由场景描述推断：{accident_tag}）"
                    logger.info("Accident inferred from structured tag: %s", accident_tag)

            # --- Construction (event_id=6) ---
            construction_tag = tag_map.get("工程车", "")
            construction_result = results_by_id.get(6)
            if construction_result and not construction_result.detected:
                if construction_tag.startswith("无"):
                    construction_result.detected = False
                    construction_result.confidence = 0.0
                    construction_result.instances = []
                    construction_result.summary = "道路施工未检测到（scene_description 结构化标签为 无）"
                    logger.info("Construction tag is '%s' — marking not detected", construction_tag)
                elif construction_tag.startswith("有"):
                    inferred_confidence = 0.65
                    construction_result.detected = True
                    construction_result.confidence = max(construction_result.confidence, inferred_confidence)
                    construction_result.instances = [
                        EventInstance(
                            event_id=6,
                            event_name=construction_result.event_name,
                            event_name_en=construction_result.event_name_en,
                            start_time_sec=0.0,
                            end_time_sec=0.0,
                            confidence=inferred_confidence,
                            description=f"道路施工（由场景描述推断：{construction_tag}）",
                            reasoning="场景描述中 {工程车：...} 标签显示存在道路施工。",
                        )
                    ]
                    construction_result.summary = f"道路施工（由场景描述推断：{construction_tag}）"
                    logger.info("Construction inferred from structured tag: %s", construction_tag)

            # --- Emergency Lane (event_id=1) ---
            # Note: Rule 1 (illegal parking -> emergency) may have already set
            # detected=True. Scene tags are fallback only and do not override.
            emergency_tag = tag_map.get("应急车道车辆", "")
            emergency_result = results_by_id.get(1)
            if emergency_result and not emergency_result.detected:
                if emergency_tag.startswith("无"):
                    emergency_result.detected = False
                    emergency_result.confidence = 0.0
                    emergency_result.instances = []
                    emergency_result.summary = "应急车道占用未检测到（scene_description 结构化标签为 无）"
                    logger.info("Emergency lane tag is '%s' — marking not detected", emergency_tag)
                elif emergency_tag.startswith("有"):
                    inferred_confidence = 0.65
                    emergency_result.detected = True
                    emergency_result.confidence = max(emergency_result.confidence, inferred_confidence)
                    emergency_result.instances = [
                        EventInstance(
                            event_id=1,
                            event_name=emergency_result.event_name,
                            event_name_en=emergency_result.event_name_en,
                            start_time_sec=0.0,
                            end_time_sec=0.0,
                            confidence=inferred_confidence,
                            description=f"应急车道占用（由场景描述推断：{emergency_tag}）",
                            reasoning="场景描述中 {应急车道车辆：...} 标签显示存在应急车道占用。",
                        )
                    ]
                    emergency_result.summary = f"应急车道占用（由场景描述推断：{emergency_tag}）"
                    logger.info("Emergency lane inferred from structured tag: %s", emergency_tag)

            # --- Pedestrian (event_id=3) — fallback when bool field is None ---
            pedestrian_tag = tag_map.get("行人", "")
            pedestrian_result = results_by_id.get(3)
            if (
                pedestrian_result
                and not pedestrian_result.detected
                and scene_info.pedestrian_present is None
            ):
                if pedestrian_tag.startswith("无"):
                    pedestrian_result.detected = False
                    pedestrian_result.confidence = 0.0
                    pedestrian_result.instances = []
                    pedestrian_result.summary = "行人未检测到（scene_description 结构化标签为 无）"
                    logger.info("Pedestrian tag is '%s' — marking not detected", pedestrian_tag)
                elif pedestrian_tag.startswith("有"):
                    inferred_confidence = 0.65
                    pedestrian_result.detected = True
                    pedestrian_result.confidence = max(pedestrian_result.confidence, inferred_confidence)
                    pedestrian_result.instances = [
                        EventInstance(
                            event_id=3,
                            event_name=pedestrian_result.event_name,
                            event_name_en=pedestrian_result.event_name_en,
                            start_time_sec=0.0,
                            end_time_sec=0.0,
                            confidence=inferred_confidence,
                            description=f"高速公路行人出现（由场景描述推断：{pedestrian_tag}）",
                            reasoning="场景描述中 {行人：...} 标签显示有行人存在。",
                        )
                    ]
                    pedestrian_result.summary = f"行人（由场景描述推断：{pedestrian_tag}）"
                    logger.info("Pedestrian inferred from structured tag: %s", pedestrian_tag)

            # --- Non-motor vehicle (event_id=4) — fallback when bool field is None ---
            nm_tag = tag_map.get("非机动车", "")
            nm_result = results_by_id.get(4)
            if (
                nm_result
                and not nm_result.detected
                and scene_info.non_motor_vehicle_present is None
            ):
                if nm_tag.startswith("无"):
                    nm_result.detected = False
                    nm_result.confidence = 0.0
                    nm_result.instances = []
                    nm_result.summary = "非机动车未检测到（scene_description 结构化标签为 无）"
                    logger.info("Non-motor vehicle tag is '%s' — marking not detected", nm_tag)
                elif nm_tag.startswith("有"):
                    inferred_confidence = 0.65
                    nm_result.detected = True
                    nm_result.confidence = max(nm_result.confidence, inferred_confidence)
                    nm_result.instances = [
                        EventInstance(
                            event_id=4,
                            event_name=nm_result.event_name,
                            event_name_en=nm_result.event_name_en,
                            start_time_sec=0.0,
                            end_time_sec=0.0,
                            confidence=inferred_confidence,
                            description=f"非机动车出现（由场景描述推断：{nm_tag}）",
                            reasoning="场景描述中 {非机动车：...} 标签显示有非机动车存在。",
                        )
                    ]
                    nm_result.summary = f"非机动车（由场景描述推断：{nm_tag}）"
                    logger.info("Non-motor vehicle inferred from structured tag: %s", nm_tag)

        return event_results

    def _cross_validate(
        self,
        event_results: List[EventResult],
        context: AnalysisContext,
    ) -> List[EventResult]:
        """Cross-validate VLM results with CV track data."""
        if not self.external_adapter or not context.cv_tracks:
            return event_results

        roads = context.scene_understanding.roads if context.scene_understanding else []
        validated_results: List[EventResult] = []

        for result in event_results:
            if not result.detected or not result.instances:
                validated_results.append(result)
                continue

            try:
                validated_instances = self.external_adapter.cross_validate_direction(
                    vlm_instances=result.instances,
                    tracks=context.cv_tracks,
                    roads=roads,
                    fps=context.video_meta.fps if context.video_meta else 15.0,
                )
                result.instances = validated_instances
                # Recalculate confidence
                if validated_instances:
                    result.confidence = max(i.confidence for i in validated_instances)
                validated_results.append(result)
            except Exception as exc:
                logger.warning("Cross-validation failed for %s: %s", result.event_name, exc)
                validated_results.append(result)

        return validated_results

    @staticmethod
    def _extract_video_meta(video_path: str) -> VideoMetadata:
        """Extract video metadata using the preprocessor."""
        import cv2
        cap = cv2.VideoCapture(video_path)
        try:
            fps = cap.get(cv2.CAP_PROP_FPS)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            duration_sec = total_frames / fps if fps > 0 else 0.0
            return VideoMetadata(
                file_path=video_path,
                file_name=Path(video_path).name,
                duration_sec=duration_sec,
                fps=fps,
                total_frames=total_frames,
                width=width,
                height=height,
            )
        finally:
            cap.release()
