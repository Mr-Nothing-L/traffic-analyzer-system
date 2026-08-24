"""Quick-chat HTTP routes (per-IP state, upload, ask SSE, files).

[文件说明]
作用:快速对话路由。GET /api/chat/state 返回当前信源(display_name + 可预览
文件 URL)与消息列表(含消息 id,images 转 /api/chat/files/<name> URL);POST /api/chat/upload
接收视频(恰1个)或图片(≥1张,混合 400,扩展名白名单,单文件 ≤500MB 流式写盘,
超限 413,落盘名 <uuid>_<安全化原名>;视频写盘后 best-effort 生成中间帧封面
<同名>.thumb.jpg,失败仅 log)写入 output/chat_uploads/incoming/ 并切换信源;POST /api/chat/ask
SSE 流式问答(text/event-stream,异常兜底 error 事件;body 可带 attachments
相对名,校验在上传根内且存在后随 user 消息落库,供气泡内展示附件);DELETE /api/chat/history
清空(204);POST /api/chat/messages/delete 撤回一条消息及其后的 assistant
回复(非本 IP 消息 404,成功 204);GET /api/chat/files/{name} 提供上传/产出图(限制在 chat_uploads 根内);
GET /api/chat/video/{name} 视频流播放(同 /api/videos/{stem}/stream:Range +
非浏览器兼容编码按需 ffmpeg 转 faststart H.264,复用 video_stream._stream_response)。
状态/消息按 IP 隔离(auth._request_ip)。切换信源写 divider 消息「已切换到 …」。
上游:web/app.py(include_router)、前端快速对话面板。
下游:web/chat/store.py、qa.py、paths.py;web/video_stream.py(视频流/转码)。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from traffic_analyzer.web.auth import _request_ip
from traffic_analyzer.web.chat import paths, qa, store
from traffic_analyzer.web.frames import read_frame_jpeg, read_video_meta
from traffic_analyzer.web.video_stream import _stream_response

logger = logging.getLogger(__name__)

router = APIRouter()

_VIDEO_EXT = (".mp4", ".avi", ".mov", ".mkv", ".ts")
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
_ALLOWED_EXT = _VIDEO_EXT + _IMAGE_EXT
_MAX_FILE_BYTES = 500 * 1024 * 1024


class AskRequest(BaseModel):
    question: str
    """chat-files 相对名(如 incoming/<uuid>.png),随 user 消息落库用于气泡展示。"""
    attachments: List[str] = []


class DeleteMessageRequest(BaseModel):
    id: int


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
            "id": m["id"],
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


def _dest_name(original: str, ext: str) -> str:
    """``<uuid>_<安全化原始文件名><ext>``:非法字符替换为 _,原名截断 ~60 字符。

    前端展示时剥掉 uuid 前缀(取第一个 _ 之后);历史无原名文件按全显兜底。
    """
    stem = Path(original).stem
    safe = re.sub(r"[^\w.-]", "_", stem)[:60].strip("._") or "file"
    return f"{uuid.uuid4().hex}_{safe}{ext}"


def _write_video_thumb(dest: Path) -> None:
    """Best-effort cover thumbnail → ``<dest 去扩展名>.thumb.jpg``(中间帧,失败退第 0 帧)。

    失败仅 log,不影响上传;前端约定同路径换 .thumb.jpg 扩展名取封面,
    不存在时回退文件 chip。缩略图由现有 /api/chat/files/{name} 直接服务。
    """
    try:
        meta = read_video_meta(dest)
        if not meta:
            logger.warning("[chat] video thumb skipped (meta unreadable): %s", dest)
            return
        index = int(meta["frame_count"]) // 2
        jpeg = read_frame_jpeg(dest, index) or read_frame_jpeg(dest, 0)
        if not jpeg:
            logger.warning("[chat] video thumb skipped (frame unreadable): %s", dest)
            return
        dest.with_suffix(".thumb.jpg").write_bytes(jpeg)
    except Exception as exc:
        logger.warning("[chat] video thumb failed %s: %s", dest, exc)


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
        dest = paths.INCOMING_DIR / _dest_name(file.filename or "", ext)
        _write_upload(file, dest)
        if kind == "upload_video":
            _write_video_thumb(dest)
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
    # 附件白名单:仅保留解析后仍在上传根内且真实存在的相对名(防穿越,静默丢弃)
    root = paths.UPLOAD_DIR.resolve()
    attachments = []
    for name in body.attachments:
        target = (root / name).resolve()
        if target != root and root in target.parents and target.is_file():
            attachments.append(Path(name).as_posix())

    def event_stream():
        try:
            for event in qa.ask(ip, question, tuple(attachments)):
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


@router.post("/api/chat/messages/delete", status_code=204)
def delete_chat_message(body: DeleteMessageRequest, request: Request) -> Response:
    """Recall one message and its assistant reply (own-IP only, else 404)."""
    ok = store.delete_message_and_reply(_request_ip(request), body.id)
    if not ok:
        raise HTTPException(status_code=404, detail="message not found")
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


@router.get("/api/chat/video/{name:path}")
def get_chat_video(name: str, ss: Optional[float] = Query(None, ge=0)) -> object:
    """Stream a chat-upload video, confined to output/chat_uploads/.

    Same behavior as /api/videos/{stem}/stream (video_stream._stream_response):
    browser-native codecs served directly with Range; H.265/.ts/.mkv etc.
    transcoded on demand to faststart H.264 MP4 (LRU-cached, 501/503 on
    ffmpeg failure/overload).
    """
    root = paths.UPLOAD_DIR.resolve()
    target = (root / name).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=404, detail="file not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return _stream_response(target, ss)
