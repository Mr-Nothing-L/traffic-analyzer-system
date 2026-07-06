"""
Failover unit tests for VLMInferenceEngine.

These tests exercise the multi-provider failover logic introduced in Wave 3.
All external SDK calls are mocked; no real network requests are made.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import openai
import pytest

from traffic_analyzer.core.vlm_engine import (
    FatalAPIError,
    VLMInferenceEngine,
)
from traffic_analyzer.models.schemas import LLMProviderConfig, PromptTemplate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_openai_error(cls: type, status_code: int, message: str) -> openai.APIStatusError:
    """Build an OpenAI SDK error with a real HTTP response."""
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status_code, text=message, request=request)
    return cls(message, response=response, body=None)


def _make_anthropic_error(cls_name: str, message: str, status_code: int = 400) -> anthropic.APIStatusError:
    """Build an Anthropic SDK error when available."""
    cls = getattr(anthropic, cls_name, None)
    if cls is None:
        pytest.skip(f"anthropic.{cls_name} not available")
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, text=message, request=request)
    return cls(message, response=response, body=None)


def _anthropic_success_response(data: Dict[str, Any]) -> MagicMock:
    """Return a MagicMock that mimics a successful Anthropic messages response."""
    response = MagicMock()
    response.content = [MagicMock(type="text", text=json.dumps(data))]
    response.usage = MagicMock(input_tokens=4, output_tokens=2)
    return response


def _aliyun_success_response(data: Dict[str, Any]) -> MagicMock:
    """Return a MagicMock that mimics a successful Aliyun (OpenAI-compatible) response."""
    choice = MagicMock()
    choice.message.content = json.dumps(data)
    response = MagicMock()
    response.choices = [choice]
    response.usage = MagicMock(prompt_tokens=4, completion_tokens=2, total_tokens=6)
    return response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_template() -> PromptTemplate:
    return PromptTemplate(
        template_id="failover_test",
        name="Failover Test",
        system_prompt="You are a test assistant.",
        user_prompt="Analyze this: {{ description }}",
        output_format_hint="JSON",
    )


@pytest.fixture
def aliyun_primary_config() -> LLMProviderConfig:
    return LLMProviderConfig(
        provider="aliyun",
        api_key="fake-aliyun-key",
        model="qwen-vl-max",
        max_tokens=1024,
        temperature=0.1,
        timeout=30.0,
        max_retries=1,
        enable_cache=False,
    )


@pytest.fixture
def anthropic_backup_config() -> LLMProviderConfig:
    return LLMProviderConfig(
        provider="anthropic",
        api_key="fake-anthropic-key",
        model="claude-sonnet-4-6",
        max_tokens=1024,
        temperature=0.1,
        timeout=30.0,
        max_retries=1,
        enable_cache=False,
    )


@pytest.fixture
def anthropic_single_config() -> LLMProviderConfig:
    return LLMProviderConfig(
        provider="anthropic",
        api_key="fake-anthropic-key",
        model="claude-sonnet-4-6",
        max_tokens=1024,
        temperature=0.1,
        timeout=30.0,
        max_retries=1,
        enable_cache=False,
    )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


@patch("traffic_analyzer.core.vlm_engine.openai.OpenAI")
@patch("traffic_analyzer.core.vlm_engine.anthropic.Anthropic")
def test_failover_when_primary_rate_limited(
    mock_anthropic_cls: MagicMock,
    mock_openai_cls: MagicMock,
    aliyun_primary_config: LLMProviderConfig,
    anthropic_backup_config: LLMProviderConfig,
    simple_template: PromptTemplate,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Primary provider RateLimitError -> failover to backup and succeed."""
    caplog.set_level(logging.ERROR, logger="traffic_analyzer.core.vlm_engine")

    mock_aliyun_client = MagicMock()
    mock_openai_cls.return_value = mock_aliyun_client
    mock_aliyun_client.chat.completions.create.side_effect = _make_openai_error(
        openai.RateLimitError, 429, "rate limited"
    )

    mock_anthropic_client = MagicMock()
    mock_anthropic_cls.return_value = mock_anthropic_client
    mock_anthropic_client.messages.create.return_value = _anthropic_success_response(
        {"failover": True}
    )

    engine = VLMInferenceEngine([aliyun_primary_config, anthropic_backup_config])
    response = engine.call(
        simple_template, images=[], context_vars={"description": "test"}
    )

    assert response.success is True
    assert response.provider == "anthropic"
    assert response.parsed_data == {"failover": True}

    record = engine.create_call_record("failover_test", response)
    assert record.provider == "anthropic"

    assert "FAILOVER" in caplog.text
    assert mock_aliyun_client.chat.completions.create.call_count == 1
    assert mock_anthropic_client.messages.create.call_count == 1


