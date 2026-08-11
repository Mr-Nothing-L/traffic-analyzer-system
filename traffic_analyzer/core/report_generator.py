"""Report generator for the traffic analyzer framework.

Produces structured reports (JSON, Markdown, binary encoding) from
event detection results, scene understanding, and video metadata.

[文件说明]
作用:报告生成器(ReportGenerator),将事件检测结果、场景理解、视频元数据与
     使用统计组装为 Report 模型,并导出 JSON、Markdown 与二进制编码。
上游:orchestrator/analysis_orchestrator.py、orchestrator/reject_report_factory.py、cli.py。
下游:core/report_markdown_renderer.py(_render_markdown 渲染 Markdown)、
     models/schemas.py 的 Report/EventResult 等模型。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from traffic_analyzer.core.report_markdown_renderer import _render_markdown
from traffic_analyzer.models.schemas import (
    BinaryEncoding,
    EventResult,
    Report,
    SceneInfo,
    VideoMetadata,
)

logger = logging.getLogger(__name__)


class ReportGenerator:
    """Generates human-readable and machine-readable traffic analysis reports."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(
        self,
        event_results: List[EventResult],
        scene_info: SceneInfo,
        video_meta: VideoMetadata,
        usage_stats: Dict[str, Any],
        overall_traffic_description: Optional[str] = None,
        analysis_duration_sec: float = 0.0,
        adjudication_reasoning: str = "",
        reasoning_chain: Optional[List[Dict[str, Any]]] = None,
        audit_log: Optional[List[Any]] = None,
        expert_candidates: Optional[List[Dict[str, Any]]] = None,
        total_categories: Optional[int] = None,
    ) -> Report:
        """
        Build a complete :class:`Report` from analysis artefacts.

        Parameters
        ----------
        event_results:
            Per-category detection results.
        scene_info:
            Global scene understanding.
        video_meta:
            Input video metadata.
        usage_stats:
            Arbitrary LLM / system usage statistics.
        overall_traffic_description:
            Optional pre-computed overall description.  When ``None`` a
            template-based description is generated from ``scene_info``.

        Returns
        -------
        Report
            Fully populated Pydantic model ready for serialization.
        """
        from traffic_analyzer.models.schemas import SceneInfo

        common_kwargs = {
            "video_info": video_meta,
            "scene_summary": scene_info or SceneInfo(),
            "llm_usage_stats": usage_stats,
            "analysis_duration_sec": analysis_duration_sec,
            "generated_at": datetime.now(),
            "adjudication_reasoning": adjudication_reasoning,
            "reasoning_chain": reasoning_chain or [],
            "audit_log": audit_log or [],
        }

        try:
            # Sort results by event_id for deterministic output
            sorted_results = sorted(event_results, key=lambda r: r.event_id)

            # Determine total categories for binary encoding
            effective_total = total_categories if total_categories is not None and total_categories > 0 else self._infer_total_categories(sorted_results)

            binary_encoding = self.to_binary_encoding(sorted_results, effective_total)
            final_classification = self._build_final_classification(binary_encoding)
            disposal_recommendations = self._build_disposal_recommendations(sorted_results)
            overall_desc = overall_traffic_description or self._generate_overall_description(
                scene_info, sorted_results
            )

            return Report(
                **common_kwargs,
                overall_traffic_description=overall_desc,
                event_results=sorted_results,
                expert_candidates=expert_candidates or [],
                binary_encoding=binary_encoding,
                final_classification=final_classification,
                disposal_recommendations=disposal_recommendations,
            )
        except Exception as exc:
            logger.error(
                "[report_generator:generate] GENERATE_ERROR | events=%d | %s",
                len(event_results),
                exc,
                exc_info=True,
            )
            return Report(
                **common_kwargs,
                overall_traffic_description=f"报告生成过程中发生错误: {exc}",
                event_results=[],
                binary_encoding=BinaryEncoding(
                    encoding_string="_".join(["_"] * 11),
                    event_count=0,
                    detected_events=[],
                ),
                final_classification="报告生成失败，请检查日志。",
                disposal_recommendations=[],
            )

    def to_json(self, report: Report) -> str:
        """Serialize *report* to a pretty-printed JSON string."""
        return report.model_dump_json(indent=2, ensure_ascii=False)

    def to_markdown(self, report: Report) -> str:
        """
        Render *report* as a human-readable Markdown document (Chinese UI).

        Sections
        --------
        1. Video metadata header
        2. Overall traffic situation (Chinese)
        3. Per-category analysis with evidence / reasoning
        4. Final classification with binary encoding explanation
        5. Disposal recommendations
        """
        try:
            return _render_markdown(report)
        except Exception as exc:
            logger.error(
                "[report_generator:to_markdown] RENDER_ERROR | events=%d | %s",
                len(report.event_results),
                exc,
                exc_info=True,
            )
            # Fallback: simplified error report
            vm = report.video_info
            lines: List[str] = [
                "# 交通事件分析报告",
                "",
                "## 视频信息",
                f"- **文件名**: {vm.file_name}",
                f"- **时长**: {vm.duration_sec:.1f} s",
                "",
                "---",
                "",
                "**报告渲染过程中发生错误，以下为简化输出。**",
                "",
                f"错误信息: `{exc}`",
                "",
                "---",
                f"*报告生成时间: {report.generated_at.isoformat()}*",
                "",
            ]
            return "\n".join(lines)

    def to_binary_encoding(
        self, event_results: List[EventResult], total_categories: int
    ) -> BinaryEncoding:
        """
        Create a :class:`BinaryEncoding` from detection results.

        The encoding string uses the format ``{bit_1_bit_2_..._bit_N}``
        where *bit_i* is ``1`` when the event category with global
        ``event_id == i`` was detected and ``0`` otherwise. Bit positions
        follow the annotation document v4.5 action numbers (1..11); id 9 is
        the normal indicator: set to 1 when no events detected, 0 otherwise.

        Parameters
        ----------
        event_results:
            Detection results (need not be sorted).
        total_categories:
            Maximum event_id that defines the bit width (bits 1..N).
            If ``0`` the width is inferred from the maximum ``event_id``
            present in *event_results*.

        Returns
        -------
        BinaryEncoding
        """
        try:
            if total_categories <= 0:
                total_categories = self._infer_total_categories(event_results)

            detected_map = {r.event_id: r.detected for r in event_results}
            detected_events: List[int] = []
            bits: List[str] = []

            for eid in range(1, total_categories + 1):
                if detected_map.get(eid, False):
                    bits.append("1")
                    detected_events.append(eid)
                else:
                    bits.append("0")

            # ADR-0001: bit 9 is the normal indicator — set to 1 when no
            # events were detected at all and bit 9 exists in the encoding.
            if len(detected_events) == 0 and total_categories >= 9:
                bits[8] = "1"

            encoding_string = "_".join(bits)
            return BinaryEncoding(
                encoding_string=encoding_string,
                event_count=len(detected_events),
                detected_events=detected_events,
            )
        except Exception as exc:
            logger.error(
                "[report_generator:to_binary_encoding] ENCODING_ERROR | events=%d total_categories=%d | %s",
                len(event_results),
                total_categories,
                exc,
                exc_info=True,
            )
            return BinaryEncoding(
                encoding_string="_".join(["_"] * 11),
                event_count=0,
                detected_events=[],
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _infer_total_categories(self, event_results: List[EventResult]) -> int:
        """Infer bit width from results (max event_id, at least 0)."""
        if not event_results:
            return 0
        return max(r.event_id for r in event_results)

    def _generate_overall_description(
        self, scene_info: SceneInfo, event_results: List[EventResult]
    ) -> str:
        """Build a Chinese overall description from scene + events."""
        parts: List[str] = []

        # Scene description
        if scene_info is None:
            parts.append("暂无场景描述信息。")
        elif scene_info.scene_description:
            parts.append(scene_info.scene_description)
        else:
            parts.append(
                f"当前场景共 {scene_info.road_count} 条道路，"
                f"天气状况为 {scene_info.weather}，"
                f"光照条件为 {scene_info.lighting}，"
                f"交通密度评估为 {scene_info.traffic_density}。"
            )

        # Event summary
        detected = [r for r in event_results if r.detected]
        if detected:
            names = "、".join(r.event_name for r in detected)
            parts.append(f"检测到 {len(detected)} 类交通事件（{names}），需关注后续处置建议。")
        else:
            parts.append("未检测到显著交通事件，交通状况平稳。")

        return "".join(parts)

    def _build_final_classification(self, binary_encoding: BinaryEncoding) -> str:
        """Generate a concise Chinese final classification sentence."""
        enc = binary_encoding.encoding_string
        if binary_encoding.detected_events:
            return (
                f"根据二进制编码 `{{{enc}}}`，"
                f"共识别出 {binary_encoding.event_count} 类交通事件，"
                f"建议结合视频复核并启动相应处置流程。"
            )
        return (
            f"根据二进制编码 `{{{enc}}}`，"
            "**该视频未识别出任何交通事件，当前交通状况正常。**"
        )

    def _build_disposal_recommendations(
        self, event_results: List[EventResult]
    ) -> List[str]:
        """Aggregate disposal suggestions from all detected instances."""
        recommendations: List[str] = []
        for result in event_results:
            if not result.detected:
                continue
            # Collect instance-level suggestions
            for inst in result.instances:
                if inst.disposal_suggestion:
                    recommendations.append(inst.disposal_suggestion)
            # Fallback to a generic suggestion if none provided
            if not any(inst.disposal_suggestion for inst in result.instances):
                recommendations.append(
                    f"【{result.event_name}】已触发，建议人工复核并记录。"
                )
        return recommendations
