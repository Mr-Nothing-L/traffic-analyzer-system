"""Serial subprocess job queue package for the web UI.

Inference jobs run as child processes, one at a time, on a single
worker thread. Progress flows through a structured per-job JSONL file
(``TRAFFIC_ANALYZER_PROGRESS_FILE``) instead of stdout text markers; the
child's merged stdout/stderr is still read line by line for the rolling
log tail (~30 lines). Progress and terminal transitions are published to
the realtime event bus (``job.progress`` / ``job.done``).

[文件说明]
作用:web 任务队列包(自原单文件 jobs.py 拆分)。queue.py 为 JobManager +
worker + 子进程生命周期(进度文件尾随、SIGTERM→SIGKILL、超时、cancel
竞态);job.py 为 Job 数据类与子进程辅助;progress_feed.py 为进度文件
(JSONL)尾随;routes.py 为 /api/infer、/api/jobs、/api/jobs/{id}/cancel
路由;本模块聚合导出,保持 ``from traffic_analyzer.web import jobs`` 与
``jobs.router``/``jobs.JobManager``/``jobs.build_infer_command`` 的既有用法。
上游:web/app.py(挂载路由,lifespan/atexit 时调用 shutdown);
web/evidence_api.py(PUT 检查同 stem 在跑 infer,并在首次 SFT 编辑前冻结
<stem>_raw.json;post_infer 复用其 _put_locks 与 find_active_infer_job)。
下游:web/jobs/queue.py、web/jobs/routes.py。
"""

from __future__ import annotations

from traffic_analyzer.web.jobs.job import (
    REPO_ROOT,
    Job,
    _discard_frozen_raw,
    _terminate_proc,
    build_infer_command,
)
from traffic_analyzer.web.jobs.queue import JobManager
from traffic_analyzer.web.jobs.routes import router

__all__ = [
    "REPO_ROOT",
    "Job",
    "JobManager",
    "build_infer_command",
    "router",
]
