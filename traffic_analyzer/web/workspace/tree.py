"""Single-level workspace directory tree and the /api/workspace/tree route.

[文件说明]
作用:list_tree 返回工作区单层目录(目录在前,跳过点文件;视频条目带 stem
与 has_results,结果判定遵循扁平 workspace/analysis/<stem>/ 契约;单层
列举不做 PRUNED_DIR_NAMES 剪枝,用户可见产出目录),以及
GET /api/workspace/tree 路由。
上游:web/workspace/core.py(resolve_tree_dir、has_results、
require_workspace、VIDEO_EXTENSIONS、router);web/workspace/__init__.py(聚合导出)。
下游:仅读工作区文件系统。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from fastapi import HTTPException, Request

from traffic_analyzer.web.workspace.core import (
    VIDEO_EXTENSIONS,
    has_results,
    require_workspace,
    resolve_tree_dir,
    router,
)


def list_tree(workspace: Path, rel: str) -> Dict[str, Any]:
    """One directory level of the workspace (dirs first, dotfiles skipped).

    Video entries carry ``stem`` and ``has_results`` at any depth; results
    follow the flat ``workspace/analysis/<stem>/`` contract.
    """
    target = resolve_tree_dir(workspace, rel)
    entries: List[Dict[str, Any]] = []
    try:
        children = list(target.iterdir())
    except OSError:
        raise HTTPException(status_code=404, detail="Unknown tree path")
    for path in children:
        if path.name.startswith("."):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        child_rel = f"{rel}/{path.name}" if rel else path.name
        if path.is_dir():
            entries.append({"name": path.name, "rel": child_rel, "type": "dir"})
        else:
            is_video = path.suffix.lower() in VIDEO_EXTENSIONS
            entry: Dict[str, Any] = {
                "name": path.name,
                "rel": child_rel,
                "type": "file",
                "is_video": is_video,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            }
            if is_video:
                entry["stem"] = path.stem
                entry["has_results"] = has_results(workspace, path.stem)
            entries.append(entry)
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return {"path": rel, "entries": entries}


@router.get("/api/workspace/tree")
def get_workspace_tree(request: Request, path: str = "") -> Dict[str, Any]:
    return list_tree(require_workspace(request), path)
