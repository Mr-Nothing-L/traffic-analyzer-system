"""RAG 检索函数式 API:四种查询结构化返回,供 CLI / toolserver / web 复用。

[文件说明]
作用:把 scripts/rag_search.py 的四种查询(text 查询词 hybrid 检索、
related video_vec 相似、adjacent 同 site 时间邻近、site 桩号+方向+时间窗)
抽为函数式 API;run_search(workspace, mode, ...) 统一入口,返回
{"results": [...], "mode": ..., "elapsed_ms": ...}。单条结果契约:
video_path / score(4 位小数,无相似分的模式为 null)/ events(无标注 null)/
site / start_ts / duration_s(缺失 null)/ has_annotation / human_edited /
review_status。elapsed_ms 仅计库内检索耗时,不含 text 模式查询词 embedding
时间。库不存在抛 RagIndexNotFound(引导先跑 scripts/build_rag_index.py);
参数/目标问题抛 RagQueryError(带 status_code 供 HTTP 层映射)。
上游:traffic_analyzer/rag/store.py(RagStore)、embed_client(embed_texts)。
下游:scripts/rag_search.py(CLI 薄封装)、toolserver/server.py
(/tools/search_videos)、web/rag.py(/api/rag/search)。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from traffic_analyzer.rag.embed_client import embed_texts
from traffic_analyzer.rag.store import RagStore

MODES = ("text", "related", "adjacent", "site")


class RagIndexNotFound(Exception):
    """<workspace>/.agent/rag/vectors.db 不存在。"""


class RagQueryError(Exception):
    """检索参数/目标问题;status_code 供 HTTP 层映射(400/404/502)。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def parse_time(value) -> float | None:
    """epoch 秒(float/int/数字字符串)或 ISO 时间字符串 → epoch 秒;None 透传。"""
    if value is None or isinstance(value, (int, float)):
        return None if value is None else float(value)
    try:
        return float(value)
    except ValueError:
        return datetime.fromisoformat(value).timestamp()


def _find_record(records: list[dict], name: str) -> dict | None:
    """按 video_path(文件名)精确匹配,退而按 stem 匹配。"""
    for r in records:
        if r["video_path"] == name:
            return r
    for r in records:
        if Path(r["video_path"]).stem == name:
            return r
    return None


def _adjacent_ok(a_start, a_end, b_start, b_end, gap_s: float) -> bool:
    if a_start is None or b_start is None:
        return False
    a_end = a_end if a_end is not None else a_start
    b_end = b_end if b_end is not None else b_start
    overlap = b_start <= a_end and a_start <= b_end
    return overlap or abs(a_end - b_start) <= gap_s or abs(b_end - a_start) <= gap_s


def _result_item(rec: dict, score: float | None = None) -> dict:
    """单条结果契约:score 保留 4 位小数;缺失字段给 null。"""
    return {
        "video_path": rec["video_path"],
        "score": round(float(score), 4) if score is not None else None,
        "events": rec.get("events") or None,
        "site": rec.get("site"),
        "start_ts": rec.get("start_ts"),
        "duration_s": rec.get("duration_s"),
        "has_annotation": bool(rec.get("has_annotation")),
        "human_edited": bool(rec.get("human_edited")),
        "review_status": rec.get("review_status"),
    }


