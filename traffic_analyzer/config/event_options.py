"""event_options.yaml 的加载与 {event_id: [属性组, ...]} 索引(唯一事实源)。

[文件说明]
作用:解析同目录的 event_options.yaml(结构化属性封闭枚举),构造
{event_id: [groups]} 索引(保持声明顺序)。按 (路径, mtime) 用 lru_cache
缓存:运行中编辑 yaml 后下一次读取自动失效,无需重启。路径默认为本模块
同目录的真实文件,也可显式传入(web/evidence 的包装函数借此让测试
monkeypatch 其模块级路径常量后仍生效)。
上游:web/event_config.py(/api/config/events 与 SFT 校验的薄封装)、
core/sft_label_rewrite.py(生成侧归一/校验,与 web 侧同口径,保证产出的
event_attributes 一定能通过 PUT 的严格枚举校验)。
下游:traffic_analyzer/config/event_options.yaml(只读)。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# yaml 与本模块同目录,路径定位不依赖仓库根布局。
EVENT_OPTIONS_YAML = Path(__file__).resolve().parent / "event_options.yaml"


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
    path = path or EVENT_OPTIONS_YAML
    return _event_options_index_cached(str(path), _yaml_mtime_ns(path))
