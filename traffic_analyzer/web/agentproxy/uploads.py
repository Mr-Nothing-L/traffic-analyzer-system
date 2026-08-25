"""Agent chat file/video upload endpoints.

[文件说明]
作用:统一对话的视频/图片上传与预览(不透传到 TS agent,web 层本地处理;
经 agentproxy.routes 聚合挂在 /api/agent 前缀下):
- POST /uploads:multipart 字段 ``file`` → 落盘
  ``<当前工作区>/.agent/uploads/<yyyyMMdd_HHmmss>_<清洗后的文件名>``。
  文件名先剥掉路径成分(/ 与 \\),再只保留 [A-Za-z0-9._-](其余折叠为
  ``_``,首尾 ``.``/``_`` 去除),防路径穿越;同秒同名冲突时在扩展名前
  追加 -1/-2 避免静默覆盖。MIME 限定 video/* 与 image/*(否则 415);
  大小上限默认 500MB,可用环境变量 AGENT_UPLOAD_MAX_MB 覆盖(超限 413,
  已写入的残文件删除);未选工作区 → 400。成功返回
  {path(绝对路径), name(原始文件名), size, contentType}。
- GET /uploads/{name}:流式返回 .agent/uploads/ 下的文件(FileResponse
  自带 Range 支持,供前端 <video> 拖动预览);name 含路径成分或越出
  uploads 目录 → 403,不存在 → 404。
上游:web/agentproxy/routes.py(include_router 聚合进 /api/agent 前缀)。
下游:web/workspace/core.py WorkspaceState(当前工作区,与 routes.py 的
workspaceDir 注入同一来源)。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_UPLOAD_MB_ENV_VAR = "AGENT_UPLOAD_MAX_MB"
DEFAULT_MAX_UPLOAD_MB = 500.0
_ALLOWED_MIME_PREFIXES = ("video/", "image/")
_SAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_CHUNK_BYTES = 1024 * 1024


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


def _max_upload_bytes() -> int:
    """大小上限:AGENT_UPLOAD_MAX_MB(MB,可小数)> 默认 500MB;非法值回退默认。"""
    raw = os.environ.get(MAX_UPLOAD_MB_ENV_VAR)
    try:
        mb = float(raw) if raw else DEFAULT_MAX_UPLOAD_MB
    except ValueError:
        mb = DEFAULT_MAX_UPLOAD_MB
    if mb <= 0:
        mb = DEFAULT_MAX_UPLOAD_MB
    return int(mb * 1024 * 1024)


def _sanitize_filename(filename: str) -> str:
    """清洗文件名:剥掉路径成分,只保留 [A-Za-z0-9._-];空结果回退 'file'。"""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _SAFE_NAME_CHARS.sub("_", base).strip("._")
    return cleaned or "file"


def _unique_path(directory: Path, filename: str) -> Path:
    """同名冲突时在扩展名前追加 -1/-2...,避免同秒上传静默覆盖。"""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, sep, suffix = filename.rpartition(".")
    for index in range(1, 100):
        name = f"{stem}-{index}{sep}{suffix}" if sep else f"{filename}-{index}"
        candidate = directory / name
        if not candidate.exists():
            return candidate
    # 极端情况(同秒 100 个同名上传):落到异常,由 FastAPI 统一 500。
    raise RuntimeError(f"too many name collisions for upload: {filename}")


def _uploads_dir(request: Request) -> Path | None:
    """当前工作区的 .agent/uploads/;未选工作区返回 None。"""
    workspace = request.app.state.workspace.get()
    if workspace is None:
        return None
    return Path(workspace) / ".agent" / "uploads"


@router.post("/uploads")
async def upload_file(request: Request, file: UploadFile = File(...)) -> JSONResponse:
    """上传对话附件:清洗文件名后落盘 .agent/uploads/,返回绝对路径供 /chat 引用。"""
    uploads_dir = _uploads_dir(request)
    if uploads_dir is None:
        return _error(400, "no_workspace", "No workspace selected")
    content_type = (file.content_type or "").lower()
    if not content_type.startswith(_ALLOWED_MIME_PREFIXES):
        return _error(
            415,
            "unsupported_media_type",
            f"only video/* and image/* are allowed, got {content_type or 'unknown'}",
        )
    uploads_dir.mkdir(parents=True, exist_ok=True)
    original_name = file.filename or "file"
    target = _unique_path(
        uploads_dir, f"{datetime.now():%Y%m%d_%H%M%S}_{_sanitize_filename(original_name)}"
    )
    max_bytes = _max_upload_bytes()
    size = 0
    too_big = False
    with target.open("wb") as fh:
        while True:
            chunk = await file.read(_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                too_big = True
                break
            fh.write(chunk)
    if too_big:
        target.unlink(missing_ok=True)
        return _error(
            413, "file_too_large", f"upload exceeds the {max_bytes}-byte limit"
        )
    logger.info("agent upload saved: %s (%d bytes)", target, size)
    return JSONResponse(
        {
            "path": str(target.resolve()),
            "name": original_name,
            "size": size,
            "contentType": content_type,
        }
    )


@router.get("/uploads/{name}")
async def get_upload(name: str, request: Request) -> Any:
    """流式预览已上传文件;FileResponse 自带 Range 支持(206/Content-Range)。"""
    uploads_dir = _uploads_dir(request)
    if uploads_dir is None:
        return _error(400, "no_workspace", "No workspace selected")
    if name in ("", ".", "..") or "/" in name or "\\" in name:
        return _error(403, "forbidden", f"invalid upload name: {name!r}")
    base = uploads_dir.resolve()
    target = (base / name).resolve()
    if target.parent != base:  # 兜底:符号链接等越出 uploads 目录
        return _error(403, "forbidden", f"invalid upload name: {name!r}")
    if not target.is_file():
        return _error(404, "upload_not_found", f"unknown upload: {name}")
    return FileResponse(target)
