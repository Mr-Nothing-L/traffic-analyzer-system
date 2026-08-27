"""Serial single-worker job queue (JobManager) and subprocess lifecycle.

Inference jobs run as child processes, one at a time, on a single
worker thread. The child writes structured progress events (JSONL) to a
per-job file named by ``TRAFFIC_ANALYZER_PROGRESS_FILE``; a tailer thread
(jobs/progress_feed.py) polls that file and feeds events to the
web/progress.py state machine, while the worker keeps reading the child's
merged stdout/stderr line by line for the rolling log tail (~30 lines).

[文件说明]
作用:单工作线程串行任务队列(JobManager + worker + 子进程生命周期)。
每个任务启动前创建专属进度文件(jobs/progress_feed.py),经
TRAFFIC_ANALYZER_PROGRESS_FILE 传给子进程,尾随线程驱动进度状态机,
任务结束/取消后删除该文件;stdout 仍逐行读入 log_tail,但不再解析文本
标记。cancel/shutdown 先 SIGTERM 后 SIGKILL;cancel 竞态处理:锁内读
job.proc,worker 挂上 proc 后复查 status(非 running 立即 terminate),
收尾块只在 status=='running' 时改写 status/returncode;任务超时(默认
4 小时,TRAFFIC_ANALYZER_JOB_TIMEOUT_SECONDS 可调,<=0 禁用)到点
terminate 并标 failed;shutdown 后置 _shutdown 标志,submit 拒绝新任务;
infer 成功(rc==0)后调度关键帧自动智能挑选(_schedule_keyframe_auto_pick,
daemon 线程,失败仅告警)。
进度更新与任务终态经 EventBus(realtime)publish job.progress/job.done。
上游:web/jobs/__init__.py(聚合导出);web/jobs/routes.py(路由层)。
下游:web/jobs/job.py(Job/子进程辅助)、web/jobs/progress_feed.py(进度
文件尾随)、web/workspace.py(infer 完成后缓存失效)、web/realtime.py(事件
总线)、traffic_analyzer CLI(python -m traffic_analyzer analyze)。
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from traffic_analyzer.utils import progress as utils_progress
from traffic_analyzer.web import workspace as workspace_mod
from traffic_analyzer.web.jobs import progress_feed
from traffic_analyzer.web.jobs.job import (
    REPO_ROOT,
    Job,
    _discard_frozen_raw,
    _terminate_proc,
)
from traffic_analyzer.web.progress import _finish_stage_lanes

logger = logging.getLogger(__name__)

# 任务超时:默认 4 小时,环境变量可调(<=0 禁用)。
_JOB_TIMEOUT_ENV = "TRAFFIC_ANALYZER_JOB_TIMEOUT_SECONDS"
_DEFAULT_JOB_TIMEOUT_SEC = 4 * 3600.0


def _schedule_keyframe_auto_pick(job: Job) -> None:
    """infer 成功后的关键帧自动智能挑选(延迟导入避免 jobs↔keyframes 环)。

    无 SFT 标注/已有关键帧时 keyframes 侧静默跳过;任何异常仅告警,
    绝不影响已完成任务的状态。
    """
    try:
        from traffic_analyzer.web.keyframes import schedule_after_infer

        schedule_after_infer(job.workspace, job.stem)
    except Exception as exc:  # pragma: no cover - 钩子自身故障兜底
        logger.warning("schedule keyframe auto-pick failed for %s: %s", job.stem, exc)


class JobManager:
    """Single-worker serial queue; job ids auto-increment from 1.

    ``timeout_sec``: per-job wall-clock limit; ``None`` reads
    ``TRAFFIC_ANALYZER_JOB_TIMEOUT_SECONDS`` (default 4h), <=0 disables.
    A timed-out job is terminated (SIGTERM→SIGKILL) and marked failed.
    ``bus``: optional realtime.EventBus; progress/terminal transitions are
    published as ``job.progress`` / ``job.done`` events.
    """

    def __init__(self, timeout_sec: Optional[float] = None, bus: Any = None) -> None:
        if timeout_sec is None:
            try:
                timeout_sec = float(
                    os.environ.get(_JOB_TIMEOUT_ENV, _DEFAULT_JOB_TIMEOUT_SEC)
                )
            except ValueError:
                logger.warning(
                    "Invalid %s=%r, falling back to default %ss",
                    _JOB_TIMEOUT_ENV, os.environ.get(_JOB_TIMEOUT_ENV),
                    _DEFAULT_JOB_TIMEOUT_SEC,
                )
                timeout_sec = _DEFAULT_JOB_TIMEOUT_SEC
        self._timeout_sec: Optional[float] = (
            timeout_sec if timeout_sec and timeout_sec > 0 else None
        )
        self._bus = bus
        self._lock = threading.Lock()
        self._jobs: Dict[int, Job] = {}
        self._queue: "queue.Queue[Job]" = queue.Queue()
        self._next_id = 1
        self._shutdown = False
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="traffic-web-jobs"
        )
        self._worker.start()

    def submit(
        self,
        kind: str,
        argv: List[str],
        stem: Optional[str] = None,
        rel: Optional[str] = None,
        workspace: Optional[Path] = None,
        cwd: Path = REPO_ROOT,
    ) -> int:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("JobManager is shut down; not accepting new jobs")
            job_id = self._next_id
            self._next_id += 1
            self._jobs[job_id] = Job(
                id=job_id, kind=kind, argv=list(argv), stem=stem, rel=rel,
                workspace=workspace, cwd=Path(cwd),
            )
        self._queue.put(self._jobs[job_id])
        return job_id

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = [self._jobs[job_id] for job_id in sorted(self._jobs)]
        return [job.to_dict() for job in jobs]

    def _publish_job(self, job: Job, etype: str) -> None:
        """Publish a job snapshot to the event bus (no-op without one)."""
        bus = self._bus
        if bus is None:
            return
        with self._lock:
            payload = progress_feed.job_snapshot(job)
        bus.publish(etype, payload)

    def _worker_loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                self._run(job)
            except Exception:  # never let the worker thread die
                logger.exception("job #%s runner crashed", job.id)
                with self._lock:
                    job.status = "failed"
                    job.log_tail.append("job runner crashed")
                self._publish_job(job, "job.done")
            finally:
                self._queue.task_done()

    def _run(self, job: Job) -> None:
        with self._lock:
            if job.status != "queued":
                return  # cancelled / shut down while still in the queue
            job.status = "running"
            job.step_label = "推理中"
        self._publish_job(job, "job.progress")
        progress_path = progress_feed.new_progress_file(job.id)
        with self._lock:
            job.progress_path = progress_path
        env = dict(os.environ)
        env[utils_progress.PROGRESS_FILE_ENV] = str(progress_path)
        try:
            proc = subprocess.Popen(
                job.argv,
                cwd=str(job.cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                # Own session: the child's fate is controlled by us (shutdown /
                # cancel), not by the terminal's process group.
                start_new_session=True,
            )
        except OSError as exc:
            progress_feed.unlink_quiet(progress_path)
            with self._lock:
                job.status = "failed"
                job.returncode = -1
                job.log_tail.append(f"failed to start: {exc}")
            self._publish_job(job, "job.done")
            return

        with self._lock:
            job.proc = proc
            still_running = job.status == "running"
        if not still_running:
            # cancel/timeout/shutdown landed while Popen was starting: kill the
            # child we just spawned instead of letting it run unattached.
            _terminate_proc(proc)
            progress_feed.unlink_quiet(progress_path)
            with self._lock:
                job.returncode = proc.returncode
            return

        timer: Optional[threading.Timer] = None
        if self._timeout_sec is not None:
            timer = threading.Timer(self._timeout_sec, self._on_job_timeout, args=(job,))
            timer.daemon = True
            timer.start()
        tailer = threading.Thread(
            target=progress_feed.tail_progress_file,
            args=(job, proc, progress_path, self._lock),
            kwargs={"on_progress": lambda: self._publish_job(job, "job.progress")},
            daemon=True,
            name=f"traffic-web-jobs-progress-{job.id}",
        )
        try:
            tailer.start()
            assert proc.stdout is not None
            for line in proc.stdout:
                with self._lock:
                    job.log_tail.append(line.rstrip("\n"))
            returncode = proc.wait()
        finally:
            if timer is not None:
                timer.cancel()
            # 收尾前放干进度文件(子进程退出前的最后一批事件已落盘)。
            tailer.join(timeout=10.0)
            progress_feed.unlink_quiet(progress_path)
        with self._lock:
            # cancel/timeout/shutdown may have marked the job failed while we
            # were reading stdout; only a still-running job gets its final
            # status from the exit code.
            if job.status == "running":
                job.returncode = returncode
                job.status = "done" if returncode == 0 else "failed"
                if returncode == 0:
                    _finish_stage_lanes(job)
                    job.fraction = 1.0
        self._publish_job(job, "job.done")
        if returncode == 0 and job.kind == "infer" and job.stem and job.workspace:
            # 重推理成功:新的 <stem>.json 已是当前原始输出,冻结的旧快照
            # (<stem>_raw.json)失去「编辑前基线」意义,删除以免 dashboard
            # 继续把旧快照当作原始输出。
            _discard_frozen_raw(job.workspace, job.stem)
            _schedule_keyframe_auto_pick(job)
        if job.kind == "infer":
            # infer 完成(无论成败,<stem>.json / 快照可能已变化):
            # 看板与视频列表缓存失效,下一 GET 重算。
            workspace_mod.invalidate_caches()

    def _on_job_timeout(self, job: Job) -> None:
        """Timer callback: terminate the child and mark the job failed."""
        with self._lock:
            if job.status != "running":
                return
            proc = job.proc
            job.log_tail.append(
                f"job timed out after {self._timeout_sec:.0f}s, terminating"
            )
        if proc is not None and proc.poll() is None:
            _terminate_proc(proc)
        with self._lock:
            if job.status == "running":
                job.returncode = proc.returncode if proc is not None else job.returncode
                job.status = "failed"
        self._publish_job(job, "job.done")

    def shutdown(self) -> None:
        """Stop all queued/running jobs (server shutdown; safe to call twice).

        Queued jobs are failed without being started (the worker skips them
        when it later dequeues them); running children get SIGTERM, then
        SIGKILL after ~3s. After shutdown, ``submit`` refuses new jobs.
        """
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            jobs = list(self._jobs.values())
            for job in jobs:
                if job.status == "queued":
                    job.status = "failed"
                    job.log_tail.append("server shutdown")
        for job in jobs:
            proc = job.proc
            if job.status == "running" and proc is not None and proc.poll() is None:
                with self._lock:
                    job.log_tail.append("server shutdown")
                _terminate_proc(proc)
                with self._lock:
                    if job.status == "running":
                        job.returncode = proc.returncode
                        job.status = "failed"

    def cancel(self, job_id: int) -> Job:
        """Cancel a queued or running job.

        Raises KeyError for an unknown id, ValueError if the job already
        reached a terminal state.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status in ("done", "failed"):
                raise ValueError(job.status)
            if job.status == "queued":
                job.status = "failed"
                job.log_tail.append("cancelled by user")
                queued = True
            else:
                queued = False
            # 锁内读 job.proc:worker 可能正在 Popen 与「挂上 proc」之间,
            # 此时 proc 为 None —— 不在这里误读旧值/漏读新值;worker 挂上
            # proc 后会复查 status,看到非 running 立即自行 terminate。
            proc = job.proc
        if not queued:
            if proc is not None and proc.poll() is None:
                _terminate_proc(proc)
            with self._lock:
                if job.status == "running":
                    job.returncode = (
                        proc.returncode if proc is not None else job.returncode
                    )
                    job.status = "failed"
                job.log_tail.append("cancelled by user")
        self._publish_job(job, "job.done")
        return job
