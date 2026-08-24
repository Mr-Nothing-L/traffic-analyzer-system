"""Quick-chat HTTP routes (per-IP state, upload, ask SSE, files).

[文件说明]
作用:快速对话路由。GET /api/chat/state 返回当前信源(display_name + 可预览
文件 URL)与消息列表(images 转 /api/chat/files/<name> URL);POST /api/chat/upload
接收视频(恰1个)或图片(≥1张,混合 400,扩展名白名单,单文件 ≤500MB 流式写盘,
超限 413)写入 output/chat_uploads/incoming/ 并切换信源;POST /api/chat/ask
SSE 流式问答(text/event-stream,异常兜底 error 事件);DELETE /api/chat/history
清空(204);GET /api/chat/files/{name} 提供上传/产出图(限制在 chat_uploads 根内)。
状态/消息按 IP 隔离(auth._request_ip)。切换信源写 divider 消息「已切换到 …」。
上游:web/app.py(include_router)、前端快速对话面板。
下游:web/chat/store.py、qa.py、paths.py。
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from traffic_analyzer.web.auth import _request_ip
from traffic_analyzer.web.chat import paths, qa, store

logger = logging.getLogger(__name__)

router = APIRouter()

_VIDEO_EXT = (".mp4", ".avi", ".mov", ".mkv", ".ts")
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
_ALLOWED_EXT = _VIDEO_EXT + _IMAGE_EXT
_MAX_FILE_BYTES = 500 * 1024 * 1024


class AskRequest(BaseModel):
    question: str


def _file_url(abs_path: str) -> str | None:
    """Map an absolute path under the uploads root to its /api/chat/files URL."""
    try:
        rel = Path(abs_path).resolve().relative_to(paths.UPLOAD_DIR.resolve())
    except ValueError:
        return None
    return f"/api/chat/files/{rel.as_posix()}"


def _state_payload(ip: str) -> Dict[str, Any]:
    state = store.get_state(ip)
    source = None
    kind = state.get("source_kind")
    if kind:
        ref = state.get("source_ref") or {}
        if kind == "workspace_video":
            display_name = Path(ref.get("rel") or ref.get("path") or "").name
            file_urls = []
        else:
            display_name = "、".join(ref.get("names") or [])
            file_urls = [u for u in (_file_url(p) for p in ref.get("files") or []) if u]
        source = {"kind": kind, "display_name": display_name, "files": file_urls}
    messages = [
        {
            "role": m["role"],
            "content": m["content"],
            "think": m.get("think") or "",
            "images": [f"/api/chat/files/{n}" for n in m.get("images") or []],
            "created_at": m["created_at"],
        }
        for m in store.list_messages(ip)
    ]
    return {
        "source": source,
        "messages": messages,
        "has_summary": bool(state.get("summary")),
    }


@router.get("/api/chat/state")
def get_chat_state(request: Request) -> Dict[str, Any]:
    return _state_payload(_request_ip(request))


def _write_upload(file: UploadFile, dest: Path) -> None:
    """Stream one upload to disk (413 past the per-file size limit)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(dest, "wb") as out:
            shutil.copyfileobj(file.file, out, length=1024 * 1024)
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"upload write failed: {exc}")
    if dest.stat().st_size > _MAX_FILE_BYTES:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail="file exceeds 500MB limit")


@router.post("/api/chat/upload")
def upload_chat_files(
    request: Request, files: List[UploadFile] = File(...)
) -> Dict[str, Any]:
    """Switch source to an uploaded video (exactly 1) or images (>=1)."""
    ip = _request_ip(request)
    if not files:
        raise HTTPException(status_code=400, detail="no files uploaded")
    exts = [Path(f.filename or "").suffix.lower() for f in files]
    bad = [e for e in exts if e not in _ALLOWED_EXT]
    if bad:
        raise HTTPException(status_code=400, detail=f"unsupported file type: {bad[0]}")
    video_flags = [e in _VIDEO_EXT for e in exts]
    if any(video_flags):
        if len(files) != 1 or not all(video_flags):
            raise HTTPException(
                status_code=400, detail="video and images cannot be mixed"
            )
        kind = "upload_video"
    else:
        kind = "upload_images"

    saved: List[str] = []
    names: List[str] = []
    for file, ext in zip(files, exts):
        dest = paths.INCOMING_DIR / f"{uuid.uuid4().hex}{ext}"
        _write_upload(file, dest)
        saved.append(str(dest))
        names.append(file.filename or dest.name)

    store.set_source(ip, kind, {"files": saved, "names": names})
    store.add_message(ip, "divider", f"已切换到 {'、'.join(names)}")
    logger.info("[chat] upload source: ip=%s kind=%s files=%d", ip, kind, len(saved))
    return _state_payload(ip)


@router.post("/api/chat/ask")
def ask_chat(body: AskRequest, request: Request) -> StreamingResponse:
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must be non-empty")
    ip = _request_ip(request)

    def event_stream():
        try:
            for event in qa.ask(ip, question):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:
            logger.exception("[chat] ask failed: %s", exc)
            payload = json.dumps(
                {"type": "error", "message": str(exc)}, ensure_ascii=False
            )
            yield f"data: {payload}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.delete("/api/chat/history", status_code=204)
def clear_chat_history(request: Request) -> Response:
    store.clear(_request_ip(request))
    return Response(status_code=204)


@router.get("/api/chat/files/{name:path}")
def get_chat_file(name: str) -> FileResponse:
    """Serve a generated/uploaded file confined to output/chat_uploads/."""
    root = paths.UPLOAD_DIR.resolve()
    target = (root / name).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=404, detail="file not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target)
