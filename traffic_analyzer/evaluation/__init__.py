"""Evaluation utilities shared by scripts/batch_evaluate.py and the web dashboard.

[文件说明]
作用:提供 extract_gt_from_filename,从视频文件名前缀提取 ground-truth 事件 ID
集合;事件名(event_names)从 event_categories.yaml 派生(单一真相源,不在本
文件维护字面量清单)。scripts/batch_evaluate.py(批量评估脚本)与
web/dashboard/metrics.py(看板指标)共用,避免 importlib 按路径加载脚本的耦合。
上游:scripts/batch_evaluate.py、web/dashboard/metrics.py。
下游:traffic_analyzer/web/event_config.py(event_id → 事件中文名索引)。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional, Set

from traffic_analyzer.web.event_config import event_id_names

# ADR-0001:位 9 = 正常指示位,不对应任何事件类别,YAML 不定义它;文件名 GT
# 提取需把 09 前缀识别为「正常」,占位名在此唯一声明。
NORMAL_EVENT_ID = 9
NORMAL_EVENT_NAME = "无异常事件"


def event_names(path: Optional[Path] = None) -> Dict[int, str]:
    """event_id → 事件中文名:event_categories.yaml 派生 + 9 = 正常占位。

    复用 web/event_config 的 (路径, mtime) 缓存,运行中编辑 yaml 后下一次
    读取自动失效;显式传 path 供测试注入 fixture。
    """
    return {**event_id_names(path), NORMAL_EVENT_ID: NORMAL_EVENT_NAME}


def extract_gt_from_filename(
    filename: str, names: Optional[Dict[int, str]] = None
) -> Set[int]:
    """Extract ground-truth event IDs from a video filename.

    Default pattern: numbers before ``_Event_`` are action IDs from the
    annotation document (v4.5), which are used directly as global event_ids.
    Examples:
        ``01-02-07-11_Event_65536_...``  -> {1, 2, 7, 11}
        ``02-04-07-08-10_Event_...``     -> {2, 4, 7, 8, 10}
        ``06_Event_...``                  -> {6}

    Action ID ``9`` (Normal) is included as 'normal'; any other number not
    a known event category is silently skipped.

    Supports two filename patterns:
        ``01-02-08_Event_xxx_...``  -> standard format
        ``01-02-08_20260514-...``    -> date-stamp format (no _Event_)

    ``names`` 可注入事件名索引(测试用 fixture yaml);缺省从
    event_categories.yaml 派生。
    """
    # Try standard pattern first: prefix before _Event_
    match = re.match(r"^([\d\-]+)_Event_", filename)
    if match:
        prefix = match.group(1)
    else:
        # Fallback: leading digit-dash prefix before any _ that is NOT _Event_
        # Handles date-stamp filenames like 01-02-08_20260514-173730_前半段.mp4
        match = re.match(r"^([\d\-]+)_(?!Event_)", filename)
        if not match:
            return set()
        prefix = match.group(1)
    if names is None:
        names = event_names()
    event_ids: Set[int] = set()
    for part in prefix.split("-"):
        part = part.strip()
        if not part.isdigit():
            continue
        num = int(part)
        if num in names:
            event_ids.add(num)
    return event_ids
