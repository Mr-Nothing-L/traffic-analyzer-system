"""
Report generator for the traffic analyzer framework.

Produces structured reports (JSON, Markdown, binary encoding) from
event detection results, scene understanding, and video metadata.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from traffic_analyzer.models.schemas import (
    BinaryEncoding,
    EventResult,
    EventInstance,
    Report,
    SceneInfo,
    VideoMetadata,
)


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
        # Sort results by event_id for deterministic output
        sorted_results = sorted(event_results, key=lambda r: r.event_id)

        # Determine total categories for binary encoding
        total_categories = self._infer_total_categories(sorted_results)

        binary_encoding = self.to_binary_encoding(sorted_results, total_categories)
        final_classification = self._build_final_classification(binary_encoding)
        disposal_recommendations = self._build_disposal_recommendations(sorted_results)
        overall_desc = overall_traffic_description or self._generate_overall_description(
            scene_info, sorted_results
        )

        return Report(
            video_info=video_meta,
            scene_summary=scene_info,
            overall_traffic_description=overall_desc,
            event_results=sorted_results,
            binary_encoding=binary_encoding,
            final_classification=final_classification,
            disposal_recommendations=disposal_recommendations,
            llm_usage_stats=usage_stats,
            analysis_duration_sec=analysis_duration_sec,
            generated_at=datetime.now(),
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
        3. Per-category analysis with confidence / evidence / reasoning
        4. Final classification with binary encoding explanation
        5. Disposal recommendations
        """
        lines: List[str] = []

        # ---- Title ---------------------------------------------------------
        lines.append("# 交通事件分析报告")
        lines.append("")

        # ---- Video Info ----------------------------------------------------
        vm = report.video_info
        lines.append("## 视频信息")
        lines.append(f"- **文件名**: {vm.file_name}")
        lines.append(f"- **时长**: {vm.duration_sec:.1f} s")
        lines.append(f"- **帧率**: {vm.fps:.2f} fps")
        lines.append(f"- **分辨率**: {vm.width} x {vm.height}")
        lines.append(f"- **总帧数**: {vm.total_frames}")
        if vm.codec:
            lines.append(f"- **编码格式**: {vm.codec}")
        if vm.camera_id:
            lines.append(f"- **摄像头编号**: {vm.camera_id}")
        if vm.record_time:
            lines.append(f"- **录制时间**: {vm.record_time.isoformat()}")
        lines.append("")

        # ---- Overall Traffic Description -----------------------------------
        lines.append("## 整体交通态势")
        lines.append(report.overall_traffic_description)
        lines.append("")

        # ---- Scene Summary -------------------------------------------------
        sc = report.scene_summary
        lines.append("### 场景概览")
        lines.append(f"- **天气**: {sc.weather}")
        lines.append(f"- **光照**: {sc.lighting}")
        lines.append(f"- **交通密度**: {sc.traffic_density}")
        lines.append(f"- **道路数量**: {sc.road_count}")
        lines.append(f"- **预估车辆总数**: {sc.total_vehicles_estimate}")
        if sc.scene_description:
            lines.append(f"- **场景描述**: {sc.scene_description}")
        if sc.pedestrian_present is not None:
            lines.append(f"- **行人**: {'有' if sc.pedestrian_present else '无'}")
        if sc.non_motor_vehicle_present is not None:
            lines.append(f"- **非机动车**: {'有' if sc.non_motor_vehicle_present else '无'}")
        if sc.thrown_object_present is not None:
            lines.append(f"- **抛洒物**: {'有' if sc.thrown_object_present else '无'}")
        lines.append("")

        # ---- Direction Analysis (6-step detailed) ------------------------
        if sc.direction_analysis:
            da = sc.direction_analysis
            lines.append("### 车流方向分析（六步逻辑链）")
            lines.append("")

            # Step 1: Anchor Points
            if da.anchor_points:
                lines.append("#### Step 1: 静态锚点")
                for ap in da.anchor_points:
                    name = ap.get("name", "未知")
                    pos = ap.get("position", "")
                    typ = ap.get("type", "")
                    extra = f" [{typ}]" if typ else ""
                    lines.append(f"- **{name}**: {pos}{extra}")
                lines.append("")

            # Step 2: Vehicle Motions
            if da.vehicle_motions:
                lines.append("#### Step 2: 运动向量")
                for vm in da.vehicle_motions:
                    desc = vm.description or vm.vehicle_id
                    disp = vm.displacement or "未记录"
                    dire = vm.movement_direction or "unknown"
                    lines.append(f"- **{desc}**: {disp} → 方向: {dire}")
                lines.append("")

            # Step 3: Head Orientations
            if da.head_orientations:
                lines.append("#### Step 3: 车头朝向")
                for ho in da.head_orientations:
                    vid = ho.vehicle_id or "未知车辆"
                    ori = ho.head_orientation or "unknown"
                    evi = ho.evidence or "未记录"
                    lines.append(f"- **{vid}**: 朝向={ori}，依据：{evi}")
                lines.append("")

            # Step 4: Consistency Check
            if da.consistency_check:
                lines.append("#### Step 4: 一致性校验")
                lines.append("| 车辆ID | 运动方向 | 车头朝向 | 是否一致 | 异常判定 |")
                lines.append("|--------|----------|----------|----------|----------|")
                for cc in da.consistency_check:
                    c_icon = "是" if cc.consistent else "**否**"
                    a_icon = "**异常**" if cc.anomaly else "正常"
                    lines.append(
                        f"| {cc.vehicle_id} | {cc.movement} | {cc.head_orientation} | {c_icon} | {a_icon} |"
                    )
                lines.append("")

            # Step 5: Perspective Check
            if da.perspective_check:
                lines.append("#### Step 5: 透视校验")
                for pc in da.perspective_check:
                    vid = pc.vehicle_id or "未知车辆"
                    sz = pc.size_change or "未记录"
                    md = "一致" if pc.matches_direction else "**不一致**"
                    tp = "平行" if pc.trajectory_parallel_to_lanes else "**不平行**"
                    lines.append(f"- **{vid}**: 大小变化={sz}，透视匹配={md}，轨迹与车道={tp}")
                lines.append("")

            # Step 6: Conclusions
            if da.conclusions:
                lines.append("#### Step 6: 结论")
                for conc in da.conclusions:
                    lines.append(f"- **{conc.name} (道路 {conc.road_id})**:")
                    lines.append(f"  - 正常方向: **{conc.normal_direction}**")
                    lines.append(f"  - 置信度: {conc.confidence:.2f}")
                    lines.append(f"  - 依据摘要: {conc.evidence_summary}")
                lines.append("")

        # ---- Road Details (summary) --------------------------------------
        if sc.roads:
            lines.append("### 道路详情")
            for road in sc.roads:
                lines.append(f"**道路 {road.road_id}**: {road.name}")
                lines.append(f"- **正常方向**: {road.normal_direction} (置信度: {road.direction_confidence:.2f})")
                lines.append(f"- **车道数**: {road.lane_count}")
                lines.append(f"- **应急车道**: {'有' if road.has_emergency_lane else '无'}")
                if road.direction_evidence:
                    lines.append("- **方向证据**:")
                    for ev in road.direction_evidence:
                        ev_str = f"  - {ev.vehicle}: {ev.movement}"
                        if ev.location_earlier or ev.location_later:
                            ev_str += f" (位置变化: {ev.location_earlier} → {ev.location_later})"
                        if ev.frames_compared:
                            ev_str += f" [{ev.frames_compared}]"
                        lines.append(ev_str)
                lines.append("")

        # ---- Per-Category Analysis -----------------------------------------
        lines.append("## 事件类别分析")
        lines.append("")

        if not report.event_results:
            lines.append("_未检测到任何事件类别。_")
            lines.append("")
        elif all(not r.detected for r in report.event_results):
            # All events are zero — show a clear summary instead of listing every "not detected".
            lines.append("> **该视频无任何交通事件。** 所有事件类别均未触发。"
            )
            lines.append("")
        else:
            for result in report.event_results:
                lines.extend(self._render_event_result(result))

        # ---- Final Classification ------------------------------------------
        lines.append("## 最终分类")
        lines.append("")
        lines.append(f"**二进制编码**: `{{{report.binary_encoding.encoding_string}}}`")
        lines.append("")
        lines.append("- **编码说明**: 每一位对应一个事件类别（按 event_id 升序），")
        lines.append("  `1` 表示该类别被检测到，`0` 表示未检测到。")
        lines.append("")
        if report.binary_encoding.detected_events:
            detected_str = ", ".join(
                str(eid) for eid in report.binary_encoding.detected_events
            )
            lines.append(f"- **检测到的事件 ID**: {detected_str}")
        else:
            lines.append("- **检测到的事件 ID**: 无")
        lines.append("")
        lines.append(f"{report.final_classification}")
        lines.append("")

        # ---- Disposal Recommendations --------------------------------------
        lines.append("## 处置建议")
        lines.append("")
        if report.disposal_recommendations:
            for idx, rec in enumerate(report.disposal_recommendations, start=1):
                lines.append(f"{idx}. {rec}")
        else:
            lines.append("_暂无处置建议。_")
        lines.append("")

        # ---- Analysis Stats ------------------------------------------------
        lines.append("## 分析统计")
        lines.append("")
        lines.append(f"- **分析耗时**: {report.analysis_duration_sec:.2f} s")
        usage = report.llm_usage_stats
        if usage:
            lines.append(f"- **VLM 提供商**: {usage.get('provider', 'unknown')}")
            lines.append(f"- **模型**: {usage.get('model', 'unknown')}")
            lines.append(f"- **调用次数**: {usage.get('total_calls', 0)}")
            lines.append(f"- **Prompt Tokens**: {usage.get('total_prompt_tokens', 0)}")
            lines.append(f"- **Completion Tokens**: {usage.get('total_completion_tokens', 0)}")
            lines.append(f"- **总 Tokens**: {usage.get('total_tokens', 0)}")
            if usage.get('failed_calls', 0):
                lines.append(f"- **失败调用**: {usage.get('failed_calls', 0)}")
        lines.append("")

        # ---- Footer --------------------------------------------------------
        lines.append("---")
        lines.append(
            f"*报告生成时间: {report.generated_at.isoformat()}*"
        )
        lines.append("")

        return "\n".join(lines)

    def to_binary_encoding(
        self, event_results: List[EventResult], total_categories: int
    ) -> BinaryEncoding:
        """
        Create a :class:`BinaryEncoding` from detection results.

        The encoding string uses the format ``{bit_0_bit_1_..._bit_n}``
        where *bit_i* is ``1`` when the corresponding event category was
        detected and ``0`` otherwise.

        Parameters
        ----------
        event_results:
            Detection results (need not be sorted).
        total_categories:
            Total number of event categories that define the bit width.
            If ``0`` the width is inferred from the maximum ``event_id``
            present in *event_results* plus one.

        Returns
        -------
        BinaryEncoding
        """
        if total_categories <= 0:
            total_categories = self._infer_total_categories(event_results)

        detected_map = {r.event_id: r.detected for r in event_results}
        detected_events: List[int] = []
        bits: List[str] = []

        for eid in range(total_categories):
            if detected_map.get(eid, False):
                bits.append("1")
                detected_events.append(eid)
            else:
                bits.append("0")

        encoding_string = "_".join(bits)
        return BinaryEncoding(
            encoding_string=encoding_string,
            event_count=len(detected_events),
            detected_events=detected_events,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _infer_total_categories(self, event_results: List[EventResult]) -> int:
        """Infer bit width from results (max event_id + 1, at least 0)."""
        if not event_results:
            return 0
        return max(r.event_id for r in event_results) + 1

    def _generate_overall_description(
        self, scene_info: SceneInfo, event_results: List[EventResult]
    ) -> str:
        """Build a Chinese overall description from scene + events."""
        parts: List[str] = []

        # Scene description
        if scene_info.scene_description:
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

    def _render_event_result(self, result: EventResult) -> List[str]:
        """Render a single :class:`EventResult` as Markdown lines."""
        lines: List[str] = []
        status_icon = "✅" if result.detected else "❌"
        name_line = f"### {status_icon} 事件 {result.event_id}: {result.event_name}"
        if result.event_name_en:
            name_line += f" / {result.event_name_en}"
        lines.append(name_line)
        lines.append("")
        lines.append(f"- **是否检测到**: {'是' if result.detected else '否'}")
        lines.append(f"- **综合置信度**: {result.confidence:.2f}")
        if result.summary:
            lines.append(f"- **摘要**: {result.summary}")
        if result.reasoning:
            lines.append(f"- **推理过程**: {result.reasoning}")
        lines.append("")

        if result.detected and result.instances:
            lines.append("#### 检测实例")
            for idx, inst in enumerate(result.instances, start=1):
                lines.append(f"**实例 {idx}**")
                lines.append(f"- **置信度**: {inst.confidence:.2f} ({inst.confidence_level.value})")
                if inst.vehicle_id:
                    lines.append(f"- **车辆 ID**: {inst.vehicle_id}")
                if inst.road_id is not None:
                    lines.append(f"- **道路 ID**: {inst.road_id}")
                if inst.evidence_frames:
                    frames_str = ", ".join(str(f) for f in inst.evidence_frames)
                    lines.append(f"- **证据帧**: {frames_str}")
                if inst.start_time_sec or inst.end_time_sec:
                    lines.append(
                        f"- **时间区间**: {inst.start_time_sec:.1f}s - {inst.end_time_sec:.1f}s"
                    )
                if inst.description:
                    lines.append(f"- **描述**: {inst.description}")
                if inst.reasoning:
                    lines.append(f"- **推理过程**: {inst.reasoning}")
                if inst.disposal_suggestion:
                    lines.append(f"- **处置建议**: {inst.disposal_suggestion}")
                lines.append("")
        elif result.detected and not result.instances:
            lines.append("_检测到事件，但无详细实例信息。_")
            lines.append("")

        if result.analysis_process:
            lines.append("#### 分析过程")
            for step in result.analysis_process:
                lines.append(f"- {step}")
            lines.append("")

        return lines
