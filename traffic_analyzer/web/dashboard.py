"""Dashboard aggregation endpoints (GT vs prediction overview).

GET /api/dashboard walks the workspace videos and joins, per row, the
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
(``{stem: {status, updated_at}}``) with the same atomic write as
``evidence_api._atomic_write_json``.

[文件说明]
作用:dashboard 聚合接口。GET /api/dashboard 按工作区视频逐行合并文件名 GT
(importlib 按路径加载 scripts/batch_evaluate.py 的 extract_gt_from_filename)
与 analysis/<stem>/<stem>.json 的 action 预测,产出四态行
(consistent/diff/no_gt/no_results;missing/extra 仅 diff 行填充)、summary
计数、event_names(str(event_id) → name_zh,取自 event_config 的
event_categories 索引)与集合运算指标(无 GT/未推理行不参与);存在冻结快照
<stem>_raw.json 时附 edited 与编辑前后 action 差异(edit_missing/edit_extra)。
PUT /api/dashboard/review 校验三态(unconfirmed/confirmed/needs_review,非法
422)并原子写 <workspace>/analysis/review_states.json。
上游:web/app.py(挂载路由);web/static 前端(dashboard 页)。
下游:web/workspace.py(视频发现与路径契约)、web/event_config.py(事件名索引)、
web/evidence_api.py(_read_json / _atomic_write_json)、scripts/batch_evaluate.py。
"""

from __future__ import annotations

import importlib.util
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from traffic_analyzer.web import event_config
from traffic_analyzer.web import evidence_api
from traffic_analyzer.web import workspace as workspace_mod

router = APIRouter()

# scripts/batch_evaluate.py 不是包(脚本),按路径加载,与 tests 的加载方式一致。
_BATCH_EVALUATE_PY = (
    Path(__file__).resolve().parents[2] / "scripts" / "batch_evaluate.py"
)

_extract_gt_from_filename: Optional[Callable[[str], Set[int]]] = None


def _gt_extractor() -> Callable[[str], Set[int]]:
    """Lazy-load ``extract_gt_from_filename`` from scripts/batch_evaluate.py."""
    global _extract_gt_from_filename
    if _extract_gt_from_filename is None:
        spec = importlib.util.spec_from_file_location(
            "batch_evaluate", _BATCH_EVALUATE_PY
        )
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise HTTPException(
                status_code=500, detail="scripts/batch_evaluate.py not loadable"
            )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _extract_gt_from_filename = module.extract_gt_from_filename
    return _extract_gt_from_filename


# ---------------------------------------------------------------------------
# Review states (<workspace>/analysis/review_states.json)
# ---------------------------------------------------------------------------

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
        }
        states[body.stem] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        evidence_api._atomic_write_json(path, states)
    return {"stem": body.stem, **entry}


# ---------------------------------------------------------------------------
# Dashboard rows / metrics
# ---------------------------------------------------------------------------


def _action_ids(payload: Optional[Any]) -> List[int]:
    """SFT 样本的 action 字段 → 排序去重的事件 id 列表(非列表/非整数忽略)。"""
    if not isinstance(payload, dict):
        return []
    action = payload.get("action")
    if not isinstance(action, list):
        return []
    return sorted({int(a) for a in action if isinstance(a, int)})


def _precision_recall_f1(tp: int, fp: int, fn: int) -> tuple:
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return precision, recall, f1


