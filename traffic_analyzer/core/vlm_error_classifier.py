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
    AllProvidersExhaustedError,
    PromptRenderError,
    ResponseParseError,
    SchemaValidationError,
)

logger = logging.getLogger(__name__)

# Google API core transient errors — optional dependency, only present when
# the Google provider SDK is installed.
try:
    from google.api_core import exceptions as _google_api_exceptions
except ImportError:  # pragma: no cover
    _google_api_exceptions = None

_GOOGLE_TRANSIENT_ERRORS: tuple = ()
if _google_api_exceptions is not None:
    _GOOGLE_TRANSIENT_ERRORS = tuple(
        exc_cls
        for exc_cls in (
            getattr(_google_api_exceptions, name, None)
            for name in (
                "ServiceUnavailable",
                "InternalServerError",
                "DeadlineExceeded",
                "GatewayTimeout",
                "TooManyRequests",
                "ResourceExhausted",
                "Aborted",
            )
        )
        if exc_cls is not None
    )


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
        anthropic_connection = getattr(anthropic, "APIConnectionError", None)
        anthropic_internal = getattr(anthropic, "InternalServerError", None)
        anthropic_overloaded = getattr(anthropic, "OverloadedError", None)
        anthropic_status = getattr(anthropic, "APIStatusError", None)
        anthropic_auth = getattr(anthropic, "AuthenticationError", None)
        anthropic_bad_request = getattr(anthropic, "BadRequestError", None)
        if anthropic_rate_limit and isinstance(candidate, anthropic_rate_limit):
            return True
        if anthropic_timeout and isinstance(candidate, anthropic_timeout):
            return True
        if anthropic_connection and isinstance(candidate, anthropic_connection):
            return True
        if anthropic_internal and isinstance(candidate, anthropic_internal):
            return True
        if anthropic_overloaded and isinstance(candidate, anthropic_overloaded):
            return True
        if anthropic_status and isinstance(candidate, anthropic_status):
            # 5xx server errors are retryable; 4xx client errors are not
            status = getattr(candidate, "status_code", None) or 0
            if status >= 500:
                return True
        if anthropic_auth and isinstance(candidate, anthropic_auth):
            return False
        if anthropic_bad_request and isinstance(candidate, anthropic_bad_request):
            return False

        # Google API core transient errors (rate limit, 5xx, deadline exceeded)
        if _GOOGLE_TRANSIENT_ERRORS and isinstance(candidate, _GOOGLE_TRANSIENT_ERRORS):
            return True

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
        # All providers exhausted — fatal by definition
        if isinstance(candidate, AllProvidersExhaustedError):
            return True
        # OpenAI SDK — auth / permission / quota / 402 payment required
        if isinstance(
            candidate,
            (openai.AuthenticationError, openai.PermissionDeniedError),
        ):
            return True
        if isinstance(candidate, openai.APIStatusError):
            status = getattr(candidate, "status_code", None) or 0
            if status == 402:
                return True
        # Anthropic SDK — auth / 402 payment required
        anthropic_auth = getattr(anthropic, "AuthenticationError", None)
        if anthropic_auth and isinstance(candidate, anthropic_auth):
            return True
        if isinstance(candidate, anthropic.APIStatusError):
            status = getattr(candidate, "status_code", None) or 0
            if status == 402:
                return True
        # Check error message for quota / balance / billing / payment keywords
        msg = str(candidate).lower()
        fatal_keywords = (
            "quota", "insufficient", "balance", "billing", "exhausted",
            "payment required", "membership", "unable to verify your membership benefits",
            "unauthorized", "invalid api key", "access denied",
            "余额", "配额", "欠费", "未授权", "无效的",
        )
        if any(kw in msg for kw in fatal_keywords):
            return True

    return False


# Keywords that indicate account-level issues and should trigger a provider
# failover rather than retrying the same provider indefinitely.
_FAILOVER_KEYWORDS = (
    "quota",
    "insufficient",
    "balance",
    "billing",
    "exhausted",
    "payment required",
    "membership",
    "unable to verify your membership benefits",
    "余额",
    "配额",
    "欠费",
    "未授权",
    "invalid api key",
    "access denied",
)


def _error_message_matches_failover_keywords(exc: Exception) -> bool:
    """Return True if *exc*'s string representation contains failover keywords."""
    msg = str(exc).lower()
    return any(kw in msg for kw in _FAILOVER_KEYWORDS)


def is_failover_trigger(exc: Exception) -> bool:
    """Return True if *exc* should trigger a provider failover.

    Failover is appropriate for provider-side account or capacity issues that
    are unlikely to resolve by retrying the same provider:

      - Rate limits (rate will not improve by immediate retry).
      - Authentication / permission / quota / billing errors.
      - Server-side 5xx errors (provider may be degraded).
      - Error messages containing quota / balance / billing keywords.

    Failover is NOT appropriate for:

      - Prompt/render/parse/validation errors (these are client-side).
      - BadRequest / NotFound errors (likely a client bug).
      - Transient timeout / connection errors (the provider retry layer should
        handle those; we only failover after that layer exhausts its retries).
    """
    # Our own exceptions — never failover
    if isinstance(exc, (PromptRenderError, ResponseParseError, SchemaValidationError)):
        return False

    # Unwrap wrapped exceptions so that SDK-specific checks work even when
    # errors are chained through wrapper exceptions.
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    candidates = [exc]
    if cause is not None:
        candidates.append(cause)

    # Anthropic SDK classes may differ across versions; use getattr defensively.
    anthropic_rate_limit = getattr(anthropic, "RateLimitError", None)
    anthropic_auth = getattr(anthropic, "AuthenticationError", None)
    anthropic_bad_request = getattr(anthropic, "BadRequestError", None)

    for candidate in candidates:
        if candidate is None:
            continue

        # OpenAI rate / auth / permission errors
        if isinstance(
            candidate,
            (openai.RateLimitError, openai.AuthenticationError, openai.PermissionDeniedError),
        ):
            return True

        # Anthropic rate / auth errors
        if anthropic_rate_limit and isinstance(candidate, anthropic_rate_limit):
            return True
        if anthropic_auth and isinstance(candidate, anthropic_auth):
            return True

        # OpenAI / Anthropic bad requests — client-side, don't failover
        if isinstance(candidate, openai.BadRequestError):
            return False
        if anthropic_bad_request and isinstance(candidate, anthropic_bad_request):
            return False

        # OpenAI not found — client-side
        if isinstance(candidate, openai.NotFoundError):
            return False

        # Catch-all message-based check for quota / balance / billing keywords.
        # This must come before the generic 4xx check so that a 401/403/429
        # response whose body says "quota exhausted" still triggers failover.
        if _error_message_matches_failover_keywords(candidate):
            return True

        # OpenAI / Anthropic APIStatusError with HTTP status code
        if isinstance(
            candidate, (openai.APIStatusError, anthropic.APIStatusError)
        ):
            status = getattr(candidate, "status_code", None) or 0
            if status >= 500:
                return True
            # 402 Payment Required means the account is not billable/membership
            # is inactive; switch to another provider immediately.
            if status == 402:
                return True
            if 400 <= status < 500:
                return False

    return False
