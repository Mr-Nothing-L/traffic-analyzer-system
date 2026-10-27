"""SFT Forge 初稿提供器:复用批量推理链路(AnalysisOrchestrator)产出初稿。

[文件说明]
作用:PipelineDraftProvider 以进程内方式跑批量链路([1/4]抽帧预处理 →
  [2/4]事件专家 → [3/4]裁决 → [3.5/4]SFT 改写;不含可选的锚定核验层),
  读取改写产出的 chunk JSON(sft_json/description + action),作为 forge
  的初稿(DraftResult.output = 规范 <think>/<answer> 文本)。prompt 与生产
  检测同源(config/prompts 专家模板 + sft_rewrite.yaml),label 风格与批量
  推理天然对齐。
  步骤进度复用管线既有结构化 sink(TRAFFIC_ANALYZER_PROGRESS_FILE JSONL,
  见 utils/progress.py):tailer 线程尾随转 on_step 回调,供 web 面板展示
  初稿阶段进度(无法逐字流式,多步管线)。
  env(SFT_LABEL_ENABLE/SFT_LABEL_OUTPUT_DIR/TRAFFIC_ANALYZER_PROGRESS_FILE)
  是进程级全局,模块级锁 _PIPELINE_LOCK 串行化管线运行,避免并发任务互踩。
上游:sftforge/engine.py(draft_provider 注入)、web/sftforge(service
  build_engine)、scripts/sft_forge.py(CLI)。
下游:orchestrator/analysis_orchestrator.py、contracts.py(sft_json/
  sft_quarantine_json)、utils/progress.py(进度 JSONL 协议)。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Union

from traffic_analyzer.contracts import sft_json, sft_quarantine_json
from traffic_analyzer.utils.progress import PROGRESS_FILE_ENV

logger = logging.getLogger(__name__)

# 管线 env 是进程级全局(SFT_LABEL_*/PROGRESS_FILE_ENV),串行化以防并发互踩。
_PIPELINE_LOCK = threading.Lock()

# 进度 tailer 轮询间隔(秒)。
_TAIL_INTERVAL = 0.3


class DraftError(RuntimeError):
    """初稿生成失败(预筛拒绝/改写隔离/样本缺失等)。"""


class DraftResult(NamedTuple):
    """初稿:规范 <think>/<answer> 文本 + 检出事件编号 + 链路标记。"""

    output: str
    classes: List[int]
    via: str = "pipeline"  # 链路标识(trace/日志用)


# on_step 回调:payload 为管线进度 JSONL 事件原样 dict(见 utils/progress.py
# 与 contracts.ProgressEvent),调用方自行映射展示。
StepHook = Callable[[Dict[str, Any]], None]


def _tail_progress(path: Path, stop: threading.Event, on_step: StepHook) -> None:
    """尾随进度 JSONL 文件,逐行回调(尽力而为,任何异常静默退出)。"""
    offset = 0
    try:
        while not stop.is_set() or path.exists():
            try:
                if path.exists():
                    with open(path, "r", encoding="utf-8") as fh:
                        fh.seek(offset)
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                on_step(json.loads(line))
                            except Exception:  # noqa: BLE001 - 单行坏数据不中断
                                continue
                        offset = fh.tell()
            except Exception:  # noqa: BLE001 - 文件暂未创建/读取竞争
                pass
            if stop.is_set():
                break
            time.sleep(_TAIL_INTERVAL)
    except Exception:  # noqa: BLE001 - tailer 绝不影响推理
        return


class PipelineDraftProvider:
    """批量链路初稿提供器;签名与 engine 注入协议一致:

    ``provider(video_path, on_step=None) -> DraftResult``
    """

    def __init__(
        self,
        config_dir: Union[str, Path] = "traffic_analyzer/config",
        env_file: Optional[Union[str, Path]] = None,
        orchestrator_factory: Optional[Callable[[str], Any]] = None,
    ) -> None:
        # env_file 给出时从该 .env 所在目录加载配置(与 vlm_video.load_providers
        # 同约定:复用主仓 .env,避免 secret 入 os.environ)。
        if env_file is not None:
            env_path = Path(env_file)
            if env_path.name != ".env":
                raise ValueError(f"env_file 必须指向名为 .env 的文件,收到: {env_path}")
            config_dir = env_path.parent
        self._config_dir = str(config_dir)
        # 测试注入点:默认 AnalysisOrchestrator.from_config_dir
        if orchestrator_factory is None:
            from traffic_analyzer.orchestrator.analysis_orchestrator import (
                AnalysisOrchestrator,
            )

            orchestrator_factory = AnalysisOrchestrator.from_config_dir
        self._factory = orchestrator_factory

    def __call__(
        self, video_path: Union[str, Path], on_step: Optional[StepHook] = None
    ) -> DraftResult:
        path = Path(video_path)
        with _PIPELINE_LOCK, tempfile.TemporaryDirectory(prefix="sftforge_draft_") as tmp:
            tmp_dir = Path(tmp)
            progress_file = tmp_dir / "progress.jsonl"
            # 配置加载期读取 SFT_LABEL_*;运行期读取 PROGRESS_FILE_ENV。
            saved_env = {
                key: os.environ.get(key)
                for key in ("SFT_LABEL_ENABLE", "SFT_LABEL_OUTPUT_DIR", PROGRESS_FILE_ENV)
            }
            stop = threading.Event()
            tailer: Optional[threading.Thread] = None
            try:
                os.environ["SFT_LABEL_ENABLE"] = "true"
                os.environ["SFT_LABEL_OUTPUT_DIR"] = str(tmp_dir)
                os.environ[PROGRESS_FILE_ENV] = str(progress_file)
                if on_step is not None:
                    tailer = threading.Thread(
                        target=_tail_progress,
                        args=(progress_file, stop, on_step),
                        name="sftforge-draft-tail",
                        daemon=True,
                    )
                    tailer.start()

                orchestrator = self._factory(self._config_dir)
                report = orchestrator.analyze(str(path))
            finally:
                stop.set()
                if tailer is not None:
                    tailer.join(timeout=2.0)
                for key, value in saved_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

            if getattr(report, "rejected", False):
                raise DraftError(
                    f"prefilter_reject: {getattr(report, 'reject_reason', 'unknown')}"
                )

            sample_path = sft_json(tmp_dir, path.stem)
            if not sample_path.exists():
                if sft_quarantine_json(tmp_dir, path.stem).exists():
                    raise DraftError(
                        "pipeline_quarantine: 改写样本阳性事件无法锚定,已被管线隔离"
                    )
                raise DraftError("pipeline_no_sample: 管线未产出 SFT 样本 JSON")

            sample = json.loads(sample_path.read_text(encoding="utf-8"))
            description = str(sample.get("description") or "").strip()
            if not description:
                raise DraftError("pipeline_empty_description: 样本 description 为空")
            classes = [int(a) for a in sample.get("action") or []]
            return DraftResult(output=description, classes=sorted(classes))
