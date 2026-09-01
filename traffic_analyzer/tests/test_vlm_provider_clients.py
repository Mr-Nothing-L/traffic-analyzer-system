"""
Unit tests for provider-specific callers in vlm_provider_clients.py.

[文件说明]
作用:测试各 provider 专用调用封装,覆盖 Anthropic(含 tools)与 Google 调用的请求构造与响应解析。
上游:pytest 自动发现并执行本文件测试。
下游:traffic_analyzer/core/vlm_provider_clients.py(被测模块)。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from traffic_analyzer.core.vlm_provider_clients import (
    _build_aliyun_payload,
    _call_anthropic,
    _call_anthropic_with_tools,
    _call_google,
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


def _make_google_response(text: str = "{}") -> MagicMock:
    response = MagicMock()
    response.parts = [SimpleNamespace(text=text)]
    response.usage_metadata = SimpleNamespace(
        prompt_token_count=1,
        candidates_token_count=2,
        total_token_count=3,
    )
    return response


def test_call_google_passes_timeout_request_options() -> None:
    """The per-request timeout must align with the other providers (config.timeout)."""
    client_model = MagicMock()
    client_model.generate_content.return_value = _make_google_response()

    text, prompt_tokens, completion_tokens, total_tokens = _call_google(
        client_model, ["hi"], {"max_output_tokens": 8}, timeout=30.0
    )

    assert text == "{}"
    assert total_tokens == 3
    _, kwargs = client_model.generate_content.call_args
    assert kwargs["request_options"] == {"timeout": 30.0}


def test_call_google_without_timeout_uses_default_request_options() -> None:
    client_model = MagicMock()
    client_model.generate_content.return_value = _make_google_response()

    _call_google(client_model, ["hi"], {"max_output_tokens": 8})

    _, kwargs = client_model.generate_content.call_args
    assert kwargs["request_options"] is None


# ---------------------------------------------------------------------------
# _build_aliyun_payload: enable_thinking 经 extra_body 注入(OpenAI 兼容后端)
# ---------------------------------------------------------------------------


def test_build_aliyun_payload_enable_thinking_false_uses_extra_body() -> None:
    """chat_template_kwargs 是 OpenAI SDK 未收录字段,必须走 extra_body。"""
    _, kwargs = _build_aliyun_payload(
        "sys", "user", [], "qwen3.8-27b-fp8", 1024, 0.1, enable_thinking=False
    )
    assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_build_aliyun_payload_enable_thinking_true_uses_extra_body() -> None:
    _, kwargs = _build_aliyun_payload(
        "sys", "user", [], "qwen3.8-27b-fp8", 1024, 0.1, enable_thinking=True
    )
    assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}


def test_build_aliyun_payload_default_has_no_extra_body() -> None:
    """不传参保持原请求形状(服务端默认),不引入额外字段。"""
    _, kwargs = _build_aliyun_payload("sys", "user", [], "qwen3.8-27b-fp8", 1024, 0.1)
    assert "extra_body" not in kwargs


def test_build_aliyun_payload_reasoning_effort_injects_extra_body() -> None:
    _, kwargs = _build_aliyun_payload(
        "sys", "user", [], "qwen3.8-27b-fp8", 1024, 0.1, reasoning_effort="medium"
    )
    assert kwargs["extra_body"] == {
        "chat_template_kwargs": {"reasoning_effort": "medium"}
    }


def test_build_aliyun_payload_reasoning_effort_suppressed_when_thinking_disabled() -> None:
    """enable_thinking=False 时 reasoning_effort 被抑制,避免矛盾 kwargs。"""
    _, kwargs = _build_aliyun_payload(
        "sys",
        "user",
        [],
        "qwen3.8-27b-fp8",
        1024,
        0.1,
        enable_thinking=False,
        reasoning_effort="medium",
    )
    assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_build_aliyun_payload_all_thinking_kwargs_together() -> None:
    _, kwargs = _build_aliyun_payload(
        "sys",
        "user",
        [],
        "qwen3.8-27b-fp8",
        1024,
        0.1,
        enable_thinking=True,
        thinking_budget=1024,
        reasoning_effort="xhigh",
    )
    assert kwargs["extra_body"] == {
        "chat_template_kwargs": {
            "enable_thinking": True,
            "thinking_budget": 1024,
            "reasoning_effort": "xhigh",
        }
    }
