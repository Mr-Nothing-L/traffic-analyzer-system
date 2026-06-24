"""
Exception hierarchy shared by the VLM inference engine modules.
"""

from __future__ import annotations


class VLMEngineError(Exception):
    """Base exception for VLM engine errors."""


class ProviderNotSupportedError(VLMEngineError):
    """Raised when the configured provider is not supported."""


class PromptRenderError(VLMEngineError):
    """Raised when prompt template rendering fails."""


class ResponseParseError(VLMEngineError):
    """Raised when the LLM response cannot be parsed."""


class SchemaValidationError(VLMEngineError):
    """Raised when parsed response fails schema validation."""


class FatalAPIError(VLMEngineError):
    """Raised when the API is unusable (quota exhausted, auth failed, etc.).

    This error propagates through all fallback layers to signal batch_infer
    that subsequent videos will also fail — processing should stop immediately.
    """


class AllProvidersExhaustedError(VLMEngineError):
    """Raised when all configured providers fail and failover is exhausted."""
