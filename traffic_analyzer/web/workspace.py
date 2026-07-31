"""Workspace state and video discovery for the web UI.

A *workspace* is a directory of videos the user analyzes. Inference results
live under ``<workspace>/analysis/<video_stem>/`` (see the shared contract):
``report.md``, ``<video_stem>.json`` (SFT sample), ``<video_stem>_evidence.json``
and an ``images/`` subdirectory.

[文件说明]
作用:工作区状态(WorkspaceState)与视频发现、路径安全校验(防目录穿越);定义
analysis/<stem>/ 结果目录契约,并提供 /api/workspace、/api/workspace/videos、
/api/workspace/tree 路由。
上游:web/app.py(挂载路由);web/ 下 jobs、evidence_api、frames、video_stream
均复用其 require_workspace/validate_stem/analysis_dir 等辅助函数。
下游:无包内模块依赖,仅读写工作区文件系统;VIDEO_EXTENSIONS 与 scripts/batch_evaluate.py
的视频发现保持一致。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()

# Aligned with scripts/batch_evaluate.py video discovery.
VIDEO_EXTENSIONS = (".mp4", ".avi", ".mov", ".mkv", ".wmv")


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
    """All videos in the workspace at any depth (dot-dirs skipped).

    Each entry carries the workspace-relative path (``rel``) used by the
    frontend as its unique key; ``has_results`` follows the flat
    ``analysis/<stem>/`` contract for every depth.
    """
    videos: List[Dict[str, Any]] = []
    root = workspace.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
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
    request.app.state.workspace.set(path)
    return {"path": str(path)}


@router.get("/api/workspace/videos")
def get_workspace_videos(request: Request) -> List[Dict[str, Any]]:
    return list_videos(require_workspace(request))


@router.get("/api/workspace/tree")
def get_workspace_tree(request: Request, path: str = "") -> Dict[str, Any]:
    return list_tree(require_workspace(request), path)
