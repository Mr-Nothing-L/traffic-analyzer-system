"""RAG 检索 / 建库路由:POST /api/rag/search、/api/rag/build(/status、/cancel)。

[文件说明]
作用:把 RAG 库四种检索(text/related/adjacent/site)与建库能力暴露给前端;进程内
直调 traffic_analyzer.rag.query / rag.build(不走 HTTP/toolserver),工作区取 web 层
当前激活工作区(require_workspace);库不存在返回 404 + 引导文案(提示先运行
scripts/build_rag_index.py);site 模式的桩号经 query 传入。建库在后台 daemon 线程
跑(纯 HTTP IO),单进程全局同时只跑一个(重复启动 409),状态存模块级 dict(锁保护),
支持 cancel 置标志(条间停止,partial 标记)。
上游:web/app.py(挂载路由);traffic_analyzer/rag/query.py(检索逻辑)、rag/build.py(建库)。
下游:<workspace>/.agent/rag/vectors.db(检索只读,建库写入)。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from traffic_analyzer.rag import query as rag_query
from traffic_analyzer.rag.build import build_index, list_pending
from traffic_analyzer.rag.store import RagStore
from traffic_analyzer.web.workspace import require_workspace

router = APIRouter()


class RagSearchRequest(BaseModel):
    """RAG 检索请求;契约与 toolserver /tools/search_videos 一致。"""

    mode: str  # text | related | adjacent | site
    query: Optional[str] = None
    video: Optional[str] = None
    k: int = Field(default=10, ge=1, le=100)
    alpha: float = Field(default=0.6, ge=0.0, le=1.0)
    only_confirmed: bool = False
    human_edited: bool = False
    gap_s: float = Field(default=600.0, ge=0.0)
    direction: Optional[str] = None
    before: Optional[Any] = None  # epoch 秒或 ISO 时间
    after: Optional[Any] = None

    @model_validator(mode="after")
    def _check_mode_params(self) -> "RagSearchRequest":
        if self.mode not in rag_query.MODES:
            raise ValueError(f"unknown mode: {self.mode}")
        if self.mode in ("text", "site") and not self.query:
            raise ValueError(f"mode={self.mode} requires query")
        if self.mode in ("related", "adjacent") and not self.video:
            raise ValueError(f"mode={self.mode} requires video")
        return self


@router.post("/api/rag/search")
def rag_search(body: RagSearchRequest, request: Request) -> Dict[str, Any]:
    workspace = require_workspace(request)
    try:
        return rag_query.run_search(
            workspace,
            body.mode,
            query=body.query,
            video=body.video,
            k=body.k,
            alpha=body.alpha,
            only_confirmed=body.only_confirmed,
            human_edited=body.human_edited,
            gap_s=body.gap_s,
            direction=body.direction,
            before=body.before,
            after=body.after,
        )
    except rag_query.RagIndexNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except rag_query.RagQueryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


# ---------------------------------------------------------------------------
# 建库:后台线程 + 模块级状态(单进程单构建)
# ---------------------------------------------------------------------------

_build_lock = threading.Lock()
_build_cancel = threading.Event()
_build_state: Dict[str, Any] = {
    "running": False,
    "done": 0,
    "total": 0,
    "failed": 0,
    "started_at": None,
    "finished_at": None,
    "last_error": None,
    "partial": False,
}


class RagBuildRequest(BaseModel):
    """建库请求;body 可整体省略(用默认值)。"""

    concurrency: int = Field(default=8, ge=1, le=64)
    refresh_annotations: bool = False


def _library_info(workspace) -> Dict[str, Any]:
    """当前工作区库概况;库不存在返回 exists=False(不创建 db 文件)。"""
    db_path = workspace / ".agent" / "rag" / "vectors.db"
    if not db_path.is_file():
        return {"exists": False, "count": 0, "built_at": None}
    with RagStore(workspace) as store:
        stats = store.stats()
    try:
        built_at = float(stats["meta"].get("built_at") or 0) or None
    except (TypeError, ValueError):
        built_at = None
    return {"exists": True, "count": stats["total"], "built_at": built_at}


def _build_progress(done: int, total: int, failed: int) -> None:
    with _build_lock:
        _build_state.update(done=done, total=total, failed=failed)


def _build_worker(workspace, concurrency: int, refresh_annotations: bool) -> None:
    try:
        result = build_index(
            workspace,
            concurrency=concurrency,
            refresh_annotations=refresh_annotations,
            progress_cb=_build_progress,
            cancel_flag=_build_cancel.is_set,
        )
        with _build_lock:
            _build_state["failed"] = len(result["failed"])
            _build_state["partial"] = bool(result["partial"])
    except Exception as exc:  # noqa: BLE001 — 构建线程任何异常进 last_error,不裸死
        with _build_lock:
            _build_state["last_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        with _build_lock:
            _build_state["running"] = False
            _build_state["finished_at"] = time.time()


@router.post("/api/rag/build")
def rag_build(request: Request, body: Optional[RagBuildRequest] = None) -> Dict[str, Any]:
    workspace = require_workspace(request)
    body = body or RagBuildRequest()
    _, _, pending = list_pending(
        workspace, only_missing=True, refresh_annotations=body.refresh_annotations
    )
    with _build_lock:
        if _build_state["running"]:
            raise HTTPException(status_code=409, detail="build already running")
        _build_cancel.clear()
        _build_state.update(
            running=True,
            done=0,
            total=len(pending),
            failed=0,
            started_at=time.time(),
            finished_at=None,
            last_error=None,
            partial=False,
        )
    threading.Thread(
        target=_build_worker,
        args=(workspace, body.concurrency, body.refresh_annotations),
        daemon=True,
    ).start()
    return {"started": True, "total": len(pending)}


@router.get("/api/rag/build/status")
def rag_build_status(request: Request) -> Dict[str, Any]:
    workspace = require_workspace(request)
    with _build_lock:
        state = dict(_build_state)
    state["library"] = _library_info(workspace)
    # 空闲时给出待更新条目数(新视频 + 标注变更需重算),供前端禁用空转按钮;
    # 构建中不重复扫描,以 state.total 为准。
    if state["running"]:
        state["pending"] = None
    else:
        try:
            _, _, pending = list_pending(
                workspace, only_missing=True, refresh_annotations=True
            )
            state["pending"] = len(pending)
        except Exception:  # noqa: BLE001 — 扫描失败不阻断状态查询
            state["pending"] = None
    return state


@router.post("/api/rag/build/cancel")
def rag_build_cancel() -> Dict[str, Any]:
    _build_cancel.set()
    return {"cancelling": True}
