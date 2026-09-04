"""Evaluation utilities shared by scripts/batch_evaluate.py and the web dashboard.

[文件说明]
作用:提供 extract_gt_from_filename,从视频文件名前缀提取 ground-truth 事件 ID
集合;并从 event_categories.yaml(SSOT)懒加载派生 EVENT_NAMES 映射。
scripts/batch_evaluate.py(批量评估脚本)与 web/dashboard/metrics.py
(看板指标)共用此模块,避免 importlib 按路径加载脚本的耦合。
上游:scripts/batch_evaluate.py、web/dashboard/metrics.py。
下游:core/config_manager.py(函数内延迟 import,避免 import 时读 YAML 与循环依赖)。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Set

_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@lru_cache(maxsize=1)
def get_event_names() -> Dict[int, str]:
    """Derive event_id -> Chinese name mapping from event_categories.yaml.

    The YAML config (via ``ConfigManager``) is the single source of truth.
    Loaded lazily on first access and cached, so importing this module stays
    cheap and free of circular imports.

    event_id 9 是正常占位(二进制编码恒为 0),YAML 中无对应条目,
    按标注文档 v4.5 在此补上。
    """
    from traffic_analyzer.core.config_manager import ConfigManager

    manager = ConfigManager(str(_DEFAULT_CONFIG_DIR))
    manager.load_all()
    names = {cat.event_id: cat.name_zh for cat in manager.get_event_categories()}
    names.setdefault(9, "无异常事件")
    return dict(sorted(names.items()))


def __getattr__(name: str) -> Dict[int, str]:
    """Lazily expose ``EVENT_NAMES`` (PEP 562) without import-time YAML reads."""
    if name == "EVENT_NAMES":
        return get_event_names()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def extract_gt_from_filename(filename: str) -> Set[int]:
    """Extract ground-truth event IDs from a video filename.

    Default pattern: numbers before ``_Event_`` are action IDs from the
    annotation document (v4.5), which are used directly as global event_ids.
    Examples:
        ``01-02-07-11_Event_65536_...``  -> {1, 2, 7, 11}
        ``02-04-07-08-10_Event_...``     -> {2, 4, 7, 8, 10}
        ``06_Event_...``                  -> {6}

    Action ID ``9`` (Normal) is included as 'normal'; any other number not
    in ``get_event_names()`` is silently skipped.

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
        if num in get_event_names():
            event_ids.add(num)
    return event_ids
