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
    argv = [_FFMPEG, "-v", "error", "-i", str(video)]
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

    def generate() -> Iterator[bytes]:
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            # Client disconnected or stream ended: never leave ffmpeg running.
            if proc.poll() is None:
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
    _container, codec = probe_video(video)
    if is_browser_native(codec, video.suffix):
        media_type = _MEDIA_TYPES.get(video.suffix.lower(), "application/octet-stream")
        return FileResponse(video, media_type=media_type)
    return _transcode_response(video, ss)
