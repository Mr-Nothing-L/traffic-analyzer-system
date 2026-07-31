"""Video streaming endpoint with on-the-fly transcoding.

Browser-native files (h264 in .mp4/.mov, vp8/vp9/av1 in .webm/.mkv) are
served directly via ``FileResponse`` (HTTP Range supported). Anything else
the workspace may hold — the real surveillance clips are MPEG-4 Part 2
(Xvid-class), plus possible H.265/MJPEG — is transcoded to fragmented MP4
(h264/yuv420p) by piping ``ffmpeg`` stdout into a ``StreamingResponse``.
Probe results are cached per path+mtime. When ffprobe/ffmpeg is missing or
the transcode cannot start, the endpoint answers 501 so the frontend can
fall back to frame-stepping.

[文件说明]
作用:视频流播放接口。浏览器可原生播放的编码(h264/mp4、vp8/vp9/av1 等)直接
FileResponse(支持 HTTP Range);其余监控码流(Xvid 类、H.265、MJPEG)用 ffmpeg 实时转码为
fragmented MP4 经 StreamingResponse 输出。转码为 async 生成器:每个 tick 轮询
request.is_disconnected(),断连/流结束的 finally 里可靠回收 ffmpeg(不再依赖 GC
触发 close);全局转码信号量上限 3(超限 503);首块探测中 ffmpeg 立即退出且
无输出时(无论 rc 是否为 0)降级 501;ffprobe 探测结果按 path+mtime 缓存。
上游:web/app.py(挂载路由);web/static 前端 <video> 播放。
下游:web/workspace.py(视频路径解析);系统 ffmpeg/ffprobe 外部命令。
"""

from __future__ import annotations

import asyncio
import json
import os
import select
import shutil
import subprocess
import threading
from pathlib import Path
from typing import AsyncIterator, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

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

_CHUNK = 64 * 1024
_TICK = 0.5  # select tick; between ticks the async generator also polls disconnects
_PROBE_TIMEOUT = 5.0  # first-chunk probe before answering 200

# 全局转码并发上限:转码是 CPU 重活,超出直接 503,让前端稍后再试/降级逐帧。
# 用 threading 信号量(非 asyncio.Semaphore):获取发生在同步端点线程里,
# 与具体事件循环无绑定,TestClient / uvicorn 下行为一致。
_MAX_TRANSCODES = 3
_transcode_slots = threading.BoundedSemaphore(_MAX_TRANSCODES)


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


def _transcode_response(
    video: Path, ss: Optional[float], request: Optional[Request] = None
) -> StreamingResponse:
    if _FFMPEG is None:
        raise HTTPException(status_code=501, detail="ffmpeg not found on server")
    if not _transcode_slots.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail="Too many concurrent transcodes; retry later",
        )
    try:
        argv = [_FFMPEG, "-nostdin", "-v", "error", "-i", str(video)]
        if ss:
            argv += ["-ss", f"{ss:.3f}"]
        argv += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-pix_fmt", "yuv420p", "-movflags", "frag_keyframe+empty_moov",
            "-an", "-f", "mp4", "-",
        ]
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except OSError as exc:
            raise HTTPException(status_code=501, detail=f"ffmpeg failed to start: {exc}")

        # Non-blocking pipe + select tick: reads happen via asyncio.to_thread so
        # the event loop stays free and client disconnects are observed promptly.
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        os.set_blocking(fd, False)

        def _read_chunk(timeout: float) -> bytes:
            """Wait up to ``timeout``s for pipe data; b"" on timeout or EOF."""
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                return b""
            try:
                return os.read(fd, _CHUNK)
            except BlockingIOError:
                return b""

        # First-chunk probe: if ffmpeg dies before producing anything (corrupt
        # input, unsupported codec), answer 501 so the frontend can fall back
        # instead of getting a 200 with an empty body. rc==0 with no output is
        # just as useless to the player — degrade the same way.
        first = b""
        ready, _, _ = select.select([fd], [], [], _PROBE_TIMEOUT)
        if ready:
            first = _read_chunk(0)
            if not first:
                returncode = proc.wait()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                raise HTTPException(
                    status_code=501,
                    detail=f"ffmpeg produced no output (exit {returncode})",
                )
    except BaseException:
        _transcode_slots.release()
        raise

    async def generate() -> AsyncIterator[bytes]:
        try:
            if first:
                yield first
            while True:
                # 可靠断连检测:不再依赖 starlette 关闭生成器(GC 时机),
                # 每个 tick 主动轮询;starlette 的 aclose/cancel 同样会走 finally。
                if request is not None and await request.is_disconnected():
                    break
                chunk = await asyncio.to_thread(_read_chunk, _TICK)
                if not chunk:
                    if proc.poll() is not None:
                        break  # EOF with the process gone
                    continue
                yield chunk
        finally:
            # Client disconnected or stream ended: never leave ffmpeg running,
            # and always hand the transcode slot back.
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            proc.wait()
            _transcode_slots.release()

    return StreamingResponse(generate(), media_type="video/mp4")


@router.get("/api/videos/{stem}/stream")
def stream_video(
    stem: str, request: Request, ss: Optional[float] = Query(None, ge=0)
) -> object:
    workspace = workspace_mod.require_workspace(request)
    workspace_mod.validate_stem(stem)
    video = workspace_mod.find_video(workspace, stem)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return _stream_response(video, ss, request)


@router.get("/api/workspace/stream")
def stream_workspace_video(
    request: Request, path: str, ss: Optional[float] = Query(None, ge=0)
) -> object:
    """Stream a workspace-relative video file (nested tree videos)."""
    video = workspace_mod.resolve_workspace_video(request, path)
    return _stream_response(video, ss, request)


def _stream_response(
    video: Path, ss: Optional[float], request: Optional[Request] = None
) -> object:
    _container, codec = probe_video(video)
    if is_browser_native(codec, video.suffix):
        media_type = _MEDIA_TYPES.get(video.suffix.lower(), "application/octet-stream")
        return FileResponse(video, media_type=media_type)
    return _transcode_response(video, ss, request)
