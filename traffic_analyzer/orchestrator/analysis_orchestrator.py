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
    CrossEventInferenceRule,
    DirectionEvidence,
    EventCategory,
    EventInstance,
    EventResult,
    Keyframe,
    KeyframeSequence,
    PromptTemplate,
    Report,
    SceneInfo,
    VideoMetadata,
)

logger = logging.getLogger(__name__)


def _annotate_frame(image: Any, label: str) -> bytes:
    """Overlay *label* on the top-left corner of *image* and return as JPEG bytes.

    The label is drawn with a dark semi-transparent background so it remains
    readable regardless of image content.
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

    # Resize to 720p max to reduce VLM payload and avoid API timeouts
    img.thumbnail((1280, 720), Image.LANCZOS)

    # Try to load a TrueType font; fall back to default bitmap font
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
    except Exception:
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 28)
        except Exception:
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

    # Draw text
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

        # Extract video metadata early for scene understanding frame selection
        video_meta = self._extract_video_meta(video_path)
        context.video_meta = video_meta

        # Step 2: Global scene understanding
        logger.info("[2/7] Scene understanding...")
        scene_info = self._scene_understanding(
            keyframes,
            video_path=video_path,
            duration_sec=video_meta.duration_sec,
        )
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

    def _scene_understanding(
        self,
        keyframes: KeyframeSequence,
        video_path: str,
        duration_sec: float = 0.0,
    ) -> SceneInfo:
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

        # ------------------------------------------------------------------
        # Frame selection: uniformly distributed across entire video
        # ------------------------------------------------------------------
        coarse_frames = keyframes.coarse_frames
        total_coarse = len(coarse_frames)

        # Target frame count from configuration (default 30, configurable via CLI/env)
        min_frames = 30
        if (
            self.config_manager._system_config is not None
            and self.config_manager._system_config.scene_understanding_min_frames > 0
        ):
            min_frames = self.config_manager._system_config.scene_understanding_min_frames

        target_count = min_frames
        # But cannot exceed available coarse frames
        target_count = min(target_count, total_coarse)

        if total_coarse >= min_frames:
            # Uniformly select target_count frames from coarse_frames
            if target_count >= total_coarse:
                raw_frames = coarse_frames[:]
            else:
                indices = [int(i * (total_coarse - 1) / (target_count - 1)) for i in range(target_count)]
                raw_frames = [coarse_frames[i] for i in indices]
        else:
            # Not enough coarse frames — supplement from video file
            logger.info(
                "Coarse frames insufficient (%d < %d), supplementing from video",
                total_coarse,
                min_frames,
            )
            raw_frames = self._supplement_frames_from_video(
                video_path, coarse_frames, min_frames, duration_sec
            )
        images: List[Any] = []
        total = len(raw_frames)
        for idx, kf in enumerate(raw_frames):
            img = kf.image_data or kf.image_path
            if img is None:
                continue
            # Label must make the temporal order absolutely unambiguous.
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
        # Direction Analysis: parsed from scene_understanding result
        # (direction_analysis is now integrated into scene_understanding prompt)
        # ------------------------------------------------------------------
        dir_data = data.get("direction_analysis", {})
        if dir_data:
            from traffic_analyzer.models.schemas import (
                DirectionAnalysis,
                DirectionConclusion,
                ConsistencyCheck,
                HeadOrientation,
                PerspectiveCheck,
                VehicleMotion,
            )

            conclusions = dir_data.get("conclusions", [])
            vehicle_motions = dir_data.get("vehicle_motions", [])
            updated_count = 0

            # Update road directions from conclusions
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

            # Build full DirectionAnalysis object for reporting
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
                "Direction analysis parsed from scene_understanding: updated %d/%d roads, built full DirectionAnalysis object",
                updated_count,
                len(scene_info.roads),
            )

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

    def _supplement_frames_from_video(
        self,
        video_path: str,
        existing_frames: List[Keyframe],
        target_count: int,
        duration_sec: float,
    ) -> List[Keyframe]:
        """Extract additional frames from video to reach target_count.

        Frames are uniformly distributed across the entire video duration,
        avoiding timestamps too close to already-existing frames.
        """
        import cv2

        cap = cv2.VideoCapture(video_path)
        try:
            fps = cap.get(cv2.CAP_PROP_FPS)
            total_vid_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if fps <= 0 or total_vid_frames <= 0 or duration_sec <= 0:
                return existing_frames

            existing_timestamps = {kf.timestamp_sec for kf in existing_frames}
            result = existing_frames[:]
            extra_needed = target_count - len(existing_frames)
            if extra_needed <= 0:
                return result

            for i in range(extra_needed):
                # Uniformly spaced timestamps across video
                target_time = (i + 0.5) * duration_sec / extra_needed

                # Skip if too close to an existing frame (< 0.3s)
                if any(abs(target_time - ts) < 0.3 for ts in existing_timestamps):
                    continue

                frame_idx = min(int(target_time * fps), total_vid_frames - 1)
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                ret, frame = cap.read()
                if not ret:
                    continue

                from PIL import Image as PILImage
                import io

                img = PILImage.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)

                result.append(
                    Keyframe(
                        frame_id=len(result),
                        timestamp_sec=round(target_time, 2),
                        image_data=buf.getvalue(),
                        quality_score=0.5,
                    )
                )
                existing_timestamps.add(target_time)

            return result
        finally:
            cap.release()

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
                elif category.detection_mode == "scene_tag":
                    # No VLM call; result is determined by post-processing from
                    # scene boolean fields (pedestrian_present, etc.) or
                    # structured tags in scene_description.
                    result = EventResult(
                        event_id=category.event_id,
                        event_name=category.name_zh,
                        detected=False,
                        summary="等待场景标签后处理",
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
        template_id = category.prompt_template_id
        if not template_id:
            raise ValueError(f"Event {category.name_zh} (direct_vlm) has no prompt_template_id configured")
        try:
            template = self.config_manager.get_prompt_template(template_id)
        except KeyError:
            template = PromptTemplate(
                template_id=template_id,
                name="Direct Event Detection",
                system_prompt="You are a traffic surveillance analyst.",
                user_prompt=f"Detect {category.name_zh}: {category.description}",
            )

        images = []
        if context.keyframes:
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

    def _post_process_events(
        self,
        event_results: List[EventResult],
        scene_info: Optional[SceneInfo] = None,
    ) -> List[EventResult]:
        """Infer events that were missed by direct detection but implied by other events.

        Three inference phases (all config-driven):
        1. Cross-event inference (from YAML config).
        2. Boolean field inference (from SceneInfo structured fields).
        3. Structured tag inference (from scene_description {类别：内容} tags).
        """
        results_by_id = {r.event_id: r for r in event_results}

        # Phase 1: Cross-event inference (from YAML config)
        event_categories = {c.event_id: c for c in self.config_manager.get_event_categories()}
        for rule in self.config_manager.get_inference_rules():
            self._apply_cross_event_inference(rule, results_by_id, event_categories)

        # Phase 2: Boolean field inference (from YAML config)
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

                # Boolean field explicitly says FALSE -> force not detected
                if present is False:
                    result.detected = False
                    result.confidence = 0.0
                    result.instances = []
                    result.summary = f"{cat.name_zh}未检测到（场景理解结构化字段为 False）"
                    logger.info("%s explicitly FALSE from scene boolean field", cat.name_zh)
                    continue

                # Boolean field explicitly says TRUE -> force detected
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

        # Phase 3: Structured tag inference (from YAML config)
        if scene_info and scene_info.scene_description:
            tag_map = _parse_scene_tags(scene_info.scene_description)
            for cat in self.config_manager.get_event_categories():
                if cat.detection_mode.value != "scene_tag" or not cat.scene_tag_key:
                    continue
                result = results_by_id.get(cat.event_id)
                if not result:
                    continue

                # Boolean field already handled this event -> skip tag fallback
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

        return event_results

    def _apply_cross_event_inference(
        self,
        rule: CrossEventInferenceRule,
        results_by_id: Dict[int, EventResult],
        event_categories: Dict[int, EventCategory],
    ) -> None:
        """Apply a single cross-event inference rule."""
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
            target_result.instances = inferred_instances
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
