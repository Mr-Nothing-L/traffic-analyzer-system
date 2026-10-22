"""SFT Forge web 任务管理:后台线程跑引擎 + 事件存储/订阅。

[文件说明]
作用:ForgeJobManager 管理 SFT Forge 生成任务(内存态,单进程假设与
  jobs 队列一致)。submit() 起后台线程逐视频执行引擎;引擎轮次事件
  (round_started/round_completed,载荷含 RoundTrace)经 _EmittingEngine
  桥接,video_completed/job_completed/job_failed 由 runner 发出。全部事件
  按序存入 job.events 并扇出给 SSE 订阅者:订阅与快照在同一把锁内完成,
  中途打开面板的客户端先回放历史再收增量,可完整看到逐轮纠错过程。
  build_engine 是真实引擎装配点(ConfigManager + VideoVLMCaller),测试
  monkeypatch 本模块属性即可注入 fake engine(caller 依赖注入协议的
  web 侧对应)。
上游:web/sftforge/routes.py(三个端点);web/app.py(挂载 router,
  lifespan 退出时 shutdown)。下游:sftforge/engine.py(SftForgeEngine)、
  sftforge/vlm_video.py(load_providers/VideoVLMCaller)、
  sftforge/exporter.py(build_train_row)、core/config_manager.py。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.vlm_exceptions import (
    AllProvidersExhaustedError,
    FatalAPIError,
)
from traffic_analyzer.evaluation import event_names
from traffic_analyzer.sftforge.engine import (
    TEMPLATE_CORRECT_MISSING,
    TEMPLATE_FORWARD,
    TEMPLATE_REVIEW_EXTRA,
    SftForgeEngine,
)
from traffic_analyzer.sftforge.exporter import build_train_row
from traffic_analyzer.sftforge.models import VideoForgeResult
from traffic_analyzer.sftforge.vlm_video import VideoVLMCaller, load_providers

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_DIR = _REPO_ROOT / "traffic_analyzer" / "config"

# 轮次事件由引擎桥接发出;终态事件由 runner 发出(任一为流终点)。
TERMINAL_EVENTS = ("job_completed", "job_failed")
# 引擎事件回调签名:on_event(type, payload)。
EngineEventHook = Callable[[str, Dict[str, Any]], None]


class _EmittingEngine(SftForgeEngine):
    """在每次 VLM 调用前后发出 round_started/round_completed 事件的引擎。

    round_completed 载荷含完整 RoundTrace(prompt/response/patched),供前端
    逐轮可视化纠错过程;round_started 只带轮次元数据(此时 prompt 尚未渲染)。
    """

    def __init__(self, *args: Any, on_event: EngineEventHook, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_event = on_event

    def _call(
        self,
        result: VideoForgeResult,
        round_no: int,
        kind: str,
        event_ids: List[int],
        template_id: str,
        context_vars: Dict[str, object],
        video_path: Path,
    ) -> Optional[str]:
        self._on_event(
            "round_started",
            {
                "video": str(video_path),
                "round_no": round_no,
                "kind": kind,
                "event_ids": list(event_ids),
            },
        )
        response = super()._call(
            result, round_no, kind, event_ids, template_id, context_vars, video_path
        )
        # _call 内部必然已 append 本轮 trace(FatalAPIError 上抛时不会走到这里,
        # 终态由 runner 的 job_failed 覆盖)。
        trace = result.rounds[-1]
        self._on_event(
            "round_completed",
            {"video": str(video_path), "trace": trace.model_dump()},
        )
        return response


def build_engine(max_rounds: int, on_event: EngineEventHook) -> SftForgeEngine:
    """装配真实引擎(测试 monkeypatch 此函数注入 fake engine)。"""
    manager = ConfigManager(config_dir=str(_CONFIG_DIR))
    manager.load_all()
    templates = {
        tid: manager.get_prompt_template(tid)
        for tid in (TEMPLATE_FORWARD, TEMPLATE_CORRECT_MISSING, TEMPLATE_REVIEW_EXTRA)
    }
    names = event_names(_CONFIG_DIR / "event_categories.yaml")
    caller = VideoVLMCaller(load_providers(None, _CONFIG_DIR))
    return _EmittingEngine(
        caller=caller,
        templates=templates,
        categories=manager.get_event_categories(),
        max_rounds=max_rounds,
        names=names,
        on_event=on_event,
    )


class ForgeJob:
    """单个 SFT Forge 生成任务(状态机:queued → running → done/failed)。"""

    def __init__(self, job_id: int, videos: Sequence[Path], max_rounds: int) -> None:
        self.id = job_id
        self.videos = [Path(v) for v in videos]
        self.max_rounds = max_rounds
        self.status = "queued"
        self.events: List[Dict[str, Any]] = []
        self.subscribers: Set[asyncio.Queue] = set()
        self.lock = threading.Lock()
        # 终态产出(GET /result):训练行、quarantine 列表、逐视频摘要、汇总统计。
        self.rows: List[Dict[str, Any]] = []
        self.quarantine: List[Dict[str, Any]] = []
        self.video_summaries: List[Dict[str, Any]] = []
        self.stats = {"match": 0, "quarantine": 0, "skipped": 0}
        self.error: Optional[str] = None


class ForgeJobManager:
    """SFT Forge 任务注册表 + 后台执行器(线程安全,事件可回放)。"""

    def __init__(self) -> None:
        self._jobs: Dict[int, ForgeJob] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._threads: List[threading.Thread] = []

    # ------------------------------------------------------------------
    # Submit / lookup
    # ------------------------------------------------------------------
    def submit(self, videos: Sequence[Path], max_rounds: int) -> int:
        with self._lock:
            job_id = next(self._ids)
            job = ForgeJob(job_id, videos, max_rounds)
            self._jobs[job_id] = job
        thread = threading.Thread(
            target=self._run_job, args=(job,), name=f"sftforge-job-{job_id}", daemon=True
        )
        with self._lock:
            self._threads.append(thread)
        thread.start()
        return job_id

    def get(self, job_id: int) -> Optional[ForgeJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def shutdown(self, timeout: float = 5.0) -> None:
        """服务退出时 best-effort 等待在跑任务(daemon 线程兜底)。"""
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Events:存储 + 扇出(emit 在 worker 线程,subscribe 在 loop 线程)
    # ------------------------------------------------------------------
    def emit(self, job: ForgeJob, type_: str, payload: Dict[str, Any]) -> None:
        event = {"type": type_, "data": payload}
        with job.lock:
            job.events.append(event)
            subs = list(job.subscribers)
            loop = self._loop
        if loop is None or not subs:
            return
        for q in subs:
            try:
                loop.call_soon_threadsafe(self._offer, q, event)
            except RuntimeError:  # loop 已关闭(服务退出中)
                pass

    @staticmethod
    def _offer(q: asyncio.Queue, event: Dict[str, Any]) -> None:
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - 竞态防御
                pass
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover - 竞态防御
            pass

    def subscribe(self, job: ForgeJob) -> Tuple[asyncio.Queue, List[Dict[str, Any]]]:
        """原子地注册订阅并返回历史快照:先回放快照再收队列,不丢不重。"""
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        with job.lock:
            self._loop = asyncio.get_running_loop()
            job.subscribers.add(q)
            snapshot = list(job.events)
        return q, snapshot

    def unsubscribe(self, job: ForgeJob, q: asyncio.Queue) -> None:
        with job.lock:
            job.subscribers.discard(q)

    # ------------------------------------------------------------------
    # Runner(worker 线程)
    # ------------------------------------------------------------------
    def _run_job(self, job: ForgeJob) -> None:
        job.status = "running"
        try:
            # 经包命名空间延迟查找,测试 monkeypatch
            # traffic_analyzer.web.sftforge.build_engine 即生效(对齐
            # web/jobs 的 build_infer_command 约定)。
            from traffic_analyzer.web import sftforge as _pkg

            engine = _pkg.build_engine(
                max_rounds=job.max_rounds,
                on_event=lambda type_, payload: self.emit(job, type_, payload),
            )
        except Exception as exc:  # noqa: BLE001 - 配置/provider 缺失等装配失败
            logger.error("[sftforge-web] job %d engine build failed: %s", job.id, exc)
            job.status = "failed"
            job.error = f"engine_build_failed: {exc}"
            self.emit(job, "job_failed", {"error": job.error})
            return

        names = getattr(engine, "_names", None) or {}
        try:
            for video in job.videos:
                result = engine.run(video)
                self._handle_result(job, result, names)
        except (FatalAPIError, AllProvidersExhaustedError) as exc:
            logger.error("[sftforge-web] job %d aborted (API unavailable): %s", job.id, exc)
            job.status = "failed"
            job.error = f"api_unavailable: {exc}"
            self.emit(job, "job_failed", {"error": job.error})
            return
        except Exception as exc:  # noqa: BLE001 - 兜底:单任务失败不能拖垮 worker
            logger.exception("[sftforge-web] job %d failed", job.id)
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            self.emit(job, "job_failed", {"error": job.error})
            return

        job.status = "done"
        self.emit(job, "job_completed", {"stats": dict(job.stats)})

    def _handle_result(
        self, job: ForgeJob, result: VideoForgeResult, names: Dict[int, str]
    ) -> None:
        summary = {
            "video": result.video,
            "match": result.match,
            "skipped": result.skipped,
            "reason": result.reason,
            "gt": result.gt,
            "rounds": len(result.rounds),
        }
        with job.lock:
            job.video_summaries.append(summary)
            if result.skipped:
                job.stats["skipped"] += 1
            elif result.match:
                job.rows.append(
                    build_train_row(len(job.rows), result.video, result.final_output,
                                    result.gt, names)
                )
                job.stats["match"] += 1
            else:
                job.quarantine.append(
                    {"video": result.video, "reason": result.reason, "gt": result.gt}
                )
                job.stats["quarantine"] += 1
        self.emit(job, "video_completed", summary)


def job_result_payload(job: ForgeJob) -> Dict[str, Any]:
    """GET /result 响应:状态 + 训练 JSONL 行 + 汇总统计 + quarantine 列表。"""
    with job.lock:
        return {
            "job_id": job.id,
            "status": job.status,
            "error": job.error,
            "stats": dict(job.stats),
            "videos": list(job.video_summaries),
            "jsonl": list(job.rows),
            "quarantine": list(job.quarantine),
        }


def format_sse(event: Dict[str, Any]) -> str:
    data = json.dumps(event["data"], ensure_ascii=False)
    return f"event: {event['type']}\ndata: {data}\n\n"
