"""On-demand video frame extraction with a small LRU cache.

[文件说明]
作用:按需抽帧接口。read_frame_jpeg 用 OpenCV 读取指定帧并 JPEG 编码,带 128 项 LRU
缓存(key 含 mtime,防视频替换后返回陈旧帧);read_video_meta 返回帧数/fps/分辨率。
提供 /api/videos/{stem}/meta|frame 与 /api/workspace/meta|frame(嵌套目录视频)路由。
上游:web/app.py(挂载路由);frontend/dist 前端(逐帧步进浏览与元信息展示)。
下游:web/workspace.py(视频路径解析);cv2(OpenCV)。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
from fastapi import APIRouter, HTTPException, Query, Request, Response

from traffic_analyzer.web import workspace as workspace_mod

router = APIRouter()

# CV 底座(抽帧/元信息 + LRU)下沉 utils/video_io:web 路由与 toolserver 共享,
# 消除 toolserver 对 web 的反向依赖(P3-23)。
from traffic_analyzer.utils.video_io import read_frame_jpeg, read_video_meta

__all__ = ["read_frame_jpeg", "read_video_meta"]


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
