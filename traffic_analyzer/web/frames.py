"""On-demand video frame extraction with a small LRU cache."""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
from fastapi import APIRouter, HTTPException, Query, Request, Response

from traffic_analyzer.web import workspace as workspace_mod

router = APIRouter()

_CACHE_SIZE = 128
_cache: "OrderedDict[Tuple[str, int], bytes]" = OrderedDict()
_cache_lock = threading.Lock()


def _cached(key: Tuple[str, int]) -> Optional[bytes]:
    with _cache_lock:
        data = _cache.get(key)
        if data is not None:
            _cache.move_to_end(key)
        return data


def _store(key: Tuple[str, int], data: bytes) -> None:
    with _cache_lock:
        _cache[key] = data
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)


def read_frame_jpeg(video_path: Path, index: int) -> Optional[bytes]:
    """Return the JPEG-encoded frame at ``index``, or None when out of range."""
    key = (str(video_path), index)
    cached = _cached(key)
    if cached is not None:
        return cached

    cap = cv2.VideoCapture(str(video_path))
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if index < 0 or index >= total:
            return None
        cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = cap.read()
        if not ok or frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame)
        if not ok:
            return None
        data = buf.tobytes()
    finally:
        cap.release()

    _store(key, data)
    return data


def read_video_meta(video_path: Path) -> Optional[Dict[str, Any]]:
    """Return frame/fps/size metadata, or None when the video can't be opened."""
    cap = cv2.VideoCapture(str(video_path))
    try:
        if not cap.isOpened():
            return None
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if frame_count <= 0:
            return None
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        cap.release()
    return {
        "frame_count": frame_count,
        "fps": fps,
        "duration_sec": frame_count / fps if fps > 0 else None,
        "width": width,
        "height": height,
    }


def _resolve_stem_video(request: Request, stem: str) -> Path:
    """Resolve a top-level video by stem (404 on traversal/unknown)."""
    workspace = workspace_mod.require_workspace(request)
    workspace_mod.validate_stem(stem)
    video = workspace_mod.find_video(workspace, stem)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


def _resolve_rel_video(request: Request, path: str) -> Path:
    """Resolve a workspace-relative video file (404 on traversal/non-video)."""
    workspace = workspace_mod.require_workspace(request)
    video = workspace_mod.resolve_workspace_file(workspace, path)
    if video.suffix.lower() not in workspace_mod.VIDEO_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Not a video file")
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
    return _meta_or_404(_resolve_rel_video(request, path))


@router.get("/api/workspace/frame")
def get_workspace_frame(
    request: Request, path: str, index: int = Query(..., ge=0)
) -> Response:
    """On-demand frame for a workspace-relative path (nested tree videos)."""
    return _frame_response(_resolve_rel_video(request, path), index)
