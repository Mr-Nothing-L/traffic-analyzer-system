"""Infer/job API routes for the web UI (split from the old monolithic jobs.py).

[文件说明]
作用:任务路由层。/api/infer(同 stem 的 queued/running infer 去重,重复
409;提交前持 evidence_api._put_locks[stem],与 PUT 同一把锁、统一锁顺序
_put_locks → JobManager._lock)、/api/jobs、/api/jobs/{id}/cancel。
build_infer_command 经 jobs 包命名空间延迟查找,测试 monkeypatch
traffic_analyzer.web.jobs.build_infer_command 即生效。
上游:web/app.py(挂载 router);web/jobs/__init__.py(聚合导出)。
下游:web/jobs/queue.py(JobManager/Job/build_infer_command)、
web/evidence_api.py(_put_locks 与 find_active_infer_job)、web/workspace.py。
"""

from __future__ import annotations

import contextlib
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from traffic_analyzer.web import evidence_api as evidence_api_mod
from traffic_analyzer.web import jobs as _jobs_pkg
from traffic_analyzer.web import workspace as workspace_mod

router = APIRouter()


class InferRequest(BaseModel):
    stems: Optional[List[str]] = None  # legacy: top-level video stems
    rels: Optional[List[str]] = None   # workspace-relative video paths (any depth)


@router.post("/api/infer")
def post_infer(body: InferRequest, request: Request) -> Dict[str, Any]:
    workspace = workspace_mod.require_workspace(request)
    # Validate everything before queueing anything.
    rels: List[str] = list(body.rels or [])
    for stem in body.stems or []:  # legacy stems map to top-level rels
        workspace_mod.validate_stem(stem)
        video = workspace_mod.find_video(workspace, stem)
        if video is None:
            raise HTTPException(status_code=404, detail=f"Video not found for stem: {stem}")
        rels.append(video.name)

    videos: List[tuple] = []
    seen_stems: Dict[str, str] = {}
    for rel in rels:
        video = workspace_mod.resolve_workspace_file(workspace, rel)
        if video.suffix.lower() not in workspace_mod.VIDEO_EXTENSIONS:
            raise HTTPException(status_code=404, detail=f"Not a video file: {rel}")
        stem = video.stem
        if stem in seen_stems:
            # Results land in the flat analysis/<stem>/ — two rels sharing a
            # stem would overwrite each other, so reject the whole request.
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Stem 冲突:{stem} — {seen_stems[stem]} 与 {rel} "
                    "会写入同一个 analysis 目录,请重命名其中一个视频"
                ),
            )
        seen_stems[stem] = rel
        videos.append((stem, rel))

    # 与 PUT 同一把 per-stem 锁(evidence_api._put_locks),锁顺序统一为
    # _put_locks[stem] → JobManager._lock,闭合「检查无在跑 infer」与
    # 「提交 infer」之间的 TOCTOU 窗口;多 stem 按字典序取锁避免互相死锁。
    with contextlib.ExitStack() as stack:
        for stem in sorted(seen_stems):
            stack.enter_context(evidence_api_mod._put_locks[stem])
        for stem in seen_stems:
            active = evidence_api_mod.find_active_infer_job(request, stem)
            if active is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Inference job #{active.get('id')} for '{stem}' is "
                        f"{active.get('status')}; retry after it finishes"
                    ),
                )
        job_ids: List[int] = []
        for stem, rel in videos:
            workspace_mod.analysis_dir(workspace, stem).mkdir(parents=True, exist_ok=True)
            argv = _jobs_pkg.build_infer_command(workspace, rel, stem)
            job_ids.append(
                request.app.state.jobs.submit(
                    "infer", argv, stem=stem, rel=rel, workspace=workspace
                )
            )
    return {"job_ids": job_ids}


@router.get("/api/jobs")
def get_jobs(request: Request) -> List[Dict[str, Any]]:
    return request.app.state.jobs.list_jobs()


@router.post("/api/jobs/{job_id}/cancel")
def post_cancel_job(job_id: int, request: Request) -> Dict[str, Any]:
    try:
        job = request.app.state.jobs.cancel(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown job id: {job_id}")
    except ValueError:
        raise HTTPException(status_code=409, detail="Job already finished")
    return job.to_dict()
