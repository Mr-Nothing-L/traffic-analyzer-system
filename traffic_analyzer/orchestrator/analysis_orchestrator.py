"""
AnalysisOrchestrator module for the traffic analyzer framework.

Wires all modules together into a cohesive analysis pipeline:
1. Load configuration
2. Preprocess video (coarse + precision sampling)
3. Expert Agent Layer (event detection)
4. Adjudication (post-processing)
5. Cross-validate with CV tracks if provided
6. Generate final report

[文件说明]
作用:主流水线编排类 AnalysisOrchestrator。analyze() 串联:[1/4]视频预处理(粗/精细采样,预筛拒绝时直接出拒绝报告)→[2/4]专家层事件检测→[3/4]裁决(失败时兜底用原始候选)→[3.4/4]锚定核验(推翻幻觉检出)→[3.5/4]SFT 标签改写与证据导出(可选)→[4/4]生成最终报告;from_config_dir() 为工厂方法。
上游:traffic_analyzer/cli.py(命令行入口);tests/test_orchestrator.py、tests/test_evidence_exporter.py。
下游:core/config_manager.py(YAML 配置加载)、core/video_preprocessor.py、core/vlm_engine.py、core/pipeline_steps.py(ExpertAgentLayer/AdjudicationStep)、core/grounding_verification.py、core/sft_label_rewrite.py、core/evidence_exporter.py、core/report_generator.py、models/schemas.py(AnalysisContext/Report 等)、utils/tool_call_logger.py;同包 orchestrator_exceptions、reject_report_factory、video_meta_extractor。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.evidence_exporter import export_evidence
from traffic_analyzer.core.grounding_verification import GroundingVerificationStep
from traffic_analyzer.core.pipeline_steps import (
    AdjudicationStep,
    ExpertAgentLayer,
)
from traffic_analyzer.core.report_generator import ReportGenerator
from traffic_analyzer.core.sft_label_rewrite import SftLabelRewriteStep
from traffic_analyzer.core.video_preprocessor import VideoPrefilterError, VideoPreprocessor
from traffic_analyzer.core.vlm_engine import FatalAPIError, VLMInferenceEngine
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    BinaryEncoding,
    EventResult,
    KeyframeSequence,
    Report,
    SceneInfo,
    VideoMetadata,
)
from traffic_analyzer.utils.progress import emit_run_done, emit_step
from traffic_analyzer.utils.tool_call_logger import tool_call

from .orchestrator_exceptions import OrchestratorError
from .reject_report_factory import generate_reject_report
from .video_meta_extractor import extract_video_meta

logger = logging.getLogger(__name__)


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

        vlm_engine = VLMInferenceEngine(system_config.llm_providers)
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
        output_dir: Optional[str] = None,
    ) -> Report:
        """Run the full analysis pipeline on a video.

        Args:
            video_path: Path to the video file.
            scene_understanding: Optional pre-computed scene understanding.
            output_dir: Optional directory where the report will be written.
                When provided, far-enhancement composite images are saved
                under ``<output_dir>/tmp_img/<video_stem>`` and referenced
                with relative paths in the markdown report.

        Returns:
            Complete analysis report.
        """
        logger.info("=" * 60)
        logger.info("Starting traffic analysis for: %s", video_path)
        logger.info("=" * 60)

        analysis_start = time.perf_counter()
        step_times: Dict[str, float] = {}
        context = AnalysisContext(
            config=self.config_manager.load_all(),
            output_dir=output_dir,
        )
        if scene_understanding is not None:
            context.scene_understanding = scene_understanding

        # Extract video metadata early (needed for prefilter reject report)
        video_meta = extract_video_meta(video_path)
        context.video_meta = video_meta

        # Step 1: Video preprocessing
        logger.info("[1/4] Preprocessing video...")
        emit_step(1, 4, "预处理")
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
        except VideoPrefilterError as exc:
            logger.warning(
                "[orchestrator:analyze] PREFILTER_REJECT | video=%s | reason=%s",
                video_path,
                exc,
            )
            return generate_reject_report(
                report_generator=self.report_generator,
                video_meta=video_meta,
                reject_reason=str(exc),
                checks=getattr(exc, "checks", None),
                usage_stats=self.vlm_engine.get_usage_stats(),
                total_categories=self._max_event_id(),
            )
        except Exception as exc:
            logger.error(
                "[orchestrator:analyze] PREPROCESS_ERROR | video=%s | %s",
                video_path,
                exc,
                exc_info=True,
            )
            keyframes = KeyframeSequence(coarse_frames=[], precision_frames=[])
        if not keyframes.coarse_frames and not keyframes.precision_frames:
            reject_reason = "视频无法打开/解码失败，无可用帧"
            logger.warning(
                "[orchestrator:analyze] PREPROCESS_REJECT | video=%s | reason=%s",
                video_path,
                reject_reason,
            )
            return generate_reject_report(
                report_generator=self.report_generator,
                video_meta=video_meta,
                reject_reason=reject_reason,
                usage_stats=self.vlm_engine.get_usage_stats(),
                total_categories=self._max_event_id(),
            )
        step_times["preprocessing"] = time.perf_counter() - t0
        context.keyframes = keyframes
        logger.info("  Coarse frames: %d", len(keyframes.coarse_frames))
        logger.info("  Precision frames: %d", len(keyframes.precision_frames))

        # Step 2: Expert Agent Layer
        logger.info("[2/4] Expert Agent Layer...")
        emit_step(2, 4, "专家")
        t0 = time.perf_counter()
        candidates = []
        if self._expert_agent_layer:
            try:
                expert_result = self._expert_agent_layer.execute(context)
                if expert_result.success and expert_result.data:
                    candidates = expert_result.data
            except FatalAPIError:
                raise
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
        emit_step(3, 4, "裁决")
        t0 = time.perf_counter()
        event_results: List[EventResult] = []
        adj_reasoning = ""
        adj_reasoning_chain: List[Dict[str, Any]] = []
        adj_audit_log: List[Any] = []
        if self._adjudication_step:
            adj_result = self._adjudication_step.execute(context)
            if adj_result.success and adj_result.data:
                adj_data = adj_result.data
                event_results = adj_data.event_results
                adj_reasoning = adj_data.adjudication_reasoning
                adj_reasoning_chain = adj_data.reasoning_chain
                adj_audit_log = adj_data.audit_log
                logger.info("  Adjudication reasoning: %s", adj_reasoning[:100] + "..." if len(adj_reasoning) > 100 else adj_reasoning)
        else:
            logger.warning("No AdjudicationStep configured, skipping")
        step_times["adjudication"] = time.perf_counter() - t0
        context.event_results = {r.event_id: r for r in event_results}
        detected_count = sum(1 for r in event_results if r.detected)
        logger.info("  Events detected: %d / %d", detected_count, len(event_results))

        # Step 3.4: Grounding verification (optional; overturns hallucinated positives)
        logger.info("[3.4/4] Grounding verification...")
        t0 = time.perf_counter()
        try:
            grounding_step = GroundingVerificationStep(self.config_manager, self.vlm_engine)
            grounding_result = grounding_step.execute(context)
            if grounding_result.success and grounding_result.data:
                overturned = sum(1 for r in grounding_result.data if not r["grounded"])
                logger.info("  Grounding verification: %d positive(s) overturned", overturned)
        except FatalAPIError:
            raise
        except Exception as exc:
            logger.error(
                "[orchestrator:analyze] GROUNDING_VERIFICATION_ERROR | video=%s | %s",
                video_path,
                exc,
                exc_info=True,
            )
        step_times["grounding_verification"] = time.perf_counter() - t0
        # EventResult 为可变模型，核验步骤就地修改（推翻的 detected=False）；
        # context.event_results 与本地 event_results 共享同一批对象，天然同步。
        event_results = list(context.event_results.values())
        detected_count = sum(1 for r in event_results if r.detected)
        logger.info("  Events detected after grounding check: %d / %d", detected_count, len(event_results))

        # Step 3.5: SFT label rewrite (optional, auxiliary)
        if context.config is not None and getattr(context.config, "sft_label_enabled", False):
            logger.info("[3.5/4] SFT label rewrite...")
            emit_step(3.5, 4, "SFT")
            t0 = time.perf_counter()
            try:
                sft_step = SftLabelRewriteStep(self.config_manager, self.vlm_engine)
                sft_result = sft_step.execute(context)
                if sft_result.success and sft_result.data:
                    logger.info("  SFT sample written to: %s", sft_result.data)
            except FatalAPIError:
                raise
            except Exception as exc:
                logger.error(
                    "[orchestrator:analyze] SFT_LABEL_REWRITE_ERROR | video=%s | %s",
                    video_path,
                    exc,
                    exc_info=True,
                )
            step_times["sft_label_rewrite"] = time.perf_counter() - t0

            # Evidence export (auxiliary): editable per-video evidence.json
            # for the web UI, written next to the SFT samples.
            sft_output_dir = getattr(context.config, "sft_label_output_dir", None)
            if sft_output_dir:
                try:
                    evidence_path = export_evidence(context, Path(sft_output_dir))
                    if evidence_path is not None:
                        logger.info("  Evidence exported to: %s", evidence_path)
                except FatalAPIError:
                    raise
                except Exception as exc:
                    logger.error(
                        "[orchestrator:analyze] EVIDENCE_EXPORT_ERROR | video=%s | %s",
                        video_path,
                        exc,
                        exc_info=True,
                    )

        # Step 4: Report generation
        logger.info("[4/4] Generating report...")
        emit_step(4, 4, "报告")
        t0 = time.perf_counter()
        usage_stats = self.vlm_engine.get_usage_stats()
        total_categories = self._max_event_id()
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
                    expert_candidates=[
                        c.to_report_dict()
                        for c in context.event_candidates.values()
                    ],
                    total_categories=total_categories,
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
        emit_run_done("ok")

        return report

    # ------------------------------------------------------------------
    # Pipeline steps
    # ------------------------------------------------------------------

    def _max_event_id(self) -> int:
        """Max configured event_id; defines the binary encoding bit width (bits 1..N)."""
        return max(
            (c.event_id for c in self.config_manager.get_event_categories()),
            default=0,
        )

    @staticmethod
    def _extract_video_meta(video_path: str) -> VideoMetadata:
        """Extract video metadata using the preprocessor.

        Delegates to :func:`video_meta_extractor.extract_video_meta`.
        """
        return extract_video_meta(video_path)
