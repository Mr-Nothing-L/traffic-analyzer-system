"""FastAPI application factory and /tools/* endpoints.

[文件说明]
作用:三个 POST JSON 端点:/tools/video_meta、/tools/extract_frames、
    /tools/draw_boxes。所有 video_path 解析后必须落在允许根(allowed_roots)
    之内,越界返回 403;错误统一为 {"error": {"code", "message"}}。
    允许根:启动 --workspace 为初始根,运行期可经 POST /config/roots
    热注册新工作区(web 层切换工作区时调用,免重启)。
上游:toolserver/__init__.py(create_app 导出);__main__.py(uvicorn 启动)。
下游:web/frames.read_video_meta/read_frame_jpeg(CV 复用);
    utils/image_drawing(load_image/_draw_text_with_background/_load_scaled_font);
    utils/bbox_geometry._norm_to_px。
"""

from __future__ import annotations

import base64
import io
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from traffic_analyzer.utils.bbox_geometry import _norm_to_px
from traffic_analyzer.utils.image_drawing import (
    _draw_text_with_background,
    _load_scaled_font,
    load_image,
)
from traffic_analyzer.web.frames import read_frame_jpeg, read_video_meta

# Frame extraction limits.
_DEFAULT_MAX_FRAMES = 4
_HARD_MAX_FRAMES = 8
# JPEG quality: extracted frames are fed to the VLM, keep them compact;
# annotated frames go back to the user, keep them readable.
_EXTRACT_JPEG_QUALITY = 70
_ANNOTATED_JPEG_QUALITY = 85

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
    count: Optional[int] = None
    max_frames: int = _DEFAULT_MAX_FRAMES


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
        """热注册一个允许根(须为已存在目录);幂等,返回当前根列表。"""
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
        max_frames = max(1, min(_HARD_MAX_FRAMES, body.max_frames))
        if body.timestamps:
            timestamps = [max(0.0, float(ts)) for ts in body.timestamps][:max_frames]
        else:
            n = body.count if body.count and body.count > 0 else max_frames
            n = min(n, max_frames)
            timestamps = _even_timestamps(float(meta["duration_sec"] or 0), n)
        frames: List[Dict[str, Any]] = []
        for ts in timestamps:
            data = read_frame_jpeg(video, _frame_index(meta, ts))
            if data is None:
                continue
            compact = _reencode_jpeg(data, _EXTRACT_JPEG_QUALITY)
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
        return {"frames": frames}

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

    return app
