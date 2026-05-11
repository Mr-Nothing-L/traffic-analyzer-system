"""Unit tests for tool_call_logger context manager."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from traffic_analyzer.utils.tool_call_logger import (
    ToolCall,
    tool_call,
    tool_call_nested,
)


@pytest.fixture
def caplog_info(caplog):
    caplog.set_level(logging.INFO, logger="traffic_analyzer.tool_call")
    return caplog


@patch.dict("os.environ", {"TRAFFIC_ANALYZER_TOOL_LOG_LEVEL": "mid"})
def test_single_tool_call_logs_start_and_result(caplog_info):
    with tool_call("video_preprocessor.process", path="clip.mp4", fps=4) as call:
        call.result("20 frames extracted")
    messages = [r.getMessage() for r in caplog_info.records]
    assert any("🔧 tool_call: video_preprocessor.process(path='clip.mp4', fps=4)" in m for m in messages)
    assert any("↳ result: 20 frames extracted" in m and "elapsed=" in m for m in messages)


@patch.dict("os.environ", {"TRAFFIC_ANALYZER_TOOL_LOG_LEVEL": "mid"})
def test_nested_tool_call_indents(caplog_info):
    with tool_call("reasoning_chain.execute", event="E7", steps=2) as parent:
        with tool_call_nested(parent, 1, 2, "vlm.candidate_extraction", provider="claude") as sub:
            sub.result("candidates=2 vehicles")
        parent.result("detected=true, confidence=0.87")
    messages = [r.getMessage() for r in caplog_info.records]
    parent_line = next(m for m in messages if "reasoning_chain.execute" in m and "🔧" in m)
    nested_line = next(m for m in messages if "step[1/2]" in m and "🔧" in m)
    assert not parent_line.startswith(" "), "parent line should have no indent"
    assert nested_line.startswith("  "), "nested line should be indented 2 spaces"


@patch.dict("os.environ", {"TRAFFIC_ANALYZER_TOOL_LOG_LEVEL": "mid"})
def test_exception_path_logs_failed(caplog_info):
    with pytest.raises(ValueError):
        with tool_call("vlm.call", provider="claude"):
            raise ValueError("boom")
    messages = [r.getMessage() for r in caplog_info.records]
    assert any("✗ failed: ValueError" in m for m in messages)


@patch.dict("os.environ", {"TRAFFIC_ANALYZER_TOOL_LOG_LEVEL": "off"})
def test_off_level_is_noop(caplog_info):
    with tool_call("foo.bar", a=1) as call:
        call.result("done")
    assert len(caplog_info.records) == 0


@patch.dict("os.environ", {"TRAFFIC_ANALYZER_TOOL_LOG_LEVEL": "mid"})
def test_args_formatting_strings_and_lists(caplog_info):
    long_list = list(range(10))
    with tool_call("x.y", text="hello", items=long_list) as call:
        call.result("ok")
    messages = [r.getMessage() for r in caplog_info.records]
    start_line = next(m for m in messages if "🔧 tool_call" in m)
    assert "text='hello'" in start_line
    assert "items=[10 items]" in start_line


@patch.dict("os.environ", {"TRAFFIC_ANALYZER_TOOL_LOG_LEVEL": "macro"})
def test_macro_level_skips_nested(caplog_info):
    with tool_call("reasoning_chain.execute", event="E7", steps=2) as parent:
        with tool_call_nested(parent, 1, 2, "vlm.x") as sub:
            sub.result("ok")
        parent.result("done")
    messages = [r.getMessage() for r in caplog_info.records]
    assert any("reasoning_chain.execute" in m for m in messages), "parent should still log"
    assert not any("step[1/2]" in m for m in messages), "nested should be skipped in macro"
