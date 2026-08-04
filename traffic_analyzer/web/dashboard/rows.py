"""Dashboard rows endpoint (split from the old monolithic dashboard.py).

[文件说明]
作用:逐视频明细行。GET /api/dashboard/rows 在 metrics._get_dashboard 的
全量行(缓存共享)上做过滤与分页:consistency/review 逗号分隔多值、
edited=1、q(rel/stem 子串,不区分大小写);先过滤后分页,page/size
(size ≤ 200;page 越界 → 空 rows + 正确 total_pages)。过滤/分页均产生
新列表,不就地修改缓存行(copy=False 只读契约)。
上游:web/app.py(经 dashboard 包挂载路由);web/static 前端(dashboard 页)。
下游:web/dashboard/metrics.py(_get_dashboard)、web/workspace.py
(require_workspace)。
"""

from __future__ import annotations

from typing import Any, Dict, Set

from fastapi import APIRouter, Query, Request

from traffic_analyzer.web import workspace as workspace_mod
from traffic_analyzer.web.dashboard.metrics import _get_dashboard

router = APIRouter()


def _csv_values(raw: str) -> Set[str]:
    """逗号分隔多值参数 → 非空值集合。"""
    return {v.strip() for v in raw.split(",") if v.strip()}


@router.get("/api/dashboard/rows")
def get_dashboard_rows(
    request: Request,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    consistency: str = "",
    review: str = "",
    edited: str = "",
    q: str = "",
) -> Dict[str, Any]:
    """Filtered + paginated dashboard rows (filter first, then paginate)."""
    rows = _get_dashboard(workspace_mod.require_workspace(request))["rows"]

    statuses = _csv_values(consistency)
    if statuses:
        rows = [r for r in rows if r["status"] in statuses]
    reviews = _csv_values(review)
    if reviews:
        rows = [r for r in rows if r["review"] in reviews]
    if edited == "1":
        rows = [r for r in rows if r["edited"]]
    needle = q.strip().lower()
    if needle:
        rows = [
            r
            for r in rows
            if needle in r["rel"].lower() or needle in r["stem"].lower()
        ]

    total = len(rows)
    total_pages = (total + size - 1) // size  # total=0 → 0 页
    start = (page - 1) * size
    # page 越界:切片天然为空,total/total_pages 仍按过滤后全量返回。
    return {
        "rows": rows[start : start + size],
        "page": page,
        "size": size,
        "total": total,
        "total_pages": total_pages,
    }
