"""SFT Forge web API 路由。

[文件说明]
作用:POST /api/sftforge/jobs({dir 或 videos[], max_rounds} → job_id,
  后台线程执行引擎)、GET /api/sftforge/jobs/{job_id}/events(SSE:先回放
  历史事件再收增量,round_started/round_completed/video_completed/
  job_completed|job_failed,终态事件后关流)、GET /api/sftforge/jobs/
  {job_id}/result(训练 JSONL 行 + 汇总统计 + quarantine 列表)。
  引擎经 service.build_engine 装配,测试 monkeypatch
  traffic_analyzer.web.sftforge.build_engine 注入 fake engine。
上游:web/app.py(挂载 router,app.state.sftforge = ForgeJobManager)。
下游:web/sftforge/service.py(ForgeJobManager/ForgeJob/format_sse)。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from traffic_analyzer.web.sftforge import service as service_mod
from traffic_analyzer.web.sftforge.service import TERMINAL_EVENTS, format_sse

router = APIRouter()

_HEARTBEAT_SEC = 15.0
_VIDEO_SUFFIXES = {".mp4", ".avi", ".mov", ".mkv"}


class CreateForgeJobRequest(BaseModel):
    """创建 SFT Forge 任务:dir 取目录下全部视频,或与 videos[] 组合。"""

    dir: Optional[str] = None
    videos: Optional[List[str]] = None
    max_rounds: int = Field(default=2, ge=0, le=10)


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
    job_id = _manager(request).submit(videos, body.max_rounds)
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
