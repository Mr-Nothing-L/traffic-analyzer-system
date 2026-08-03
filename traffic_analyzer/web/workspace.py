"""Workspace state and video discovery for the web UI.

A *workspace* is a directory of videos the user analyzes. Inference results
live under ``<workspace>/analysis/<video_stem>/`` (see the shared contract):
``report.md``, ``<video_stem>.json`` (SFT sample), ``<video_stem>_evidence.json``
and an ``images/`` subdirectory.

[文件说明]
作用:工作区状态(WorkspaceState)与视频发现、路径安全校验(防目录穿越);定义
analysis/<stem>/ 结果目录契约,并提供 /api/workspace、/api/workspace/videos、
/api/workspace/tree 路由。list_videos 与 dashboard 聚合这类昂贵操作带进程内
TTL 缓存(_TTLCache + invalidate_caches,默认 15s;失效条件:workspace 变更 /
infer 完成 / review PUT / SFT/证据 PUT)。可选白名单:TRAFFIC_ANALYZER_WORKSPACE_DIRS(逗号分隔,
支持 ~ 与相对路径,config/.env 或环境变量)非空时,POST /api/workspace 与
fs 目录浏览仅允许名单目录及其子路径,越界 403;未设置/为空则不限制。
上游:web/app.py(挂载路由);web/ 下 jobs、evidence_api、frames、video_stream
均复用其 require_workspace/validate_stem/analysis_dir 等辅助函数。
下游:无包内模块依赖,仅读写工作区文件系统;VIDEO_EXTENSIONS 与 scripts/batch_evaluate.py
的视频发现保持一致。
"""

from __future__ import annotations

import copy as copy_module
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()

# Aligned with scripts/batch_evaluate.py video discovery.
VIDEO_EXTENSIONS = (".mp4", ".avi", ".mov", ".mkv", ".wmv")

# os.walk 剪枝目录:推理产出目录(analysis/<stem>/images、tmp_img、output 等)
# 在大工作区里体量极大,且其中的 mp4(中间产物/证据剪辑)不是「工作区视频」,
# 不下钻遍历;这些目录里的视频既不进 /api/workspace/videos,也不参与看板。
# 注意:has_results 按 workspace/analysis/<stem>/ 路径直接探测(不走遍历),
# 剪掉 analysis 不影响结果判定;单层的 list_tree 不做此剪枝(用户可见产出目录)。
PRUNED_DIR_NAMES = frozenset({"analysis", "tmp_img", "output", "__pycache__"})

WORKSPACE_DIRS_ENV_VAR = "TRAFFIC_ANALYZER_WORKSPACE_DIRS"

# ---------------------------------------------------------------------------
# 进程内 TTL 缓存
# 大工作区(数千视频、外接盘)下 list_videos 的 os.walk + 逐文件 stat 与
# dashboard 的逐 stem JSON 读取需 ~10s;这里给这类昂贵聚合加 15s 进程内缓存。
# 失效条件(统一走 invalidate_caches):workspace 变更(POST /api/workspace)、
# infer job 完成(jobs.py)、review PUT(dashboard.py)、SFT/证据 PUT 落盘
# (evidence_api.py)。测试可 monkeypatch _CACHE_TTL_SEC 缩短 TTL。
# ---------------------------------------------------------------------------

_CACHE_TTL_SEC = 15.0


class _TTLCache:
    """线程安全的进程内 TTL 缓存(一把锁;缓存对象对外不可变)。

    存入后不复制(约定:调用方存入的是新建对象,之后不再持有/修改);
    取出默认深拷贝(get(copy=True)),热路径可 get(copy=False) 只读使用。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str, copy: bool = True) -> Optional[Any]:
        """命中且未过期返回缓存值,否则 None。

        copy=True(默认):返回深拷贝(锁外进行),调用方随意修改不污染缓存。
        copy=False:返回缓存对象本身,仅限只读使用(过滤/切片产生新对象,
        不就地修改)——大对象(数千行)深拷贝需 ~200ms,热路径用此模式。
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.monotonic() - ts > _CACHE_TTL_SEC:
                del self._entries[key]
                return None
        if copy:
            return copy_module.deepcopy(value)
        return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic(), value)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_caches: List[_TTLCache] = []
_caches_lock = threading.Lock()


def register_cache(cache: _TTLCache) -> _TTLCache:
    """登记一个缓存实例,使其参与 invalidate_caches 的统一失效。"""
    with _caches_lock:
        _caches.append(cache)
    return cache


def invalidate_caches() -> None:
    """清空所有已登记缓存(workspace 变更 / infer 完成 / review / SFT / 证据写盘)。"""
    with _caches_lock:
        caches = list(_caches)
    for cache in caches:
        cache.clear()

# traffic_analyzer/config/.env(traffic_analyzer/web/workspace.py → parents[1])。
# web 独立启动未必经过 ConfigManager 的 load_dotenv,兜底自己读一次文件;
# 测试 monkeypatch 此常量。
_CONFIG_ENV_PATH = Path(__file__).resolve().parents[1] / "config" / ".env"


