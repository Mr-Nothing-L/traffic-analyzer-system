"""Serial subprocess job queue for the web UI.

Inference and evaluation run as child processes, one at a time, on a single
worker thread. The child's stdout and stderr are merged and read line by
line to update the job's progress (from the analyzer's ``[x/4]`` step
markers) and to keep a rolling log tail (~30 lines).

[文件说明]
作用:单工作线程串行子进程任务队列(JobManager)。build_infer_command 构造
``python -m traffic_analyzer analyze`` 命令(带 --sft-label),build_evaluate_command 构造
scripts/batch_evaluate.py 评估命令;解析子进程输出的 [x/4] 步骤标记更新进度,
提供 /api/infer、/api/jobs、/api/jobs/{id}/cancel 接口;shutdown/cancel 先 SIGTERM 后
SIGKILL,避免遗留子进程。
上游:web/app.py(挂载路由,lifespan/atexit 时调用 shutdown);web/evaluate.py(复用
build_evaluate_command);web/evidence_api.py(检查同 stem 的在跑 infer 任务)。
下游:traffic_analyzer CLI(python -m traffic_analyzer analyze)、scripts/batch_evaluate.py、
web/workspace.py 的 analysis/<stem>/ 路径契约。
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from traffic_analyzer.web import workspace as workspace_mod

router = APIRouter()

# Repository root (traffic_analyzer/web/jobs.py -> parents[2]); the analyzer
# resolves its default --config-dir relative to this directory.
REPO_ROOT = Path(__file__).resolve().parents[2]

TOTAL_STEPS = 5

_LOG_TAIL_LINES = 30

# stdout step marker -> (step_index, step_label). "[3.5/4]" must be matched
# before "[3/4]".
_STEP_MARKERS = (
    ("[3.5/4]", 4, "SFT"),
    ("[1/4]", 1, "预处理"),
    ("[2/4]", 2, "专家"),
    ("[3/4]", 3, "裁决"),
    ("[4/4]", 5, "报告"),
)


def build_infer_command(workspace: Path, video_rel: str, stem: str) -> List[str]:
    """Command analyzing one video, writing results into analysis/<stem>/.

    ``video_rel`` is the workspace-relative video path (``name`` for
    top-level videos); the results contract stays flat.
    """
    out_dir = workspace_mod.analysis_dir(workspace, stem)
    return [
        sys.executable, "-m", "traffic_analyzer", "analyze",
        "--video", str(workspace / video_rel),
        "--format", "markdown",
        "--output", str(out_dir / "report.md"),
        "--sft-label",
        "--sft-output-dir", str(out_dir),
    ]


def build_evaluate_command(workspace: Path) -> List[str]:
    """Command running batch evaluation over the whole workspace."""
    analysis = workspace / "analysis"
    return [
        sys.executable, "scripts/batch_evaluate.py",
        "--video-dir", str(workspace),
        "--report-dir", str(analysis),
        "--gt-mode", "filename",
        "--output", str(analysis / "evaluation" / "latest.json"),
    ]


@dataclass
class Job:
    """One queued/running/finished subprocess."""

    id: int
    kind: str  # "infer" | "evaluate"
    argv: List[str]
    stem: Optional[str] = None
    rel: Optional[str] = None  # workspace-relative video path (infer jobs)
    cwd: Path = REPO_ROOT
    status: str = "queued"  # queued | running | done | failed
    step_label: str = "排队中"
    step_index: int = 0
    fraction: Optional[float] = None
    returncode: Optional[int] = None
    log_tail: Deque[str] = field(default_factory=lambda: deque(maxlen=_LOG_TAIL_LINES))
    proc: Optional[subprocess.Popen] = None  # live child while running

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "stem": self.stem,
            "rel": self.rel,
            "status": self.status,
            "progress": {
                "step_label": self.step_label,
                "step_index": self.step_index,
                "total_steps": TOTAL_STEPS,
                "fraction": self.fraction,
            },
            "log_tail": list(self.log_tail),
            "returncode": self.returncode,
        }


class JobManager:
    """Single-worker serial queue; job ids auto-increment from 1."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[int, Job] = {}
        self._queue: "queue.Queue[Job]" = queue.Queue()
        self._next_id = 1
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
        cwd: Path = REPO_ROOT,
    ) -> int:
        with self._lock:
            job_id = self._next_id
            self._next_id += 1
            self._jobs[job_id] = Job(
                id=job_id, kind=kind, argv=list(argv), stem=stem, rel=rel, cwd=Path(cwd)
            )
        self._queue.put(self._jobs[job_id])
        return job_id

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = [self._jobs[job_id] for job_id in sorted(self._jobs)]
        return [job.to_dict() for job in jobs]

    def _worker_loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                self._run(job)
            except Exception:  # never let the worker thread die
                with self._lock:
                    job.status = "failed"
                    job.log_tail.append("job runner crashed")
            finally:
                self._queue.task_done()

    def _run(self, job: Job) -> None:
        with self._lock:
            if job.status != "queued":
                return  # cancelled / shut down while still in the queue
            job.status = "running"
            job.step_label = "评估中" if job.kind == "evaluate" else "推理中"
        try:
            proc = subprocess.Popen(
                job.argv,
                cwd=str(job.cwd),
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
            with self._lock:
                job.status = "failed"
                job.returncode = -1
                job.log_tail.append(f"failed to start: {exc}")
            return

        with self._lock:
            job.proc = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            with self._lock:
                job.log_tail.append(line)
                for marker, step_index, step_label in _STEP_MARKERS:
                    if marker in line:
                        job.step_index = step_index
                        job.step_label = step_label
                        job.fraction = step_index / TOTAL_STEPS
                        break
        returncode = proc.wait()
        with self._lock:
            job.returncode = returncode
            job.status = "done" if returncode == 0 else "failed"
            if returncode == 0:
                job.fraction = 1.0

    def shutdown(self) -> None:
        """Stop all queued/running jobs (server shutdown; safe to call twice).

        Queued jobs are failed without being started (the worker skips them
        when it later dequeues them); running children get SIGTERM, then
        SIGKILL after ~3s.
        """
        with self._lock:
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
                return job
        proc = job.proc
        if proc is not None and proc.poll() is None:
            _terminate_proc(proc)
        with self._lock:
            if job.status == "running":
                job.returncode = proc.returncode if proc is not None else job.returncode
                job.status = "failed"
            job.log_tail.append("cancelled by user")
        return job


def _terminate_proc(proc: subprocess.Popen, timeout: float = 3.0) -> None:
    """SIGTERM, wait up to ``timeout``, then SIGKILL."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)


class InferRequest(BaseModel):
    stems: Optional[List[str]] = None  # legacy: top-level video stems
    rels: Optional[List[str]] = None   # workspace-relative video paths (any depth)


@router.post("/api/infer")
def post_infer(body: InferRequest, request: Request) -> Dict[str, Any]:
    workspace = workspace_mod.require_workspace(request)
    # Validate everything before queueing anything.
    rels: List[str] = list(body.rels or [])
    for stem in body.stems or []:  # legacy stems map to top-level rels
        workspace_mod.validate_stem(stem)
        video = workspace_mod.find_video(workspace, stem)
        if video is None:
            raise HTTPException(status_code=404, detail=f"Video not found for stem: {stem}")
        rels.append(video.name)

    videos: List[tuple] = []
    seen_stems: Dict[str, str] = {}
    for rel in rels:
        video = workspace_mod.resolve_workspace_file(workspace, rel)
        if video.suffix.lower() not in workspace_mod.VIDEO_EXTENSIONS:
            raise HTTPException(status_code=404, detail=f"Not a video file: {rel}")
        stem = video.stem
        if stem in seen_stems:
            # Results land in the flat analysis/<stem>/ — two rels sharing a
            # stem would overwrite each other, so reject the whole request.
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Stem 冲突:{stem} — {seen_stems[stem]} 与 {rel} "
                    "会写入同一个 analysis 目录,请重命名其中一个视频"
                ),
            )
        seen_stems[stem] = rel
        videos.append((stem, rel))

    job_ids: List[int] = []
    for stem, rel in videos:
        workspace_mod.analysis_dir(workspace, stem).mkdir(parents=True, exist_ok=True)
        argv = build_infer_command(workspace, rel, stem)
        job_ids.append(request.app.state.jobs.submit("infer", argv, stem=stem, rel=rel))
    return {"job_ids": job_ids}


@router.get("/api/jobs")
def get_jobs(request: Request) -> List[Dict[str, Any]]:
    return request.app.state.jobs.list_jobs()


@router.post("/api/jobs/{job_id}/cancel")
def post_cancel_job(job_id: int, request: Request) -> Dict[str, Any]:
    try:
        job = request.app.state.jobs.cancel(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown job id: {job_id}")
    except ValueError:
        raise HTTPException(status_code=409, detail="Job already finished")
    return job.to_dict()
