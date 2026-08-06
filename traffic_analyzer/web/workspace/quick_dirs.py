"""Quick workspace dirs endpoint: ``GET /api/workspace/quick-dirs``.

[文件说明]
作用:把 TRAFFIC_ANALYZER_WORKSPACE_DIRS 白名单(core.allowed_workspace_dirs
解析后的绝对路径)连同各根的一层子目录名暴露给前端「选择工作区」弹窗的快速
跳转下拉;未配置白名单时返回空 roots(前端回退 localStorage 最近使用列表)。
只返回白名单根及其一层子目录名,不读取 config/.env 本身,不泄露其中其他内容。
上游:web/app.py(挂载路由);frontend DirPickerModal.vue。
下游:core.allowed_workspace_dirs(os.environ / config/.env 解析,支持 ~ 与相对路径);
仅 os.scandir 一层目录项,不对文件 stat,保证毫秒级返回。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter

from traffic_analyzer.web.workspace.core import allowed_workspace_dirs

router = APIRouter()


def _scan_root(root: Path) -> Optional[Dict[str, object]]:
    """List one level of subdirectory names under ``root`` (``None`` on failure).

    Dir-keeping and ordering match ``/api/fs/list`` (web/fs.py): symlinks are
    followed (``DirEntry.is_dir()`` default), dot-prefixed entries are skipped,
    unreadable entries are skipped silently, names are sorted case-insensitively.
    Any error for this root (missing, permission denied, dangling mount) yields
    ``None`` so the caller can skip just this root.
    """
    try:
        with os.scandir(root) as it:
            subs = sorted(
                (
                    entry.name
                    for entry in it
                    if not entry.name.startswith(".") and entry.is_dir()
                ),
                key=str.lower,
            )
    except OSError:
        return None
    return {"path": str(root), "subs": subs}


@router.get("/api/workspace/quick-dirs")
def get_quick_dirs() -> Dict[str, List[Dict[str, object]]]:
    """Allowlist roots with their first-level subdir names ([] when unset)."""
    roots = allowed_workspace_dirs()
    if not roots:
        return {"roots": []}
    with ThreadPoolExecutor(max_workers=len(roots)) as pool:
        results = list(pool.map(_scan_root, roots))
    return {"roots": [r for r in results if r is not None]}
