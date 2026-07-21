"""
Unit tests for VLM error classification helpers.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from traffic_analyzer.core.vlm_error_classifier import (
    _is_fatal_api_error,
    _is_retryable_error,
    is_failover_trigger,
)
from traffic_analyzer.core.vlm_exceptions import (
    AllProvidersExhaustedError,
    PromptRenderError,
    ResponseParseError,
    SchemaValidationError,
)


def _make_openai_error(cls: type, status_code: int, message: str) -> openai.APIStatusError:
    """Build an OpenAI SDK error with a real HTTP response."""
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status_code, text=message, request=request)
    return cls(message, response=response, body=None)


def _make_anthropic_error(cls_name: str, message: str, status_code: int = 400):
    """Build an Anthropic SDK error when available."""
    import anthropic

    cls = getattr(anthropic, cls_name, None)
    if cls is None:
        pytest.skip(f"anthropic.{cls_name} not available")
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, text=message, request=request)
    return cls(message, response=response, body=None)


# ---------------------------------------------------------------------------
# Failover trigger classification
# ---------------------------------------------------------------------------


def test_failover_trigger_openai_rate_limit() -> None:
    exc = _make_openai_error(openai.RateLimitError, 429, "rate limited")
    assert is_failover_trigger(exc) is True


def test_failover_trigger_openai_authentication() -> None:
    exc = _make_openai_error(openai.AuthenticationError, 401, "invalid key")
    assert is_failover_trigger(exc) is True


def test_failover_trigger_openai_permission_denied() -> None:
    exc = _make_openai_error(openai.PermissionDeniedError, 403, "permission denied")
    assert is_failover_trigger(exc) is True


def test_failover_trigger_openai_bad_request() -> None:
    exc = _make_openai_error(openai.BadRequestError, 400, "bad request")
    assert is_failover_trigger(exc) is False


def test_failover_trigger_openai_not_found() -> None:
    exc = _make_openai_error(openai.NotFoundError, 404, "not found")
    assert is_failover_trigger(exc) is False


def test_failover_trigger_openai_5xx_status_error() -> None:
    exc = _make_openai_error(openai.APIStatusError, 503, "service unavailable")
    assert is_failover_trigger(exc) is True


def test_failover_trigger_openai_4xx_status_error() -> None:
    exc = _make_openai_error(openai.APIStatusError, 418, "i'm a teapot")
    assert is_failover_trigger(exc) is False


def test_failover_trigger_openai_402_payment_required() -> None:
    exc = _make_openai_error(openai.APIStatusError, 402, "Payment Required")
    assert is_failover_trigger(exc) is True


def test_failover_trigger_anthropic_402_payment_required() -> None:
    exc = _make_anthropic_error("APIStatusError", "Payment Required", status_code=402)
    assert is_failover_trigger(exc) is True


def test_failover_trigger_message_payment_membership() -> None:
    exc = RuntimeError(
        "We're unable to verify your membership benefits at this time."
    )
    assert is_failover_trigger(exc) is True


def test_failover_trigger_anthropic_rate_limit() -> None:
    exc = _make_anthropic_error("RateLimitError", "rate limited")
    assert is_failover_trigger(exc) is True


def test_failover_trigger_anthropic_authentication() -> None:
    exc = _make_anthropic_error("AuthenticationError", "invalid key")
    assert is_failover_trigger(exc) is True


def test_failover_trigger_anthropic_bad_request() -> None:
    exc = _make_anthropic_error("BadRequestError", "bad request")
    assert is_failover_trigger(exc) is False


def test_failover_trigger_message_quota_exhausted() -> None:
    exc = RuntimeError("您的配额已耗尽，请联系客服")
    assert is_failover_trigger(exc) is True


def test_failover_trigger_message_insufficient_balance() -> None:
    exc = RuntimeError("账户余额不足")
    assert is_failover_trigger(exc) is True


def test_failover_trigger_message_invalid_api_key() -> None:
    exc = RuntimeError("invalid api key provided")
    assert is_failover_trigger(exc) is True


def test_failover_trigger_message_access_denied() -> None:
    exc = RuntimeError("Access denied for this operation")
    assert is_failover_trigger(exc) is True


def test_failover_trigger_transient_timeout() -> None:
    exc = httpx.TimeoutException("timed out")
    assert is_failover_trigger(exc) is False


def test_failover_trigger_transient_connection_error() -> None:
    exc = ConnectionError("connection reset")
    assert is_failover_trigger(exc) is False


def test_failover_trigger_prompt_render_error() -> None:
    exc = PromptRenderError("missing variable")
    assert is_failover_trigger(exc) is False


def test_failover_trigger_response_parse_error() -> None:
    exc = ResponseParseError("invalid json")
    assert is_failover_trigger(exc) is False


def test_failover_trigger_schema_validation_error() -> None:
    exc = SchemaValidationError("missing key")
    assert is_failover_trigger(exc) is False


def test_failover_trigger_unwraps_cause() -> None:
    inner = _make_openai_error(openai.RateLimitError, 429, "rate limited")
    try:
        raise RuntimeError("wrapped") from inner
    except RuntimeError as caught:
        outer = caught
    assert is_failover_trigger(outer) is True


def test_failover_trigger_unwraps_context() -> None:
    inner = _make_openai_error(openai.BadRequestError, 400, "bad request")
    outer = RuntimeError("wrapped")
    outer.__context__ = inner
    assert is_failover_trigger(outer) is False


def test_failover_trigger_unknown_error() -> None:
    exc = RuntimeError("something random")
    assert is_failover_trigger(exc) is False


# ---------------------------------------------------------------------------
# Existing helpers remain unchanged
# ---------------------------------------------------------------------------


def test_retryable_error_rate_limit() -> None:
    exc = _make_openai_error(openai.RateLimitError, 429, "rate limited")
    assert _is_retryable_error(exc) is True


def test_retryable_error_anthropic_internal_server_error() -> None:
    exc = _make_anthropic_error("InternalServerError", "internal error", status_code=500)
    assert _is_retryable_error(exc) is True


def test_retryable_error_anthropic_overloaded() -> None:
    exc = _make_anthropic_error("OverloadedError", "overloaded", status_code=529)
    assert _is_retryable_error(exc) is True


def test_retryable_error_anthropic_status_503() -> None:
    exc = _make_anthropic_error("APIStatusError", "service unavailable", status_code=503)
    assert _is_retryable_error(exc) is True


def test_retryable_error_anthropic_api_connection_error() -> None:
    import anthropic

    cls = getattr(anthropic, "APIConnectionError", None)
    if cls is None:
        pytest.skip("anthropic.APIConnectionError not available")
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = cls(request=request)
    assert _is_retryable_error(exc) is True


def test_retryable_error_anthropic_bad_request_still_not_retryable() -> None:
    exc = _make_anthropic_error("BadRequestError", "bad request", status_code=400)
    assert _is_retryable_error(exc) is False


def test_retryable_error_google_service_unavailable() -> None:
    gexc = pytest.importorskip("google.api_core.exceptions")
    assert _is_retryable_error(gexc.ServiceUnavailable("service unavailable")) is True


def test_retryable_error_google_deadline_exceeded() -> None:
    gexc = pytest.importorskip("google.api_core.exceptions")
    assert _is_retryable_error(gexc.DeadlineExceeded("deadline exceeded")) is True


def test_retryable_error_google_resource_exhausted() -> None:
    gexc = pytest.importorskip("google.api_core.exceptions")
    assert _is_retryable_error(gexc.ResourceExhausted("rate limited")) is True


def test_retryable_error_google_invalid_argument_not_retryable() -> None:
    gexc = pytest.importorskip("google.api_core.exceptions")
    assert _is_retryable_error(gexc.InvalidArgument("bad request")) is False


def test_retryable_error_bad_request() -> None:
    exc = _make_openai_error(openai.BadRequestError, 400, "bad request")
    assert _is_retryable_error(exc) is False


def test_fatal_api_error_auth() -> None:
    exc = _make_openai_error(openai.AuthenticationError, 401, "invalid key")
    assert _is_fatal_api_error(exc) is True


def test_fatal_api_error_quota_message() -> None:
    exc = RuntimeError("quota exceeded")
    assert _is_fatal_api_error(exc) is True


def test_fatal_api_error_openai_402() -> None:
    exc = _make_openai_error(openai.APIStatusError, 402, "Payment Required")
    assert _is_fatal_api_error(exc) is True


def test_fatal_api_error_anthropic_402() -> None:
    exc = _make_anthropic_error("APIStatusError", "Payment Required", status_code=402)
    assert _is_fatal_api_error(exc) is True


def test_fatal_api_error_membership_message() -> None:
    exc = RuntimeError(
        "We're unable to verify your membership benefits at this time."
    )
    assert _is_fatal_api_error(exc) is True


# ---------------------------------------------------------------------------
# New exception class
# ---------------------------------------------------------------------------


def test_all_providers_exhausted_error_inheritance() -> None:
    exc = AllProvidersExhaustedError("all providers failed")
    assert isinstance(exc, Exception)
    assert "all providers failed" in str(exc)
