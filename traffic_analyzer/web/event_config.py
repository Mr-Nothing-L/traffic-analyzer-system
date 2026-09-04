"""Cached indexes over the event config YAMLs (categories / options).

[文件说明]
作用:event_categories.yaml(事件中文名 → event_id)的读取与索引,以及
event_options.yaml 索引的薄封装(解析实现已下沉至
traffic_analyzer/config/event_options.py,本模块保留原有公开函数签名)。
按 (路径, mtime) 用 lru_cache 缓存:运行中编辑 yaml 后下一次读取自动失效,
无需重启。路径默认为 config/ 下的真实文件,也可显式传入(evidence_api 的
包装函数借此让测试 monkeypatch 其模块级路径常量后仍生效)。
上游:web/evidence_api.py(/api/config/events 端点与 SFT 校验包装)、
web/evidence_schema.py(attr_mentions 的 think 段落定位)。
下游:traffic_analyzer/config/event_categories.yaml(只读)、
traffic_analyzer/config/event_options.py(options 索引实现)。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from traffic_analyzer.config import event_options as _event_options

# Repository root (traffic_analyzer/web/event_config.py -> parents[2]).
_EVENT_CATEGORIES_YAML = (
    Path(__file__).resolve().parents[2]
    / "traffic_analyzer"
    / "config"
    / "event_categories.yaml"
)
_EVENT_OPTIONS_YAML = _event_options.EVENT_OPTIONS_YAML


def _yaml_mtime_ns(path: Path) -> int:
    """文件 mtime(纳秒);缺失时返回 -1(后续 read_text 仍按原样抛错)。"""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def event_options_index(
    path: Optional[Path] = None,
) -> Dict[int, List[Dict[str, Any]]]:
    """event_options.yaml 的封闭枚举索引;薄封装,实现见 config/event_options.py。"""
    return _event_options.event_options_index(path or _EVENT_OPTIONS_YAML)


@lru_cache(maxsize=8)
def _event_name_index_cached(path: str, mtime_ns: int) -> Dict[str, int]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {
        str(cat["name_zh"]): int(cat["event_id"])
        for cat in data.get("event_categories") or []
        if "event_id" in cat and "name_zh" in cat
    }


def event_name_index(path: Optional[Path] = None) -> Dict[str, int]:
    """事件中文名 → event_id(用于在 description 的 think 段落中定位事件文本)。

    与 event_options_index 同口径:按 (路径, mtime) 缓存,yaml 变更自动失效。
    """
    path = path or _EVENT_CATEGORIES_YAML
    return _event_name_index_cached(str(path), _yaml_mtime_ns(path))
