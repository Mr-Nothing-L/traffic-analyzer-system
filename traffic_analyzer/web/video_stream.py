"""视频流播放接口 + 按需转码(详见下方[文件说明])。

[文件说明]
作用:视频流播放接口。浏览器可原生播放的编码(h264/mp4、vp8/vp9/av1 等)直接
FileResponse(支持 HTTP Range);其余监控码流(Xvid 类、H.265、MJPEG)先用 ffmpeg
整片转码为 faststart MP4(moov 前置,写到系统临时目录)再经 FileResponse 返回 —
渐进式 fMP4 在 Safari <video> 不可播,faststart 整片 + Range 则三家浏览器通吃且
可拖动进度。转码结果走小 LRU(上限 3,key=path+mtime+ss),带在途引用保护:
FileResponse 惰性 open,取出的路径在响应发送完成(或发送异常)前不得删除 ——
淘汰项仍有引用时转入 pending,引用归零才补删;进程退出(atexit)全清。
全局转码信号量上限 3(超限 503);ffmpeg 非 0 退出或无输出 → 501;
ffprobe 探测结果按 path+mtime 缓存。
上游:web/app.py(挂载路由);frontend/dist 前端 <video> 播放。
下游:web/workspace.py(视频路径解析);系统 ffmpeg/ffprobe 外部命令。
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from traffic_analyzer.web import workspace as workspace_mod

router = APIRouter()

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")

# codec -> container suffixes browsers can play natively
_BROWSER_NATIVE = {
    "h264": (".mp4", ".mov"),
    "vp8": (".webm", ".mkv"),
    "vp9": (".webm", ".mkv"),
    "av1": (".webm", ".mkv"),
}

_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".wmv": "video/x-ms-wmv",
}

_probe_cache: Dict[Tuple[str, float], Tuple[Optional[str], Optional[str]]] = {}
_cache_lock = threading.Lock()

# 全局转码并发上限:转码是 CPU 重活,超出直接 503,让前端稍后再试/降级逐帧。
# 用 threading 信号量(非 asyncio.Semaphore):获取发生在同步端点线程里,
# 与具体事件循环无绑定,TestClient / uvicorn 下行为一致。
_MAX_TRANSCODES = 3
_transcode_slots = threading.BoundedSemaphore(_MAX_TRANSCODES)

# 转码产物 LRU:最多 3 条,超出淘汰最久未用并删除其临时文件。
# key=(src 路径, mtime, ss):ss 不同的转码各占一条(前端基本不带 ss,影响可忽略)。
_TRANSCODE_CACHE_MAX = 3
_transcode_cache: "OrderedDict[Tuple[str, float, Optional[float]], Path]" = OrderedDict()

# 在途引用保护:FileResponse 惰性 open,取出的路径在响应发完前不得被删;
# inflight>0 的淘汰项转入 pending,引用归零(_transcode_release)时再补删。
_inflight: Dict[Path, int] = {}
_pending_delete: set = set()


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _transcode_release(path: Path) -> None:
    """归还一次在途引用;路径已被淘汰且引用归零时,此刻才真正删除。"""
    with _cache_lock:
        left = _inflight.get(path, 0) - 1
        if left > 0:
            _inflight[path] = left
            return
        _inflight.pop(path, None)
        doomed = path in _pending_delete
        _pending_delete.discard(path)
    if doomed:
        _unlink_quiet(path)


def _transcode_cache_get(key: Tuple[str, float, Optional[float]]) -> Optional[Path]:
    with _cache_lock:
        path = _transcode_cache.get(key)
        if path is None:
            return None
        if not path.exists():  # 临时文件被外部清掉,缓存条目作废
            _transcode_cache.pop(key, None)
            return None
        _inflight[path] = _inflight.get(path, 0) + 1  # 调用方随响应归还
        _transcode_cache.move_to_end(key)
        return path


def _transcode_cache_put(key: Tuple[str, float, Optional[float]], path: Path) -> None:
    with _cache_lock:
        _inflight[path] = _inflight.get(path, 0) + 1  # 调用方随响应归还
        previous = _transcode_cache.get(key)
        _transcode_cache[key] = path
        _transcode_cache.move_to_end(key)
        evicted = [previous] if previous is not None and previous != path else []
        while len(_transcode_cache) > _TRANSCODE_CACHE_MAX:
            _, old = _transcode_cache.popitem(last=False)
            evicted.append(old)
        doomed = []
        for old in evicted:
            if _inflight.get(old, 0):  # 仍在途:转入 pending,引用归零时补删
                _pending_delete.add(old)
            else:
                doomed.append(old)
    for old in doomed:
        _unlink_quiet(old)


def _cleanup_transcode_cache() -> None:
    """进程退出时尽量清理:删除缓存内及 pending 的全部临时文件(atexit)。"""
    with _cache_lock:
        paths = list(_transcode_cache.values()) + list(_pending_delete)
        _transcode_cache.clear()
        _pending_delete.clear()
        _inflight.clear()
    for path in paths:
        _unlink_quiet(path)


atexit.register(_cleanup_transcode_cache)


def is_browser_native(codec: Optional[str], suffix: str) -> bool:
    """True when Chrome/Firefox can play ``codec`` in container ``suffix``."""
    return codec in _BROWSER_NATIVE and suffix.lower() in _BROWSER_NATIVE[codec]


def probe_video(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(container_format, video_codec)``; cached per path+mtime."""
    if _FFPROBE is None:
        raise HTTPException(status_code=501, detail="ffprobe not found on server")
    key = (str(path), path.stat().st_mtime)
    with _cache_lock:
        if key in _probe_cache:
            return _probe_cache[key]
    try:
        proc = subprocess.run(
            [
                _FFPROBE, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name:format=format_name",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=30, check=True,
        )
        data = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=501, detail=f"ffprobe failed: {exc}")
    streams = data.get("streams") or [{}]
    result = (data.get("format", {}).get("format_name"), streams[0].get("codec_name"))
    with _cache_lock:
        _probe_cache[key] = result
    return result


