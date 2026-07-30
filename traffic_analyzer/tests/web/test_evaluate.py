"""scripts/batch_evaluate.py script-level contract tests.

(/api/evaluate 路由及其任务类型已移除,相关用例一并删除;脚本本体保留。)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class TestBatchEvaluateAtomicWrite:
    def test_atomic_write_text(self, tmp_path: Path, batch_evaluate_module: Any) -> None:
        module = batch_evaluate_module

        target = tmp_path / "latest.json"
        module._atomic_write_text(target, '{"a": 1}')
        assert target.read_text(encoding="utf-8") == '{"a": 1}'
        assert not (tmp_path / "latest.json.tmp").exists()
        module._atomic_write_text(target, '{"a": 2}')
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}


class TestBatchEvaluateNestedVideo:
    def test_nested_video_matched_by_rglob(
        self, tmp_path: Path, batch_evaluate_module: Any
    ) -> None:
        """视频在子目录时(rglob 回退)也能被评估,不再报 No matching video。"""
        module = batch_evaluate_module

        ws = tmp_path / "ws"
        (ws / "sub").mkdir(parents=True)
        (ws / "sub" / "01_Event_129_1_1.mp4").write_bytes(b"\x00" * 64)
        out_dir = ws / "analysis" / "01_Event_129_1_1"
        out_dir.mkdir(parents=True)
        (out_dir / "report.md").write_text(
            "# 报告\n\n二进制编码: `1_0_0_0_0_0_0_0_0_0`\n", encoding="utf-8"
        )
        output = tmp_path / "latest.json"
        rc = module.main(
            [
                "--video-dir", str(ws),
                "--report-dir", str(ws / "analysis"),
                "--gt-mode", "filename",
                "--output", str(output),
            ]
        )
        assert rc == 0
        text = output.read_text(encoding="utf-8")
        assert "01_Event_129_1_1" in text
