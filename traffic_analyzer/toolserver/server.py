"""FastAPI application factory and /tools/* endpoints.

[文件说明]
作用:五个 POST JSON 端点:/tools/video_meta、/tools/extract_frames、
    /tools/draw_boxes、/tools/prepare_video(视频大小守门:超过 max_mb
    用 ffmpeg 阶梯降帧转码,产物放 <允许根>/.agent/transcoded/)与
    /tools/track_suspects(疑似目标定向跟踪,VLM 滑窗编排,产物放
    <允许根>/.agent/tracks/<stem>/<ts>/,结果带磁盘缓存)。
    所有 video_path 解析后必须落在允许根(allowed_roots)之内,
    越界返回 403;错误统一为 {"error": {"code", "message"}}。
    允许根:启动 --workspace 为初始根,运行期可经 POST /config/roots
    热注册新工作区(web 层切换工作区时调用,免重启)。
上游:toolserver/__init__.py(create_app 导出);__main__.py(uvicorn 启动)。
下游:web/frames.read_video_meta/read_frame_jpeg(CV 复用);
    utils/image_drawing(load_image/_draw_text_with_background/_load_scaled_font);
    utils/bbox_geometry._norm_to_px;tracking/(track_suspects 编排与缓存)。
"""

from __future__ import annotations

import base64
import io
import logging
import os
import shutil
import subprocess
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from traffic_analyzer.toolserver.tracking import SuspectAnchor
from traffic_analyzer.toolserver.tracking import cache as tracking_cache
from traffic_analyzer.toolserver.tracking import windows as tracking_windows
from traffic_analyzer.utils.bbox_geometry import _norm_to_px
from traffic_analyzer.utils.image_drawing import (
    _draw_text_with_background,
    _load_scaled_font,
    load_image,
)
from traffic_analyzer.web.frames import read_frame_jpeg, read_video_meta

logger = logging.getLogger(__name__)

# Frame extraction limits.
_DEFAULT_MAX_FRAMES = 4
_HARD_MAX_FRAMES = 8
# fps mode (uniform sampling at >= `fps` frames per second) is meant for
# full-coverage detection, so its frame cap is much looser and the JPEG
# quality lower to keep the response size manageable.
_FPS_MODE_MAX_FRAMES = 120
# JPEG quality: extracted frames are fed to the VLM, keep them compact;
# annotated frames go back to the user, keep them readable.
_EXTRACT_JPEG_QUALITY = 70
_FPS_MODE_JPEG_QUALITY = 60
_ANNOTATED_JPEG_QUALITY = 85

# prepare_video size gate: default cap and hard anti-misuse ceiling (MB).
_DEFAULT_PREPARE_MAX_MB = 40.0
_HARD_PREPARE_MAX_MB = 100.0
# Uniform fps-downshift ladder (duration preserved, only frame rate drops);
# the source fps itself is tried first (plain re-encode at crf 28).
_FPS_LADDER = (12.0, 8.0, 6.0, 4.0, 3.0, 2.0)

# Label->color palette convention (originally shared with the retired
# quick-chat QA; kept local, no shared module).
_PALETTE = (
    (255, 56, 56),
    (255, 157, 46),
    (46, 204, 113),
    (52, 152, 219),
    (155, 89, 182),
    (26, 188, 156),
    (241, 196, 15),
    (230, 126, 34),
)


class VideoMetaRequest(BaseModel):
    video_path: str


class AddRootRequest(BaseModel):
    path: str


class ExtractFramesRequest(BaseModel):
    video_path: str
    timestamps: Optional[List[float]] = None
    fps: Optional[float] = Field(default=None, gt=0)
    count: Optional[int] = None
    # None = mode default (4 for timestamps/count, 120 for fps mode).
    max_frames: Optional[int] = None


