"""Progress-file (JSONL) contract tests: tailing, truncation tolerance, cleanup."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .conftest import _PROGRESS_PREAMBLE, _wait_until


def _submit(script: str):
    from traffic_analyzer.web.jobs import JobManager

    manager = JobManager()
    manager.submit("infer", [sys.executable, "-c", script], stem="x")
    return manager._jobs[1]


class TestProgressFileContract:
    def test_env_var_points_at_per_job_file(self) -> None:
        """子进程能读到 TRAFFIC_ANALYZER_PROGRESS_FILE 且事件驱动进度。"""
        script = _PROGRESS_PREAMBLE + (
            "ev(type='step', step=2, total=4, name='专家')\n"
            "ev(type='register', total=1, lanes=['占道'])\n"
            "ev(type='start', lane='占道')\n"
            "ev(type='phase', lane='占道', fraction=0.5, label='掩码')\n"
        )
        job = _submit(script)
        assert _wait_until(lambda: job.status in ("done", "failed"))
        assert job.status == "done"
        assert job.step_index == 2
        assert job.experts[0]["name"] == "占道"
        assert job.experts[0]["fraction"] == 0.5
        assert job.experts[0]["label"] == "掩码"

    def test_progress_file_deleted_after_job(self) -> None:
        job = _submit(_PROGRESS_PREAMBLE + "ev(type='step', step=1, total=4, name='预处理')\n")
        assert _wait_until(lambda: job.status in ("done", "failed"))
        assert job.progress_path is not None
        assert not job.progress_path.exists()

    def test_garbage_and_truncated_lines_tolerated(self) -> None:
        """非法行跳过;末尾半行留到写全后再应用(分两次 append 的一行)。"""
        script = _PROGRESS_PREAMBLE + (
            "open(_pf, 'a').write('not json at all\\n')\n"
            "open(_pf, 'a').write('[1, 2, 3]\\n')\n"  # 合法 JSON 但不是事件 dict
            "ev(type='step', step=2, total=4, name='专家')\n"
            "ev(type='register', total=1, lanes=['占道'])\n"
            # 一行 JSON 分两次写:前半行不带换行,尾部线程必须拼接后再解析
            "_f = open(_pf, 'a')\n"
            "_f.write('{\"type\": \"phase\", \"lane\": \"占道\", ')\n"
            "_f.flush()\n"
            "time.sleep(1.0)\n"  # 横跨多次轮询
            "_f.write('\"fraction\": 0.9, \"label\": \"收尾\"}\\n')\n"
            "_f.close()\n"
        )
        job = _submit(script)
        assert _wait_until(lambda: job.status in ("done", "failed"))
        assert job.status == "done"
        assert job.step_index == 2
        assert job.experts[0]["fraction"] == 0.9
        assert job.experts[0]["label"] == "收尾"

    def test_final_line_without_newline_applied(self) -> None:
        """最后一行缺换行(缺失 EOF):子进程退出后收尾时仍尝试解析。"""
        script = _PROGRESS_PREAMBLE + (
            "ev(type='register', total=1, lanes=['占道'])\n"
            "open(_pf, 'a').write("
            "'{\"type\": \"phase\", \"lane\": \"占道\", \"fraction\": 0.7, \"label\": \"反思\"}')\n"
        )
        job = _submit(script)
        assert _wait_until(lambda: job.status in ("done", "failed"))
        assert job.status == "done"
        assert job.experts[0]["fraction"] == 0.7

    def test_child_crash_failed_via_returncode(self) -> None:
        """崩溃:文件截断、无 done 事件;终态由 returncode 判定为 failed。"""
        script = _PROGRESS_PREAMBLE + (
            "ev(type='step', step=1, total=4, name='预处理')\n"
            "open(_pf, 'a').write('{\"type\": \"phase\", \"la')\n"  # 截断半行
            "os._exit(3)\n"
        )
        job = _submit(script)
        assert _wait_until(lambda: job.status in ("done", "failed"))
        assert job.status == "failed"
        assert job.returncode == 3
        assert job.step_index == 1  # 崩溃前已落盘的事件仍生效

    def test_stdout_no_longer_drives_progress(self) -> None:
        """stdout 上的旧文本标记只进 log_tail,不再驱动进度状态机。"""
        script = (
            "print('EXPERT_PROGRESS|register|1|占道', flush=True)\n"
            "print('EXPERT_PROGRESS|phase|占道|0.50|掩码', flush=True)\n"
            "print('[3.5/4] SFT label rewrite...', flush=True)\n"
        )
        job = _submit(script)
        assert _wait_until(lambda: job.status in ("done", "failed"))
        assert job.status == "done"
        assert job.experts == []
        assert job.step_index == 0
        assert any("EXPERT_PROGRESS" in line for line in job.log_tail)
