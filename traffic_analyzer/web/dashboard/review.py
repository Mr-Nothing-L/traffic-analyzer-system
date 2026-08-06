"""Dashboard review endpoint (split from the old monolithic dashboard.py).

[文件说明]
作用:行内审核。PUT /api/dashboard/review 校验三态
(unconfirmed/confirmed/needs_review,非法 422)并原子写
<workspace>/analysis/review_states.json({stem: {status, updated_at, by}},
by = request.state.user,与 evidence_api._atomic_write_json 同一原子写);
落盘后 invalidate_caches 并经 realtime.publish_from_app 发布
dashboard.changed。_load_review_states 供 metrics 行构建合并审核态
(文件缺失/损坏 → {},GET 不被单个损坏文件拖垮)。
上游:web/app.py(经 dashboard 包挂载路由);frontend/dist 前端(dashboard 页)。
下游:web/workspace.py(require_workspace/validate_stem/invalidate_caches)、
web/evidence_api.py(_read_json/_atomic_write_json)、web/realtime.py。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from traffic_analyzer.web import evidence_api
from traffic_analyzer.web import realtime
from traffic_analyzer.web import workspace as workspace_mod

router = APIRouter()

REVIEW_STATUSES = ("unconfirmed", "confirmed", "needs_review")

# 单文件、跨 stem:一把锁串行化 read-modify-write。
_review_lock = threading.Lock()


def _review_states_path(workspace: Path) -> Path:
    return workspace / "analysis" / "review_states.json"


def _load_review_states(workspace: Path) -> Dict[str, Any]:
    """Missing file → {}; corrupt → {} (GET 不因一个损坏文件拖垮整个看板)。"""
    try:
        data = evidence_api._read_json(_review_states_path(workspace))
    except evidence_api._CorruptJsonError:
        return {}
    return data if isinstance(data, dict) else {}


class ReviewRequest(BaseModel):
    stem: str
    status: str


@router.put("/api/dashboard/review")
def put_dashboard_review(body: ReviewRequest, request: Request) -> Dict[str, Any]:
    workspace = workspace_mod.require_workspace(request)
    workspace_mod.validate_stem(body.stem)
    if body.status not in REVIEW_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {list(REVIEW_STATUSES)}",
        )
    path = _review_states_path(workspace)
    with _review_lock:
        try:
            states = evidence_api._read_json(path)
        except evidence_api._CorruptJsonError as exc:
            # 损坏 ≠ 不存在:不明确报 422 就会静默覆盖掉他人已写的复核状态。
            raise HTTPException(
                status_code=422, detail=f"Existing review states file is corrupt: {exc}"
            )
        if not isinstance(states, dict):
            states = {}
        entry = {
            "status": body.status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            # 追溯:谁做的复核(认证关闭时为 'local')。
            "by": getattr(request.state, "user", "local"),
        }
        states[body.stem] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        evidence_api._atomic_write_json(path, states)
    # review 落盘 → 看板/视频缓存失效(下一 GET 重算,反映最新审核态)
    workspace_mod.invalidate_caches()
    realtime.publish_from_app(
        request.app,
        "dashboard.changed",
        {"stem": body.stem, "status": body.status},
    )
    return {"stem": body.stem, **entry}