def _config_env_value(key: str) -> Optional[str]:
    """Read one ``KEY=value`` entry from config/.env (same idea as auth._env_value)."""
    try:
        text = _CONFIG_ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    return None


def allowed_workspace_dirs() -> List[Path]:
    """Resolved whitelist from ``TRAFFIC_ANALYZER_WORKSPACE_DIRS`` (comma-separated).

    ``os.environ`` first (ConfigManager's load_dotenv injects config/.env into
    it), then config/.env itself. ``~`` and relative paths are supported.
    Empty/unset → ``[]`` = unrestricted (legacy behavior).
    """
    raw = os.environ.get(WORKSPACE_DIRS_ENV_VAR)
    if raw is None:
        raw = _config_env_value(WORKSPACE_DIRS_ENV_VAR)
    dirs: List[Path] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if item:
            dirs.append(Path(item).expanduser().resolve())
    return dirs


def _within_allowed(path: Path, allowed: List[Path]) -> bool:
    return any(path == root or root in path.parents for root in allowed)


class WorkspaceState:
    """Currently selected workspace directory (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path: Optional[Path] = None

    def set(self, path: Path) -> None:
        with self._lock:
            self._path = path

    def get(self) -> Optional[Path]:
        with self._lock:
            return self._path


def require_workspace(request: Request) -> Path:
    """Return the current workspace or raise 400 when none is selected."""
    workspace = request.app.state.workspace.get()
    if workspace is None:
        raise HTTPException(status_code=400, detail="No workspace selected")
    return workspace


def validate_stem(stem: str) -> str:
    """Reject path-traversal stems; return the stem unchanged when safe."""
    if (
        not stem
        or stem in (".", "..")
        or "/" in stem
        or "\\" in stem
        or ".." in stem
        # 控制字符(含 \x00、换行):会污染日志/子进程 argv/HTTP 头,一并拒绝
        or any(ord(c) < 32 or ord(c) == 127 for c in stem)
    ):
        raise HTTPException(status_code=404, detail="Unknown video stem")
    return stem


def analysis_dir(workspace: Path, stem: str) -> Path:
    return workspace / "analysis" / stem


def has_results(workspace: Path, stem: str) -> bool:
    """A video "has results" when its SFT sample JSON exists."""
    return (analysis_dir(workspace, stem) / f"{stem}.json").is_file()


def find_video(workspace: Path, stem: str) -> Optional[Path]:
    for ext in VIDEO_EXTENSIONS:
        candidate = workspace / f"{stem}{ext}"
        if candidate.is_file():
            return candidate
    return None


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
    videos = list_videos(workspace)
    _videos_cache.set(key, videos)
    return videos


def _resolve_confined(workspace: Path, rel: str, detail: str) -> Path:
    """Resolve a workspace-relative path, rejecting anything escaping the root."""
    if rel:
        segments = rel.split("/")
        if any(seg in ("", ".", "..") for seg in segments):
            raise HTTPException(status_code=404, detail=detail)
    root = workspace.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=404, detail=detail)
    return target


def resolve_tree_dir(workspace: Path, rel: str) -> Path:
    """Resolve a workspace-relative dir, rejecting anything escaping the root."""
    target = _resolve_confined(workspace, rel, "Unknown tree path")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="Unknown tree path")
    return target


def resolve_workspace_file(workspace: Path, rel: str) -> Path:
    """Resolve a workspace-relative file, rejecting anything escaping the root."""
    target = _resolve_confined(workspace, rel, "Unknown workspace file")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Unknown workspace file")
    return target


def resolve_workspace_video(request: Request, rel: str) -> Path:
    """Resolve a workspace-relative video file (404 on traversal/non-video).

    Shared by frames.py and video_stream.py for their ``/api/workspace/*``
    endpoints.
    """
    workspace = require_workspace(request)
    video = resolve_workspace_file(workspace, rel)
    if video.suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Not a video file")
    return video


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


class WorkspaceSetRequest(BaseModel):
    path: str


@router.get("/api/workspace")
def get_workspace(request: Request) -> Dict[str, Any]:
    workspace = request.app.state.workspace.get()
    return {"path": str(workspace) if workspace is not None else None}


@router.post("/api/workspace")
def set_workspace(body: WorkspaceSetRequest, request: Request) -> Dict[str, Any]:
    path = Path(body.path).expanduser().resolve()
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {body.path}")
    allowed = allowed_workspace_dirs()
    if allowed and not _within_allowed(path, allowed):
        raise HTTPException(status_code=403, detail="workspace not in allowed list")
    request.app.state.workspace.set(path)
    invalidate_caches()  # 工作区变更:旧 key 的缓存一并清掉(防内存堆积/串数据)
    return {"path": str(path)}


@router.get("/api/workspace/videos")
def get_workspace_videos(request: Request) -> List[Dict[str, Any]]:
    return list_videos_cached(require_workspace(request))


@router.get("/api/workspace/tree")
def get_workspace_tree(request: Request, path: str = "") -> Dict[str, Any]:
    return list_tree(require_workspace(request), path)
