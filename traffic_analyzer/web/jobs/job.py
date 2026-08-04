"""Job dataclass and subprocess helpers for the web job queue.

[文件说明]
作用:任务数据类与子进程辅助。Job 描述一个 queued/running/done/failed
子进程(含泳道 experts 与整体 fraction 的进度快照,to_dict 为 /api/jobs
响应契约);build_infer_command 构造 ``python -m traffic_analyzer analyze``
命令(带 --sft-label);_discard_frozen_raw 在 infer 成功(rc==0)时删除该
stem 冻结的 <stem>_raw.json 快照(新推理输出取代被冻结的原始输出);
_terminate_proc 先 SIGTERM 后 SIGKILL。
上游:web/jobs/queue.py(JobManager)、web/jobs/routes.py(build_infer_command,
经包命名空间延迟查找)、web/jobs/__init__.py(聚合导出)。
下游:web/workspace.py 的 analysis/<stem>/ 路径契约、traffic_analyzer CLI。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from traffic_analyzer.web import workspace as workspace_mod
from traffic_analyzer.web.progress import TOTAL_STEPS

logger = logging.getLogger(__name__)

# Repository root (traffic_analyzer/web/jobs/job.py -> parents[3]); the
# analyzer resolves its default --config-dir relative to this directory.
REPO_ROOT = Path(__file__).resolve().parents[3]

_LOG_TAIL_LINES = 30


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


def _discard_frozen_raw(workspace: Path, stem: str) -> None:
    """Delete the frozen ``<stem>_raw.json`` snapshot (best-effort)."""
    raw_path = workspace_mod.analysis_dir(workspace, stem) / f"{stem}_raw.json"
    try:
        raw_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("failed to remove frozen raw snapshot %s: %s", raw_path, exc)


def _terminate_proc(proc: subprocess.Popen, timeout: float = 3.0) -> None:
    """SIGTERM, wait up to ``timeout``, then SIGKILL."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # SIGKILL 后仍不退出(如 D 状态):不再无限阻塞调用方。
            logger.warning("pid %s did not exit after SIGKILL", proc.pid)


@dataclass
class Job:
    """One queued/running/finished subprocess."""

    id: int
    kind: str  # "infer"
    argv: List[str]
    stem: Optional[str] = None
    rel: Optional[str] = None  # workspace-relative video path (infer jobs)
    workspace: Optional[Path] = None  # needed to drop the frozen _raw.json on success
    cwd: Path = REPO_ROOT
    status: str = "queued"  # queued | running | done | failed
    step_label: str = "排队中"
    step_index: int = 0
    fraction: Optional[float] = None
    returncode: Optional[int] = None
    log_tail: Deque[str] = field(default_factory=lambda: deque(maxlen=_LOG_TAIL_LINES))
    proc: Optional[subprocess.Popen] = None  # live child while running
    progress_path: Optional[Path] = None  # 进度 JSONL 文件(任务结束后删除)
    # Expert lanes: {name, status: queued|running|done|error,
    # detected: bool|None, fraction: float, label: str}
    experts: List[Dict[str, Any]] = field(default_factory=list)

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
                "experts": [dict(lane) for lane in self.experts],
            },
            "log_tail": list(self.log_tail),
            "returncode": self.returncode,
        }
