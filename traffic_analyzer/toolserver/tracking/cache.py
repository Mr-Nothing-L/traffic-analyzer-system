"""Disk result cache for /tools/track_suspects.

[文件说明]
作用:跟踪结果的磁盘缓存。键 = (视频解析路径, 规范化锚点集合);锚点按
    box 位置排序、坐标四舍五入到 1e-4,描述文本不参与键(同位置不同描述
    复用同一份结果)。缓存文件存 <允许根>/.agent/tracks/_cache/<key>.json,
    内容为完整响应契约 JSON。
上游:toolserver/server.py(端点读写缓存)。
下游:纯 IO(json),无其它依赖。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# 坐标参与键值的小数位(抑制浮点抖动导致的缓存未命中)
_BOX_PRECISION = 4


def cache_key(video_path: Path, anchors: Sequence[Any]) -> str:
    """计算缓存键:sha256(规范 JSON)[:32]。

    Args:
        video_path: 已解析的视频绝对路径(resolve 后)。
        anchors: SuspectAnchor 序列(或含 box 的任意对象/dict),
            取 (x1,y1,x2,y2,timestamp) 参与键;description 不进键。
    """
    norm_anchors: List[Dict[str, Any]] = []
    for a in anchors:
        box = getattr(a, "box", None)
        if box is None and isinstance(a, dict):
            box = a.get("box")
        ts = getattr(a, "timestamp", None)
        if ts is None and isinstance(a, dict):
            ts = a.get("timestamp")
        side = getattr(a, "side", None)
        if side is None and isinstance(a, dict):
            side = a.get("side")
        norm_anchors.append(
            {
                "box": [round(float(v), _BOX_PRECISION) for v in (box or [])],
                "timestamp": round(float(ts or 0.0), 3),
                "side": (side or "unknown"),
            }
        )
    # 按位置排序,锚点顺序差异不产生不同键;side 参与键(同一目标不同 side 语义不同)
    norm_anchors.sort(key=lambda item: item["box"] + [item["side"]])
    payload = {"video": str(video_path), "anchors": norm_anchors}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def load_cached(cache_dir: Path, key: str) -> Optional[Dict[str, Any]]:
    """读取缓存的响应契约;不存在/损坏时返回 None(按未命中处理)。"""
    path = cache_dir / f"{key}.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None


def store_cached(cache_dir: Path, key: str, payload: Dict[str, Any]) -> Path:
    """原子写入缓存(临时文件 + os.replace)。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(cache_dir), suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, cache_dir / f"{key}.json")
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return cache_dir / f"{key}.json"