class Box(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float
    label: Optional[str] = None


class DrawBoxesRequest(BaseModel):
    video_path: str
    timestamp: float = Field(ge=0)
    boxes: List[Box] = Field(min_length=1)


class PrepareVideoRequest(BaseModel):
    video_path: str
    max_mb: float = Field(
        default=_DEFAULT_PREPARE_MAX_MB, gt=0, le=_HARD_PREPARE_MAX_MB
    )


class SuspectBox(BaseModel):
    """track_suspects 的单个疑似目标锚点(0-1 归一化框)。"""

    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)
    x2: float = Field(ge=0, le=1)
    y2: float = Field(ge=0, le=1)
    timestamp: float = Field(ge=0)
    description: str = ""

    @model_validator(mode="after")
    def _check_box(self) -> "SuspectBox":
        if self.x2 <= self.x1 or self.y2 <= self.y1:
            raise ValueError("suspect box must satisfy x2 > x1 and y2 > y1")
        return self


class TrackSuspectsRequest(BaseModel):
    video_path: str
    suspects: List[SuspectBox] = Field(min_length=1, max_length=5)
    time_range: Optional[List[float]] = None

    @model_validator(mode="after")
    def _check_time_range(self) -> "TrackSuspectsRequest":
        if self.time_range is not None and len(self.time_range) != 2:
            raise ValueError("time_range must be [start_s, end_s]")
        return self


def _error(status_code: int, code: str, message: str) -> HTTPException:
    """Build an HTTPException whose detail already matches the error contract."""
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message}
    )


def _resolve_video(allowed_roots: List[Path], video_path: str) -> Path:
    """Resolve ``video_path``; the result must lie inside one allowed root.

    相对路径按允许根顺序逐个尝试(取首个存在的);越界候选跳过,全部
    越界才 403;落在根内但文件不存在则 404。
    """
    candidate = Path(video_path)
    search = (
        [candidate]
        if candidate.is_absolute()
        else [root / candidate for root in allowed_roots]
    )
    inside_missing = False
    for item in search:
        resolved = item.resolve()
        if not any(resolved.is_relative_to(root) for root in allowed_roots):
            continue
        if resolved.is_file():
            return resolved
        inside_missing = True
    if inside_missing:
        raise _error(404, "video_not_found", f"Video not found: {video_path}")
    raise _error(
        403,
        "path_outside_workspace",
        f"video_path resolves outside allowed roots: {video_path}",
    )


def _meta_or_error(video: Path) -> Dict[str, Any]:
    meta = read_video_meta(video)
    if meta is None:
        raise _error(404, "video_meta_unavailable", "Video metadata unreadable")
    return meta


def _transcode_dir(allowed_roots: List[Path], video: Path) -> Path:
    """Transcode output dir under the allowed root that contains ``video``."""
    root = next(r for r in allowed_roots if video.is_relative_to(r))
    out_dir = root / ".agent" / "transcoded"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _fps_candidates(src_fps: float) -> List[float]:
    """Ladder: source fps first (plain re-encode), then uniform downshifts."""
    candidates = [src_fps] if src_fps > 0 else []
    candidates.extend(n for n in _FPS_LADDER if src_fps <= 0 or n < src_fps)
    return candidates


def _frame_index(meta: Dict[str, Any], timestamp: float) -> int:
    """Clamp a timestamp (seconds) to a valid frame index."""
    fps = float(meta.get("fps") or 0)
    total = int(meta["frame_count"])
    index = int(round(timestamp * fps)) if fps > 0 else 0
    return max(0, min(total - 1, index))


def _reencode_jpeg(data: bytes, quality: int) -> Optional[bytes]:
    """Re-encode cached JPEG bytes at a controlled quality to cap response size."""
    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return None
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


def _even_timestamps(duration_s: float, n: int) -> List[float]:
    """``n`` timestamps spread evenly across the video (endpoints included)."""
    if n <= 1 or duration_s <= 0:
        return [duration_s / 2.0]
    step = duration_s / n  # last point stays clear of the final frame
    return [step * i for i in range(n)]


