"""Thread-safe per-expert progress reporter with dual output backends.

[文件说明]
作用:逐专家进度上报器(线程安全单例)。ExpertAgentLayer 并行调度前
register 全部 lane(专家类别名 + '裁决'),各专家线程内 start/phase/done/
error 打点;phase 缺省 name 时取 threading.local 记录的当前线程专家名
(worker 首行 start 时设置)。后端选择:stdout 为 TTY 时渲染 rich Live
面板(顶部总进度 + 每 lane 一条进度条,displayed fraction 向 target 线性
缓行、到达后慢速爬升并封顶于下一里程碑);否则向 stdout 打印
EXPERT_PROGRESS|... 标记行(flush=True,CLI 人类可读保留)。结构化
sink:环境变量 TRAFFIC_ANALYZER_PROGRESS_FILE 非空时,每个泳道事件
(register/start/phase/lane_done)同时向该文件 append 一行 JSON(JSONL,
带 ts);emit_step(step,total,name) 供 orchestrator 上报 [x/4] 粗粒度步骤,
emit_run_done(status) 上报整次运行终态;env 未设置时均为 no-op。阶段
fraction/label 定义见 web/expert_phases.json(类别定制→default→内置
default 三级回落),任何异常均被吞掉,绝不影响推理。
上游:core/pipeline_steps.py(ExpertAgentLayer/AdjudicationStep)、
core/expert_agent.py、core/expert_agent_far_enhancement.py、
orchestrator/analysis_orchestrator.py(emit_step/emit_run_done)。
下游:rich(仅 TTY);web/jobs 包尾随 TRAFFIC_ANALYZER_PROGRESS_FILE
JSONL 驱动 web/progress.py 状态机。

Run directly for a demo (8 experts + adjudication, staggered random sleeps):

    python -m traffic_analyzer.utils.progress
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:  # rich is optional: non-TTY / minimal installs fall back to marker lines.
    from rich.console import Group
    from rich.live import Live
    from rich.progress import BarColumn, Progress, TextColumn

    _RICH_AVAILABLE = True
except Exception:  # pragma: no cover - depends on environment
    _RICH_AVAILABLE = False


# Hard-coded fallback phase definitions, used when web/expert_phases.json is
# missing or corrupt. Fractions must stay in sync with that file's "default".
_BUILTIN_PHASES: Dict[str, Dict[str, Dict[str, object]]] = {
    "default": {
        "prepare": {"fraction": 0.05, "label": "选帧备料"},
        "main_detect": {"fraction": 0.15, "label": "主检测分析"},
        "evidence": {"fraction": 0.30, "label": "证据合成"},
        "reclassify": {"fraction": 0.45, "label": "目标复核"},
        "parse": {"fraction": 0.55, "label": "解析判定"},
        "reflect": {"fraction": 0.70, "label": "反思复核"},
        "finish": {"fraction": 0.90, "label": "收尾"},
    },
    "裁决": {
        "prepare": {"fraction": 0.3, "label": "汇总候选"},
        "main_detect": {"fraction": 0.6, "label": "交叉裁决"},
        "finish": {"fraction": 1.0, "label": "定案"},
    },
}

_PHASES_PATH = Path(__file__).resolve().parent.parent / "web" / "expert_phases.json"

_TICK_INTERVAL = 0.08
_EASE_RATE = 0.18
_CRAWL_RATE = 0.0015
_LANE_NAME_WIDTH = 14

# 结构化进度 sink:非空时每个事件向该文件 append 一行 JSON(JSONL)。
# web 子进程模式下由 web/jobs 设置并尾随解析;未设置时所有 emit 为 no-op。
PROGRESS_FILE_ENV = "TRAFFIC_ANALYZER_PROGRESS_FILE"


def _emit_event(payload: Dict[str, Any]) -> None:
    """Append one JSONL event to ``PROGRESS_FILE_ENV`` (never raises)."""
    try:
        path = os.environ.get(PROGRESS_FILE_ENV) or ""
        if not path:
            return
        payload.setdefault("ts", time.time())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:  # never affect inference
        logger.debug("[progress] emit event failed: %s", exc)


def emit_step(step: float, total: int, name: str) -> None:
    """上报 [x/4] 粗粒度步骤(orchestrator 调用;env 未设置时 no-op)。"""
    _emit_event({"type": "step", "step": step, "total": total, "name": name})


def emit_run_done(status: str = "ok") -> None:
    """上报整次运行终态(正常结束 "ok";崩溃时文件可能截断,由 web 侧按
    returncode 判定,不依赖本事件)。"""
    _emit_event({"type": "done", "status": status})


class _Lane:
    """Mutable per-lane progress state."""

    __slots__ = (
        "name",
        "category",
        "target",
        "displayed",
        "crawl_cap",
        "status",
        "status_text",
        "detected",
        "milestones",
        "task_id",
    )

    def __init__(self, name: str, category: str) -> None:
        self.name = name
        self.category = category
        self.target = 0.0
        self.displayed = 0.0
        self.crawl_cap = 0.0
        self.status = "queued"  # queued | running | done | error
        self.status_text = "排队中"
        self.detected: Optional[bool] = None
        self.milestones: List[float] = []
        self.task_id = None


class ProgressReporter:
    """Thread-safe singleton reporter. All public methods never raise."""

    def __init__(self) -> None:
        # RLock: _shutdown_live_locked() runs while holding the lock and calls
        # _tick_once(), which acquires the same lock again.
        self._lock = threading.RLock()
        self._tls = threading.local()
        self._phases: Optional[Dict] = None
        self._reset_state()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def _reset_state(self) -> None:
        self._lanes: Dict[str, _Lane] = {}
        self._order: List[str] = []
        self._total = 0
        self._done_count = 0
        self._backend = "lines"
        self._live = None
        self._overall_progress = None
        self._overall_task = None
        self._lanes_progress = None
        self._stop_event = threading.Event()
        self._tick_thread: Optional[threading.Thread] = None

    def _load_phases(self) -> Dict:
        """Load phase definitions once: JSON file, else built-in default."""
        if self._phases is None:
            try:
                data = json.loads(_PHASES_PATH.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("default"), dict):
                    raise ValueError("expert_phases.json missing 'default' section")
                default = data["default"]
                for key, val in _BUILTIN_PHASES["default"].items():
                    default.setdefault(key, val)
                data.setdefault("裁决", _BUILTIN_PHASES["裁决"])
                self._phases = data
            except Exception as exc:
                logger.warning("[progress] phase file unavailable, using builtin default: %s", exc)
                self._phases = _BUILTIN_PHASES
        return self._phases

    def _lookup_phase(self, category: str, key: str) -> Optional[Tuple[float, str]]:
        phases = self._load_phases()
        cat = phases.get(category) or {}
        entry = cat.get(key) or (phases.get("default") or {}).get(key)
        if not isinstance(entry, dict):
            return None
        try:
            return float(entry["fraction"]), str(entry["label"])
        except Exception:
            return None

    def _milestones_for(self, category: str) -> List[float]:
        phases = self._load_phases()
        merged: Dict[str, Dict] = dict(phases.get("default") or {})
        merged.update(phases.get(category) or {})
        fractions = []
        for entry in merged.values():
            try:
                fractions.append(float(entry["fraction"]))
            except Exception:
                continue
        return sorted(set(fractions))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def register(self, lanes: List[str]) -> None:
        """Register all lanes for one run (expert category names + '裁决')."""
        try:
            with self._lock:
                self._shutdown_live_locked()
                self._reset_state()
                self._total = len(lanes)
                for name in lanes:
                    lane = _Lane(name=name, category=name)
                    lane.milestones = self._milestones_for(name)
                    self._lanes[name] = lane
                    self._order.append(name)

                use_rich = _RICH_AVAILABLE and sys.stdout.isatty()
                self._backend = "rich" if use_rich else "lines"

                if self._backend == "rich":
                    self._setup_rich_locked()
                else:
                    print(
                        f"EXPERT_PROGRESS|register|{self._total}|{','.join(lanes)}",
                        flush=True,
                    )
                _emit_event({
                    "type": "register",
                    "total": self._total,
                    "lanes": list(self._order),
                })
        except Exception as exc:  # never affect inference
            logger.debug("[progress] register failed: %s", exc)

    def start(self, name: str) -> None:
        """Mark a lane as started; records the lane name on this thread."""
        try:
            self._tls.expert_name = name
            with self._lock:
                lane = self._lanes.get(name)
                if lane is None or lane.status != "queued":
                    return
                lane.status = "running"
                if self._backend == "lines":
                    print(f"EXPERT_PROGRESS|start|{name}", flush=True)
                _emit_event({"type": "start", "lane": name})
        except Exception as exc:
            logger.debug("[progress] start failed: %s", exc)

    def phase(self, name_or_key: str, key: Optional[str] = None) -> None:
        """Report a phase milestone.

        Call as ``phase(name, key)`` or ``phase(key)``; in the latter form the
        lane name comes from the current thread's local expert name (set by
        :meth:`start`).
        """
        try:
            if key is None:
                name = getattr(self._tls, "expert_name", None)
                key = name_or_key
                if not name:
                    return
            else:
                name = name_or_key
            resolved = self._lookup_phase(name, key)
            if resolved is None:
                return
            fraction, label = resolved
            with self._lock:
                lane = self._lanes.get(name)
                if lane is None or lane.status not in ("queued", "running"):
                    return
                lane.status = "running"
                lane.status_text = label
                if fraction > lane.target:
                    lane.target = fraction
                    lane.crawl_cap = self._next_milestone(lane, fraction)
                if self._backend == "lines":
                    print(f"EXPERT_PROGRESS|phase|{name}|{fraction:.2f}|{label}", flush=True)
                _emit_event({
                    "type": "phase",
                    "lane": name,
                    "fraction": fraction,
                    "label": label,
                })
        except Exception as exc:
            logger.debug("[progress] phase failed: %s", exc)

    def done(self, name: str, detected: Optional[bool] = None) -> None:
        """Mark a lane finished. ``detected`` True/False for experts, None for 裁决."""
        self._finish(name, detected, error=False)

    def error(self, name: str) -> None:
        """Mark a lane failed."""
        self._finish(name, None, error=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _finish(self, name: str, detected: Optional[bool], error: bool) -> None:
        try:
            with self._lock:
                lane = self._lanes.get(name)
                if lane is None or lane.status in ("done", "error"):
                    return
                self._done_count += 1
                if error:
                    lane.status = "error"
                    lane.status_text = "出错"
                    token = "error"
                else:
                    lane.status = "done"
                    lane.detected = detected
                    if detected is True:
                        lane.status_text = "检出"
                        token = "detected"
                    elif detected is False:
                        lane.status_text = "未检出"
                        token = "undetected"
                    else:
                        lane.status_text = "完成"
                        token = "done"
                lane.target = 1.0
                lane.crawl_cap = 1.0
                if self._backend == "lines":
                    # 收尾阶段行:让 web 泳道的缩略文字停在最终状态(检出/未检出/完成)
                    print(
                        f"EXPERT_PROGRESS|phase|{name}|1.00|{lane.status_text}",
                        flush=True,
                    )
                    print(
                        f"EXPERT_PROGRESS|done|{self._done_count}/{self._total}|{name}|{token}",
                        flush=True,
                    )
                # JSONL 与 stdout 标记同序:先 phase 1.0(终态文案)再 lane_done
                _emit_event({
                    "type": "phase",
                    "lane": name,
                    "fraction": 1.0,
                    "label": lane.status_text,
                })
                _emit_event({
                    "type": "lane_done",
                    "done": self._done_count,
                    "total": self._total,
                    "lane": name,
                    "result": token,
                })
                if self._done_count >= self._total:
                    self._stop_event.set()
        except Exception as exc:
            logger.debug("[progress] finish failed: %s", exc)

    @staticmethod
    def _next_milestone(lane: _Lane, target: float) -> float:
        for frac in lane.milestones:
            if frac > target + 1e-9:
                return frac
        return 0.99

    # ------------------------------------------------------------------
    # Rich Live panel
    # ------------------------------------------------------------------
    def _setup_rich_locked(self) -> None:
        self._overall_progress = Progress(
            TextColumn("[bold cyan]专家总进度"),
            BarColumn(bar_width=None),
            TextColumn("{task.percentage:>5.1f}%"),
            expand=True,
        )
        self._overall_task = self._overall_progress.add_task("overall", total=1.0)
        self._lanes_progress = Progress(
            TextColumn("{task.fields[title]}"),
            BarColumn(bar_width=None),
            TextColumn("{task.fields[status]}"),
            expand=True,
        )
        for name in self._order:
            lane = self._lanes[name]
            title = name if len(name) <= _LANE_NAME_WIDTH else name[: _LANE_NAME_WIDTH - 1] + "…"
            lane.task_id = self._lanes_progress.add_task(
                name, total=1.0, title=title, status=lane.status_text
            )
        self._live = Live(
            Group(self._overall_progress, self._lanes_progress),
            console=None,  # stdout; pipeline logs go to stderr
            refresh_per_second=12,
            transient=False,
        )
        self._live.start()
        self._stop_event.clear()
        self._tick_thread = threading.Thread(
            target=self._tick_loop, name="expert-progress-tick", daemon=True
        )
        self._tick_thread.start()
        atexit.register(self._shutdown_live)

    def _tick_loop(self) -> None:
        """Ease displayed fractions toward targets, then slow-crawl to the cap."""
        while not self._stop_event.wait(_TICK_INTERVAL):
            self._tick_once()
        # Settle: run a few extra ticks so completed bars reach 100%.
        for _ in range(30):
            with self._lock:
                if all(l.displayed >= 0.999 for l in self._lanes.values()):
                    break
            self._tick_once()
            time.sleep(_TICK_INTERVAL)
        self._shutdown_live()

    def _tick_once(self) -> None:
        with self._lock:
            expert_displayed: List[float] = []
            for name in self._order:
                lane = self._lanes[name]
                if lane.displayed < lane.target:
                    lane.displayed += (lane.target - lane.displayed) * _EASE_RATE
                    if lane.target - lane.displayed < 0.004:
                        lane.displayed = lane.target
                elif lane.status == "running" and lane.displayed < lane.crawl_cap:
                    lane.displayed = min(lane.displayed + _CRAWL_RATE, lane.crawl_cap)
                if lane.category != "裁决":
                    expert_displayed.append(lane.displayed)
                if lane.task_id is not None and self._lanes_progress is not None:
                    self._lanes_progress.update(
                        lane.task_id, completed=lane.displayed, status=lane.status_text
                    )
            if self._overall_progress is not None and self._overall_task is not None:
                overall = (
                    sum(expert_displayed) / len(expert_displayed) if expert_displayed else 0.0
                )
                self._overall_progress.update(self._overall_task, completed=overall)

    def _shutdown_live(self) -> None:
        try:
            with self._lock:
                self._shutdown_live_locked()
        except Exception:
            pass

    def _shutdown_live_locked(self) -> None:
        self._stop_event.set()
        if self._live is not None:
            try:
                # Snap to targets so the final frame always renders complete,
                # even if the daemon tick thread is torn down mid-settle.
                for lane in self._lanes.values():
                    lane.displayed = lane.target
                self._tick_once()
                self._live.stop()
            except Exception:
                pass
            self._live = None


_REPORTER = ProgressReporter()


def get_reporter() -> ProgressReporter:
    """Return the process-wide progress reporter singleton."""
    return _REPORTER


# ----------------------------------------------------------------------
# Demo mode: python -m traffic_analyzer.utils.progress
# ----------------------------------------------------------------------
def _demo() -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    reporter = get_reporter()
    categories = [
        "违法停车",
        "应急车道占用",
        "交通事故",
        "高速公路行人出现",
        "摩托车出现",
        "拥堵",
        "道路施工",
        "车辆逆行/倒车",
    ]
    phase_keys = [
        "prepare",
        "main_detect",
        "evidence",
        "reclassify",
        "parse",
        "reflect",
        "finish",
    ]
    reporter.register(categories + ["裁决"])

    def _run_expert(name: str) -> bool:
        reporter.start(name)
        time.sleep(random.uniform(0.1, 0.6))
        for key in phase_keys:
            reporter.phase(key)
            time.sleep(random.uniform(0.2, 0.9))
        detected = random.random() < 0.5
        reporter.done(name, detected)
        return detected

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_run_expert, name): name for name in categories}
        for future in as_completed(futures):
            future.result()

    reporter.phase("裁决", "prepare")
    time.sleep(random.uniform(0.4, 1.0))
    reporter.phase("裁决", "main_detect")
    time.sleep(random.uniform(0.6, 1.2))
    reporter.done("裁决", None)
    time.sleep(0.3)


if __name__ == "__main__":
    _demo()