def _compute_metrics(
    evaluated: List[Dict[str, Any]], event_names: Dict[str, str]
) -> Dict[str, Any]:
    """Set-algebra metrics over evaluated rows (GT 与结果都存在的行)。"""
    tp: Dict[int, int] = {}
    fp: Dict[int, int] = {}
    fn: Dict[int, int] = {}
    for row in evaluated:
        gt = set(row["gt_ids"])
        pred = set(row["pred_ids"])
        for eid in gt & pred:
            tp[eid] = tp.get(eid, 0) + 1
        for eid in pred - gt:
            fp[eid] = fp.get(eid, 0) + 1
        for eid in gt - pred:
            fn[eid] = fn.get(eid, 0) + 1

    per_event: List[Dict[str, Any]] = []
    for eid in sorted(set(tp) | set(fp) | set(fn)):
        t, f, n = tp.get(eid, 0), fp.get(eid, 0), fn.get(eid, 0)
        precision, recall, f1 = _precision_recall_f1(t, f, n)
        per_event.append(
            {
                "event_id": eid,
                "name": event_names.get(str(eid), f"事件{eid}"),
                "tp": t,
                "fp": f,
                "fn": n,
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "f1": round(f1, 4),
            }
        )

    if per_event:
        macro = {
            key: round(sum(ev[key] for ev in per_event) / len(per_event), 4)
            for key in ("precision", "recall", "f1")
        }
    else:
        macro = {"precision": 0.0, "recall": 0.0, "f1": 0.0}

    total_tp = sum(tp.values())
    total_fp = sum(fp.values())
    total_fn = sum(fn.values())
    micro_p, micro_r, micro_f1 = _precision_recall_f1(total_tp, total_fp, total_fn)
    micro = {
        "precision": round(micro_p, 4),
        "recall": round(micro_r, 4),
        "f1": round(micro_f1, 4),
    }
    return {"per_event": per_event, "macro": macro, "micro": micro}


@router.get("/api/dashboard")
def get_dashboard(request: Request) -> Dict[str, Any]:
    workspace = workspace_mod.require_workspace(request)
    extract_gt = _gt_extractor()
    reviews = _load_review_states(workspace)
    # event_categories 索引(name_zh → event_id)反转为契约的 str(event_id) → name_zh。
    event_names = {
        str(eid): name for name, eid in event_config.event_name_index().items()
    }

    rows: List[Dict[str, Any]] = []
    for video in workspace_mod.list_videos(workspace):
        stem = video["stem"]
        gt_ids = sorted(extract_gt(video["name"]))
        has_results = workspace_mod.has_results(workspace, stem)
        out_dir = workspace_mod.analysis_dir(workspace, stem)

        try:
            sft = evidence_api._read_json(out_dir / f"{stem}.json")
            raw = evidence_api._read_json(out_dir / f"{stem}_raw.json")
        except evidence_api._CorruptJsonError:
            # 单条损坏不拖垮整个看板:按「无预测/无快照」降级展示。
            sft = raw = None
        pred_ids = _action_ids(sft)
        pred_raw_ids = _action_ids(raw) if raw is not None else None

        if not has_results:
            status = "no_results"
        elif not gt_ids:
            status = "no_gt"
        elif pred_ids == gt_ids:
            status = "consistent"
        else:
            status = "diff"
        is_diff = status == "diff"

        edited = raw is not None
        raw_set = set(pred_raw_ids or [])
        review_entry = reviews.get(stem)
        review = (
            review_entry.get("status")
            if isinstance(review_entry, dict)
            and review_entry.get("status") in REVIEW_STATUSES
            else "unconfirmed"
        )
        rows.append(
            {
                "rel": video["rel"],
                "stem": stem,
                "has_results": has_results,
                "gt_ids": gt_ids,
                "pred_ids": pred_ids,
                "status": status,
                "missing": sorted(set(gt_ids) - set(pred_ids)) if is_diff else [],
                "extra": sorted(set(pred_ids) - set(gt_ids)) if is_diff else [],
                "pred_raw_ids": pred_raw_ids,
                "edited": edited,
                "edit_missing": sorted(raw_set - set(pred_ids)) if edited else [],
                "edit_extra": sorted(set(pred_ids) - raw_set) if edited else [],
                "review": review,
            }
        )

    summary = {
        "total": len(rows),
        "consistent": sum(1 for r in rows if r["status"] == "consistent"),
        "diff": sum(1 for r in rows if r["status"] == "diff"),
        "no_gt": sum(1 for r in rows if r["status"] == "no_gt"),
        "no_results": sum(1 for r in rows if r["status"] == "no_results"),
        "confirmed": sum(1 for r in rows if r["review"] == "confirmed"),
        "unconfirmed": sum(1 for r in rows if r["review"] == "unconfirmed"),
        "needs_review": sum(1 for r in rows if r["review"] == "needs_review"),
        "edited": sum(1 for r in rows if r["edited"]),
    }
    evaluated = [r for r in rows if r["status"] in ("consistent", "diff")]
    return {
        "rows": rows,
        "summary": summary,
        "event_names": event_names,
        "metrics": _compute_metrics(evaluated, event_names),
    }
