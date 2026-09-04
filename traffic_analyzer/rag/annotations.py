"""标注 / 审核态加载、文件名时间戳解析、site 归一化。

[文件说明]
作用:读取 <workspace>/analysis/<stem>/<stem>.json 生成 Label(description 取
<think>…</think> 与 <answer>…</answer> 两标签内正文拼接,去标签本身);
读取 analysis/review_states.json(缺文件 = 空 dict,缺条目由调用方按 unconfirmed 处理);
从文件名解析 13 位 epoch 毫秒时间戳;make_site 拼接站点标识。
上游:scripts/build_rag_index.py、scripts/rag_search.py。
下游:本地 JSON 文件。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_TAG_RE = re.compile(r"<(think|answer)>(.*?)</\1>", re.DOTALL)
_TS_RE = re.compile(r"(\d{13})")
_NON_HUMAN = {None, "", "auto", "vlm"}


@dataclass
class Label:
    events: list[int]
    human_edited: bool
    ann_edited_at: str | None
    text: str
    duration_s: float | None


def load_label(workspace, stem: str) -> Label | None:
    """加载单条视频的人工标注;无文件或 JSON 损坏返回 None。"""
    path = Path(workspace) / "analysis" / stem / f"{stem}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    desc = data.get("description") or ""
    parts = [m.group(2).strip() for m in _TAG_RE.finditer(desc)]
    text = "\n".join(p for p in parts if p)
    events = [int(e) for e in (data.get("action") or [])]
    edited_by = data.get("last_edited_by")
    start, end = data.get("start_timestamp"), data.get("end_timestamp")
    duration_s = None
    if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end >= start:
        duration_s = float(end - start)
    return Label(
        events=events,
        human_edited=edited_by not in _NON_HUMAN,
        ann_edited_at=data.get("last_edited_at"),
        text=text,
        duration_s=duration_s,
    )


def load_review_states(workspace) -> dict:
    """读取 analysis/review_states.json;缺文件 / 损坏返回 {}。"""
    path = Path(workspace) / "analysis" / "review_states.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_filename_ts(stem: str) -> float | None:
    """从文件名解析 13 位 epoch 毫秒 → epoch 秒;无则 None。"""
    m = _TS_RE.search(stem)
    return int(m.group(1)) / 1000.0 if m else None


def make_site(road, stake, direction, camera) -> str | None:
    """拼接站点标识,如「北京-G3京台高速-道路 K18+470-进京-3」;全空返回 None。"""
    parts = [str(p) for p in (road, stake, direction, camera) if p]
    return "-".join(parts) if parts else None