@patch("traffic_analyzer.core.vlm_engine.openai.OpenAI")
@patch("traffic_analyzer.core.vlm_engine.anthropic.Anthropic")
def test_failover_when_primary_authentication_failed(
    mock_anthropic_cls: MagicMock,
    mock_openai_cls: MagicMock,
    aliyun_primary_config: LLMProviderConfig,
    anthropic_backup_config: LLMProviderConfig,
    simple_template: PromptTemplate,
) -> None:
    """Primary provider AuthenticationError -> failover to backup and succeed."""
    mock_aliyun_client = MagicMock()
    mock_openai_cls.return_value = mock_aliyun_client
    mock_aliyun_client.chat.completions.create.side_effect = _make_openai_error(
        openai.AuthenticationError, 401, "invalid key"
    )

    mock_anthropic_client = MagicMock()
    mock_anthropic_cls.return_value = mock_anthropic_client
    mock_anthropic_client.messages.create.return_value = _anthropic_success_response(
        {"auth_failover": True}
    )

    engine = VLMInferenceEngine([aliyun_primary_config, anthropic_backup_config])
    response = engine.call(
        simple_template, images=[], context_vars={"description": "test"}
    )

    assert response.success is True
    assert response.provider == "anthropic"
    assert response.parsed_data == {"auth_failover": True}
    assert mock_anthropic_client.messages.create.call_count == 1


