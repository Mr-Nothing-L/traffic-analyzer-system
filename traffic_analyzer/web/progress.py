"""Swimlane progress state machine for analyzer subprocess output.

The analyzer child emits coarse ``[x/4]`` step markers and fine-grained
``EXPERT_PROGRESS|<kind>|...`` lane markers on stdout; the functions here
turn one such line into updates of ``job.experts`` (expert lanes plus the
SFT/report stage lanes) and the monotonic overall ``job.fraction``.

[文件说明]
作用:泳道进度状态机(自 jobs.py 抽出,纯函数、操作 duck-typed Job,不反向依赖)。
解析 EXPERT_PROGRESS|register/start/phase/done 标记与 [x/4] 步骤标记,维护
Job.experts 泳道列表(类别专家 + 裁决 + SFT 标注/报告 阶段泳道)与整体
fraction(全部泳道均值,单调不降;无泳道时退化为 step_index/5)。
上游:web/jobs.py 的 worker 读循环按行调用(持 JobManager 锁)。
下游:无包内依赖。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

TOTAL_STEPS = 5

# Analyzer stdout also emits EXPERT_PROGRESS|<kind>|... lanes markers.
_EXPERT_MARKER = "EXPERT_PROGRESS|"

# stdout step marker -> (step_index, step_label). "[3.5/4]" must be matched
# before "[3/4]".
_STEP_MARKERS = (
    ("[3.5/4]", 4, "SFT"),
    ("[1/4]", 1, "预处理"),
    ("[2/4]", 2, "专家"),
    ("[3/4]", 3, "裁决"),
    ("[4/4]", 5, "报告"),
)

# 专家/裁决之后的流水线阶段泳道(SFT 标注、报告):由 [3.5/4]、[4/4] 步骤标记驱动,
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


def _apply_expert_progress(job: Any, line: str) -> None:
    """Update ``job.experts`` from one ``EXPERT_PROGRESS|...`` stdout line.

    Contract (emitted by the analyzer child)::

        EXPERT_PROGRESS|register|<total>|<name1>,<name2>,…   (last is 裁决)
        EXPERT_PROGRESS|start|<name>
        EXPERT_PROGRESS|phase|<name>|<fraction 0-1>|<label>
        EXPERT_PROGRESS|done|<done>/<total>|<name>|<detected|undetected|error>

    Malformed lines are ignored. Caller must hold the job lock.
    """
    parts = line[len(_EXPERT_MARKER):].split("|")
    kind = parts[0]
    if kind == "register" and len(parts) >= 3:
        # 重复 register(如重发标记)合并而非重置:保留已有 lane 的进度与
        # 阶段泳道,只补充缺失的 lane。
        for name in parts[2].split(","):
            if name and _expert_lane(job, name) is None:
                job.experts.append(_new_lane(name))
        # 阶段泳道从一开始就占位(排队态),面板即刻展示完整流水线
        for stage in (_SFT_LANE, _REPORT_LANE):
            if _expert_lane(job, stage) is None:
                job.experts.append(_new_lane(stage))
    elif kind == "start" and len(parts) >= 2:
        lane = _expert_lane(job, parts[1])
        if lane is not None:
            lane["status"] = "running"
    elif kind == "phase" and len(parts) >= 4:
        lane = _expert_lane(job, parts[1])
        if lane is not None:
            try:
                fraction = float(parts[2])
            except ValueError:
                fraction = math.nan
            # 只接受有限的 0..1 进度;非法值(含 nan/inf)整行忽略
            if math.isfinite(fraction) and 0.0 <= fraction <= 1.0:
                lane["fraction"] = fraction
                lane["label"] = parts[3]
    elif kind == "done" and len(parts) >= 4:
        lane = _expert_lane(job, parts[2])
        if lane is not None:
            if parts[3] == "error":
                lane["status"] = "error"
            else:
                lane["status"] = "done"
                # "done" token(裁决收尾)→ detected 保持 None(无检出语义)
                lane["detected"] = (
                    True if parts[3] == "detected"
                    else False if parts[3] == "undetected"
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
    """Sync the SFT/report lanes from the coarse step markers. Caller holds lock."""
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

    Monotonic guard: register 初期均值远低于 [x/4] 阶段估算值,因此
    fraction 只升不降。No lanes (legacy children): step_index / 5.
    Caller must hold the job lock.
    """
    if job.experts:
        mean = sum(e["fraction"] for e in job.experts) / len(job.experts)
        base = job.fraction if job.fraction is not None else 0.0
        job.fraction = max(base, mean)
        return
    job.fraction = job.step_index / TOTAL_STEPS
