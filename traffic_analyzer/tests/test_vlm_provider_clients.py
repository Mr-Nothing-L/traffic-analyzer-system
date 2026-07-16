"""
Unit tests for provider-specific callers in vlm_provider_clients.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from traffic_analyzer.core.vlm_provider_clients import (
    _call_anthropic,
    _call_anthropic_with_tools,
)


def _make_response(content: list[dict[str, Any]], input_tokens: int = 1, output_tokens: int = 2) -> MagicMock:
    response = MagicMock()
    response.content = [
        SimpleNamespace(**block) for block in content
    ]
    response.usage.input_tokens = input_tokens
    response.usage.output_tokens = output_tokens
    response.stop_reason = "max_tokens"
    return response


def test_call_anthropic_text_only() -> None:
    client = MagicMock()
    client.messages.create.return_value = _make_response(
        [{"type": "text", "text": "hello"}],
        input_tokens=10,
        output_tokens=5,
    )

    text, prompt_tokens, completion_tokens, total_tokens = _call_anthropic(client, {})

    assert text == "hello"
    assert prompt_tokens == 10
    assert completion_tokens == 5
    assert total_tokens == 15


def test_call_anthropic_thinking_only(caplog: pytest.LogCaptureFixture) -> None:
    client = MagicMock()
    client.messages.create.return_value = _make_response(
        [{"type": "thinking", "thinking": "step one"}],
        input_tokens=10,
        output_tokens=4096,
    )

    with caplog.at_level("WARNING"):
        text, prompt_tokens, completion_tokens, total_tokens = _call_anthropic(client, {})

    assert "[THINKING_ONLY_RESPONSE]" in text
    assert "step one" in text
    assert "[END_THINKING_ONLY_RESPONSE]" in text
    assert prompt_tokens == 10
    assert completion_tokens == 4096
    assert total_tokens == 4106
    assert "thinking blocks (no text)" in caplog.text
    assert "max_tokens" in caplog.text
    assert "4096" in caplog.text


def test_call_anthropic_with_tools_prefers_text_over_thinking() -> None:
    client = MagicMock()
    client.messages.create.return_value = _make_response(
        [
            {"type": "thinking", "thinking": "internal reasoning"},
            {"type": "text", "text": "final answer"},
        ],
        input_tokens=20,
        output_tokens=8,
    )

    text, tool_uses, prompt_tokens, completion_tokens, total_tokens = _call_anthropic_with_tools(
        client, {}
    )

    assert text == "final answer"
    assert tool_uses == []
    assert total_tokens == 28


def test_call_anthropic_with_tools_thinking_only() -> None:
    client = MagicMock()
    client.messages.create.return_value = _make_response(
        [{"type": "thinking", "thinking": "no text produced"}],
        input_tokens=20,
        output_tokens=100,
    )

    text, tool_uses, prompt_tokens, completion_tokens, total_tokens = _call_anthropic_with_tools(
        client, {}
    )

    assert "[THINKING_ONLY_RESPONSE]" in text
    assert "no text produced" in text
    assert tool_uses == []
    assert total_tokens == 120
