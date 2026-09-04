"""Unified video I/O: metadata probe, single-frame JPEG, batch window extraction.

[文件说明]
作用:全项目唯一的 OpenCV 视频读取模块,合并原 web/frames.py 与
    toolserver/tracking/windows.py 两份实现。
    - read_video_meta:返回 {frame_count, fps, duration_sec, width, height},
      打不开或帧数非法返回 None(异常策略由调用方决定)。
    - read_frame_jpeg:单帧 JPEG,带 128 项 LRU 缓存(key 含 mtime,
      防视频替换后返回陈旧帧)。
    - extract_window_jpegs:按帧号批量抽 JPEG 序列(读不到的帧跳过),无缓存。
上游:web/frames.py(薄封装 re-export)、toolserver/server.py、
    toolserver/tracking/windows.py。
下游:cv2(OpenCV)。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2

_CACHE_SIZE = 128
# Key: (path, frame_index, mtime) — mtime included so a replaced video file
# never serves stale frames (same convention as video_stream's probe cache).
_cache: "OrderedDict[Tuple[str, int, float], bytes]" = OrderedDict()
_cache_lock = threading.Lock()

# JPEG quality for batch window extraction (frames fed to the VLM).
_WINDOW_JPEG_QUALITY = 80


def _cached(key: Tuple[str, int, float]) -> Optional[bytes]:
    with _cache_lock:
        data = _cache.get(key)
        if data is not None:
            _cache.move_to_end(key)
        return data


def _store(key: Tuple[str, int, float], data: bytes) -> None:
    with _cache_lock:
        _cache[key] = data
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)


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


def read_frame_jpeg(video_path: Path, index: int) -> Optional[bytes]:
    """Return the JPEG-encoded frame at ``index``, or None when out of range."""
    key = (str(video_path), index, video_path.stat().st_mtime)
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


def extract_window_jpegs(video_path: Path, frames: Sequence[int]) -> List[bytes]:
    """按帧号顺序抽取 JPEG 字节序列(读不到的帧跳过)。"""
    cap = cv2.VideoCapture(str(video_path))
    out: List[bytes] = []
    try:
        for fi in frames:
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            ok, buf = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, _WINDOW_JPEG_QUALITY]
            )
            if ok:
                out.append(buf.tobytes())
    finally:
        cap.release()
    return out
