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
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.external_adapter import ExternalAdapter
from traffic_analyzer.core.logic_engine import LogicEngine, _parse_scene_tags
from traffic_analyzer.core.pipeline_steps import (
    EventDetectionStep,
    PostProcessStep,
    SceneUnderstandingStep,
)
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
from traffic_analyzer.utils.event_detection import (
    detect_logic_chain as _detect_logic_chain_impl,
    parse_direct_vlm_response as _parse_direct_vlm_response_impl,
    select_event_images as _select_event_images_impl,
)
from traffic_analyzer.utils.image_overlay import annotate_frame
from traffic_analyzer.utils.tool_call_logger import tool_call

logger = logging.getLogger(__name__)


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
        scene_understanding_step: Optional[SceneUnderstandingStep] = None,
        event_detection_step: Optional[EventDetectionStep] = None,
        post_process_step: Optional[PostProcessStep] = None,
    ) -> None:
        """Initialize the orchestrator with all required modules.

        Args:
            config_manager: Loaded configuration manager.
            video_preprocessor: Video preprocessing module.
            vlm_engine: VLM inference engine.
            logic_engine: Logic chain execution engine.
            report_generator: Report generation module.
            external_adapter: Optional external CV data adapter.
            scene_understanding_step: Optional custom scene understanding step.
            event_detection_step: Optional custom event detection step.
            post_process_step: Optional custom post-processing step.
        """
        self.config_manager = config_manager
        self.video_preprocessor = video_preprocessor
        self.vlm_engine = vlm_engine
        self.logic_engine = logic_engine
        self.report_generator = report_generator
        self.external_adapter = external_adapter

        # Pipeline steps (created lazily if not provided)
        self._scene_understanding_step = scene_understanding_step
        self._event_detection_step = event_detection_step
        self._post_process_step = post_process_step

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

        # Create pipeline steps
        scene_step = SceneUnderstandingStep(config_manager, vlm_engine, max_retries=1)
        event_step = EventDetectionStep(config_manager, vlm_engine, logic_engine)
        post_step = PostProcessStep(config_manager)

        return cls(
            config_manager=config_manager,
            video_preprocessor=video_preprocessor,
            vlm_engine=vlm_engine,
            logic_engine=logic_engine,
            report_generator=report_generator,
            external_adapter=external_adapter,
            scene_understanding_step=scene_step,
            event_detection_step=event_step,
            post_process_step=post_step,
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
        step_times: Dict[str, float] = {}
        context = AnalysisContext(config=self.config_manager.load_all())

        # Step 1: Video preprocessing
        logger.info("[1/7] Preprocessing video...")
        t0 = time.perf_counter()
        with tool_call(
            "video_preprocessor.process",
            video=os.path.basename(video_path),
        ) as _tc:
            keyframes = self.video_preprocessor.process(video_path)
            _tc.result(
                f"coarse={len(keyframes.coarse_frames)}, "
                f"precision={len(keyframes.precision_frames)}"
            )
        step_times["preprocessing"] = time.perf_counter() - t0
        context.keyframes = keyframes
        logger.info("  Coarse frames: %d", len(keyframes.coarse_frames))
        logger.info("  Precision frames: %d", len(keyframes.precision_frames))

        # Extract video metadata early for scene understanding frame selection
        video_meta = self._extract_video_meta(video_path)
        context.video_meta = video_meta

        # Step 2: Global scene understanding (via PipelineStep if available)
        logger.info("[2/7] Scene understanding...")
        t0 = time.perf_counter()
        with tool_call(
            "vlm_engine.scene_understanding",
            provider=self.vlm_engine.provider,
            frames=len(keyframes.coarse_frames),
        ) as _tc:
            if self._scene_understanding_step:
                su_result = self._scene_understanding_step.execute(context)
                if su_result.success and su_result.data:
                    scene_info = su_result.data
                else:
                    logger.warning("Scene understanding step failed, using fallback")
                    scene_info = SceneInfo()
            else:
                scene_info = self._scene_understanding(
                    keyframes,
                    video_path=video_path,
                    duration_sec=video_meta.duration_sec,
                )
            _tc.result(
                f"roads={scene_info.road_count}, "
                f"density={scene_info.traffic_density}"
            )
        step_times["scene_understanding"] = time.perf_counter() - t0
        context.scene_understanding = scene_info
        logger.info("  Roads detected: %d", scene_info.road_count)
        logger.info("  Traffic density: %s", scene_info.traffic_density)

        # Step 3: Load CV tracks if provided
        logger.info("[3/7] Loading CV tracks...")
        t0 = time.perf_counter()
        if cv_tracks_path and self.external_adapter:
            with tool_call(
                "external_adapter.load_cv_tracks",
                path=os.path.basename(cv_tracks_path),
            ) as _tc:
                tracks = self.external_adapter.load_cv_tracks(cv_tracks_path)
                context.cv_tracks = tracks
                _tc.result(f"tracks={len(tracks)}")
            logger.info("  Tracks loaded: %d", len(tracks))
        else:
            logger.info("  No CV tracks provided, skipping external validation")
        step_times["cv_tracks"] = time.perf_counter() - t0

        # Step 4: Event detection (via PipelineStep if available)
        logger.info("[4/7] Detecting events...")
        t0 = time.perf_counter()
        if self._event_detection_step:
            ed_result = self._event_detection_step.execute(context)
            if ed_result.success and ed_result.data:
                event_results = ed_result.data
            else:
                logger.error("Event detection step failed: %s", ed_result.error)
                event_results = list(context.event_results.values())
        else:
            event_results = self._detect_events(context)
        step_times["event_detection"] = time.perf_counter() - t0
        context.event_results = {r.event_id: r for r in event_results}
        detected_count = sum(1 for r in event_results if r.detected)
        logger.info("  Events detected: %d / %d", detected_count, len(event_results))

        # Step 5: Post-process inferred events (via PipelineStep if available)
        logger.info("[5/7] Post-processing inferred events...")
        t0 = time.perf_counter()
        before_detected = sum(1 for r in event_results if r.detected)
        with tool_call(
            "post_process.run_inference",
            phases=3,
            event_count=len(event_results),
        ) as _tc:
            if self._post_process_step:
                pp_result = self._post_process_step.execute(context)
                if pp_result.success and pp_result.data:
                    event_results = pp_result.data
                else:
                    logger.error("Post-processing step failed: %s", pp_result.error)
            else:
                event_results = self._post_process_events(
                    event_results, context.scene_understanding
                )
            after_detected = sum(1 for r in event_results if r.detected)
            inferred = after_detected - before_detected
            _tc.result(
                f"inferred_added={inferred}, total_detected={after_detected}"
            )
        step_times["post_processing"] = time.perf_counter() - t0
        context.event_results = {r.event_id: r for r in event_results}

        # Step 6: Cross-validation with CV tracks
        logger.info("[6/7] Cross-validating with CV tracks...")
        t0 = time.perf_counter()
        if context.cv_tracks and self.external_adapter:
            event_results = self._cross_validate(event_results, context)
            context.event_results = {r.event_id: r for r in event_results}
        step_times["cross_validation"] = time.perf_counter() - t0

        # Step 7: Report generation
        logger.info("[7/7] Generating report...")
        t0 = time.perf_counter()
        usage_stats = self.vlm_engine.get_usage_stats()
        with tool_call(
            "report_generator.generate",
            formats=["markdown", "json", "binary"],
            events=len(event_results),
        ) as _tc:
            report = self.report_generator.generate(
                event_results=event_results,
                scene_info=scene_info,
                video_meta=video_meta,
                usage_stats=usage_stats,
                analysis_duration_sec=round(0.0, 2),  # placeholder, updated below
            )
            _tc.result(
                f"binary_code={report.binary_encoding.encoding_string}"
            )
        step_times["report_generation"] = time.perf_counter() - t0
        context.final_report = report

        analysis_duration_sec = time.perf_counter() - analysis_start
        report.analysis_duration_sec = round(analysis_duration_sec, 2)

        # Timing breakdown
        logger.info("=" * 40)
        logger.info("Step timing breakdown:")
        for name, duration in step_times.items():
            logger.info("  %s: %.2fs", name, duration)
        logger.info("  TOTAL: %.2fs", analysis_duration_sec)
        logger.info("=" * 40)

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
        # Frame selection: TWO-STAGE sampling (dense front + uniform tail).
        #   Stage 1: First 5s dense — 5 frames from the first 5 seconds
        #            (high density for reliable motion/direction analysis).
        #   Stage 2: Remaining uniform — 5 frames evenly distributed
        #            across the REST of the video (catches late events).
        # This balances density for direction analysis with coverage for
        # event detection across the full duration.
        # ------------------------------------------------------------------
        coarse_frames = keyframes.coarse_frames
        total_coarse = len(coarse_frames)
        target_count = 20

        if total_coarse >= target_count:
            # Stage 1: first 5 frames (first ~5 seconds with coarse_fps=1.0)
            front_count = min(5, total_coarse)
            front_frames = coarse_frames[:front_count]

            # Stage 2: remaining frames uniformly from the tail
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

            raw_frames = front_frames + back_frames
            # Ensure we don't exceed target_count
            raw_frames = raw_frames[:target_count]
        else:
            # Not enough coarse frames — use all we have
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
            # Label must make the temporal order absolutely unambiguous.
            if idx == 0:
                label = f"第1帧(最早) | Frame 1/{total} | t={kf.timestamp_sec:.1f}s | [VIDEO START]"
            elif idx == total - 1:
                label = f"第{total}帧(最新) | Frame {total}/{total} | t={kf.timestamp_sec:.1f}s | [VIDEO END]"
            else:
                label = f"第{idx + 1}帧 | Frame {idx + 1}/{total} | t={kf.timestamp_sec:.1f}s"
            annotated = annotate_frame(img, label)
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

        # NOTE: Removed redundant _verify_directions call.
        # The scene_understanding prompt already contains a complete 6-step
        # direction analysis; the secondary VLM call wasted 30-60 seconds.
        # The _verify_directions method is kept intact for backward compatibility.

        return scene_info

    def _extract_dense_frames_for_scene(
        self,
        video_path: str,
        duration_sec: float,
        max_seconds: float = 5.0,
        target_fps: float = 2.0,
    ) -> List[Keyframe]:
        """Extract dense frames from the FIRST N seconds of video for scene understanding.

        Uses a higher FPS (default 2.0 = 0.5s interval) so vehicle displacement
        between consecutive frames is small, making motion tracking and direction
        analysis more reliable. Only the first `max_seconds` are sampled.

        Args:
            video_path: Path to the video file.
            duration_sec: Total video duration in seconds.
            max_seconds: How many seconds from the start to sample (default 5.0).
            target_fps: Target sampling rate (default 2.0 = one frame every 0.5s).

        Returns:
            List of Keyframe objects with dense temporal coverage.
        """
        import cv2

        cap = cv2.VideoCapture(video_path)
        try:
            fps = cap.get(cv2.CAP_PROP_FPS)
            total_vid_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if fps <= 0 or total_vid_frames <= 0 or duration_sec <= 0:
                return []

            sample_duration = min(max_seconds, duration_sec)
            interval = max(1, int(round(fps / target_fps)))
            max_frames = int(sample_duration * target_fps)

            keyframes: List[Keyframe] = []
            frame_idx = 0
            local_id = 0

            while frame_idx < total_vid_frames and local_id < max_frames:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                ret, frame = cap.read()
                if not ret:
                    break

                timestamp = frame_idx / fps
                if timestamp > sample_duration:
                    break

                from PIL import Image as PILImage
                import io

                img = PILImage.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)

                keyframes.append(
                    Keyframe(
                        frame_id=local_id,
                        timestamp_sec=round(timestamp, 2),
                        image_data=buf.getvalue(),
                        image_path=None,
                        quality_score=0.5,
                        is_precision=False,
                    )
                )
                local_id += 1
                frame_idx += interval

            logger.info(
                "Dense frame extraction: %d frames from first %.1fs (fps=%.1f, interval=%d)",
                len(keyframes),
                sample_duration,
                target_fps,
                interval,
            )
            return keyframes
        finally:
            cap.release()

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
            annotated = annotate_frame(img, label)
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

    def _get_event_images(self, context: AnalysisContext) -> List[Any]:
        """Select images from keyframes for VLM event detection."""
        system_config = self.config_manager._system_config
        vlm_max_frames = system_config.vlm_max_frames if system_config is not None else 0
        return _select_event_images_impl(context, vlm_max_frames)

    def _parse_direct_vlm_response(self, response: Any, category: EventCategory) -> EventResult:
        """Parse a VLM response into an EventResult for direct_vlm detection."""
        return _parse_direct_vlm_response_impl(response, category)

    def _detect_events(self, context: AnalysisContext) -> List[EventResult]:
        """Detect all configured events.

        direct_vlm events are processed in parallel via batch_call.
        logic_chain and scene_tag events remain sequential so that
        logic chains can read prior event results from context.
        """
        event_categories = self.config_manager.get_event_categories()
        results: List[EventResult] = []

        # Split into parallelizable direct_vlm and sequential others
        direct_vlm_categories: List[EventCategory] = []
        sequential_categories: List[EventCategory] = []

        for category in event_categories:
            if not category.is_active:
                continue
            if category.detection_mode == "direct_vlm":
                direct_vlm_categories.append(category)
            else:
                sequential_categories.append(category)

        # --- Parallel direct_vlm batch ---
        if direct_vlm_categories:
            logger.info("[并行检测] 准备并行检测 %d 个 direct_vlm 事件", len(direct_vlm_categories))
            batch_requests: List[Dict[str, Any]] = []
            batched_categories: List[EventCategory] = []
            shared_images = self._get_event_images(context)

            for category in direct_vlm_categories:
                template_id = category.prompt_template_id
                if not template_id:
                    logger.error("Event %s (direct_vlm) has no prompt_template_id configured", category.name_zh)
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

                # Build context vars: always include event info + optional scene understanding
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

            batch_start = time.perf_counter()
            batch_responses = self.vlm_engine.batch_call(
                batch_requests,
                parallel=True,
                max_workers=min(4, len(batched_categories)),
            )
            batch_duration = time.perf_counter() - batch_start

            # Defensive: verify response count matches request count
            if len(batch_responses) != len(batched_categories):
                logger.error(
                    "Batch response count mismatch: expected %d, got %d",
                    len(batched_categories),
                    len(batch_responses),
                )

            valid_count = 0
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

        # --- Sequential logic_chain / scene_tag / others ---
        for category in sequential_categories:
            try:
                if category.detection_mode == "logic_chain":
                    result = self._detect_logic_chain(category, context)
                elif category.detection_mode == "scene_tag":
                    # No VLM call; result is determined by post-processing from
                    # scene boolean fields (pedestrian_present, etc.) or
                    # structured tags in scene_description.
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
                            f"detected={result.detected}, "
                            f"confidence={result.confidence:.2f}"
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
        """Detect an event using a single direct VLM call.

        Used as a fallback for sequential execution or when batch_call is
        unavailable. The parallel batch path in _detect_events uses the same
        helpers (_get_event_images, _parse_direct_vlm_response) for consistency.
        """
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

        images = self._get_event_images(context)
        ctx_vars: Dict[str, Any] = {
            "event_name": category.name_zh,
            "event_definition": category.definition,
            "visual_indicators": category.visual_indicators,
        }
        if context.scene_understanding:
            ctx_vars["scene_understanding"] = context.scene_understanding

        with tool_call(
            "event_detector.detect",
            event=category.event_id,
            mode="direct_vlm",
        ) as _tc:
            response = self.vlm_engine.call(
                template=template,
                images=images,
                context_vars=ctx_vars,
            )
            result = self._parse_direct_vlm_response(response, category)
            _tc.result(
                f"detected={result.detected}, "
                f"confidence={result.confidence:.2f}"
            )
            return result

    def _detect_logic_chain(self, category: EventCategory, context: AnalysisContext) -> EventResult:
        """Detect an event using a configured logic chain."""
        return _detect_logic_chain_impl(category, context, self.config_manager, self.logic_engine)

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
