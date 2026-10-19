"""analysis 产物布局与推理进度协议的代码契约(单一真相源)。

[文件说明]
作用:analysis/<stem>/ 目录内产物路径的构造函数与 JSONL 进度事件协议
(事件类型集合 + Pydantic 模型)。此前布局只存在于各调用点的 f-string 与
注释里,漂移只能人肉对齐;收敛于此,web 层(evidence / keyframes /
dashboard / jobs)与 core(sft_label_rewrite 隔离目录)统一引用。
约定:路径函数只做拼接不做 IO;存在性/回退语义归调用方(如
web/evidence.sft_json_path 的顶层优先、quarantine 回退)。
上游:web/workspace(analysis_dir)、web/evidence、web/keyframes、
web/dashboard/metrics、web/jobs、core/sft_label_rewrite、
utils/progress.py(事件类型集合)。
下游:文件系统布局本身;utils/progress.py(发射端)与 web/progress.py
(消费端)共享的 JSONL 协议。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, TypeAdapter

# ---------------------------------------------------------------------------
# analysis/<stem>/ 目录布局
# ---------------------------------------------------------------------------

# grounding 核验不过的阳性样本隔离目录(相对 analysis/<stem>/;写入见
# core/sft_label_rewrite,读取回退见 web/evidence.sft_json_path)。
QUARANTINE_DIR_NAME = "quarantine"

# chunk 级关键帧目录(文件名 NN=两位序号承载顺序、sec 一位小数,
# 命名契约与维护逻辑见 web/keyframes.py)。
KEYFRAME_DIR_NAME = "关键帧"


def sft_json(out_dir: Path, stem: str) -> Path:
    """SFT 样本 JSON(顶层正式版;人工编辑保存即转正到此)。"""
    return out_dir / f"{stem}.json"


def sft_quarantine_json(out_dir: Path, stem: str) -> Path:
    """SFT 样本 JSON 的隔离副本(grounding 核验不过的阳性样本)。"""
    return out_dir / QUARANTINE_DIR_NAME / f"{stem}.json"


def stem_raw_json(out_dir: Path, stem: str) -> Path:
    """模型原始推理快照(<stem>_raw.json;人工修正 action 后仍保持推理标记)。"""
    return out_dir / f"{stem}_raw.json"


def evidence_json(out_dir: Path, stem: str) -> Path:
    """证据标注 JSON(<stem>_evidence.json,归一化多边形/矩形)。"""
    return out_dir / f"{stem}_evidence.json"


def report_md(out_dir: Path) -> Path:
    """Markdown 分析报告。"""
    return out_dir / "report.md"


def keyframe_dir(out_dir: Path) -> Path:
    """chunk 级关键帧目录。"""
    return out_dir / KEYFRAME_DIR_NAME


# ---------------------------------------------------------------------------
# JSONL 进度事件协议(发射端 utils/progress.py,消费端 web/progress.py)
# ---------------------------------------------------------------------------

# 所有合法的 JSONL 事件类型(发射端与消费端共享;模型见下方各 Event 类)。
PROGRESS_EVENT_TYPES: frozenset[str] = frozenset(
    {"register", "start", "phase", "lane_done", "step", "done"}
)

# 泳道(专家/裁决)事件子集 —— web/progress.py 用它路由到 _apply_expert_progress。
LANE_EVENT_TYPES: frozenset[str] = frozenset(
    {"register", "start", "phase", "lane_done"}
)

# 单个事件 dict 的类型别名(发射端 _emit_event 的载荷 / 消费端 apply_event
# 的入参;宽松 dict 流转,形状契约由下方 Pydantic 模型守护)。
ProgressEvent = Dict[str, Any]

# 泳道收尾结果token(lane_done.result)。
LaneResult = Literal["detected", "undetected", "done", "error"]


class _ProgressEventBase(BaseModel):
    """公共字段:发射端 _emit_event 统一补的时间戳。"""

    ts: float


class RegisterEvent(_ProgressEventBase):
    """注册全部泳道(专家类别名 + 末位「裁决」)。"""

    type: Literal["register"]
    total: int
    lanes: List[str]


class StartEvent(_ProgressEventBase):
    type: Literal["start"]
    lane: str


class PhaseEvent(_ProgressEventBase):
    type: Literal["phase"]
    lane: str
    fraction: float
    label: str


class LaneDoneEvent(_ProgressEventBase):
    type: Literal["lane_done"]
    done: int
    total: int
    lane: str
    result: LaneResult


class StepEvent(_ProgressEventBase):
    """orchestrator 的 [x/4] 粗粒度步骤(step 可为 3.5 等半步)。"""

    type: Literal["step"]
    step: float
    total: int
    name: str


class RunDoneEvent(_ProgressEventBase):
    """整次运行终态(仅作心跳;崩溃时文件可能截断,终态由 worker 判定)。"""

    type: Literal["done"]
    status: str


# 按 type 判别的标签联合;校验入口 progress_event_adapter(按 type 路由到
# 具体事件类,typing 联合本身无 model_validate,须经 TypeAdapter)。
ProgressEventModel = Annotated[
    Union[RegisterEvent, StartEvent, PhaseEvent, LaneDoneEvent, StepEvent, RunDoneEvent],
    Field(discriminator="type"),
]
progress_event_adapter = TypeAdapter(ProgressEventModel)


def parse_progress_line(line: str) -> Optional[ProgressEvent]:
    """解析一行 JSONL 进度事件;非 JSON / 非对象 / 未知类型返回 None。

    与 jobs/progress_feed 尾随循环的既有容忍语义一致(截断行、垃圾行跳过,
    不抛错);返回宽松 dict,形状由 ProgressEventModel 守护(测试 round-trip)。
    """
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("type") not in PROGRESS_EVENT_TYPES:
        return None
    return event