def _transcode_faststart(video: Path, ss: Optional[float]) -> Path:
    """Transcode ``video`` to a faststart MP4 in the system temp dir.

    faststart moves the moov atom ahead of mdat, which requires a seekable
    output — hence a temp file instead of a pipe. The result is cached in a
    small LRU; a hit skips the ffmpeg run entirely. The returned path holds
    one inflight reference: callers must hand it to ``_InflightFileResponse``
    (or call ``_transcode_release`` themselves) to give it back. Raises 501
    when ffmpeg is missing/fails (non-zero exit or no output), 503 when the
    global transcode semaphore is full.
    """
    if _FFMPEG is None:
        raise HTTPException(status_code=501, detail="ffmpeg not found on server")
    key = (str(video), video.stat().st_mtime, ss)
    cached = _transcode_cache_get(key)
    if cached is not None:
        return cached
    if not _transcode_slots.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail="Too many concurrent transcodes; retry later",
        )
    fd, tmp_name = tempfile.mkstemp(prefix="ta_transcode_", suffix=".mp4")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        argv = [_FFMPEG, "-y", "-nostdin", "-v", "error", "-i", str(video)]
        if ss:
            argv += ["-ss", f"{ss:.3f}"]
        argv += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-an", "-f", "mp4", str(tmp),
        ]
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError as exc:
            raise HTTPException(
                status_code=501, detail=f"ffmpeg failed to start: {exc}"
            )
        rc = proc.wait()
        if rc != 0 or tmp.stat().st_size == 0:
            raise HTTPException(
                status_code=501,
                detail=f"ffmpeg transcode failed (exit {rc})",
            )
    except BaseException:
        _unlink_quiet(tmp)
        raise
    finally:
        _transcode_slots.release()
    _transcode_cache_put(key, tmp)
    return tmp


@router.get("/api/videos/{stem}/stream")
def stream_video(
    stem: str, request: Request, ss: Optional[float] = Query(None, ge=0)
) -> object:
    workspace = workspace_mod.require_workspace(request)
    workspace_mod.validate_stem(stem)
    video = workspace_mod.find_video(workspace, stem)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return _stream_response(video, ss)


@router.get("/api/workspace/stream")
def stream_workspace_video(
    request: Request, path: str, ss: Optional[float] = Query(None, ge=0)
) -> object:
    """Stream a workspace-relative video file (nested tree videos)."""
    video = workspace_mod.resolve_workspace_video(request, path)
    return _stream_response(video, ss)


class _InflightFileResponse(FileResponse):
    """发送完成(或发送异常)后归还转码产物在途引用的 FileResponse。"""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            _transcode_release(Path(self.path))


def _stream_response(video: Path, ss: Optional[float]) -> object:
    _container, codec = probe_video(video)
    if is_browser_native(codec, video.suffix):
        media_type = _MEDIA_TYPES.get(video.suffix.lower(), "application/octet-stream")
        return FileResponse(video, media_type=media_type)
    # FileResponse 自带 Range 支持;在途引用随响应发送完毕归还,临时文件留给 LRU 淘汰/atexit。
    return _InflightFileResponse(_transcode_faststart(video, ss), media_type="video/mp4")
