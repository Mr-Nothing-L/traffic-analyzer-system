"""Dashboard metrics + aggregate endpoint (split from the old monolithic dashboard.py).

[文件说明]
作用:看板构建与汇总视图。按工作区视频逐行合并文件名 GT(从
traffic_analyzer.evaluation 导入 extract_gt_from_filename)与
analysis/<stem>/<stem>.json 的 action 预测,产出四态行
(consistent/diff/no_gt/no_results;missing/extra 仅 diff 行填充);存在冻结
快照 <stem>_raw.json 时附 edited 与编辑前后 action 差异
(edit_missing/edit_extra);review 列合并 review._load_review_states。
metrics 为集合运算(仅 consistent/diff 行参与;per_event + macro/micro)。
GET /api/dashboard 只回汇总视图(summary 计数、event_names
(str(event_id) → name_zh,event_config 的 event_categories 索引反转)、
metrics),全部基于未过滤全量行。
_build_dashboard 结果带进程内缓存(key=workspace 路径,长 TTL + 主动失效,
TTL 见 metrics._DASHBOARD_CACHE_TTL_SEC),GET /api/dashboard 与 rows 端点共用;
失效统一走 workspace.invalidate_caches;copy=False 只读契约(调用方不得
就地修改)。_build_dashboard 经 dashboard 包命名空间延迟查找,测试
monkeypatch traffic_analyzer.web.dashboard._build_dashboard 即生效。
上游:web/app.py(经 dashboard 包挂载路由);frontend/dist 前端(dashboard 页)。
下游:web/workspace.py(视频发现与路径契约)、web/event_config.py(事件名
索引)、web/evidence_api.py(_read_json)、web/dashboard/review.py(审核态)、
traffic_analyzer.evaluation(extract_gt_from_filename)。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, HTTPException, Request

from traffic_analyzer.evaluation import extract_gt_from_filename
from traffic_analyzer.web import dashboard as _dashboard_pkg
from traffic_analyzer.web import event_config
from traffic_analyzer.web import evidence_api
from traffic_analyzer.web import workspace as workspace_mod
from traffic_analyzer.web.dashboard.review import (
    REVIEW_STATUSES,
    _load_review_states,
)
from traffic_analyzer.web.workspace.videos import _LongTTLCache

router = APIRouter()


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


def _read_video_payloads(
    workspace: Path, stem: str
) -> Tuple[bool, Optional[Any], Optional[Any]]:
    """单个视频的看板输入(纯 IO):has_results + SFT JSON + 冻结快照 JSON。

    单条损坏不拖垮整个看板:JSON 损坏按「无预测/无快照」降级展示。
    """
    out_dir = workspace_mod.analysis_dir(workspace, stem)
    try:
        sft = evidence_api._read_json(out_dir / f"{stem}.json")
        raw = evidence_api._read_json(out_dir / f"{stem}_raw.json")
    except evidence_api._CorruptJsonError:
        sft = raw = None
    return workspace_mod.has_results(workspace, stem), sft, raw


def _build_dashboard(workspace: Path) -> Dict[str, Any]:
    """Full unfiltered dashboard: rows + summary + event_names + metrics."""
    reviews = _load_review_states(workspace)
    # event_categories 索引(name_zh → event_id)反转为契约的 str(event_id) → name_zh。
    event_names = {
        str(eid): name for name, eid in event_config.event_name_index().items()
    }

    videos = workspace_mod.list_videos_cached(workspace)
    # 逐视频的 has_results 探测与两个 JSON 读取是纯 IO(大工作区外接盘上为冷建
    # 大头),线程池并发;executor.map 保序,rows 仍按 videos 顺序装配,结果确定。
    with ThreadPoolExecutor() as pool:
        payloads = list(
            pool.map(lambda v: _read_video_payloads(workspace, v["stem"]), videos)
        )

    rows: List[Dict[str, Any]] = []
    for video, (has_results, sft, raw) in zip(videos, payloads):
        stem = video["stem"]
        gt_ids = sorted(extract_gt_from_filename(video["name"]))
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


# _build_dashboard 的进程内缓存(key=workspace 路径):「长 TTL + 主动失效」主导。
# 原 15s 短 TTL 下,大工作区全量构建需秒级~11s(外接盘逐文件 stat/读 JSON),
# 翻页/切换频繁踩自然过期的冷建;正确性本就由 invalidate_caches 主动失效保证
# (workspace 变更 / infer 完成 / review PUT / SFT/证据 PUT),TTL 拉长到分钟级,
# 仅作进程外直接改盘的兜底刷新。GET /api/dashboard 与 /api/dashboard/rows 共用
# 同一份构建结果;命中后响应 <100ms。
# copy=False 只读使用:两个端点都不就地修改返回值——aggregate 端点只读取
# summary/event_names/metrics 三个键;rows 端点的过滤/分页均产生新列表,
# 行字典只读序列化(4393 行的深拷贝需 ~200ms,会破坏命中 <100ms 的目标)。
_DASHBOARD_CACHE_TTL_SEC = 600.0

_dashboard_cache = workspace_mod.register_cache(
    _LongTTLCache(_DASHBOARD_CACHE_TTL_SEC)
)


def _get_dashboard(workspace: Path) -> Dict[str, Any]:
    """_build_dashboard 的缓存版:命中直接返回(TTL 内),未命中重算并写入。

    返回值是缓存对象本身:调用方只读使用,不得就地修改。
    """
    key = str(workspace)
    cached = _dashboard_cache.get(key, copy=False)
    if cached is not None:
        return cached
    # 经包命名空间延迟查找:monkeypatch dashboard._build_dashboard 即生效。
    data = _dashboard_pkg._build_dashboard(workspace)
    _dashboard_cache.set(key, data)
    return data


@router.get("/api/dashboard")
def get_dashboard(request: Request) -> Dict[str, Any]:
    """Aggregate overview only (rows moved to GET /api/dashboard/rows)."""
    data = _get_dashboard(workspace_mod.require_workspace(request))
    return {
        "summary": data["summary"],
        "event_names": data["event_names"],
        "metrics": data["metrics"],
    }
