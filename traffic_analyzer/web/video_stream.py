"""Video streaming endpoint with on-the-fly transcoding.

Browser-native files (h264 in .mp4/.mov, vp8/vp9/av1 in .webm/.mkv) are
served directly via ``FileResponse`` (HTTP Range supported). Anything else
the workspace may hold — the real surveillance clips are MPEG-4 Part 2
(Xvid-class), plus possible H.265/MJPEG — is transcoded to fragmented MP4
(h264/yuv420p) by piping ``ffmpeg`` stdout into a ``StreamingResponse``.
Probe results are cached per path+mtime. When ffprobe/ffmpeg is missing or
the transcode cannot start, the endpoint answers 501 so the frontend can
fall back to frame-stepping.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple

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
_TICK = 0.5  # select tick; the generator suspends at yield between ticks
_PROBE_TIMEOUT = 5.0  # first-chunk probe before answering 200


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


def _transcode_response(video: Path, ss: Optional[float]) -> StreamingResponse:
    if _FFMPEG is None:
        raise HTTPException(status_code=501, detail="ffmpeg not found on server")
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

    # Non-blocking pipe + select tick: the generator spends most of its time
    # suspended at a ``yield``, so when the client disconnects starlette can
    # actually close it and the ``finally`` below kills ffmpeg. With a blocking
    # ``read`` the close never lands and ffmpeg transcodes to EOF (zombie).
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
    # instead of getting a 200 with an empty body.
    first = b""
    ready, _, _ = select.select([fd], [], [], _PROBE_TIMEOUT)
    if ready:
        first = _read_chunk(0)
        if not first:
            returncode = proc.wait()
            if returncode != 0:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                raise HTTPException(
                    status_code=501,
                    detail=f"ffmpeg produced no output (exit {returncode})",
                )

    def generate() -> Iterator[bytes]:
        try:
            if first:
                yield first
            while True:
                chunk = _read_chunk(_TICK)
                if not chunk:
                    if proc.poll() is not None:
                        break  # EOF with the process gone
                    continue
                yield chunk
        finally:
            # Client disconnected or stream ended: never leave ffmpeg running.
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            proc.wait()

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
    return _stream_response(video, ss)


@router.get("/api/workspace/stream")
def stream_workspace_video(
    request: Request, path: str, ss: Optional[float] = Query(None, ge=0)
) -> object:
    """Stream a workspace-relative video file (nested tree videos)."""
    workspace = workspace_mod.require_workspace(request)
    video = workspace_mod.resolve_workspace_file(workspace, path)
    if video.suffix.lower() not in workspace_mod.VIDEO_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Not a video file")
    return _stream_response(video, ss)


def _stream_response(video: Path, ss: Optional[float]) -> object:
    _container, codec = probe_video(video)
    if is_browser_native(codec, video.suffix):
        media_type = _MEDIA_TYPES.get(video.suffix.lower(), "application/octet-stream")
        return FileResponse(video, media_type=media_type)
    return _transcode_response(video, ss)
