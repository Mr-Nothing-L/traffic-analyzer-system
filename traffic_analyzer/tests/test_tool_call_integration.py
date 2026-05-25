"""Smoke test: tool_call lines appear during a simulated pipeline shape."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from traffic_analyzer.utils.tool_call_logger import tool_call, tool_call_nested


@pytest.fixture
def caplog_info(caplog):
    caplog.set_level(logging.INFO, logger="traffic_analyzer.tool_call")
    return caplog


@patch.dict("os.environ", {"TRAFFIC_ANALYZER_TOOL_LOG_LEVEL": "mid"})
def test_simulated_pipeline_emits_expected_tool_calls(caplog_info):
    """Simulate the orchestrator's instrumented call shape and verify
    the expected lines are emitted. Real orchestrator integration is
    covered by test_orchestrator.py.
    """
    with tool_call("video_preprocessor.process", video="clip.mp4") as c:
        c.result("coarse=20, precision=41")

    # v2.0.0: ExpertAgentLayer - parallel expert agents
    for event in ["E0", "E5", "E6", "E8"]:
        with tool_call("expert_agent.detect", event=event) as c:
            c.result("detected=False, confidence=0.92")

    # v2.0.0: Adjudication step
    with tool_call("adjudication.adjudicate", candidates=4) as c:
        c.result("events=0, reasoning='no violations found'")

    with tool_call("report_generator.generate", formats=["md", "json"]) as c:
        c.result("binary_code=0_1_0_0_0_0_0_1_0_0")

    messages = [r.getMessage() for r in caplog_info.records]

    for name in [
        "video_preprocessor.process",
        "expert_agent.detect",
        "adjudication.adjudicate",
        "report_generator.generate",
    ]:
        assert any(name in m for m in messages), f"missing top-level {name}"

    expert_calls = [m for m in messages if "expert_agent.detect" in m and "🔧" in m]
    assert len(expert_calls) == 4

    starts = sum(1 for m in messages if "🔧" in m)
    ends = sum(1 for m in messages if "↳ result" in m or "✗ failed" in m)
    assert starts == ends, f"start/end mismatch: starts={starts}, ends={ends}"
