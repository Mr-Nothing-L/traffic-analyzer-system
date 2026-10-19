"""前端契约类型生成物(frontend/src/api/contracts.gen.ts)的同步守护。

仿 test_agent_event_contract 的 --check 模式:evidence_schema.py 变更后
未重新生成 contracts.gen.ts 即 fail(生成必须幂等)。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GEN_SCRIPT = ROOT / "scripts" / "gen_frontend_contracts.py"
GEN_TS = ROOT / "frontend" / "src" / "api" / "contracts.gen.ts"


class TestFrontendContractsSync:
    def test_generated_ts_matches_schema(self):
        """contracts.gen.ts 与 evidence_schema.py 同步(--check 漂移即失败)。"""
        proc = subprocess.run(
            [sys.executable, str(GEN_SCRIPT), "--check"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        assert proc.returncode == 0, (
            f"contracts.gen.ts 与 evidence_schema.py 漂移:\n{proc.stdout}\n{proc.stderr}"
        )

    def test_generation_is_idempotent(self):
        """连续生成两次输出逐字节一致(幂等,不引入时间戳等噪声)。"""
        outs = []
        for _ in range(2):
            outs.append(subprocess.run(
                [sys.executable, "-c",
                 f"import runpy; runpy.run_path(r'{GEN_SCRIPT}', run_name='__main__')"],
                capture_output=True, text=True, cwd=ROOT, check=True,
            ) and GEN_TS.read_text(encoding="utf-8"))
        assert outs[0] == outs[1]
        # 幂等生成后 --check 仍同步
        proc = subprocess.run(
            [sys.executable, str(GEN_SCRIPT), "--check"],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert proc.returncode == 0
