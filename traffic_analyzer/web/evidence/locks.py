"""Per-stem PUT locks and the active-infer-job guard (split from evidence_api).

[文件说明]
作用:per-stem PUT 锁设施。_put_locks 闭合并发 read-compare-write 的 PUT
窗口;jobs.post_infer 提交同 stem 的 infer 前也持有同一把锁(锁顺序统一
为 _put_locks[stem] → JobManager._lock,反向路径不存在,不会死锁)。
find_active_infer_job 供 evidence/SFT PUT(409 守卫)与 jobs.post_infer
(重复提交守卫)共用同一判定条件(调用方持有 _put_locks[stem]);
_reject_active_infer 在锁内复查 409,消除「检查与写文件之间插入新
infer」的 TOCTOU。
上游:web/evidence/evidence_put.py、web/evidence/sft_api.py(PUT 锁内
复查)、web/jobs/routes.py(post_infer 反向复用);经
web/evidence/__init__.py 聚合后由老路径
traffic_analyzer.web.evidence_api 暴露。
下游:request.app.state.jobs(JobManager.list_jobs)。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request

# Per-stem locks closing the concurrent read-compare-write PUT window.
# jobs.post_infer 提交同 stem 的 infer 前也持有同一把锁(锁顺序统一为
# _put_locks[stem] → JobManager._lock,反向路径不存在,不会死锁)。
_put_locks: "defaultdict[str, threading.Lock]" = defaultdict(threading.Lock)


def find_active_infer_job(request: Request, stem: str) -> Optional[Dict[str, Any]]:
    """The queued/running infer job for ``stem``, if any.

    Shared by the evidence/SFT PUT endpoints (409 guard) and
    ``jobs.post_infer`` (duplicate-submit guard), so both sides test the
    same condition. Caller holds ``_put_locks[stem]``.
    """
    jobs = getattr(request.app.state, "jobs", None)
    if jobs is None:
        return None
    for job in jobs.list_jobs():
        if (
            job.get("kind") == "infer"
            and job.get("stem") == stem
            and job.get("status") in ("queued", "running")
        ):
            return job
    return None


def _reject_active_infer(request: Request, stem: str) -> None:
    """409 when a queued/running infer job targets ``stem``.

    The job would overwrite the very files the PUT is editing (PUT-vs-infer
    race), so the edit must wait until the job finishes.
    """
    job = find_active_infer_job(request, stem)
    if job is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Inference job #{job.get('id')} for '{stem}' is "
                f"{job.get('status')}; retry after it finishes"
            ),
        )