def search_text(
    store: RagStore,
    query: str,
    k: int = 10,
    alpha: float = 0.6,
    only_confirmed: bool = False,
    human_edited: bool = False,
    embed_fn=None,
) -> dict:
    """查询词 hybrid 检索;embed_fn 可注入(测试 mock),默认 embed_texts。"""
    if not query:
        raise RagQueryError("mode=text requires query")
    try:
        query_vec = (embed_fn or embed_texts)([query])[0]
    except RagQueryError:
        raise
    except Exception as exc:  # noqa: BLE001 — embedding 服务不可达等
        raise RagQueryError(f"embedding failed: {exc}", status_code=502) from exc
    t0 = time.perf_counter()
    review_filter = "confirmed" if only_confirmed else None
    fetch_k = k * 10 if human_edited else k
    hits = store.search(query_vec, "hybrid", fetch_k, review_filter=review_filter, alpha=alpha)
    rec_map = {r["video_path"]: r for r in store.records()}
    results = []
    for path, score in hits:
        rec = rec_map.get(path, {})
        if human_edited and not rec.get("human_edited"):
            continue
        results.append(_result_item(rec, score))
        if len(results) >= k:
            break
    return {
        "results": results,
        "mode": "text",
        # 仅库内检索耗时,不含查询词 embedding 时间。
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def search_related(store: RagStore, video: str, k: int = 10) -> dict:
    """以库内视频的 video_vec 为查询向量找相似视频(排除自身)。"""
    if not video:
        raise RagQueryError("mode=related requires video")
    t0 = time.perf_counter()
    records = store.records()
    target = _find_record(records, video)
    if target is None:
        raise RagQueryError(f"record not found: {video}", status_code=404)
    vec = store.get_vec(target["video_path"], "video")
    if vec is None:
        raise RagQueryError(f"no video_vec: {target['video_path']}", status_code=404)
    hits = [
        (p, s)
        for p, s in store.search(vec, "video", k + 1)
        if p != target["video_path"]
    ][:k]
    rec_map = {r["video_path"]: r for r in records}
    return {
        "results": [_result_item(rec_map.get(p, {}), s) for p, s in hits],
        "mode": "related",
        "target": target["video_path"],
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def search_adjacent(store: RagStore, video: str, gap_s: float = 600.0) -> dict:
    """同 site 候选按时间邻近过滤;site 缺失回退 video_vec top-50 候选。"""
    if not video:
        raise RagQueryError("mode=adjacent requires video")
    t0 = time.perf_counter()
    records = store.records()
    target = _find_record(records, video)
    if target is None:
        raise RagQueryError(f"record not found: {video}", status_code=404)
    rec_map = {r["video_path"]: r for r in records}
    if target.get("site"):
        candidates = [
            r
            for r in records
            if r["video_path"] != target["video_path"] and r.get("site") == target["site"]
        ]
        source = f"site={target['site']}"
    else:
        vec = store.get_vec(target["video_path"], "video")
        candidates = []
        if vec is not None:
            candidates = [
                rec_map[p]
                for p, _ in store.search(vec, "video", 50)
                if p != target["video_path"] and p in rec_map
            ]
        source = "site missing, fallback video_vec top-50"
    a_start = target.get("start_ts")
    a_end = a_start + target["duration_s"] if a_start is not None and target.get("duration_s") else None
    hits = [
        r
        for r in candidates
        if _adjacent_ok(
            a_start, a_end, r.get("start_ts"),
            r["start_ts"] + r["duration_s"] if r.get("start_ts") is not None and r.get("duration_s") else None,
            gap_s,
        )
    ]
    hits.sort(key=lambda r: r.get("start_ts") or 0.0)
    return {
        "results": [_result_item(r) for r in hits],
        "mode": "adjacent",
        "target": target["video_path"],
        "source": source,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def search_site(
    store: RagStore,
    stake: str,
    direction: str | None = None,
    before=None,
    after=None,
) -> dict:
    """桩号子串 + 方向 + start_ts 时间窗过滤;before/after 为 epoch 秒或 ISO 时间。"""
    if not stake:
        raise RagQueryError("mode=site requires query (stake)")
    t0 = time.perf_counter()
    before_ts = parse_time(before)
    after_ts = parse_time(after)
    hits = []
    for r in store.records():
        if stake not in (r.get("stake") or ""):
            continue
        if direction and r.get("direction") != direction:
            continue
        ts = r.get("start_ts")
        if before_ts is not None and (ts is None or ts > before_ts):
            continue
        if after_ts is not None and (ts is None or ts < after_ts):
            continue
        hits.append(r)
    hits.sort(key=lambda r: r.get("start_ts") or 0.0)
    return {
        "results": [_result_item(r) for r in hits],
        "mode": "site",
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def run_search(
    workspace,
    mode: str,
    *,
    query: str | None = None,
    video: str | None = None,
    k: int = 10,
    alpha: float = 0.6,
    only_confirmed: bool = False,
    human_edited: bool = False,
    gap_s: float = 600.0,
    direction: str | None = None,
    before=None,
    after=None,
    embed_fn=None,
) -> dict:
    """统一入口:校验库存在后按 mode 分发;site 模式的桩号经 query 传入。"""
    workspace = Path(workspace)
    db_path = workspace / ".agent" / "rag" / "vectors.db"
    if not db_path.is_file():
        raise RagIndexNotFound(
            f"RAG 索引不存在: {db_path};请先运行 scripts/build_rag_index.py 构建索引"
        )
    with RagStore(workspace) as store:
        if mode == "text":
            return search_text(
                store, query, k, alpha, only_confirmed, human_edited, embed_fn
            )
        if mode == "related":
            return search_related(store, video, k)
        if mode == "adjacent":
            return search_adjacent(store, video, gap_s)
        if mode == "site":
            return search_site(store, query, direction, before, after)
    raise RagQueryError(f"unknown mode: {mode}")
