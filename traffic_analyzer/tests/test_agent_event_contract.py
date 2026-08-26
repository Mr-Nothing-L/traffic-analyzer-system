"""agent 事件契约生成物(agent/config/event_contract.json)的同步守护。

D3 收敛(事件定义/编码/工具限额单一权威源)的漂移防护:
  1. --check:生成物必须与 event_categories.yaml / annotation_spec.yaml 同步;
  2. toolset.json 模型可见限额数字必须与 toolserver 真实约束一致。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from traffic_analyzer.toolserver import server as toolserver_server

ROOT = Path(__file__).resolve().parents[2]
AGENT_CONFIG_DIR = ROOT / "agent" / "config"


class TestAgentEventContractSync:
    def test_generated_contract_matches_yaml(self):
        """event_contract.json 与权威 YAML 同步(--check 漂移即失败)。"""
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "gen_agent_event_contract.py"), "--check"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        assert proc.returncode == 0, (
            f"event_contract.json 与 YAML 漂移:\n{proc.stdout}\n{proc.stderr}"
        )

    def test_contract_active_ids_match_config_manager(self):
        """契约中的活跃事件编号与 ConfigManager 视角一致。"""
        from traffic_analyzer.core.config_manager import ConfigManager

        contract = json.loads((AGENT_CONFIG_DIR / "event_contract.json").read_text(encoding="utf-8"))
        manager = ConfigManager(str(ROOT / "traffic_analyzer" / "config"))
        manager.load_all()
        active_ids = [cat.event_id for cat in manager.get_active_event_categories()]
        assert contract["active_event_ids"] == active_ids


class TestToolsetLimitsMatchToolserver:
    """toolset.json 是模型可见限额文案的单一来源,数字须与 toolserver 常量一致。"""

    @classmethod
    def setup_class(cls):
        toolset = json.loads((AGENT_CONFIG_DIR / "toolset.json").read_text(encoding="utf-8"))
        cls.tools = {tool["name"]: tool for tool in toolset["tools"]}

    def test_extract_frames_frame_caps(self):
        props = self.tools["extract_frames"]["parameters"]["properties"]
        assert props["max_frames"]["maximum"] == toolserver_server._FPS_MODE_MAX_FRAMES
        limits_text = " ".join(self.tools["extract_frames"]["limits"])
        assert str(toolserver_server._HARD_MAX_FRAMES) in limits_text
        assert str(toolserver_server._FPS_MODE_MAX_FRAMES) in limits_text
        assert str(toolserver_server._DEFAULT_MAX_FRAMES) in props["max_frames"]["description"]

    def test_load_video_size_gate(self):
        props = self.tools["load_video"]["parameters"]["properties"]
        assert props["max_mb"]["default"] == toolserver_server._DEFAULT_PREPARE_MAX_MB
        # toolserver 对 max_mb 请求参数的硬上限(pydantic le=100)
        assert props["max_mb"]["maximum"] == toolserver_server._HARD_PREPARE_MAX_MB
