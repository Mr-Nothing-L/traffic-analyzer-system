"""Swimlane progress state machine for analyzer progress events.

The analyzer child writes structured progress events as JSONL to the file
named by ``TRAFFIC_ANALYZER_PROGRESS_FILE`` (see utils/progress.py); the
worker tails that file and feeds each parsed event dict to
:func:`apply_event`, which updates ``job.experts`` (expert lanes plus the
SFT/report stage lanes) and the monotonic overall ``job.fraction``.

[文件说明]
作用:泳道进度状态机(自 jobs 抽出,纯函数、操作 duck-typed Job,不反向依赖)。
消费子进程进度文件的 JSONL 事件(register/start/phase/lane_done 泳道事件与
step 粗粒度步骤事件),维护 Job.experts 泳道列表(类别专家 + 裁决 + SFT 标注/
报告 阶段泳道)与整体 fraction(全部泳道均值,单调不降;无泳道时退化为
step_index/5)。非法/截断行由调用方(jobs 尾随循环)丢弃,事件字段缺失或
非法时整条事件忽略。终态不由事件流判定:子进程崩溃时文件可能截断/无
done 事件,web 侧按 returncode 定终态。
上游:web/jobs/queue.py 的进度文件尾随循环按事件调用(持 JobManager 锁)。
下游:无包内依赖。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from traffic_analyzer.utils.progress import LANE_EVENT_TYPES, ProgressEvent

TOTAL_STEPS = 5

# step 事件的 step 值([x/4] 中的 x)-> (step_index, step_label),与旧 stdout
# 文本标记的映射一致;"[3.5/4]" 落在步骤 4(SFT)。
_STEP_EVENTS = {
    1: (1, "预处理"),
    2: (2, "专家"),
    3: (3, "裁决"),
    3.5: (4, "SFT"),
    4: (5, "报告"),
}

# 专家/裁决之后的流水线阶段泳道(SFT 标注、报告):由 step 3.5/4 事件驱动,
# 让泳道覆盖整个任务周期(此前裁决完成后泳道全满,但任务仍在 SFT/报告阶段)
_SFT_LANE = "SFT 标注"
_REPORT_LANE = "报告"


def _new_lane(name: str) -> Dict[str, Any]:
    return {
        "name": name, "status": "queued", "detected": None,
        "fraction": 0.0, "label": "",
    }


def _expert_lane(job: Any, name: str) -> Optional[Dict[str, Any]]:
    for lane in job.experts:
        if lane["name"] == name:
            return lane
    return None


def apply_event(job: Any, event: ProgressEvent) -> bool:
    """Consume one structured progress event dict; True when state changed.

    Contract (emitted by the analyzer child via utils/progress.py)::

        {"type": "register", "total": N, "lanes": [name, ...]}  (last is 裁决)
        {"type": "start", "lane": name}
        {"type": "phase", "lane": name, "fraction": 0..1, "label": str}
        {"type": "lane_done", "done": k, "total": N, "lane": name,
         "result": "detected"|"undetected"|"error"|"done"}
        {"type": "step", "step": 1|2|3|3.5|4, "total": 4, "name": str}
        {"type": "done", "status": "ok"}   (整次运行终态;仅作心跳,不驱动状态)

    Malformed events are ignored. Caller must hold the job lock.
    """
    if not isinstance(event, dict):
        return False
    etype = event.get("type")
    if etype == "step":
        return _apply_step(job, event)
    if etype in LANE_EVENT_TYPES:
        _apply_expert_progress(job, event)
        return True
    return False  # "done"(运行终态)/未知类型:不驱动状态机


def _apply_step(job: Any, event: ProgressEvent) -> bool:
    mapping = _STEP_EVENTS.get(event.get("step"))
    if mapping is None:
        return False
    step_index, step_label = mapping
    job.step_index = step_index
    job.step_label = step_label
    _advance_stage_lanes(job, step_index)
    _recompute_fraction(job)
    return True


def _apply_expert_progress(job: Any, event: ProgressEvent) -> None:
    """Update ``job.experts`` from one lane event. Caller must hold the job lock."""
    kind = event["type"]
    if kind == "register":
        lanes = event.get("lanes")
        if not isinstance(lanes, list):
            return
        # 重复 register(如重发事件)合并而非重置:保留已有 lane 的进度与
        # 阶段泳道,只补充缺失的 lane。
        for name in lanes:
            if isinstance(name, str) and name and _expert_lane(job, name) is None:
                job.experts.append(_new_lane(name))
        # 阶段泳道从一开始就占位(排队态),面板即刻展示完整流水线
        for stage in (_SFT_LANE, _REPORT_LANE):
            if _expert_lane(job, stage) is None:
                job.experts.append(_new_lane(stage))
    elif kind == "start":
        lane = _expert_lane(job, str(event.get("lane")))
        if lane is not None:
            lane["status"] = "running"
    elif kind == "phase":
        lane = _expert_lane(job, str(event.get("lane")))
        if lane is not None:
            try:
                fraction = float(event.get("fraction"))
            except (TypeError, ValueError):
                fraction = math.nan
            # 只接受有限的 0..1 进度;非法值(含 nan/inf)整事件忽略
            if math.isfinite(fraction) and 0.0 <= fraction <= 1.0:
                lane["fraction"] = fraction
                lane["label"] = str(event.get("label", ""))
    elif kind == "lane_done":
        lane = _expert_lane(job, str(event.get("lane")))
        if lane is not None:
            result = event.get("result")
            if result == "error":
                lane["status"] = "error"
            else:
                lane["status"] = "done"
                # "done" result(裁决收尾)→ detected 保持 None(无检出语义)
                lane["detected"] = (
                    True if result == "detected"
                    else False if result == "undetected"
                    else None
                )
            lane["fraction"] = 1.0
    _recompute_fraction(job)


def _stage_lane(job: Any, name: str) -> Dict[str, Any]:
    lane = _expert_lane(job, name)
    if lane is None:
        lane = _new_lane(name)
        job.experts.append(lane)
    return lane


def _advance_stage_lanes(job: Any, step_index: int) -> None:
    """Sync the SFT/report lanes from the coarse step events. Caller holds lock."""
    if not job.experts:
        return
    if step_index >= 4:
        sft = _stage_lane(job, _SFT_LANE)
        if sft["status"] not in ("done",):
            sft["status"] = "running"
            sft["fraction"] = 0.5
            sft["label"] = "SFT 标签改写"
    if step_index >= 5:
        sft = _stage_lane(job, _SFT_LANE)
        sft.update(status="done", fraction=1.0, label="SFT 完成")
        rep = _stage_lane(job, _REPORT_LANE)
        if rep["status"] not in ("done",):
            rep["status"] = "running"
            rep["fraction"] = 0.5
            rep["label"] = "生成报告"


def _finish_stage_lanes(job: Any) -> None:
    """rc==0: mark every stage lane done. Caller holds lock."""
    for name in (_SFT_LANE, _REPORT_LANE):
        lane = _expert_lane(job, name)
        if lane is not None:
            lane.update(status="done", fraction=1.0)
    rep = _expert_lane(job, _REPORT_LANE)
    if rep is not None:
        rep["label"] = "报告完成"


def _recompute_fraction(job: Any) -> None:
    """Overall fraction = mean of ALL lane fractions (类别 + 裁决 + SFT/报告
    阶段泳道),与「专家工作间」面板同刻度:侧栏迷你条满格 ⟺ 全部泳道完成。

    Monotonic guard: register 初期均值远低于 step 阶段估算值,因此
    fraction 只升不降。No lanes (legacy children): step_index / 5.
    Caller must hold the job lock.
    """
    if job.experts:
        mean = sum(e["fraction"] for e in job.experts) / len(job.experts)
        base = job.fraction if job.fraction is not None else 0.0
        job.fraction = max(base, mean)
        return
    job.fraction = job.step_index / TOTAL_STEPS
