"""Server-side directory listing for the in-page workspace picker.

``GET /api/fs/list`` backs the frontend's directory-navigator modal: it
replaces the old native OS folder dialog (zenity/tkinter) so workspace
selection also works on headless or remote servers. Only *subdirectories*
are returned — the picker never needs file names.

[文件说明]
作用:提供 GET /api/fs/list,为前端工作区选择弹窗列目录(仅子目录),替代原生系统
文件对话框,使无头/远程服务器上也能选择工作区。
上游:web/app.py(挂载路由);web/static 前端的目录选择弹窗。
下游:读取 app.state.workspace(web/workspace.py 维护的状态),仅访问文件系统。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from . import workspace as workspace_mod

router = APIRouter()


@router.get("/api/fs/list")
def list_dirs(
    request: Request,
    path: Optional[str] = Query(default=None),
    hidden: bool = Query(default=False),
) -> Dict[str, Any]:
    """List the subdirectories of ``path`` (absolute; ``~`` is expanded).

    Without ``path`` the current workspace is listed, or the user's home
    directory when no workspace is selected yet (the first allowed dir when
    ``TRAFFIC_ANALYZER_WORKSPACE_DIRS`` is set). Symlinks are resolved and
    the returned ``path`` is the normalized absolute path; entries that
    cannot be stat'ed (e.g. permission denied) are skipped silently.

    When the workspace allowlist is non-empty, only the allowed directories
    and their subpaths may be listed — anything else gets 403.
    """
    allowed = workspace_mod.allowed_workspace_dirs()
    if not path:
        workspace = request.app.state.workspace.get()
        if workspace is not None:
            base = workspace
        elif allowed:
            base = allowed[0]
        else:
            base = Path.home()
    else:
        base = Path(path).expanduser()
        if not base.is_absolute():
            raise HTTPException(status_code=400, detail=f"Not an absolute path: {path}")
    if not base.is_dir():
        raise HTTPException(status_code=404, detail=f"Not a directory: {base}")
    base = base.resolve()
    if allowed and not workspace_mod._within_allowed(base, allowed):
        raise HTTPException(status_code=403, detail="path not in allowed workspace list")

    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        raise HTTPException(status_code=404, detail=f"Cannot list directory: {base}") from exc

    dirs: List[Dict[str, str]] = []
    for entry in entries:
        if not hidden and entry.name.startswith("."):
            continue
        try:
            if not entry.is_dir():  # follows symlinks
                continue
        except OSError:
            continue  # unreadable entry (permission denied, dangling mount, ...)
        dirs.append({"name": entry.name, "path": str(entry)})

    parent = base.parent
    return {
        "path": str(base),
        "parent": str(parent) if parent != base else None,
        "dirs": dirs,
    }
