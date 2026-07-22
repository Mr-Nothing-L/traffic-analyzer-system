"""On-demand video frame extraction with a small LRU cache."""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional, Tuple

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


@router.get("/api/videos/{stem}/frame")
def get_frame(stem: str, request: Request, index: int = Query(..., ge=0)) -> Response:
    workspace = workspace_mod.require_workspace(request)
    workspace_mod.validate_stem(stem)
    video = workspace_mod.find_video(workspace, stem)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")
    data = read_frame_jpeg(video, index)
    if data is None:
        raise HTTPException(status_code=404, detail="Frame index out of range")
    return Response(content=data, media_type="image/jpeg")
