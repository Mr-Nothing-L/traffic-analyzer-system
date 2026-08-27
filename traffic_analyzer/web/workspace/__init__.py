"""Workspace state and video discovery for the web UI.

A *workspace* is a directory of videos the user analyzes. Inference results
live under ``<workspace>/analysis/<video_stem>/`` (see the shared contract):
``report.md``, ``<video_stem>.json`` (SFT sample), ``<video_stem>_evidence.json``
and an ``images/`` subdirectory.

[文件说明]
作用:工作区包(自原单文件 workspace.py 拆分)。core.py 为工作区状态
(WorkspaceState)、路径契约(analysis/<stem>/)、TTL 缓存设施与
GET/POST /api/workspace 路由;videos.py 为视频发现(list_videos +
长 TTL 缓存,见 videos._VIDEOS_CACHE_TTL_SEC)、/api/workspace/videos 路由
与分析报告删除(DELETE /api/workspace/analysis/{stem}、批量 POST
/api/workspace/analysis/delete);tree.py 为单层目录树与
/api/workspace/tree 路由;quick_dirs.py 为 GET /api/workspace/quick-dirs
(白名单根及一层子目录名对前端弹窗的暴露)。本模块聚合导出,保持
``from traffic_analyzer.web import workspace`` 及
``workspace.<名字>``(含 _TTLCache/_CACHE_TTL_SEC/_CONFIG_ENV_PATH 等
monkeypatch 目标,经 core._pkg_var 从包命名空间读取)的既有用法。
可选白名单:TRAFFIC_ANALYZER_WORKSPACE_DIRS(逗号分隔,支持 ~ 与相对路径,
config/.env 或环境变量)非空时,POST /api/workspace 与 fs 目录浏览仅允许
名单目录及其子路径,越界 403;未设置/为空则不限制。
上游:web/app.py(挂载路由);web/ 下 jobs、evidence_api、frames、
video_stream、dashboard、fs 均复用其 require_workspace/validate_stem/
analysis_dir 等辅助函数。
下游:仅读写工作区文件系统;VIDEO_EXTENSIONS 与 scripts/batch_evaluate.py
的视频发现保持一致。
"""

from __future__ import annotations

from traffic_analyzer.web.workspace.core import (
    DEFAULT_DEMO_WORKSPACE,
    LAST_WORKSPACE_PATH,
    PRUNED_DIR_NAMES,
    VIDEO_EXTENSIONS,
    WORKSPACE_DIRS_ENV_VAR,
    _CACHE_TTL_SEC,
    _CONFIG_ENV_PATH,
    _TTLCache,
    WorkspaceSetRequest,
    WorkspaceState,
    _config_env_value,
    _pkg_var,
    _resolve_confined,
    _within_allowed,
    allowed_workspace_dirs,
    analysis_dir,
    find_video,
    get_workspace,
    has_results,
    invalidate_caches,
    read_last_workspace,
    record_last_workspace,
    register_cache,
    require_workspace,
    resolve_tree_dir,
    resolve_workspace_file,
    resolve_workspace_video,
    router,
    set_workspace,
    validate_stem,
)
from traffic_analyzer.web.workspace.tree import get_workspace_tree, list_tree
from traffic_analyzer.web.workspace.videos import (
    AnalysisDeleteRequest,
    _videos_cache,
    delete_analysis,
    delete_analysis_batch,
    get_workspace_videos,
    list_videos,
    list_videos_cached,
)

__all__ = [
    "AnalysisDeleteRequest",
    "DEFAULT_DEMO_WORKSPACE",
    "LAST_WORKSPACE_PATH",
    "PRUNED_DIR_NAMES",
    "VIDEO_EXTENSIONS",
    "WORKSPACE_DIRS_ENV_VAR",
    "WorkspaceSetRequest",
    "WorkspaceState",
    "allowed_workspace_dirs",
    "analysis_dir",
    "delete_analysis",
    "delete_analysis_batch",
    "find_video",
    "get_workspace",
    "get_workspace_tree",
    "get_workspace_videos",
    "has_results",
    "invalidate_caches",
    "list_tree",
    "list_videos",
    "list_videos_cached",
    "read_last_workspace",
    "record_last_workspace",
    "register_cache",
    "require_workspace",
    "resolve_tree_dir",
    "resolve_workspace_file",
    "resolve_workspace_video",
    "router",
    "set_workspace",
    "validate_stem",
]
