"""Precision-evaluation endpoints backed by ``scripts/batch_evaluate.py``.

The evaluation runs as a serial job; its JSON output lands at
``<workspace>/analysis/evaluation/latest.json`` and is served verbatim.

[文件说明]
作用:精度评估接口。POST /api/evaluate 将 scripts/batch_evaluate.py 作为串行任务提交
(命令由 jobs.build_evaluate_command 构造),结果写入
<workspace>/analysis/evaluation/latest.json;GET /api/evaluate/latest 原样返回该 JSON。
上游:web/app.py(挂载路由);web/static 前端(评估页)。
下游:web/jobs.py(任务提交与命令构造)、web/workspace.py(结果存在性检查)、
scripts/batch_evaluate.py。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from traffic_analyzer.web import jobs as jobs_mod
from traffic_analyzer.web import workspace as workspace_mod

router = APIRouter()


def latest_path(workspace: Path) -> Path:
    return workspace / "analysis" / "evaluation" / "latest.json"


def _has_analysis_results(workspace: Path) -> bool:
    analysis = workspace / "analysis"
    if not analysis.is_dir():
        return False
    return any(workspace_mod.has_results(workspace, child.name) for child in analysis.iterdir())


@router.post("/api/evaluate")
def post_evaluate(request: Request) -> Dict[str, Any]:
    workspace = workspace_mod.require_workspace(request)
    if not _has_analysis_results(workspace):
        raise HTTPException(status_code=400, detail="No analysis results to evaluate")
    argv = jobs_mod.build_evaluate_command(workspace)
    job_id = request.app.state.jobs.submit("evaluate", argv)
    return {"job_id": job_id}


@router.get("/api/evaluate/latest")
def get_evaluate_latest(request: Request) -> Dict[str, Any]:
    workspace = workspace_mod.require_workspace(request)
    path = latest_path(workspace)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No evaluation result yet")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=404,
            detail="Evaluation result unreadable (previous run died mid-write)",
        )
