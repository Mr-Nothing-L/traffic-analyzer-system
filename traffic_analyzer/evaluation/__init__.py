"""Evaluation utilities shared by scripts/batch_evaluate.py and the web dashboard.

[文件说明]
作用:提供 extract_gt_from_filename,从视频文件名前缀提取 ground-truth 事件 ID
集合。scripts/batch_evaluate.py(批量评估脚本)与 web/dashboard/metrics.py
(看板指标)共用此函数,避免 importlib 按路径加载脚本的耦合。
上游:scripts/batch_evaluate.py、web/dashboard/metrics.py。
下游:无。
"""

from __future__ import annotations

import re
from typing import Set


# event_id 全局采用标注文档 v4.5 的 action 编号;9 = 正常(无事件)。
EVENT_NAMES: dict[int, str] = {
    1: "违法停车",
    2: "应急车道占用",
    3: "交通事故",
    4: "高速公路行人出现",
    5: "摩托车出现",
    6: "严重拥堵",
    7: "道路施工",
    8: "车辆逆行/倒车",
    9: "无异常事件",
    10: "抛洒物",
    11: "实线变道",
}


def extract_gt_from_filename(filename: str) -> Set[int]:
    """Extract ground-truth event IDs from a video filename.

    Default pattern: numbers before ``_Event_`` are action IDs from the
    annotation document (v4.5), which are used directly as global event_ids.
    Examples:
        ``01-02-07-11_Event_65536_...``  -> {1, 2, 7, 11}
        ``02-04-07-08-10_Event_...``     -> {2, 4, 7, 8, 10}
        ``06_Event_...``                  -> {6}

    Action ID ``9`` (Normal) is included as 'normal'; any other number not
    in ``EVENT_NAMES`` is silently skipped.

    Supports two filename patterns:
        ``01-02-08_Event_xxx_...``  -> standard format
        ``01-02-08_20260514-...``    -> date-stamp format (no _Event_)
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
    event_ids: Set[int] = set()
    for part in prefix.split("-"):
        part = part.strip()
        if not part.isdigit():
            continue
        num = int(part)
        if num in EVENT_NAMES:
            event_ids.add(num)
    return event_ids
