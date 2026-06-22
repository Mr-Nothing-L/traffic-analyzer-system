"""
Error classification helpers for VLM API calls.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic
import httpx
import openai

from traffic_analyzer.core.vlm_exceptions import (
    PromptRenderError,
    ResponseParseError,
    SchemaValidationError,
)

logger = logging.getLogger(__name__)


def _is_retryable_error(exc: Exception) -> bool:
    """Return True if *exc* is a transient error worth retrying.

    Retryable: rate limits, connection issues, timeouts, server-side 5xx.
    Non-retryable: auth errors, bad requests, parse/validation errors.
    """
    # Our own exceptions — never retry
    if isinstance(exc, (PromptRenderError, ResponseParseError, SchemaValidationError)):
        return False

    # Unwrap wrapped exceptions (e.g. SDK wrappers, chained causes)
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    candidates = [exc]
    if cause is not None:
        candidates.append(cause)

    for candidate in candidates:
        # OpenAI SDK errors
        if isinstance(candidate, openai.RateLimitError):
            return True
        if isinstance(candidate, openai.APIConnectionError):
            return True
        if isinstance(candidate, openai.APITimeoutError):
            return True
        if isinstance(candidate, openai.InternalServerError):
            return True
        if isinstance(candidate, openai.APIStatusError):
            # 5xx server errors are retryable; 4xx client errors are not
            status = getattr(candidate, "status_code", None) or 0
            if status >= 500:
                return True
            # Explicit non-retryable OpenAI client errors
            if isinstance(
                candidate,
                (openai.AuthenticationError, openai.BadRequestError, openai.NotFoundError),
            ):
                return False
            # Other 4xx — don't retry by default
            if 400 <= status < 500:
                return False

        # Anthropic SDK errors (use getattr for safety in case SDK version differs)
        anthropic_rate_limit = getattr(anthropic, "RateLimitError", None)
        anthropic_timeout = getattr(anthropic, "APITimeoutError", None)
        anthropic_auth = getattr(anthropic, "AuthenticationError", None)
        anthropic_bad_request = getattr(anthropic, "BadRequestError", None)
        if anthropic_rate_limit and isinstance(candidate, anthropic_rate_limit):
            return True
        if anthropic_timeout and isinstance(candidate, anthropic_timeout):
            return True
        if anthropic_auth and isinstance(candidate, anthropic_auth):
            return False
        if anthropic_bad_request and isinstance(candidate, anthropic_bad_request):
            return False

        # httpx timeouts
        if isinstance(candidate, httpx.TimeoutException):
            return True

        # Generic Python network / timeout errors
        if isinstance(candidate, (ConnectionError, TimeoutError)):
            return True

    # Default: if we can't classify it, be conservative and don't retry
    # (avoids burning API quota on unknown errors)
    return False


def _is_fatal_api_error(exc: Exception) -> bool:
    """Return True if *exc* means the API is unusable and will not recover.

    Fatal errors should stop batch processing immediately to avoid wasting
    quota on retries and subsequent videos.
    """
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    candidates = [exc]
    if cause is not None:
        candidates.append(cause)

    for candidate in candidates:
        if candidate is None:
            continue
        # OpenAI SDK — auth / permission / quota
        if isinstance(
            candidate,
            (openai.AuthenticationError, openai.PermissionDeniedError),
        ):
            return True
        # Anthropic SDK — auth
        anthropic_auth = getattr(anthropic, "AuthenticationError", None)
        if anthropic_auth and isinstance(candidate, anthropic_auth):
            return True
        # Check error message for quota / balance / billing keywords
        msg = str(candidate).lower()
        fatal_keywords = (
            "quota", "insufficient", "balance", "billing", "exhausted",
            "unauthorized", "invalid api key", "access denied",
            "余额", "配额", "欠费", "未授权", "无效的",
        )
        if any(kw in msg for kw in fatal_keywords):
            return True

    return False
