"""Cached indexes over the event config YAMLs (categories / options).

[文件说明]
作用:event_categories.yaml(事件中文名 → event_id)与 event_options.yaml
(event_id → 结构化属性组,封闭枚举)的读取与索引。按 (路径, mtime) 用 lru_cache
缓存:运行中编辑 yaml 后下一次读取自动失效,无需重启。路径默认为 config/ 下的
真实文件,也可显式传入(evidence_api 的包装函数借此让测试 monkeypatch 其模块级
路径常量后仍生效)。
上游:web/evidence_api.py(/api/config/events 端点与 SFT 校验包装)、
web/evidence_schema.py(attr_mentions 的 think 段落定位)。
下游:traffic_analyzer/config/event_categories.yaml、event_options.yaml(只读)。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Repository root (traffic_analyzer/web/event_config.py -> parents[2]).
_EVENT_CATEGORIES_YAML = (
    Path(__file__).resolve().parents[2]
    / "traffic_analyzer"
    / "config"
    / "event_categories.yaml"
)
_EVENT_OPTIONS_YAML = (
    Path(__file__).resolve().parents[2]
    / "traffic_analyzer"
    / "config"
    / "event_options.yaml"
)


def _yaml_mtime_ns(path: Path) -> int:
    """文件 mtime(纳秒);缺失时返回 -1(后续 read_text 仍按原样抛错)。"""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


@lru_cache(maxsize=8)
def _event_options_index_cached(
    path: str, mtime_ns: int
) -> Dict[int, List[Dict[str, Any]]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    index: Dict[int, List[Dict[str, Any]]] = {}
    for ev in data.get("event_options") or []:
        groups = [
            {
                "key": str(g["key"]),
                "label": str(g.get("label") or g["key"]),
                "options": [str(o) for o in g.get("options") or []],
                "required": bool(g.get("required", False)),
                "multi": bool(g.get("multi", False)),
            }
            for g in ev.get("groups") or []
            if "key" in g
        ]
        if "event_id" in ev:
            index[int(ev["event_id"])] = groups
    return index


def event_options_index(
    path: Optional[Path] = None,
) -> Dict[int, List[Dict[str, Any]]]:
    """event_options.yaml 的封闭枚举定义:{event_id: [属性组, ...]}(保持声明顺序)。

    按 (路径, mtime) 缓存:运行中编辑 yaml 后下一次读取自动失效,无需重启。
    """
    path = path or _EVENT_OPTIONS_YAML
    return _event_options_index_cached(str(path), _yaml_mtime_ns(path))


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
