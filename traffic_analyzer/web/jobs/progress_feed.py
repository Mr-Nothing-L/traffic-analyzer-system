"""Per-job structured progress file (JSONL) tailing for the job worker.

Each job subprocess gets its own progress file in the system temp dir,
passed via ``TRAFFIC_ANALYZER_PROGRESS_FILE``; the analyzer child appends
one JSON event per line (see utils/progress.py). :func:`tail_progress_file`
polls the file and feeds events to the web/progress.py state machine.

[文件说明]
作用:任务进度文件(JSONL)的创建/尾随/清理。mkstemp 建文件(名内嵌
job id,并发唯一);tail_progress_file 轮询(~0.5s)增量读取,逐事件交给
web/progress.py 状态机(容忍截断/非法行:JSON 解析失败的行跳过,文件
末尾半行留到下一轮拼接);终态不由事件流判定(子进程崩溃时文件可能
截断、无 done 事件),由 worker 按 returncode 收尾后删除文件。
上游:web/jobs/queue.py(worker 为每个任务起尾随线程)。
下游:web/progress.py(泳道状态机)、utils/progress.py(事件 schema)。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict

from traffic_analyzer.web import progress as progress_mod

# 进度文件轮询间隔(秒)。
POLL_INTERVAL_SEC = 0.5


def new_progress_file(job_id: int) -> Path:
    """任务专属进度文件(系统临时目录,mkstemp 保证并发唯一)。"""
    fd, raw = tempfile.mkstemp(prefix=f"traffic-progress-{job_id}-", suffix=".jsonl")
    os.close(fd)
    return Path(raw)


def unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def job_snapshot(job: Any) -> Dict[str, Any]:
    """SSE 用任务快照(to_dict 去掉 log_tail:进度事件高频,不带日志)。"""
    payload = job.to_dict()
    payload.pop("log_tail", None)
    return payload


def tail_progress_file(
    job: Any,
    proc: Any,
    path: Path,
    lock: threading.Lock,
    on_progress: Callable[[], None],
) -> None:
    """轮询尾随进度文件,逐事件驱动状态机;``on_progress`` 在状态变化后调用。

    ``lock`` 为 JobManager._lock(事件应用与快照读取的串行化);子进程退出
    且文件无增量后返回。
    """
    buf = ""
    offset = 0
    while True:
        data = ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                data = fh.read()
                offset = fh.tell()
        except OSError:
            pass
        if data:
            buf += data
            lines = buf.split("\n")
            buf = lines.pop()  # 末尾半行留到下一轮拼接
            changed = False
            with lock:
                for raw in lines:
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue  # 截断/非法行:容忍,跳过
                    changed = progress_mod.apply_event(job, event) or changed
            if changed:
                on_progress()
        if proc.poll() is not None and not data:
            # 收尾:缓冲区里可能还有一行「写完但未换行」的完整 JSON(缺失
            # EOF 的容忍);真是崩溃截断的半行则解析失败、丢弃。
            if buf.strip():
                try:
                    event = json.loads(buf)
                except ValueError:
                    event = None
                if isinstance(event, dict):
                    with lock:
                        changed = progress_mod.apply_event(job, event)
                    if changed:
                        on_progress()
            break
        time.sleep(POLL_INTERVAL_SEC)
