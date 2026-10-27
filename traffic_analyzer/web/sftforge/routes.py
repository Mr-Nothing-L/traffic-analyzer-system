"""SFT Forge web API 路由。

[文件说明]
作用:POST /api/sftforge/jobs({dir 或 videos[], max_rounds} → job_id,
  后台线程执行引擎)、GET /api/sftforge/jobs/{job_id}/events(SSE:先回放
  历史事件再收增量,round_started/round_completed/video_completed/
  job_completed|job_failed,终态事件后关流)、GET /api/sftforge/jobs/
  {job_id}/result(训练 JSONL 行 + 汇总统计 + quarantine 列表)、
  GET/DELETE /api/sftforge/store(当前工作区持久化结果的读/删,任务终态
  由 service 落盘,见 store.py)。
  引擎经 service.build_engine 装配,测试 monkeypatch
  traffic_analyzer.web.sftforge.build_engine 注入 fake engine。
上游:web/app.py(挂载 router,app.state.sftforge = ForgeJobManager)。
下游:web/sftforge/service.py(ForgeJobManager/ForgeJob/format_sse)。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from traffic_analyzer.web.sftforge import service as service_mod
from traffic_analyzer.web.sftforge import store as store_mod
from traffic_analyzer.web.sftforge.service import TERMINAL_EVENTS, format_sse
from traffic_analyzer.web.workspace import require_workspace

router = APIRouter()

_HEARTBEAT_SEC = 15.0
_VIDEO_SUFFIXES = {".mp4", ".avi", ".mov", ".mkv"}


class CreateForgeJobRequest(BaseModel):
    """创建 SFT Forge 任务:dir 取目录下全部视频,或与 videos[] 组合。"""

    dir: Optional[str] = None
    videos: Optional[List[str]] = None
    max_rounds: int = Field(default=2, ge=0, le=10)
    thinking: Literal["off", "low", "medium", "high"] = "medium"
    # 初稿链路:pipeline=批量推理链路(专家+裁决+改写);prompt=单次 forward
    draft: Literal["pipeline", "prompt"] = "pipeline"


def _manager(request: Request) -> service_mod.ForgeJobManager:
    return request.app.state.sftforge


def _resolve_videos(body: CreateForgeJobRequest) -> List[Path]:
    videos: List[Path] = []
    if body.dir:
        src = Path(body.dir).expanduser()
        if not src.is_dir():
            raise HTTPException(status_code=400, detail=f"目录不存在: {body.dir}")
        videos.extend(
            sorted(p for p in src.iterdir() if p.suffix.lower() in _VIDEO_SUFFIXES)
        )
    for item in body.videos or []:
        path = Path(item).expanduser()
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"视频不存在: {item}")
        if path.suffix.lower() not in _VIDEO_SUFFIXES:
            raise HTTPException(status_code=400, detail=f"不是视频文件: {item}")
        videos.append(path)
    # 去重保序(按绝对路径)。
    seen = set()
    unique: List[Path] = []
    for v in videos:
        key = str(v.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(v)
    if not unique:
        raise HTTPException(status_code=400, detail="没有可处理的视频(dir 或 videos 至少其一)")
    return unique


@router.post("/api/sftforge/jobs")
def post_sftforge_job(body: CreateForgeJobRequest, request: Request) -> Dict[str, Any]:
    videos = _resolve_videos(body)
    job_id = _manager(request).submit(videos, body.max_rounds, body.thinking, body.draft)
    return {"job_id": job_id, "videos": [str(v) for v in videos]}


@router.get("/api/sftforge/jobs/{job_id}/events")
async def stream_sftforge_events(job_id: int, request: Request) -> StreamingResponse:
    """逐任务 SSE:回放历史 + 实时增量,收到终态事件后结束流。"""
    manager = _manager(request)
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown sftforge job id: {job_id}")
    q, snapshot = manager.subscribe(job)

    async def _gen() -> Any:
        try:
            terminal = False
            for event in snapshot:
                yield format_sse(event)
                terminal = event["type"] in TERMINAL_EVENTS
            while not terminal:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_SEC)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield format_sse(event)
                terminal = event["type"] in TERMINAL_EVENTS
        finally:
            manager.unsubscribe(job, q)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/sftforge/jobs/{job_id}/result")
def get_sftforge_result(job_id: int, request: Request) -> Dict[str, Any]:
    job = _manager(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown sftforge job id: {job_id}")
    return service_mod.job_result_payload(job)


@router.get("/api/sftforge/store")
def get_sftforge_store(request: Request) -> Dict[str, Any]:
    """读当前工作区的持久化结果(任务终态落盘;无缓存 404)。"""
    payload = store_mod.load_result(require_workspace(request))
    if payload is None:
        raise HTTPException(status_code=404, detail="No stored sftforge result")
    return payload


@router.delete("/api/sftforge/store")
def delete_sftforge_store(request: Request) -> Dict[str, Any]:
    """删除当前工作区的持久化结果缓存(无缓存也算成功:目标状态一致)。"""
    deleted = store_mod.delete_result(require_workspace(request))
    return {"deleted": deleted}
