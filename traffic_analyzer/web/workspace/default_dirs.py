"""Default workspace dirs endpoint: ``GET /api/workspace/default-dirs``.

[文件说明]
作用:把 TRAFFIC_ANALYZER_WORKSPACE_DIRS 白名单(core.allowed_workspace_dirs
解析后的绝对路径)暴露给前端「选择工作区」弹窗的「默认工作区」快速跳转下拉;
未配置白名单时返回空数组(前端回退 localStorage 最近使用列表)。只返回解析后
的目录列表,不泄露 config/.env 的其他内容。
上游:web/app.py(挂载路由);frontend DirPickerModal.vue。
下游:core.allowed_workspace_dirs(os.environ / config/.env 解析,支持 ~ 与相对路径)。
"""

from __future__ import annotations

from typing import Dict, List

from fastapi import APIRouter

from traffic_analyzer.web.workspace.core import allowed_workspace_dirs

router = APIRouter()


@router.get("/api/workspace/default-dirs")
def get_default_dirs() -> Dict[str, List[str]]:
    """Resolved allowlist dirs for the picker's quick-jump dropdown ([] when unset)."""
    return {"dirs": [str(p) for p in allowed_workspace_dirs()]}
