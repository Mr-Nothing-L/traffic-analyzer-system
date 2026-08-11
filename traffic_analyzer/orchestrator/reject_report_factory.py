"""Factory for building rejection reports from the prefilter step.

[文件说明]
作用:工厂函数 generate_reject_report()——为预处理预筛拒绝(或视频无法解码)的视频生成标记 rejected=True、含拒绝原因与全宽二进制编码的 Report。
上游:orchestrator/analysis_orchestrator.py(预筛拒绝/无可用帧分支);tests/test_orchestrator.py。
下游:core/report_generator.py(ReportGenerator.generate)、models/schemas.py(Report、VideoMetadata)。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from traffic_analyzer.core.report_generator import ReportGenerator
from traffic_analyzer.models.schemas import BinaryEncoding, Report, VideoMetadata

logger = logging.getLogger(__name__)


def generate_reject_report(
    report_generator: ReportGenerator,
    video_meta: VideoMetadata,
    reject_reason: str,
    checks: Optional[Dict[str, Any]] = None,
    usage_stats: Optional[Dict[str, Any]] = None,
    total_categories: Optional[int] = None,
) -> Report:
    """Generate a report for a video rejected by the preprocessor prefilter.

    Args:
        report_generator: Report generation module.
        video_meta: Metadata for the rejected video.
        reject_reason: Human-readable reason for rejection.
        checks: Optional prefilter check details for debugging.
        usage_stats: Optional LLM usage statistics.
        total_categories: Max configured event_id, used to give the
            binary encoding its full bit width (no detection was performed).

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
        total_categories=total_categories,
    )
    report.rejected = True
    report.binary_encoding = BinaryEncoding(
        encoding_string="_".join(["_"] * 11),
        event_count=0,
        detected_events=[],
    )
    report.reject_reason = reject_reason
    report.final_classification = "视频被筛除/无法分析，未进行事件检测。"
    # Store prefilter checks in context for debugging
    if checks:
        logger.info("[orchestrator:analyze] PREFILTER_CHECKS | %s", checks)
    return report
