"""
Report generator for the traffic analyzer framework.

Produces structured reports (JSON, Markdown, binary encoding) from
event detection results, scene understanding, and video metadata.
"""

from __future__ import annotations

import json
import logging
import re
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
        try:
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

            from traffic_analyzer.models.schemas import SceneInfo
            return Report(
                video_info=video_meta,
                scene_summary=scene_info or SceneInfo(),
                overall_traffic_description=overall_desc,
                event_results=sorted_results,
                binary_encoding=binary_encoding,
                final_classification=final_classification,
                disposal_recommendations=disposal_recommendations,
                llm_usage_stats=usage_stats,
                analysis_duration_sec=analysis_duration_sec,
                generated_at=datetime.now(),
                adjudication_reasoning=adjudication_reasoning,
                reasoning_chain=reasoning_chain or [],
                audit_log=audit_log or [],
            )
        except Exception as exc:
            logger.error(
                "[report_generator:generate] GENERATE_ERROR | events=%d | %s",
                len(event_results),
                exc,
                exc_info=True,
            )
            from traffic_analyzer.models.schemas import SceneInfo
            return Report(
                video_info=video_meta,
                scene_summary=scene_info or SceneInfo(),
                overall_traffic_description=f"报告生成过程中发生错误: {exc}",
                event_results=[],
                binary_encoding=BinaryEncoding(
                    encoding_string="error",
                    event_count=0,
                    detected_events=[],
                ),
                final_classification="报告生成失败，请检查日志。",
                disposal_recommendations=[],
                llm_usage_stats=usage_stats,
                analysis_duration_sec=analysis_duration_sec,
                generated_at=datetime.now(),
                adjudication_reasoning=adjudication_reasoning,
                reasoning_chain=reasoning_chain or [],
                audit_log=audit_log or [],
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
            return self._render_markdown(report)
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

    def _render_markdown(self, report: Report) -> str:
        """Internal: render report as Markdown (may raise)."""
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
        lines.append("")
        lines.append("| 属性 | 值 |")
        lines.append("|------|-----|")
        lines.append(f"| 天气 | {sc.weather} |")
        lines.append(f"| 光照 | {sc.lighting} |")
        lines.append(f"| 交通密度 | {sc.traffic_density} |")
        lines.append(f"| 道路数量 | {sc.road_count} |")
        lines.append(f"| 预估车辆总数 | {sc.total_vehicles_estimate} |")
        if sc.pedestrian_present is not None:
            lines.append(f"| 行人 | {'有' if sc.pedestrian_present else '无'} |")
        if sc.non_motor_vehicle_present is not None:
            lines.append(f"| 非机动车 | {'有' if sc.non_motor_vehicle_present else '无'} |")
        if sc.thrown_object_present is not None:
            lines.append(f"| 抛洒物 | {'有' if sc.thrown_object_present else '无'} |")
        lines.append("")
        if sc.scene_description:
            lines.append(f"**场景描述**: {sc.scene_description}")
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
                    lines.append(f"  - 依据摘要: {conc.evidence_summary}")
                lines.append("")

        # ---- Road Details (summary) --------------------------------------
        if sc.roads:
            lines.append("### 道路详情")
            for road in sc.roads:
                lines.append(f"**道路 {road.road_id}**: {road.name}")
                lines.append(f"- **正常方向**: {road.normal_direction}")
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

        # ---- Event Summary Table -------------------------------------------
        lines.append("## 事件类别分析")
        lines.append("")

        if not report.event_results:
            lines.append("_未检测到任何事件类别。_")
            lines.append("")
        else:
            # Summary table for all events
            lines.append("### 事件检测总览")
            lines.append("")
            lines.append("| 事件ID | 事件名称 | 检测结果 | 描述 |")
            lines.append("|--------|----------|----------|------|")
            for result in report.event_results:
                detected_str = "**是**" if result.detected else "否"
                desc = result.summary or (result.instances[0].description if result.instances else "—")
                # Truncate long descriptions for the summary table
                if len(desc) > 40:
                    desc = desc[:37] + "..."
                lines.append(
                    f"| {result.event_id} | {result.event_name} | {detected_str} | {desc} |"
                )
            lines.append("")

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

        # ---- Adjudication Details ------------------------------------------
        lines.append("## 裁决详情")
        lines.append("")
        if report.adjudication_reasoning:
            lines.append("### 总体裁决推理")
            lines.append(report.adjudication_reasoning)
            lines.append("")

        if report.reasoning_chain:
            lines.append("### 逐事件推理链")
            lines.append("")
            lines.append("| 事件ID | 事件名称 | 决策 | 思考过程 | 决策依据 |")
            lines.append("|--------|----------|------|----------|----------|")
            for rc in report.reasoning_chain:
                eid = rc.get('event_id', '—')
                ename = rc.get('event_name', '—')
                decision = rc.get('decision', '—')
                thought = rc.get('thought_process', '—')
                basis = rc.get('basis', '—')
                # Truncate long text for table
                if len(thought) > 30:
                    thought = thought[:27] + "..."
                if len(basis) > 30:
                    basis = basis[:27] + "..."
                lines.append(f"| {eid} | {ename} | {decision} | {thought} | {basis} |")
            lines.append("")
        else:
            lines.append("_未记录详细裁决推理链。_")
            lines.append("")

        if report.audit_log:
            lines.append("### 审计日志")
            lines.append("| 事件 | 动作 | 原因 | 规则 |")
            lines.append("|------|------|------|------|")
            for entry in report.audit_log:
                action_icon = "保留" if entry.action == "included" else "**排除**"
                rule_str = entry.rule_id or "无"
                lines.append(f"| {entry.event_name} | {action_icon} | {entry.reason} | {rule_str} |")
            lines.append("")
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
        try:
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
        except Exception as exc:
            logger.error(
                "[report_generator:to_binary_encoding] ENCODING_ERROR | events=%d total_categories=%d | %s",
                len(event_results),
                total_categories,
                exc,
                exc_info=True,
            )
            return BinaryEncoding(
                encoding_string="error",
                event_count=0,
                detected_events=[],
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

    def _clean_expert_description(self, text: str) -> str:
        """Remove fenced code blocks and standalone JSON objects from expert text.

        Keeps only natural-language paragraphs.
        """
        try:
            # 1. Strip fenced code blocks (```json ... ``` or ``` ... ```)
            cleaned = re.sub(r"```[a-zA-Z]*\n.*?\n```", "", text, flags=re.DOTALL)
            # Also handle single-backtick fenced blocks that may not have a trailing newline
            cleaned = re.sub(r"```[a-zA-Z]*.*?```", "", cleaned, flags=re.DOTALL)

            # 2. Remove multi-line JSON objects/arrays
            # Detect blocks that start with { or [ and end with } or ],
            # where most lines look like JSON (contain ":", commas, quotes)
            cleaned = self._strip_json_blocks(cleaned)

            # 3. Remove standalone JSON objects — lines that are just a JSON dict/array
            lines: List[str] = []
            for line in cleaned.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                # Skip lines that look like a complete JSON object/array
                if (stripped.startswith("{") and stripped.endswith("}")) or \
                   (stripped.startswith("[") and stripped.endswith("]")):
                    continue
                lines.append(line)

            # 4. Collapse multiple blank lines and strip edges
            text_out = "\n".join(lines).strip()
            text_out = re.sub(r"\n{3,}", "\n\n", text_out)

            # 5. Normalize markdown formatting (tables, horizontal rules)
            text_out = self._normalize_markdown(text_out)

            # 6. Downgrade markdown headings so they don't exceed parent level (#####)
            text_out = self._downgrade_headings(text_out, max_level=5)
            return text_out
        except Exception as exc:
            logger.error(
                "[report_generator:_clean_expert_description] CLEAN_ERROR | text_len=%d | %s",
                len(text),
                exc,
                exc_info=True,
            )
            return text

    def _strip_json_blocks(self, text: str) -> str:
        """Remove contiguous blocks that look like JSON objects or arrays."""
        lines = text.splitlines()
        result: List[str] = []
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            # Detect start of a JSON block
            if stripped.startswith("{") or stripped.startswith("["):
                # Try to find where this JSON block ends
                depth = 0
                in_string = False
                escape_next = False
                block_lines: List[str] = []
                j = i
                while j < len(lines):
                    line = lines[j]
                    block_lines.append(line)
                    for ch in line:
                        if escape_next:
                            escape_next = False
                            continue
                        if ch == "\\":
                            escape_next = True
                            continue
                        if ch == '"' and not in_string:
                            in_string = True
                        elif ch == '"' and in_string:
                            in_string = False
                        elif not in_string:
                            if ch in "{[":
                                depth += 1
                            elif ch in "}]":
                                depth -= 1
                    # End of block: depth back to 0 and line ends with } or ]
                    if depth == 0 and not in_string:
                        last_stripped = line.strip()
                        if last_stripped.endswith("}") or last_stripped.endswith("]"):
                            break
                    j += 1
                else:
                    # Block didn't close — treat as regular text
                    result.extend(block_lines)
                i = j + 1
                continue
            result.append(lines[i])
            i += 1
        return "\n".join(result)

    def _downgrade_headings(self, text: str, max_level: int) -> str:
        """Downgrade markdown headings (# → #####) so they don't exceed parent level."""
        lines = text.splitlines()
        result: List[str] = []
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("#"):
                # Count leading #
                level = 0
                for ch in stripped:
                    if ch == "#":
                        level += 1
                    else:
                        break
                if level > 0 and level < max_level:
                    # Pad to max_level
                    new_line = "#" * max_level + stripped[level:]
                    result.append(new_line)
                    continue
            result.append(line)
        return "\n".join(result)

    def _normalize_markdown(self, text: str) -> str:
        """Ensure proper markdown spacing around tables and horizontal rules."""
        lines = text.splitlines()
        result: List[str] = []
        prev_was_table = False

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Detect table row: starts with | and has at least one more |
            is_table = stripped.startswith("|") and stripped.count("|") >= 2

            # Detect horizontal rule
            is_hr = stripped == "---" or stripped == "***" or stripped == "___"

            # Add blank line before table if previous line is not blank and not a table
            if is_table and result and result[-1].strip() and not prev_was_table:
                result.append("")

            # Add blank line before and after horizontal rule
            if is_hr:
                if result and result[-1].strip():
                    result.append("")
                result.append(line)
                # Add blank line after if next line exists and is not blank
                if i + 1 < len(lines) and lines[i + 1].strip():
                    result.append("")
                prev_was_table = False
                continue

            result.append(line)
            prev_was_table = is_table

        return "\n".join(result)

    def _render_event_result(self, result: EventResult) -> List[str]:
        """Render a single :class:`EventResult` as Markdown lines."""
        try:
            lines: List[str] = []
            status_icon = "✅" if result.detected else "❌"
            name_line = f"### {status_icon} 事件 {result.event_id}: {result.event_name}"
            if result.event_name_en:
                name_line += f" / {result.event_name_en}"
            lines.append(name_line)
            lines.append("")

            # Main result info as a compact table
            lines.append("| 字段 | 内容 |")
            lines.append("|------|------|")
            lines.append(f"| 是否检测到 | {'**是**' if result.detected else '否'} |")
            if result.summary:
                lines.append(f"| 摘要 | {result.summary} |")
            if result.reasoning:
                lines.append(f"| 推理过程 | {result.reasoning} |")
            lines.append("")

            if result.detected and result.instances:
                lines.append("#### 检测实例")
                lines.append("")
                # Instance table header
                lines.append("| 实例 | 时间区间 | 车辆 | 道路 | 描述 |")
                lines.append("|------|----------|------|------|------|")
                for idx, inst in enumerate(result.instances, start=1):
                    time_range = "—"
                    if inst.start_time_sec or inst.end_time_sec:
                        time_range = f"{inst.start_time_sec:.1f}s - {inst.end_time_sec:.1f}s"
                    vehicle = inst.vehicle_id or "—"
                    road = str(inst.road_id) if inst.road_id is not None else "—"
                    desc = inst.description or "—"
                    if len(desc) > 30:
                        desc = desc[:27] + "..."
                    lines.append(
                        f"| {idx} | {time_range} | {vehicle} | {road} | {desc} |"
                    )
                lines.append("")

                # Detailed instance info as bullet points below the table
                for idx, inst in enumerate(result.instances, start=1):
                    has_detail = (
                        inst.reasoning
                        or inst.disposal_suggestion
                        or inst.evidence_frames
                    )
                    if not has_detail:
                        continue
                    lines.append(f"**实例 {idx} 详情**")
                    if inst.evidence_frames:
                        frames_str = ", ".join(str(f) for f in inst.evidence_frames)
                        lines.append(f"- **证据帧**: {frames_str}")
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

            # 展示裁决层对该事件的推理
            if result.adjudication_reasoning:
                lines.append("#### 裁决推理")
                lines.append(result.adjudication_reasoning)
                lines.append("")

            # 展示专家原始分析（进入裁决层之前的决策）
            if result.expert_raw_description:
                cleaned = self._clean_expert_description(result.expert_raw_description)
                if cleaned:
                    lines.append("#### 专家原始分析")
                    lines.append(cleaned)
                    lines.append("")

            # 展示CV辅助检测证据（如有）
            if result.cv_evidence:
                lines.append("#### CV辅助检测证据")
                lines.append(result.cv_evidence)
                lines.append("")

            return lines
        except Exception as exc:
            logger.error(
                "[report_generator:_render_event_result] RENDER_EVENT_ERROR | event_id=%d | %s",
                result.event_id,
                exc,
                exc_info=True,
            )
            return [f"[ERROR: 无法渲染事件 {result.event_id} 详情]"]
