"""共享的按需抽帧与视频元信息工具(原 web/frames.py 的 CV 底座)。

[文件说明]
作用:read_frame_jpeg 按帧号读取并 JPEG 编码(128 项 LRU,key 含 mtime 防视频
     替换后返回陈旧帧);read_video_meta 返回帧数/fps/分辨率。web(路由)与
     toolserver(取证)共同消费——toolserver 不再反向依赖 web(P3-23)。
上游:web/frames.py(路由层复用)、toolserver/server.py。
下游:cv2(OpenCV)。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2

_CACHE_SIZE = 128
# Key: (path, frame_index, mtime) — mtime included so a replaced video file
# never serves stale frames (same convention as video_stream's probe cache).
_cache: "OrderedDict[Tuple[str, int, float], bytes]" = OrderedDict()
_cache_lock = threading.Lock()


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


