"""Workspace state and video discovery for the web UI.

A *workspace* is a directory of videos the user analyzes. Inference results
live under ``<workspace>/analysis/<video_stem>/`` (see the shared contract):
``report.md``, ``<video_stem>.json`` (SFT sample), ``<video_stem>_evidence.json``
and an ``images/`` subdirectory.
"""

from __future__ import annotations

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
    if not stem or stem in (".", "..") or "/" in stem or "\\" in stem or ".." in stem:
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
    videos: List[Dict[str, Any]] = []
    for path in sorted(workspace.iterdir()):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            stat = path.stat()
            videos.append(
                {
                    "name": path.name,
                    "stem": path.stem,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "has_results": has_results(workspace, path.stem),
                }
            )
    return videos


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