def create_app(workspace: Path | str) -> FastAPI:
    """Build the tool server app with ``workspace`` as the initial allowed root."""
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ValueError(f"Workspace is not a directory: {workspace}")

    app = FastAPI(title="traffic-analyzer toolserver")
    app.state.workspace = root
    # 路径安全根集合:初始根 + 运行期经 /config/roots 热注册的工作区。
    app.state.allowed_roots = [root]
    # /config/roots 的共享管理 token:未配置时保持开放(本地开发兼容),但打 warning。
    app.state.admin_token = os.environ.get("TOOLSERVER_ADMIN_TOKEN")
    if app.state.admin_token is None:
        logger.warning(
            "toolserver: TOOLSERVER_ADMIN_TOKEN not set; /config/roots is open to local processes"
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        if not (isinstance(detail, dict) and "code" in detail):
            detail = {"code": "http_error", "message": str(detail)}
        return JSONResponse(status_code=exc.status_code, content={"error": detail})

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {"code": "invalid_request", "message": str(exc.errors())}
            },
        )

    def _resolve(request: Request, video_path: str) -> Path:
        return _resolve_video(request.app.state.allowed_roots, video_path)

    @app.get("/health")
    def health(request: Request) -> Dict[str, Any]:
        return {
            "status": "ok",
            "workspace": str(root),
            "roots": [str(r) for r in request.app.state.allowed_roots],
        }

    @app.post("/config/roots")
    def add_root(body: AddRootRequest, request: Request) -> Dict[str, Any]:
        """热注册一个允许根(须为已存在目录);幂等,返回当前根列表。

        若配置了 TOOLSERVER_ADMIN_TOKEN,请求头须带 ``X-Token`` 且值匹配,
        否则返回 401;未配置时保持开放(本地开发兼容)。
        """
        admin_token: Optional[str] = request.app.state.admin_token
        if admin_token is not None:
            if request.headers.get("X-Token") != admin_token:
                raise _error(
                    401,
                    "unauthorized",
                    "Missing or invalid X-Token header for /config/roots",
                )
        new_root = Path(body.path).expanduser().resolve()
        if not new_root.is_dir():
            raise _error(400, "invalid_root", f"Not a directory: {body.path}")
        roots: List[Path] = request.app.state.allowed_roots
        if new_root not in roots:
            roots.append(new_root)
        return {"roots": [str(r) for r in roots]}

    @app.post("/tools/video_meta")
    def video_meta(body: VideoMetaRequest, request: Request) -> Dict[str, Any]:
        video = _resolve(request, body.video_path)
        meta = _meta_or_error(video)
        return {
            "duration_s": meta["duration_sec"],
            "fps": meta["fps"],
            "width": meta["width"],
            "height": meta["height"],
            "frame_count": meta["frame_count"],
        }

    @app.post("/tools/extract_frames")
    def extract_frames(
        body: ExtractFramesRequest, request: Request
    ) -> Dict[str, Any]:
        video = _resolve(request, body.video_path)
        meta = _meta_or_error(video)
        truncated = False
        if body.timestamps:
            # timestamps mode: explicit moments, tight cap.
            max_frames = max(
                1, min(_HARD_MAX_FRAMES, body.max_frames or _DEFAULT_MAX_FRAMES)
            )
            timestamps = [max(0.0, float(ts)) for ts in body.timestamps]
            truncated = len(timestamps) > max_frames
            timestamps = timestamps[:max_frames]
            quality = _EXTRACT_JPEG_QUALITY
        elif body.fps is not None:
            # fps mode: uniform sampling across the whole video at ~`fps`
            # frames per second; loose cap, lower JPEG quality.
            max_frames = max(
                1,
                min(_FPS_MODE_MAX_FRAMES, body.max_frames or _FPS_MODE_MAX_FRAMES),
            )
            duration = float(meta["duration_sec"] or 0)
            step = 1.0 / body.fps
            n = int(duration * body.fps)
            truncated = n > max_frames
            timestamps = [step * i for i in range(min(n, max_frames))]
            quality = _FPS_MODE_JPEG_QUALITY
        else:
            # count mode: even spread over the whole video, tight cap.
            max_frames = max(
                1, min(_HARD_MAX_FRAMES, body.max_frames or _DEFAULT_MAX_FRAMES)
            )
            n = body.count if body.count and body.count > 0 else max_frames
            n = min(n, max_frames)
            timestamps = _even_timestamps(float(meta["duration_sec"] or 0), n)
            quality = _EXTRACT_JPEG_QUALITY
        frames: List[Dict[str, Any]] = []
        for ts in timestamps:
            data = read_frame_jpeg(video, _frame_index(meta, ts))
            if data is None:
                continue
            compact = _reencode_jpeg(data, quality)
            if compact is None:
                continue
            frames.append(
                {
                    "timestamp": ts,
                    "jpeg_base64": base64.b64encode(compact).decode("ascii"),
                    "width": meta["width"],
                    "height": meta["height"],
                }
            )
        return {"frames": frames, "truncated": truncated}

    @app.post("/tools/draw_boxes")
    def draw_boxes(body: DrawBoxesRequest, request: Request) -> Dict[str, Any]:
        from PIL import ImageDraw

        video = _resolve(request, body.video_path)
        meta = _meta_or_error(video)
        data = read_frame_jpeg(video, _frame_index(meta, body.timestamp))
        if data is None:
            raise _error(
                404, "frame_unavailable", f"No frame at timestamp {body.timestamp}"
            )
        img = load_image(data)
        width, height = img.size
        draw = ImageDraw.Draw(img)
        font = _load_scaled_font(16)
        for box in body.boxes:
            label = (box.label or "target")[:40]
            color = _PALETTE[zlib.crc32(label.encode("utf-8")) % len(_PALETTE)]
            px = _norm_to_px([box.x1, box.y1, box.x2, box.y2], width, height)
            px = [
                max(0, min(width - 1, px[0])),
                max(0, min(height - 1, px[1])),
                max(0, min(width - 1, px[2])),
                max(0, min(height - 1, px[3])),
            ]
            draw.rectangle(px, outline=color, width=3)
            _draw_text_with_background(
                draw, label, (px[0], max(0, px[1] - 18)), background=color, font=font
            )
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=_ANNOTATED_JPEG_QUALITY)
        return {
            "jpeg_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
            "width": width,
            "height": height,
        }

    @app.post("/tools/prepare_video")
    def prepare_video(
        body: PrepareVideoRequest, request: Request
    ) -> Dict[str, Any]:
        """Size gate for direct video upload: transcode-downshift if oversized.

        <= max_mb: pass through untouched; else re-encode with ffmpeg along the
        fps ladder (duration preserved, audio dropped) until under the cap.
        """
        video = _resolve(request, body.video_path)
        meta = _meta_or_error(video)
        max_bytes = int(body.max_mb * 1024 * 1024)
        duration_s = meta["duration_sec"]
        size = video.stat().st_size
        if size <= max_bytes:
            return {
                "path": str(video),
                "size_bytes": size,
                "fps": meta["fps"],
                "duration_s": duration_s,
                "transcoded": False,
            }
        if shutil.which("ffmpeg") is None:
            raise _error(500, "tool_unavailable", "ffmpeg not found in PATH")
        out_dir = _transcode_dir(request.app.state.allowed_roots, video)
        for fps in _fps_candidates(float(meta["fps"] or 0)):
            tag = f"{fps:g}"
            out = out_dir / f"{video.stem}_fps{tag}.mp4"
            if not (out.is_file() and out.stat().st_size < max_bytes):
                proc = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(video),
                        "-vf",
                        f"fps={tag}",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "28",
                        "-an",
                        str(out),
                    ],
                    capture_output=True,
                )
                if proc.returncode != 0:
                    continue
            if out.is_file() and out.stat().st_size < max_bytes:
                return {
                    "path": str(out),
                    "size_bytes": out.stat().st_size,
                    "fps": fps,
                    "duration_s": duration_s,
                    "transcoded": True,
                }
        raise _error(
            422,
            "transcode_failed",
            f"All fps ladder candidates stayed >= {body.max_mb} MB: {video}",
        )

    # --- /tools/track_suspects:疑似目标定向跟踪 --------------------------

    def _get_tracking_engine(request: Request) -> Any:
        """懒构建并缓存 VLMInferenceEngine(配置来自 ConfigManager 默认目录)。

        单独成函数以便测试注入 mock:monkeypatch server._build_engine。
        """
        if getattr(request.app.state, "tracking_engine", None) is None:
            request.app.state.tracking_engine = _build_default_engine()
        return request.app.state.tracking_engine

    @app.post("/tools/track_suspects")
    def track_suspects(body: TrackSuspectsRequest, request: Request) -> Dict[str, Any]:
        video = _resolve(request, body.video_path)
        meta = _meta_or_error(video)
        del meta
        anchors = [
            SuspectAnchor(
                box=[s.x1, s.y1, s.x2, s.y2],
                timestamp=s.timestamp,
                description=s.description,
            )
            for s in body.suspects
        ]
        time_range = (
            [float(body.time_range[0]), float(body.time_range[1])]
            if body.time_range
            else None
        )

        # 磁盘结果缓存:键 = (视频解析路径, 规范化锚点集合),描述不进键
        root = next(r for r in request.app.state.allowed_roots if video.is_relative_to(r))
        cache_dir = root / ".agent" / "tracks" / "_cache"
        key = tracking_cache.cache_key(video.resolve(), anchors)
        cached = tracking_cache.load_cached(cache_dir, key)
        if cached is not None:
            return dict(cached)

        # debug bundle 目录:<允许根>/.agent/tracks/<stem>/<ts>/
        out_dir = root / ".agent" / "tracks" / video.stem / _timestamp_tag()
        try:
            engine = _get_tracking_engine(request)
            result = tracking_windows.run_tracking(
                engine,
                video,
                anchors,
                time_range=time_range,
                out_dir=out_dir,
                deadline=time.monotonic() + _TRACK_TIMEOUT_S,
            )
        except tracking_windows.TrackingFailure as exc:
            result = {"failed": True, "failure_reason": str(exc)}

        clips = result.pop("clip", None)
        csvs = result.pop("csv", None)
        artifacts_dir = result.pop("artifacts_dir", None)
        rel_dir = _rel_to_root(root, artifacts_dir) if artifacts_dir is not None else None
        rel_dir_str = str(rel_dir) if rel_dir is not None else None
        payload = {
            "tracks": result.get("tracks", []),
            "annotated_image": result.get("annotated_image"),
            "artifacts": {
                "dir": rel_dir_str,
                "clip": f"{rel_dir}/{clips}" if (rel_dir_str and clips) else None,
                "csv": f"{rel_dir}/{csvs}" if (rel_dir_str and csvs) else None,
            },
            "failed": bool(result.get("failed", False)),
            "failure_reason": result.get("failure_reason"),
            "env_flow": result.get("env_flow"),
            "fps_used": result.get("fps_used"),
            "events": result.get("events", []),
        }
        if not payload["failed"] and payload["tracks"]:
            tracking_cache.store_cached(cache_dir, key, payload)
        return payload

    return app