@patch("traffic_analyzer.core.vlm_engine.openai.OpenAI")
@patch("traffic_analyzer.core.vlm_engine.anthropic.Anthropic")
def test_failover_on_quota_keyword_message(
    mock_anthropic_cls: MagicMock,
    mock_openai_cls: MagicMock,
    aliyun_primary_config: LLMProviderConfig,
    anthropic_backup_config: LLMProviderConfig,
    simple_template: PromptTemplate,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Error message with quota/balance keywords triggers failover."""
    caplog.set_level(logging.ERROR, logger="traffic_analyzer.core.vlm_engine")

    mock_aliyun_client = MagicMock()
    mock_openai_cls.return_value = mock_aliyun_client
    # Use a plain Exception so the message-based failover keyword check is exercised.
    mock_aliyun_client.chat.completions.create.side_effect = Exception("余额不足")

    mock_anthropic_client = MagicMock()
    mock_anthropic_cls.return_value = mock_anthropic_client
    mock_anthropic_client.messages.create.return_value = _anthropic_success_response(
        {"quota_failover": True}
    )

    engine = VLMInferenceEngine([aliyun_primary_config, anthropic_backup_config])
    response = engine.call(
        simple_template, images=[], context_vars={"description": "test"}
    )

    assert response.success is True
    assert response.provider == "anthropic"
    assert response.parsed_data == {"quota_failover": True}
    assert "FAILOVER" in caplog.text


@patch("traffic_analyzer.core.vlm_engine.openai.OpenAI")
@patch("traffic_analyzer.core.vlm_engine.anthropic.Anthropic")
def test_failover_on_api_status_error_quota_keyword(
    mock_anthropic_cls: MagicMock,
    mock_openai_cls: MagicMock,
    aliyun_primary_config: LLMProviderConfig,
    anthropic_backup_config: LLMProviderConfig,
    simple_template: PromptTemplate,
) -> None:
    """A plain 4xx APIStatusError whose message contains quota keywords triggers failover."""
    mock_aliyun_client = MagicMock()
    mock_openai_cls.return_value = mock_aliyun_client
    mock_aliyun_client.chat.completions.create.side_effect = _make_openai_error(
        openai.APIStatusError, 401, "配额已耗尽"
    )

    mock_anthropic_client = MagicMock()
    mock_anthropic_cls.return_value = mock_anthropic_client
    mock_anthropic_client.messages.create.return_value = _anthropic_success_response(
        {"api_status_failover": True}
    )

    engine = VLMInferenceEngine([aliyun_primary_config, anthropic_backup_config])
    response = engine.call(
        simple_template, images=[], context_vars={"description": "test"}
    )

    assert response.success is True
    assert response.provider == "anthropic"
    assert response.parsed_data == {"api_status_failover": True}


@patch("traffic_analyzer.core.vlm_engine.openai.OpenAI")
@patch("traffic_analyzer.core.vlm_engine.anthropic.Anthropic")
def test_failover_when_primary_returns_402_payment_required(
    mock_anthropic_cls: MagicMock,
    mock_openai_cls: MagicMock,
    aliyun_primary_config: LLMProviderConfig,
    anthropic_backup_config: LLMProviderConfig,
    simple_template: PromptTemplate,
) -> None:
    """Primary provider 402 Payment Required -> failover to backup and succeed."""
    mock_aliyun_client = MagicMock()
    mock_openai_cls.return_value = mock_aliyun_client
    mock_aliyun_client.chat.completions.create.side_effect = _make_openai_error(
        openai.APIStatusError, 402, "Payment Required"
    )

    mock_anthropic_client = MagicMock()
    mock_anthropic_cls.return_value = mock_anthropic_client
    mock_anthropic_client.messages.create.return_value = _anthropic_success_response(
        {"payment_failover": True}
    )

    engine = VLMInferenceEngine([aliyun_primary_config, anthropic_backup_config])
    response = engine.call(
        simple_template, images=[], context_vars={"description": "test"}
    )

    assert response.success is True
    assert response.provider == "anthropic"
    assert response.parsed_data == {"payment_failover": True}


@patch("traffic_analyzer.core.vlm_engine.openai.OpenAI")
@patch("traffic_analyzer.core.vlm_engine.anthropic.Anthropic")
def test_bad_request_does_not_trigger_failover(
    mock_anthropic_cls: MagicMock,
    mock_openai_cls: MagicMock,
    aliyun_primary_config: LLMProviderConfig,
    anthropic_backup_config: LLMProviderConfig,
    simple_template: PromptTemplate,
) -> None:
    """BadRequestError must not trigger failover and should propagate."""
    mock_aliyun_client = MagicMock()
    mock_openai_cls.return_value = mock_aliyun_client
    mock_aliyun_client.chat.completions.create.side_effect = _make_openai_error(
        openai.BadRequestError, 400, "bad request"
    )

    mock_anthropic_client = MagicMock()
    mock_anthropic_cls.return_value = mock_anthropic_client
    mock_anthropic_client.messages.create.return_value = _anthropic_success_response(
        {"should_not_run": True}
    )

    engine = VLMInferenceEngine([aliyun_primary_config, anthropic_backup_config])
    response = engine.call(simple_template, images=[], context_vars={"description": "test"})

    # BadRequest is a client-side error; the engine should return a failed
    # response instead of failover to the backup provider.
    assert response.success is False
    assert response.provider == "aliyun"

    # Backup provider must never be touched.
    assert mock_anthropic_client.messages.create.call_count == 0


@patch("traffic_analyzer.core.vlm_engine.openai.OpenAI")
@patch("traffic_analyzer.core.vlm_engine.anthropic.Anthropic")
def test_all_providers_exhausted_raises_fatal(
    mock_anthropic_cls: MagicMock,
    mock_openai_cls: MagicMock,
    aliyun_primary_config: LLMProviderConfig,
    anthropic_backup_config: LLMProviderConfig,
    simple_template: PromptTemplate,
) -> None:
    """When every provider fails with a quota-style rate limit, a fatal error is raised."""
    mock_aliyun_client = MagicMock()
    mock_openai_cls.return_value = mock_aliyun_client
    mock_aliyun_client.chat.completions.create.side_effect = _make_openai_error(
        openai.RateLimitError, 429, "insufficient quota"
    )

    mock_anthropic_client = MagicMock()
    mock_anthropic_cls.return_value = mock_anthropic_client
    mock_anthropic_client.messages.create.side_effect = _make_anthropic_error(
        "RateLimitError", "insufficient quota", 429
    )

    engine = VLMInferenceEngine([aliyun_primary_config, anthropic_backup_config])
    with pytest.raises(FatalAPIError):
        engine.call(simple_template, images=[], context_vars={"description": "test"})


@patch("traffic_analyzer.core.vlm_engine.anthropic.Anthropic")
def test_single_provider_behavior_unchanged(
    mock_anthropic_cls: MagicMock,
    anthropic_single_config: LLMProviderConfig,
    simple_template: PromptTemplate,
) -> None:
    """A single-provider config keeps working exactly as before."""
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client
    mock_client.messages.create.return_value = _anthropic_success_response(
        {"single": True}
    )

    engine = VLMInferenceEngine(anthropic_single_config)
    response = engine.call(
        simple_template, images=[], context_vars={"description": "test"}
    )

    assert response.success is True
    assert response.provider == "anthropic"
    assert response.parsed_data == {"single": True}
    assert mock_client.messages.create.call_count == 1


@patch("traffic_analyzer.core.vlm_engine.openai.OpenAI")
@patch("traffic_analyzer.core.vlm_engine.anthropic.Anthropic")
def test_subsequent_calls_use_backup_provider(
    mock_anthropic_cls: MagicMock,
    mock_openai_cls: MagicMock,
    aliyun_primary_config: LLMProviderConfig,
    anthropic_backup_config: LLMProviderConfig,
    simple_template: PromptTemplate,
) -> None:
    """After a failover, the engine keeps using the backup provider."""
    mock_aliyun_client = MagicMock()
    mock_openai_cls.return_value = mock_aliyun_client
    mock_aliyun_client.chat.completions.create.side_effect = _make_openai_error(
        openai.RateLimitError, 429, "rate limited"
    )

    mock_anthropic_client = MagicMock()
    mock_anthropic_cls.return_value = mock_anthropic_client
    mock_anthropic_client.messages.create.return_value = _anthropic_success_response(
        {"provider": "anthropic"}
    )

    engine = VLMInferenceEngine([aliyun_primary_config, anthropic_backup_config])

    # First call fails over to anthropic.
    response1 = engine.call(
        simple_template, images=[], context_vars={"description": "first"}
    )
    assert response1.success is True
    assert response1.provider == "anthropic"

    # Second call should go straight to anthropic without touching aliyun again.
    response2 = engine.call(
        simple_template, images=[], context_vars={"description": "second"}
    )
    assert response2.success is True
    assert response2.provider == "anthropic"

    assert mock_aliyun_client.chat.completions.create.call_count == 1
    assert mock_anthropic_client.messages.create.call_count == 2
