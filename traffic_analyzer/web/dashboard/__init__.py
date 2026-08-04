"""Dashboard aggregation package (GT vs prediction overview).

GET /api/dashboard returns the aggregate overview — ``summary`` counts,
``event_names`` (str(event_id) → name_zh, from event_config's
event_categories index) and set-algebra ``metrics`` — all computed over the
full, unfiltered row set. GET /api/dashboard/rows returns the per-video rows
with filtering (``consistency``/``review`` comma-separated multi-value,
``edited=1``, ``q`` = case-insensitive rel/stem substring) and pagination
(``page``/``size``, filter before paginate, size capped at 200; out-of-range
page → empty rows with the correct ``total_pages``). Each row joins the
filename ground truth (via ``scripts/batch_evaluate.py``'s
``extract_gt_from_filename``, loaded by path with importlib since the script
is not a package) with the prediction read from ``analysis/<stem>/<stem>.json``
(``action`` field). Rows carry a four-state status
(``consistent`` / ``diff`` / ``no_gt`` / ``no_results``); ``missing``/``extra``
are only populated for ``diff`` rows. When the frozen raw snapshot
``<stem>_raw.json`` exists (first SFT edit froze it), the row reports
``edited`` plus the raw-vs-current action diff (``edit_missing`` / ``edit_extra``
= ids removed / added by the edit). Metrics use plain set algebra over the
evaluated rows only (rows without GT or without results do not participate).

PUT /api/dashboard/review validates the three-state review status and
persists it to ``<workspace>/analysis/review_states.json``
(``{stem: {status, updated_at, by}}``, ``by`` = ``request.state.user``) with
the same atomic write as ``evidence_api._atomic_write_json``.

[文件说明]
作用:dashboard 聚合包(自原单文件 dashboard.py 拆分)。metrics.py 为 GT
提取懒加载、行构建(_build_dashboard)、集合运算指标、TTL 缓存与
GET /api/dashboard;rows.py 为过滤/分页与 GET /api/dashboard/rows;
review.py 为复核状态持久化与 PUT /api/dashboard/review(成功后 realtime
发布 dashboard.changed)。本模块聚合导出,保持
``from traffic_analyzer.web import dashboard`` 及 ``dashboard.router`` /
``dashboard._build_dashboard`` 等既有引用/monkeypatch 路径可用。
_build_dashboard 结果带进程内缓存(key=workspace 路径,长 TTL + 主动失效,
TTL 见 metrics._DASHBOARD_CACHE_TTL_SEC),两个 GET 端点共用一份构建结果;
失效统一走 workspace.invalidate_caches(workspace 变更 / infer 完成 /
review PUT / SFT/证据 PUT)。大工作区(数千视频、外接盘)全量构建 ~11s,
缓存命中 <100ms。
上游:web/app.py(挂载路由);web/static 前端(dashboard 页)。
下游:web/dashboard/metrics.py、web/dashboard/rows.py、
web/dashboard/review.py。
"""

from __future__ import annotations

from fastapi import APIRouter

from traffic_analyzer.web.dashboard import metrics as _metrics_mod
from traffic_analyzer.web.dashboard import review as _review_mod
from traffic_analyzer.web.dashboard import rows as _rows_mod
from traffic_analyzer.web.dashboard.metrics import (
    _BATCH_EVALUATE_PY,
    _action_ids,
    _build_dashboard,
    _compute_metrics,
    _dashboard_cache,
    _extract_gt_from_filename,
    _get_dashboard,
    _gt_extractor,
    _precision_recall_f1,
    get_dashboard,
)
from traffic_analyzer.web.dashboard.review import (
    REVIEW_STATUSES,
    ReviewRequest,
    _load_review_states,
    _review_lock,
    _review_states_path,
    put_dashboard_review,
)
from traffic_analyzer.web.dashboard.rows import _csv_values, get_dashboard_rows

# 三个子模块各持 router;此处聚合为单一 router 供 app.include_router 挂载,
# 注册顺序保持原单文件顺序(review PUT → GET /api/dashboard → GET rows)。
router = APIRouter()
router.include_router(_review_mod.router)
router.include_router(_metrics_mod.router)
router.include_router(_rows_mod.router)

__all__ = [
    "REVIEW_STATUSES",
    "ReviewRequest",
    "router",
]