_TRACK_TIMEOUT_S = 900.0  # track_suspects 同步执行总超时(秒)


def _timestamp_tag() -> str:
    """产物目录时间戳标签:YYYYmmdd_HHMMSS。"""
    return time.strftime("%Y%m%d_%H%M%S")


def _rel_to_root(root: Path, path: Path) -> Optional[Path]:
    """产物绝对路径转相对允许根的展示路径;转换失败返回原路径。"""
    try:
        return path.relative_to(root)
    except ValueError:
        return path


def _build_default_engine() -> Any:
    """从 ConfigManager 默认配置目录构建生产用 VLMInferenceEngine。

    构建失败(token 未配置等)抛异常,由端点统一转 failed:true 响应;
    缓存走引擎自带 .vlm_cache.db 磁盘缓存约定路径。
    """
    from traffic_analyzer.core.config_manager import ConfigManager
    from traffic_analyzer.core.vlm_engine import VLMInferenceEngine

    default_config_dir = Path(__file__).resolve().parents[1] / "config"
    config_manager = ConfigManager(str(default_config_dir))
    system_config = config_manager.load_all()
    disk_cache_path = Path(os.environ.get("TRAFFIC_ANALYZER_HOME", ".")) / ".vlm_cache.db"
    for provider in system_config.llm_providers:
        provider.disk_cache_path = str(disk_cache_path)
    return VLMInferenceEngine(system_config.llm_providers)
