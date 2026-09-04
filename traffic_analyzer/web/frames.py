"""On-demand video frame extraction routes.

[文件说明]
作用:按需抽帧路由。read_frame_jpeg/read_video_meta 的实际实现已收敛到
    utils/video_io.py(单帧带 LRU 缓存,key 含 mtime 防视频替换后返回陈旧帧),
    本模块只做 re-export 以保持既有调用方签名,并挂载
    /api/videos/{stem}/meta|frame 与 /api/workspace/meta|frame(嵌套目录视频)路由。
上游:web/app.py(挂载路由);frontend/dist 前端(逐帧步进浏览与元信息展示)。
下游:utils/video_io.py(抽帧/元信息);web/workspace.py(视频路径解析)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request, Response

from traffic_analyzer.utils.video_io import read_frame_jpeg, read_video_meta
from traffic_analyzer.web import workspace as workspace_mod

__all__ = ["read_frame_jpeg", "read_video_meta"]

router = APIRouter()


def _resolve_stem_video(request: Request, stem: str) -> Path:
    """Resolve a top-level video by stem (404 on traversal/unknown)."""
    workspace = workspace_mod.require_workspace(request)
    workspace_mod.validate_stem(stem)
    video = workspace_mod.find_video(workspace, stem)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


def _meta_or_404(video: Path) -> Dict[str, Any]:
    meta = read_video_meta(video)
    if meta is None:
        raise HTTPException(status_code=404, detail="Video metadata unreadable")
    return meta


def _frame_response(video: Path, index: int) -> Response:
    data = read_frame_jpeg(video, index)
    if data is None:
        raise HTTPException(status_code=404, detail="Frame index out of range")
    return Response(content=data, media_type="image/jpeg")


@router.get("/api/videos/{stem}/meta")
def get_meta(stem: str, request: Request) -> Dict[str, Any]:
    return _meta_or_404(_resolve_stem_video(request, stem))


@router.get("/api/videos/{stem}/frame")
def get_frame(stem: str, request: Request, index: int = Query(..., ge=0)) -> Response:
    return _frame_response(_resolve_stem_video(request, stem), index)


@router.get("/api/workspace/meta")
def get_workspace_meta(request: Request, path: str) -> Dict[str, Any]:
    """Video metadata for a workspace-relative path (nested tree videos)."""
    return _meta_or_404(workspace_mod.resolve_workspace_video(request, path))


@router.get("/api/workspace/frame")
def get_workspace_frame(
    request: Request, path: str, index: int = Query(..., ge=0)
) -> Response:
    """On-demand frame for a workspace-relative path (nested tree videos)."""
    return _frame_response(workspace_mod.resolve_workspace_video(request, path), index)
