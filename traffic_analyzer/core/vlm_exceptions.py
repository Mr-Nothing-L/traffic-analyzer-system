"""
Exception hierarchy shared by the VLM inference engine modules.

[文件说明]
作用:VLM 引擎模块共享的异常体系。定义 VLMEngineError 基类及
  ProviderNotSupportedError、PromptRenderError、ResponseParseError、
  SchemaValidationError、FatalAPIError(API 不可用,中止批处理)、
  AllProvidersExhaustedError(全部 provider 均失败)。
上游:core/vlm_engine.py、core/vlm_error_classifier.py、
  core/vlm_response_parser.py(抛出/捕获);并经 vlm_engine.py 重导出,
  被 orchestrator/analysis_orchestrator.py、core/sft_label_rewrite.py、
  core/grounding_verification.py 等捕获 FatalAPIError 以中止批处理。
下游:无(纯异常定义,不依赖其他模块)。
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
