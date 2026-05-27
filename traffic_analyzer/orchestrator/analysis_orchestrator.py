"""
AnalysisOrchestrator module for the traffic analyzer framework.

Wires all modules together into a cohesive analysis pipeline:
1. Load configuration
2. Preprocess video (coarse + precision sampling)
3. Expert Agent Layer (event detection)
4. Adjudication (post-processing)
5. Cross-validate with CV tracks if provided
6. Generate final report
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.pipeline_steps import (
    AdjudicationStep,
    ExpertAgentLayer,
)
from traffic_analyzer.core.report_generator import ReportGenerator
from traffic_analyzer.core.video_preprocessor import VideoPreprocessor
from traffic_analyzer.core.vlm_engine import VLMInferenceEngine
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    BinaryEncoding,
    EventResult,
    KeyframeSequence,
    Report,
    SceneInfo,
    VideoMetadata,
)
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
        report_generator: ReportGenerator,
        expert_agent_layer: Optional[ExpertAgentLayer] = None,
        adjudication_step: Optional[AdjudicationStep] = None,
    ) -> None:
        """Initialize the orchestrator with all required modules.

        Args:
            config_manager: Loaded configuration manager.
            video_preprocessor: Video preprocessing module.
            vlm_engine: VLM inference engine.
            report_generator: Report generation module.
            expert_agent_layer: Optional custom expert agent layer.
            adjudication_step: Optional custom adjudication step.
        """
        self.config_manager = config_manager
        self.video_preprocessor = video_preprocessor
        self.vlm_engine = vlm_engine
        self.report_generator = report_generator

        # Pipeline steps (created lazily if not provided)
        self._expert_agent_layer = expert_agent_layer
        self._adjudication_step = adjudication_step

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
        report_generator = ReportGenerator()

        # Create pipeline steps
        expert_layer = ExpertAgentLayer(config_manager, vlm_engine)
        adj_step = AdjudicationStep(config_manager, vlm_engine)

        return cls(
            config_manager=config_manager,
            video_preprocessor=video_preprocessor,
            vlm_engine=vlm_engine,
            report_generator=report_generator,
            expert_agent_layer=expert_layer,
            adjudication_step=adj_step,
        )

    # ------------------------------------------------------------------
    # Main analysis flow
    # ------------------------------------------------------------------

    def analyze(
        self,
        video_path: str,
        scene_understanding: Optional[SceneInfo] = None,
    ) -> Report:
        """Run the full analysis pipeline on a video.

        Args:
            video_path: Path to the video file.
            scene_understanding: Optional pre-computed scene understanding.

        Returns:
            Complete analysis report.
        """
        logger.info("=" * 60)
        logger.info("Starting traffic analysis for: %s", video_path)
        logger.info("=" * 60)

        analysis_start = time.perf_counter()
        step_times: Dict[str, float] = {}
        context = AnalysisContext(config=self.config_manager.load_all())
        if scene_understanding is not None:
            context.scene_understanding = scene_understanding

        # Step 1: Video preprocessing
        logger.info("[1/4] Preprocessing video...")
        t0 = time.perf_counter()
        keyframes: KeyframeSequence
        try:
            with tool_call(
                "video_preprocessor.process",
                video=os.path.basename(video_path),
            ) as _tc:
                keyframes = self.video_preprocessor.process(video_path)
                _tc.result(
                    f"coarse={len(keyframes.coarse_frames)}, "
                    f"precision={len(keyframes.precision_frames)}"
                )
        except Exception as exc:
            logger.error(
                "[orchestrator:analyze] PREPROCESS_ERROR | video=%s | %s",
                video_path,
                exc,
                exc_info=True,
            )
            keyframes = KeyframeSequence(coarse_frames=[], precision_frames=[])
        step_times["preprocessing"] = time.perf_counter() - t0
        context.keyframes = keyframes
        logger.info("  Coarse frames: %d", len(keyframes.coarse_frames))
        logger.info("  Precision frames: %d", len(keyframes.precision_frames))

        # Extract video metadata early
        video_meta = self._extract_video_meta(video_path)
        context.video_meta = video_meta

        # Step 2: Expert Agent Layer
        logger.info("[2/4] Expert Agent Layer...")
        t0 = time.perf_counter()
        candidates = []
        if self._expert_agent_layer:
            try:
                expert_result = self._expert_agent_layer.execute(context)
                if expert_result.success and expert_result.data:
                    candidates = expert_result.data
            except Exception as exc:
                logger.error(
                    "[orchestrator:analyze] EXPERT_LAYER_ERROR | video=%s | %s",
                    video_path,
                    exc,
                    exc_info=True,
                )
                candidates = []
        else:
            logger.warning("No ExpertAgentLayer configured, skipping")
        step_times["expert_agent_layer"] = time.perf_counter() - t0
        logger.info("  Candidates generated: %d", len(candidates))

        # Step 3: Adjudication
        logger.info("[3/4] Adjudication...")
        t0 = time.perf_counter()
        event_results: List[EventResult] = []
        adj_reasoning = ""
        adj_reasoning_chain: List[Dict[str, Any]] = []
        adj_audit_log: List[Any] = []
        if self._adjudication_step:
            try:
                adj_result = self._adjudication_step.execute(context)
                if adj_result.success and adj_result.data:
                    adj_data = adj_result.data
                    event_results = adj_data.event_results
                    adj_reasoning = adj_data.adjudication_reasoning
                    adj_reasoning_chain = adj_data.reasoning_chain
                    adj_audit_log = adj_data.audit_log
                    logger.info("  Adjudication reasoning: %s", adj_reasoning[:100] + "..." if len(adj_reasoning) > 100 else adj_reasoning)
            except Exception as exc:
                logger.error(
                    "[orchestrator:analyze] ADJUDICATION_ERROR | video=%s | %s",
                    video_path,
                    exc,
                    exc_info=True,
                )
                # Fallback: convert raw candidates to EventResults
                event_results = self._fallback(candidates)
        else:
            logger.warning("No AdjudicationStep configured, skipping")
        step_times["adjudication"] = time.perf_counter() - t0
        context.event_results = {r.event_id: r for r in event_results}
        detected_count = sum(1 for r in event_results if r.detected)
        logger.info("  Events detected: %d / %d", detected_count, len(event_results))

        # Step 4: Report generation
        logger.info("[4/4] Generating report...")
        t0 = time.perf_counter()
        usage_stats = self.vlm_engine.get_usage_stats()
        try:
            with tool_call(
                "report_generator.generate",
                formats=["markdown", "json", "binary"],
                events=len(event_results),
            ) as _tc:
                report = self.report_generator.generate(
                    event_results=event_results,
                    scene_info=None,
                    video_meta=video_meta,
                    usage_stats=usage_stats,
                    analysis_duration_sec=round(0.0, 2),  # placeholder, updated below
                    adjudication_reasoning=adj_reasoning,
                    reasoning_chain=adj_reasoning_chain or None,
                    audit_log=adj_audit_log or None,
                )
                _tc.result(
                    f"binary_code={report.binary_encoding.encoding_string}"
                )
        except Exception as exc:
            logger.error(
                "[orchestrator:analyze] REPORT_ERROR | video=%s | %s",
                video_path,
                exc,
                exc_info=True,
            )
            # Fallback: minimal report with error info
            report = Report(
                video_info=video_meta,
                scene_summary=SceneInfo(),
                event_results=event_results,
                binary_encoding=BinaryEncoding(),
                overall_traffic_description=f"[Report generation failed: {exc}]",
                llm_usage_stats=usage_stats,
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
        except Exception as exc:
            logger.error(
                "[orchestrator:_extract_video_meta] META_ERROR | video=%s | %s",
                video_path,
                exc,
                exc_info=True,
            )
            return VideoMetadata(
                file_path=video_path,
                file_name=Path(video_path).name,
                duration_sec=0.0,
                fps=0.0,
                total_frames=0,
                width=0,
                height=0,
            )
        finally:
            cap.release()
