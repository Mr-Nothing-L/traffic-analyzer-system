"""Factory for building rejection reports from the prefilter step."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from traffic_analyzer.core.report_generator import ReportGenerator
from traffic_analyzer.models.schemas import Report, VideoMetadata

logger = logging.getLogger(__name__)


def generate_reject_report(
    report_generator: ReportGenerator,
    video_meta: VideoMetadata,
    reject_reason: str,
    checks: Optional[Dict[str, Any]] = None,
    usage_stats: Optional[Dict[str, Any]] = None,
) -> Report:
    """Generate a report for a video rejected by the preprocessor prefilter.

    Args:
        report_generator: Report generation module.
        video_meta: Metadata for the rejected video.
        reject_reason: Human-readable reason for rejection.
        checks: Optional prefilter check details for debugging.
        usage_stats: Optional LLM usage statistics.

    Returns:
        A Report marked as rejected.
    """
    report = report_generator.generate(
        event_results=[],
        scene_info=None,
        video_meta=video_meta,
        usage_stats=usage_stats,
        analysis_duration_sec=0.0,
        overall_traffic_description=f"视频被预处理筛除: {reject_reason}",
    )
    report.rejected = True
    report.reject_reason = reject_reason
    # Store prefilter checks in context for debugging
    if checks:
        logger.info("[orchestrator:analyze] PREFILTER_CHECKS | %s", checks)
    return report
