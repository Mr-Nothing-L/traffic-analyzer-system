"""Markdown rendering for traffic analysis reports.

This module contains the presentation layer that turns a populated
:class:`Report` model into a human-readable Markdown document.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from traffic_analyzer.core.report_far_enhancement_renderer import (
    _render_far_enhancement,
)
from traffic_analyzer.core.report_text_utils import _clean_expert_description
from traffic_analyzer.models.schemas import EventResult, Report

logger = logging.getLogger(__name__)


def _render_markdown(report: Report) -> str:
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

    # ---- Scene Summary (kept for internal use, not rendered) ----------
    sc = report.scene_summary

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
            lines.extend(_render_event_result(result, report.expert_candidates))

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


def _render_event_result(result: EventResult, expert_candidates: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """Render a single :class:`EventResult` as Markdown lines."""
    try:
        lines: List[str] = []
        status_icon = "✅" if result.detected else "❌"
        name_line = f"### {status_icon} 事件 {result.event_id}: {result.event_name}"
        if result.event_name_en:
            name_line += f" / {result.event_name_en}"
        lines.append(name_line)
        lines.append("")

        # 展示专家原始输出（裁决前）
        candidate = None
        if expert_candidates:
            candidate = next((c for c in expert_candidates if c.get("event_id") == result.event_id), None)
            if candidate:
                lines.append("#### 专家原始输出（裁决前）")
                lines.append(f"- **检测到**: {'是' if candidate.get('detected') else '否'}")
                if candidate.get('summary'):
                    lines.append(f"- **摘要**: {candidate['summary']}")
                if candidate.get('reasoning'):
                    lines.append(f"- **推理**: {candidate['reasoning']}")
                lines.append("")

        # Main result info as a compact table
        lines.append("#### 裁决后结果")
        lines.append("| 字段 | 内容 |")
        lines.append("|------|------|")
        lines.append(f"| 是否检测到 | {'**是**' if result.detected else '否'} |")
        if result.summary:
            lines.append(f"| 摘要 | {result.summary} |")
        if result.reasoning:
            lines.append(f"| 推理过程 | {result.reasoning} |")
        # 展示工具调用结果（如果有）
        if result.tool_results:
            for tr in result.tool_results:
                tool_name = tr.get("tool_name", "unknown")
                lines.append(f"| 工具调用 | {tool_name} |")
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
            cleaned = _clean_expert_description(result.expert_raw_description)
            if cleaned:
                lines.append("#### 专家原始分析")
                lines.append(cleaned)
                lines.append("")

        # 展示CV辅助检测证据（如有）
        if result.cv_evidence:
            lines.append("#### CV辅助检测证据")
            lines.append(result.cv_evidence)
            lines.append("")

        # 展示远距离目标增强合成图（如有）
        raw_vlm_response = (
            candidate.get("raw_vlm_response", {}) if candidate else {}
        )
        has_far_evidence = bool(
            raw_vlm_response.get("composite_image_path")
            or raw_vlm_response.get("motion_composite_image_path")
            or raw_vlm_response.get("gallery_image_path")
            or raw_vlm_response.get("far_enhancement")
        )
        if has_far_evidence:
            lines.extend(
                _render_far_enhancement(candidate, result.event_id)
            )

        return lines
    except Exception as exc:
        logger.error(
            "[report_markdown_renderer:_render_event_result] RENDER_EVENT_ERROR | event_id=%d | %s",
            result.event_id,
            exc,
            exc_info=True,
        )
        return [f"[ERROR: 无法渲染事件 {result.event_id} 详情]"]
