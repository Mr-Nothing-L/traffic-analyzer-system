"""Serial subprocess job queue for the web UI.

Inference and evaluation run as child processes, one at a time, on a single
worker thread. The child's stdout and stderr are merged and read line by
line to update the job's progress (from the analyzer's ``[x/4]`` step
markers) and to keep a rolling log tail (~30 lines).
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


def build_infer_command(workspace: Path, video_name: str, stem: str) -> List[str]:
    """Command analyzing one video, writing results into analysis/<stem>/."""
    out_dir = workspace_mod.analysis_dir(workspace, stem)
    return [
        sys.executable, "-m", "traffic_analyzer", "analyze",
        "--video", str(workspace / video_name),
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
    cwd: Path = REPO_ROOT
    status: str = "queued"  # queued | running | done | failed
    step_label: str = "排队中"
    step_index: int = 0
    fraction: Optional[float] = None
    returncode: Optional[int] = None
    log_tail: Deque[str] = field(default_factory=lambda: deque(maxlen=_LOG_TAIL_LINES))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "stem": self.stem,
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
        cwd: Path = REPO_ROOT,
    ) -> int:
        with self._lock:
            job_id = self._next_id
            self._next_id += 1
            self._jobs[job_id] = Job(id=job_id, kind=kind, argv=list(argv), stem=stem, cwd=Path(cwd))
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
            )
        except OSError as exc:
            with self._lock:
                job.status = "failed"
                job.returncode = -1
                job.log_tail.append(f"failed to start: {exc}")
            return

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


class InferRequest(BaseModel):
    stems: List[str]


@router.post("/api/infer")
def post_infer(body: InferRequest, request: Request) -> Dict[str, Any]:
    workspace = workspace_mod.require_workspace(request)
    # Validate everything before queueing anything.
    videos = []
    for stem in body.stems:
        workspace_mod.validate_stem(stem)
        video = workspace_mod.find_video(workspace, stem)
        if video is None:
            raise HTTPException(status_code=404, detail=f"Video not found for stem: {stem}")
        videos.append((stem, video))

    job_ids: List[int] = []
    for stem, video in videos:
        workspace_mod.analysis_dir(workspace, stem).mkdir(parents=True, exist_ok=True)
        argv = build_infer_command(workspace, video.name, stem)
        job_ids.append(request.app.state.jobs.submit("infer", argv, stem=stem))
    return {"job_ids": job_ids}


@router.get("/api/jobs")
def get_jobs(request: Request) -> List[Dict[str, Any]]:
    return request.app.state.jobs.list_jobs()
