"""RAG 索引建库编排:遍历工作区视频 → embedding + OSD 站点 → 写 RagStore。

[文件说明]
作用:build_index() 为建库核心(原 scripts/build_rag_index.py 编排抽函数):
列 workspace 下 *.mp4,按 only_missing / refresh_annotations 过滤待处理,线程池内
只做 HTTP/计算(视频 embedding、标注文本 embedding、OSD 站点抽取),主线程串行
upsert;单视频失败记失败清单继续;progress_cb(done, total, failed) 每条落库后回调,
cancel_flag() 返回 True 时条间停止(已完成的保留,返回 partial=True);结束更新
meta(model/dim/count/built_at)。
上游:scripts/build_rag_index.py(CLI 薄封装)、traffic_analyzer/web/rag.py(后台建库)。
下游:<workspace>/.agent/rag/vectors.db、osd_cache.json。
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from traffic_analyzer.rag import RagStore, embed_texts, embed_video_bytes, load_label
from traffic_analyzer.rag.annotations import load_review_states, make_site, parse_filename_ts
from traffic_analyzer.rag.embed_client import _DEFAULT_MODEL
from traffic_analyzer.rag.site_osd import extract_site


def _process(video: Path, workspace: Path, review_states: dict, osd_cache: Path) -> dict:
    """worker:只做 HTTP / 计算,不写库;任何异常向上抛由主线程记录。"""
    stem = video.stem
    video_vec = embed_video_bytes(video.read_bytes(), video.suffix.lstrip(".") or "mp4")
    label = load_label(workspace, stem)
    ann_vec = embed_texts([label.text])[0] if label and label.text else None
    site_info = extract_site(video, osd_cache)
    review = review_states.get(stem) or {}
    return {
        "video_path": video.name,
        "video_vec": video_vec,
        "ann_vec": ann_vec,
        "events": label.events if label else [],
        "has_annotation": int(label is not None and bool(label.text)),
        "human_edited": int(label.human_edited) if label else 0,
        "review_status": review.get("status", "unconfirmed"),
        "road": site_info.get("road"),
        "stake": site_info.get("stake"),
        "direction": site_info.get("direction"),
        "camera": site_info.get("camera"),
        "site": make_site(
            site_info.get("road"),
            site_info.get("stake"),
            site_info.get("direction"),
            site_info.get("camera"),
        ),
        "start_ts": parse_filename_ts(stem),
        "duration_s": label.duration_s if label else None,
        "ann_edited_at": label.ann_edited_at if label else None,
    }


def list_pending(
    workspace,
    only_missing: bool = True,
    refresh_annotations: bool = False,
    limit: Optional[int] = None,
) -> tuple[int, int, list[Path]]:
    """返回 (videos_total, existing_count, pending);供建库与 web 预估 total 共用。"""
    workspace = Path(workspace)
    with RagStore(workspace) as store:
        existing = store.existing_paths()
        ann_edited_map = (
            {r["video_path"]: r.get("ann_edited_at") for r in store.records()}
            if refresh_annotations
            else {}
        )
    videos = sorted(workspace.glob("*.mp4"))
    if only_missing and not refresh_annotations:
        pending = [v for v in videos if v.name not in existing]
    elif only_missing and refresh_annotations:
        # 新视频 + 标注时间戳与建库时不一致的(真正被编辑过)才重算;
        # 有标注但未变更的不重复 embedding。
        pending = []
        for v in videos:
            if v.name not in existing:
                pending.append(v)
                continue
            label = load_label(workspace, v.stem)
            if label is not None and label.ann_edited_at != ann_edited_map.get(v.name):
                pending.append(v)
    else:
        pending = videos
    if limit:
        pending = pending[:limit]
    return len(videos), len(existing), pending


def build_index(
    workspace,
    concurrency: int = 8,
    only_missing: bool = True,
    refresh_annotations: bool = False,
    limit: Optional[int] = None,
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
    cancel_flag: Optional[Callable[[], bool]] = None,
) -> dict:
    """建库主流程;返回 {workspace, elapsed_s, success, failed, stats, total, partial}。"""
    workspace = Path(workspace)
    osd_cache = workspace / ".agent" / "rag" / "osd_cache.json"
    review_states = load_review_states(workspace)
    started = time.time()
    partial = False

    videos_total, existing_count, pending = list_pending(
        workspace, only_missing, refresh_annotations, limit
    )
    total = len(pending)
    print(f"videos={videos_total} existing={existing_count} pending={total}")

    success: list[str] = []
    failed: list[dict] = []
    done = 0
    with RagStore(workspace) as store:
        pool = ThreadPoolExecutor(max_workers=concurrency)
        try:
            futures = {
                pool.submit(_process, v, workspace, review_states, osd_cache): v
                for v in pending
            }
            for fut in as_completed(futures):
                video = futures[fut]
                try:
                    row = fut.result()
                    store.upsert_record(row.pop("video_path"), **row)
                    success.append(video.name)
                except Exception as e:  # noqa: BLE001
                    failed.append({"video": video.name, "error": f"{type(e).__name__}: {e}"})
                    print(f"[FAIL] {video.name}: {e}")
                done += 1
                if progress_cb is not None:
                    progress_cb(done, total, len(failed))
                if done % 20 == 0 or done == total:
                    print(f"progress {done}/{total} (fail={len(failed)})")
                if cancel_flag is not None and cancel_flag():
                    partial = True
                    break
        finally:
            # 取消时不等跑完的 worker(纯 HTTP,无果强等);未启动的 futures 直接丢弃。
            pool.shutdown(wait=not partial, cancel_futures=True)

        model = os.environ.get("WEMM_MODEL", _DEFAULT_MODEL)
        dim = None
        if success:
            vec = store.get_vec(success[0], "video")
            dim = int(vec.shape[0]) if vec is not None else None
        store.set_meta("model", model)
        store.set_meta("dim", dim if dim is not None else "")
        store.set_meta("count", len(store.existing_paths()))
        store.set_meta("built_at", time.time())
        stats = store.stats()

    return {
        "workspace": str(workspace),
        "elapsed_s": round(time.time() - started, 2),
        "success": success,
        "failed": failed,
        "stats": stats,
        "total": total,
        "partial": partial,
    }
