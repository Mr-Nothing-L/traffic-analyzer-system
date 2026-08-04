"""Video discovery and the /api/workspace/videos route.

[文件说明]
作用:工作区视频发现(list_videos,任意深度,跳过点目录与 PRUNED_DIR_NAMES
产出目录)及其 15s 进程内缓存版 list_videos_cached(key=workspace 路径,
/api/workspace/videos 与 dashboard 聚合共用;失效统一走 invalidate_caches:
workspace 变更 / infer 完成 / review / SFT / 证据 PUT),以及
GET /api/workspace/videos 路由。大工作区不自动加载:videos/tree 均为
前端显式请求时才计算。
上游:web/workspace/core.py(缓存设施、has_results、require_workspace、
VIDEO_EXTENSIONS、router);web/workspace/__init__.py(聚合导出)。
下游:web/dashboard.py(看板聚合复用 list_videos_cached)。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from fastapi import Request

from traffic_analyzer.web.workspace.core import (
    PRUNED_DIR_NAMES,
    VIDEO_EXTENSIONS,
    _TTLCache,
    _pkg_var,
    has_results,
    register_cache,
    require_workspace,
    router,
)


def list_videos(workspace: Path) -> List[Dict[str, Any]]:
    """All videos in the workspace at any depth (dot-dirs and PRUNED_DIR_NAMES skipped).

    Each entry carries the workspace-relative path (``rel``) used by the
    frontend as its unique key; ``has_results`` follows the flat
    ``analysis/<stem>/`` contract for every depth.
    """
    videos: List[Dict[str, Any]] = []
    root = workspace.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        # 点目录与产出目录(PRUNED_DIR_NAMES)原地剔除,os.walk 不再下钻。
        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") and d not in PRUNED_DIR_NAMES
        ]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            resolved = path.resolve()
            if resolved != root and root not in resolved.parents:
                continue  # symlink escaping the workspace
            try:
                stat = path.stat()
            except OSError:
                continue
            videos.append(
                {
                    "name": name,
                    "stem": path.stem,
                    "rel": path.relative_to(root).as_posix(),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "has_results": has_results(workspace, path.stem),
                }
            )
    videos.sort(key=lambda v: v["rel"])
    return videos


_videos_cache = register_cache(_TTLCache())


def list_videos_cached(workspace: Path) -> List[Dict[str, Any]]:
    """list_videos 的 15s 进程内缓存版(key=workspace 路径)。

    /api/workspace/videos 与 dashboard 聚合共用;失效统一走
    invalidate_caches(workspace 变更 / infer 完成 / review / SFT / 证据 PUT)。
    copy=False:返回值只读使用(端点直接序列化,不就地修改)。
    """
    key = str(workspace)
    cached = _videos_cache.get(key, copy=False)
    if cached is not None:
        return cached
    # 经包命名空间调用:测试 monkeypatch workspace.list_videos(spy 计数)须生效。
    videos = _pkg_var("list_videos", list_videos)(workspace)
    _videos_cache.set(key, videos)
    return videos


@router.get("/api/workspace/videos")
def get_workspace_videos(request: Request) -> List[Dict[str, Any]]:
    return list_videos_cached(require_workspace(request))
