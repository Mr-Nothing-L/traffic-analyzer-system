"""
Unit tests for VLMInferenceEngine.

Mocks all external SDK clients to test prompt rendering, JSON parsing,
retry logic, schema validation, batch calls, and usage tracking.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from traffic_analyzer.core.vlm_engine import (
    ProviderNotSupportedError,
    PromptRenderError,
    ResponseParseError,
    SchemaValidationError,
    VLMInferenceEngine,
    _encode_image_to_base64,
    _extract_json_from_text,
    _validate_schema_basic,
)
from traffic_analyzer.models.schemas import LLMProviderConfig, PromptTemplate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def anthropic_config() -> LLMProviderConfig:
    return LLMProviderConfig(
        provider="anthropic",
        api_key="test-anthropic-key",
        model="claude-sonnet-4-6",
        max_tokens=1024,
        temperature=0.1,
        timeout=30.0,
        max_retries=2,
    )


@pytest.fixture
def openai_config() -> LLMProviderConfig:
    return LLMProviderConfig(
        provider="openai",
        api_key="test-openai-key",
        model="gpt-4o",
        max_tokens=1024,
        temperature=0.1,
        timeout=30.0,
        max_retries=2,
    )


@pytest.fixture
def google_config() -> LLMProviderConfig:
    return LLMProviderConfig(
        provider="google",
        api_key="test-google-key",
        model="gemini-2.5-pro",
        max_tokens=1024,
        temperature=0.1,
        timeout=30.0,
        max_retries=2,
    )


@pytest.fixture
def aliyun_config() -> LLMProviderConfig:
    return LLMProviderConfig(
        provider="aliyun",
        api_key="test-aliyun-key",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-vl-max",
        max_tokens=1024,
        temperature=0.1,
        timeout=30.0,
        max_retries=2,
    )


@pytest.fixture
def simple_template() -> PromptTemplate:
    return PromptTemplate(
        template_id="test_template",
        name="Test Template",
        system_prompt="You are a test assistant.",
        user_prompt="Analyze this: {{ description }}",
        output_format_hint="JSON",
    )


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def test_unsupported_provider_raises() -> None:
    config = LLMProviderConfig(provider="unknown")
    with pytest.raises(ProviderNotSupportedError):
        VLMInferenceEngine(config)


@patch("traffic_analyzer.core.vlm_engine.anthropic.Anthropic")
def test_init_anthropic(mock_anthropic: MagicMock, anthropic_config: LLMProviderConfig) -> None:
    engine = VLMInferenceEngine(anthropic_config)
    assert engine.provider == "anthropic"
    mock_anthropic.assert_called_once_with(api_key="test-anthropic-key", timeout=30.0)


@patch("traffic_analyzer.core.vlm_engine.openai.OpenAI")
def test_init_openai(mock_openai: MagicMock, openai_config: LLMProviderConfig) -> None:
    engine = VLMInferenceEngine(openai_config)
    assert engine.provider == "openai"
    mock_openai.assert_called_once_with(api_key="test-openai-key", timeout=30.0)


@patch("traffic_analyzer.core.vlm_engine.genai.configure")
@patch("traffic_analyzer.core.vlm_engine.genai")
def test_init_google(
    mock_genai: MagicMock, mock_configure: MagicMock, google_config: LLMProviderConfig
) -> None:
    engine = VLMInferenceEngine(google_config)
    assert engine.provider == "google"
    mock_configure.assert_called_once_with(api_key="test-google-key")


@patch("traffic_analyzer.core.vlm_engine.openai.OpenAI")
def test_init_aliyun(mock_openai: MagicMock, aliyun_config: LLMProviderConfig) -> None:
    engine = VLMInferenceEngine(aliyun_config)
    assert engine.provider == "aliyun"
    mock_openai.assert_called_once_with(
        api_key="test-aliyun-key",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        timeout=30.0,
    )


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def test_render_prompt_success(simple_template: PromptTemplate) -> None:
    system, user = VLMInferenceEngine.render_prompt(
        simple_template, {"description": "a red car"}
    )
    assert system == "You are a test assistant."
    assert user == "Analyze this: a red car"


def test_render_prompt_missing_variable_raises(simple_template: PromptTemplate) -> None:
    with pytest.raises(PromptRenderError):
        VLMInferenceEngine.render_prompt(simple_template, {})


def test_render_prompt_empty_template() -> None:
    template = PromptTemplate(template_id="empty", name="Empty")
    system, user = VLMInferenceEngine.render_prompt(template, {})
    assert system == ""
    assert user == ""


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


def test_extract_json_direct() -> None:
    text = '{"key": "value"}'
    result = _extract_json_from_text(text)
    assert result == {"key": "value"}


def test_extract_json_fenced() -> None:
    text = 'Some text\n```json\n{"a": 1}\n```\nMore text'
    result = _extract_json_from_text(text)
    assert result == {"a": 1}


def test_extract_json_regex_fallback() -> None:
    text = 'Here is the result: {"result": true, "count": 42} thanks!'
    result = _extract_json_from_text(text)
    assert result == {"result": True, "count": 42}


def test_extract_json_no_json_raises() -> None:
    with pytest.raises(ResponseParseError):
        _extract_json_from_text("There is no json here.")


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_validate_schema_success() -> None:
    data = {"road_count": 2, "weather": "clear"}
    schema = {"required": ["road_count", "weather"]}
    _validate_schema_basic(data, schema)  # should not raise


def test_validate_schema_missing_key_raises() -> None:
    data = {"road_count": 2}
    schema = {"required": ["road_count", "weather"]}
    with pytest.raises(SchemaValidationError):
        _validate_schema_basic(data, schema)


# ---------------------------------------------------------------------------
# Image encoding helper
# ---------------------------------------------------------------------------


def test_encode_image_bytes() -> None:
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    result = _encode_image_to_base64(data)
    assert result.startswith("data:image/png;base64,")


@patch("builtins.open", MagicMock(return_value=MagicMock(read=lambda: b"fake")))
def test_encode_image_path() -> None:
    result = _encode_image_to_base64("/fake/path.png")
    assert result.startswith("data:image/png;base64,")


def test_encode_image_unsupported_type() -> None:
    with pytest.raises(TypeError):
        _encode_image_to_base64(12345)


# ---------------------------------------------------------------------------
# Anthropic call flow
# ---------------------------------------------------------------------------


@patch("traffic_analyzer.core.vlm_engine.anthropic.Anthropic")
def test_call_anthropic_success(
    mock_anthropic_cls: MagicMock,
    anthropic_config: LLMProviderConfig,
    simple_template: PromptTemplate,
) -> None:
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client

    fake_response = MagicMock()
    fake_response.content = [MagicMock(type="text", text=json.dumps({"detected": True}))]
    fake_usage = MagicMock(input_tokens=15, output_tokens=5)
    fake_response.usage = fake_usage
    mock_client.messages.create.return_value = fake_response

    engine = VLMInferenceEngine(anthropic_config)
    resp = engine.call(simple_template, images=[], context_vars={"description": "test"})

    assert resp.success is True
    assert resp.parsed_data == {"detected": True}
    assert resp.prompt_tokens == 15
    assert resp.completion_tokens == 5
    assert resp.total_tokens == 20
    assert resp.model == "claude-sonnet-4-6"
    assert resp.retry_count == 0


@patch("traffic_analyzer.core.vlm_engine.anthropic.Anthropic")
def test_call_anthropic_retry_then_success(
    mock_anthropic_cls: MagicMock,
    anthropic_config: LLMProviderConfig,
    simple_template: PromptTemplate,
) -> None:
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client

    fake_response = MagicMock()
    fake_response.content = [MagicMock(type="text", text=json.dumps({"ok": True}))]
    fake_usage = MagicMock(input_tokens=10, output_tokens=2)
    fake_response.usage = fake_usage

    # First call raises, second succeeds
    mock_client.messages.create.side_effect = [
        Exception("Transient error"),
        fake_response,
    ]

    engine = VLMInferenceEngine(anthropic_config)
    resp = engine.call(simple_template, images=[], context_vars={"description": "x"})

    assert resp.success is True
    assert resp.retry_count == 1
    assert mock_client.messages.create.call_count == 2


@patch("traffic_analyzer.core.vlm_engine.anthropic.Anthropic")
def test_call_anthropic_all_retries_exhausted(
    mock_anthropic_cls: MagicMock,
    anthropic_config: LLMProviderConfig,
    simple_template: PromptTemplate,
) -> None:
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client
    mock_client.messages.create.side_effect = Exception("Persistent failure")

    engine = VLMInferenceEngine(anthropic_config)
    resp = engine.call(simple_template, images=[], context_vars={"description": "x"})

    assert resp.success is False
    assert resp.retry_count == 1  # max_retries=2 means 1 retry after first failure


@patch("traffic_analyzer.core.vlm_engine.anthropic.Anthropic")
def test_call_anthropic_schema_validation_failure(
    mock_anthropic_cls: MagicMock,
    anthropic_config: LLMProviderConfig,
    simple_template: PromptTemplate,
) -> None:
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client

    fake_response = MagicMock()
    fake_response.content = [MagicMock(type="text", text=json.dumps({"wrong_key": 1}))]
    fake_usage = MagicMock(input_tokens=10, output_tokens=2)
    fake_response.usage = fake_usage
    mock_client.messages.create.return_value = fake_response

    engine = VLMInferenceEngine(anthropic_config)
    schema = {"required": ["detected"]}
    resp = engine.call(
        simple_template,
        images=[],
        context_vars={"description": "x"},
        response_schema=schema,
    )

    assert resp.success is False
    assert "missing required keys" in resp.raw_text or "Schema validation" in str(resp.raw_text)


# ---------------------------------------------------------------------------
# OpenAI call flow
# ---------------------------------------------------------------------------


@patch("traffic_analyzer.core.vlm_engine.openai.OpenAI")
def test_call_openai_success(
    mock_openai_cls: MagicMock,
    openai_config: LLMProviderConfig,
    simple_template: PromptTemplate,
) -> None:
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client

    fake_choice = MagicMock()
    fake_choice.message.content = json.dumps({"result": "ok"})
    fake_response = MagicMock()
    fake_response.choices = [fake_choice]
    fake_response.usage = MagicMock(prompt_tokens=8, completion_tokens=4, total_tokens=12)
    mock_client.chat.completions.create.return_value = fake_response

    engine = VLMInferenceEngine(openai_config)
    resp = engine.call(simple_template, images=[], context_vars={"description": "y"})

    assert resp.success is True
    assert resp.parsed_data == {"result": "ok"}
    assert resp.prompt_tokens == 8
    assert resp.total_tokens == 12


# ---------------------------------------------------------------------------
# Google call flow
# ---------------------------------------------------------------------------


@patch("traffic_analyzer.core.vlm_engine.genai.configure")
@patch("traffic_analyzer.core.vlm_engine.genai")
def test_call_google_success(
    mock_genai: MagicMock,
    mock_configure: MagicMock,
    google_config: LLMProviderConfig,
    simple_template: PromptTemplate,
) -> None:
    mock_model_instance = MagicMock()
    mock_genai.GenerativeModel.return_value = mock_model_instance

    fake_response = MagicMock()
    fake_response.parts = [MagicMock(text=json.dumps({"google_result": 42}))]
    fake_response.usage_metadata = MagicMock(
        prompt_token_count=20,
        candidates_token_count=10,
        total_token_count=30,
    )
    mock_model_instance.generate_content.return_value = fake_response

    engine = VLMInferenceEngine(google_config)
    resp = engine.call(simple_template, images=[], context_vars={"description": "z"})

    assert resp.success is True
    assert resp.parsed_data == {"google_result": 42}
    assert resp.total_tokens == 30


# ---------------------------------------------------------------------------
# Aliyun call flow
# ---------------------------------------------------------------------------


@patch("traffic_analyzer.core.vlm_engine.openai.OpenAI")
def test_call_aliyun_success(
    mock_openai_cls: MagicMock,
    aliyun_config: LLMProviderConfig,
    simple_template: PromptTemplate,
) -> None:
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client

    fake_choice = MagicMock()
    fake_choice.message.content = json.dumps({"aliyun_result": True})
    fake_response = MagicMock()
    fake_response.choices = [fake_choice]
    fake_response.usage = MagicMock(prompt_tokens=5, completion_tokens=3, total_tokens=8)
    mock_client.chat.completions.create.return_value = fake_response

    engine = VLMInferenceEngine(aliyun_config)
    resp = engine.call(simple_template, images=[], context_vars={"description": "w"})

    assert resp.success is True
    assert resp.parsed_data == {"aliyun_result": True}
    assert resp.total_tokens == 8


# ---------------------------------------------------------------------------
# Batch call
# ---------------------------------------------------------------------------


@patch("traffic_analyzer.core.vlm_engine.openai.OpenAI")
def test_batch_call_sequential(mock_openai_cls: MagicMock, openai_config: LLMProviderConfig) -> None:
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client

    def _make_response(idx: int) -> MagicMock:
        choice = MagicMock()
        choice.message.content = json.dumps({"index": idx})
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage = MagicMock(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        return resp

    mock_client.chat.completions.create.side_effect = [_make_response(0), _make_response(1)]

    engine = VLMInferenceEngine(openai_config)
    template = PromptTemplate(
        template_id="batch", name="Batch", system_prompt="", user_prompt="{{ idx }}"
    )
    requests: List[Dict[str, Any]] = [
        {"template": template, "images": [], "context_vars": {"idx": 0}},
        {"template": template, "images": [], "context_vars": {"idx": 1}},
    ]
    results = engine.batch_call(requests, parallel=False)

    assert len(results) == 2
    assert results[0].parsed_data == {"index": 0}
    assert results[1].parsed_data == {"index": 1}


@patch("traffic_analyzer.core.vlm_engine.openai.OpenAI")
def test_batch_call_parallel(mock_openai_cls: MagicMock, openai_config: LLMProviderConfig) -> None:
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client

    def _make_response(idx: int) -> MagicMock:
        choice = MagicMock()
        choice.message.content = json.dumps({"index": idx})
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage = MagicMock(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        return resp

    mock_client.chat.completions.create.side_effect = [_make_response(0), _make_response(1)]

    engine = VLMInferenceEngine(openai_config)
    template = PromptTemplate(
        template_id="batch", name="Batch", system_prompt="", user_prompt="{{ idx }}"
    )
    requests: List[Dict[str, Any]] = [
        {"template": template, "images": [], "context_vars": {"idx": 0}},
        {"template": template, "images": [], "context_vars": {"idx": 1}},
    ]
    results = engine.batch_call(requests, parallel=True, max_workers=2)

    assert len(results) == 2
    for r in results:
        assert r.success is True
        assert "index" in r.parsed_data


# ---------------------------------------------------------------------------
# Usage stats
# ---------------------------------------------------------------------------


@patch("traffic_analyzer.core.vlm_engine.openai.OpenAI")
def test_get_usage_stats(mock_openai_cls: MagicMock, openai_config: LLMProviderConfig) -> None:
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client

    choice = MagicMock()
    choice.message.content = json.dumps({"a": 1})
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    mock_client.chat.completions.create.return_value = resp

    engine = VLMInferenceEngine(openai_config)
    template = PromptTemplate(template_id="t", name="T", user_prompt="hi")
    engine.call(template, images=[], context_vars={})

    stats = engine.get_usage_stats()
    assert stats["total_calls"] == 1
    assert stats["total_prompt_tokens"] == 10
    assert stats["total_completion_tokens"] == 5
    assert stats["total_tokens"] == 15
    assert stats["failed_calls"] == 0
    assert stats["average_latency_ms"] >= 0


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------


@patch("traffic_analyzer.core.vlm_engine.openai.OpenAI")
def test_create_call_record(mock_openai_cls: MagicMock, openai_config: LLMProviderConfig) -> None:
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client

    choice = MagicMock()
    choice.message.content = json.dumps({"a": 1})
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = MagicMock(prompt_tokens=3, completion_tokens=2, total_tokens=5)
    mock_client.chat.completions.create.return_value = resp

    engine = VLMInferenceEngine(openai_config)
    template = PromptTemplate(template_id="audit", name="Audit", user_prompt="x")
    llm_resp = engine.call(template, images=[], context_vars={})

    record = engine.create_call_record("audit", llm_resp)
    assert record.template_id == "audit"
    assert record.model == openai_config.model
    assert record.prompt_tokens == 3
    assert record.success is True
