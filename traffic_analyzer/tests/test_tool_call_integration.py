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
    """Simulate the orchestrator's 6+ instrumented call shape and verify
    the expected lines are emitted. Real orchestrator integration is
    covered by test_orchestrator.py.
    """
    with tool_call("video_preprocessor.process", video="clip.mp4") as c:
        c.result("coarse=20, precision=41")

    with tool_call("vlm_engine.scene_understanding", provider="claude", frames=20) as c:
        c.result("roads=4, density='normal'")

    with tool_call("external_adapter.load_cv_tracks", path="tracks.json") as c:
        c.result("tracks=15")

    for event in ["E0", "E5", "E6", "E8"]:
        with tool_call("event_detector.detect", event=event, mode="direct_vlm") as c:
            c.result("detected=False, confidence=0.92")

    with tool_call("reasoning_chain.execute", event="E7", steps=2) as parent:
        with tool_call_nested(parent, 1, 2, "vlm_call.pixel_displacement", vehicle="a") as s:
            s.result("magnitude=4.3%, significant=True")
        with tool_call_nested(parent, 2, 2, "aggregate.gather", vehicles=2) as s:
            s.result("detected=True")
        parent.result("detected=True, confidence=0.87")

    with tool_call("post_process.run_inference", phases=3) as c:
        c.result("inferred_added=2")

    with tool_call("report_generator.generate", formats=["md", "json"]) as c:
        c.result("binary_code=0_1_0_0_0_0_0_1_0_0")

    messages = [r.getMessage() for r in caplog_info.records]

    for name in [
        "video_preprocessor.process",
        "vlm_engine.scene_understanding",
        "external_adapter.load_cv_tracks",
        "reasoning_chain.execute",
        "post_process.run_inference",
        "report_generator.generate",
    ]:
        assert any(name in m for m in messages), f"missing top-level {name}"

    direct_calls = [m for m in messages if "event_detector.detect" in m and "🔧" in m]
    assert len(direct_calls) == 4

    nested = [m for m in messages if "step[" in m and "🔧" in m]
    assert len(nested) == 2
    assert all(m.startswith("  ") for m in nested)

    starts = sum(1 for m in messages if "🔧" in m)
    ends = sum(1 for m in messages if "↳ result" in m or "✗ failed" in m)
    assert starts == ends, f"start/end mismatch: starts={starts}, ends={ends}"
