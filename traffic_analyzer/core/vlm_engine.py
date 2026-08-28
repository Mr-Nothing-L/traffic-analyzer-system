"""
VLMInferenceEngine module for the traffic analyzer framework.

Provides a unified interface for calling vision-language models across
multiple providers (Anthropic, OpenAI, Google, Aliyun) with prompt
templating, JSON response parsing, schema validation, retry logic,
and usage tracking.

[文件说明]
作用:VLM 统一推理引擎入口。VLMInferenceEngine 封装 prompt 渲染(Jinja2)、
  内存/磁盘两级缓存、per-provider 重试与 sticky failover 及 token 用量统计。
上游:orchestrator/analysis_orchestrator.py(构造 VLMInferenceEngine 并注入各步骤)、
  core/pipeline_steps.py、core/expert_agent.py、core/expert_agent_far_enhancement.py、
  core/sft_label_rewrite.py、core/grounding_verification.py。
下游:core/vlm_provider_clients.py(构造并发起各 provider API 请求,API key 与
  base_url 来自环境变量配置)、core/vlm_cache.py(磁盘缓存与 cache key 计算)、
  core/vlm_response_parser.py(JSON 提取/修复/校验)、core/vlm_error_classifier.py
  (错误分类决定重试/failover/致命退出)、core/vlm_exceptions.py(异常体系)、
  models/schemas.py(LLMProviderConfig/LLMResponse/PromptTemplate 等数据结构)。
"""

from __future__ import annotations

import copy
import logging
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple, Union

from jinja2 import Template, UndefinedError, StrictUndefined

# Import SDKs at top level so tests can patch them via the module namespace.
import anthropic
import httpx
import openai

from traffic_analyzer.models.schemas import (
    LLMCallRecord,
    LLMProviderConfig,
    LLMResponse,
    PromptTemplate,
)

from traffic_analyzer.core.vlm_exceptions import (
    AllProvidersExhaustedError,
    FatalAPIError,
    PromptRenderError,
    ProviderNotSupportedError,
    ResponseParseError,
    SchemaValidationError,
    VLMEngineError,
)

from traffic_analyzer.core.vlm_cache import (
    DiskCache,
    _compute_cache_key,
)

from traffic_analyzer.core.vlm_response_parser import (
    _extract_json_from_text,
    _repair_json,
    _validate_schema_basic,
)

from traffic_analyzer.core.vlm_error_classifier import (
    _is_fatal_api_error,
    _is_retryable_error,
    is_failover_trigger,
)

from traffic_analyzer.core.vlm_provider_clients import (
    _build_aliyun_payload,
    _build_anthropic_payload,
    _build_google_payload,
    _build_openai_payload,
    _call_aliyun,
    _call_anthropic,
    _call_google,
    _call_openai,
    _encode_image_to_base64,
    _is_image_path,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Compatibility re-exports
# ---------------------------------------------------------------------------

# Public interface re-exported by this module.  Internal helpers
# (underscore-prefixed) live in the focused submodules and should be
# imported directly from there by callers that need them.

__all__ = [
    "VLMInferenceEngine",
    "VLMEngineError",
    "ProviderNotSupportedError",
    "PromptRenderError",
    "ResponseParseError",
    "SchemaValidationError",
    "FatalAPIError",
    "DiskCache",
]


# ---------------------------------------------------------------------------
# VLMInferenceEngine
# ---------------------------------------------------------------------------

class VLMInferenceEngine:
    """Unified inference engine for vision-language models.

    Supports multiple providers: anthropic, google, aliyun.
    Handles prompt templating via Jinja2, image encoding, JSON response
    extraction, basic schema validation, retry logic, and usage tracking.
    """

    SUPPORTED_PROVIDERS = ("anthropic", "google", "aliyun")

    def __init__(
        self,
        config: Union[LLMProviderConfig, List[LLMProviderConfig]],
    ) -> None:
        """Initialize the engine with one or more provider configurations.

        Args:
            config: A single provider configuration or a prioritized list of
                providers. When multiple providers are given, the engine fails
                over to the next provider on quota / auth / rate-limit / 5xx
                errors.

        Raises:
            ProviderNotSupportedError: If no providers are configured or a
                provider is not supported.
        """
        if isinstance(config, LLMProviderConfig):
            providers = [config]
        else:
            providers = list(config)

        if not providers:
            raise ProviderNotSupportedError(
                "At least one LLM provider must be configured."
            )

        self._providers: List[LLMProviderConfig] = []
        for cfg in providers:
            normalized = cfg.provider.lower().strip()
            if normalized not in self.SUPPORTED_PROVIDERS:
                raise ProviderNotSupportedError(
                    f"Provider '{normalized}' is not supported. "
                    f"Supported: {self.SUPPORTED_PROVIDERS}"
                )
            # Keep normalized provider name without mutating the caller's object.
            self._providers.append(cfg.model_copy(update={"provider": normalized}))

        self._clients: List[Any] = []
        self._current_provider_index: int = 0
        for cfg in self._providers:
            self._current_provider_index = len(self._clients)
            self._clients.append(self._init_client_for_provider(cfg))
        self._current_provider_index = 0
        # Guards the sticky provider index: several expert threads share this
        # engine, so reads/writes of _current_provider_index must hold this
        # lock (the in-flight provider context itself is kept in locals).
        self._provider_lock = threading.Lock()

        # Usage statistics
        self._total_calls: int = 0
        self._total_prompt_tokens: int = 0
        self._total_completion_tokens: int = 0
        self._total_tokens: int = 0
        self._total_latency_ms: float = 0.0
        self._total_retries: int = 0
        self._failed_calls: int = 0

        # Response cache (LRU, bounded by config.cache_max_size)
        self._cache_enabled: bool = getattr(self.config, "enable_cache", True)
        self._cache_max_size: int = getattr(self.config, "cache_max_size", 128)
        self._cache: OrderedDict[str, LLMResponse] = OrderedDict()
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        self._cache_lock = threading.Lock()

        # Disk cache (cross-process persistent cache)
        self._disk_cache: Optional[DiskCache] = None
        self._disk_cache_hits: int = 0
        disk_cache_path = getattr(self.config, "disk_cache_path", None)
        if disk_cache_path:
            try:
                self._disk_cache = DiskCache(
                    db_path=disk_cache_path,
                    max_entries=getattr(self.config, "disk_cache_max_entries", 2000),
                )
                logger.info("[VLMInferenceEngine] Disk cache enabled: %s", disk_cache_path)
            except Exception as exc:
                logger.warning("[VLMInferenceEngine] Disk cache init failed: %s", exc)

    @property
    def config(self) -> LLMProviderConfig:
        """Return the currently active provider configuration."""
        return self._providers[self._current_provider_index]

    @property
    def provider(self) -> str:
        """Return the currently active provider name."""
        return self._providers[self._current_provider_index].provider

    @property
    def _client(self) -> Any:
        """Return the SDK client for the currently active provider."""
        return self._clients[self._current_provider_index]

    def _init_client_for_provider(self, config: LLMProviderConfig) -> Any:
        """Initialize the underlying SDK client for a single provider."""
        # Create an http client that bypasses system proxies to avoid
        # socks:// proxy issues (httpx does not support SOCKS by default).
        http_client = httpx.Client(proxy=None, trust_env=False, timeout=config.timeout)
        provider = config.provider.lower().strip()

        if provider == "anthropic":
            kwargs = {"api_key": config.api_key, "http_client": http_client}
            if config.base_url:
                kwargs["base_url"] = config.base_url
            return anthropic.Anthropic(**kwargs)
        elif provider == "google":
            import google.generativeai as genai
            genai.configure(api_key=config.api_key)
            return genai
        elif provider == "aliyun":
            base_url = config.base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
            return openai.OpenAI(
                api_key=config.api_key,
                base_url=base_url,
                http_client=http_client,
            )
        else:
            raise ProviderNotSupportedError(
                f"Provider '{provider}' is not supported. "
                f"Supported: {self.SUPPORTED_PROVIDERS}"
            )

    # ------------------------------------------------------------------
    # Prompt rendering
    # ------------------------------------------------------------------

    @staticmethod
    def render_prompt(
        template: PromptTemplate,
        context_vars: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str]:
        """Render system and user prompts from a PromptTemplate.

        Args:
            template: The prompt template containing Jinja2 strings.
            context_vars: Variables to inject into the template.

        Returns:
            Tuple of (rendered_system_prompt, rendered_user_prompt).

        Raises:
            PromptRenderError: If Jinja2 rendering fails.
        """
        context_vars = context_vars or {}
        # Ensure commonly referenced template variables have default values
        # to avoid StrictUndefined errors on conditional checks like {% if x %}
        defaults = {
            "scene_understanding": None,
            "video_meta": None,
            "keyframes": None,
            "candidates_json": None,
            "business_rules": None,
        }
        render_vars = {**defaults, **context_vars}
        template_id = getattr(template, "template_id", "unknown")
        try:
            system = (
                Template(template.system_prompt, undefined=StrictUndefined).render(
                    **render_vars
                )
                if template.system_prompt
                else ""
            )
            user = (
                Template(template.user_prompt, undefined=StrictUndefined).render(
                    **render_vars
                )
                if template.user_prompt
                else ""
            )
        except UndefinedError as exc:
            logger.error(
                "[vlm_engine:render_prompt] RENDER_ERROR | template_id=%s vars=%s | %s",
                template_id,
                sorted(render_vars.keys()),
                exc,
                exc_info=True,
            )
            raise PromptRenderError(f"Undefined variable in prompt template: {exc}")
        except Exception as exc:
            logger.error(
                "[vlm_engine:render_prompt] RENDER_ERROR | template_id=%s vars=%s | %s",
                template_id,
                sorted(render_vars.keys()),
                exc,
                exc_info=True,
            )
            raise PromptRenderError(f"Prompt rendering failed: {exc}")
        return system, user

    # ------------------------------------------------------------------
    # Core call
    # ------------------------------------------------------------------

    def call(
        self,
        template: PromptTemplate,
        images: List[Any],
        context_vars: Optional[Dict[str, Any]] = None,
        response_schema: Optional[Dict[str, Any]] = None,
        enable_thinking: Optional[bool] = None,
    ) -> LLMResponse:
        """Execute a single VLM call.

        Args:
            template: Prompt template to render.
            images: List of images (PIL Image, bytes, or file paths).
            context_vars: Variables for Jinja2 prompt rendering.
            response_schema: Optional JSON schema for basic validation.
            enable_thinking: Tri-state thinking switch for OpenAI-compatible
                backends (vLLM qwen3 等):None=服务端默认;True/False 经
                extra_body 传 chat_template_kwargs.enable_thinking。参与
                cache key 计算。anthropic/google 分支不支持该参数,忽略。

        Returns:
            LLMResponse with parsed data, token usage, and latency.
        """
        system_prompt, user_prompt = self.render_prompt(template, context_vars)

        # --- Cache lookup (memory first, then disk) ---
        cache_key = ""
        if self._cache_enabled:
            cache_key = _compute_cache_key(
                system_prompt, user_prompt, images, enable_thinking=enable_thinking
            )
            # Resolve the provider that would serve this call (under lock) so a
            # response cached before a failover is not returned for the new one.
            with self._provider_lock:
                active_config = self._providers[self._current_provider_index]
            with self._cache_lock:
                cached = self._cache.get(cache_key)
                if (
                    cached is not None
                    and cached.provider == active_config.provider
                    and cached.model == active_config.model
                ):
                    self._cache_hits += 1
                    # Move to end (most recently used)
                    self._cache.move_to_end(cache_key)
                    logger.debug("[cache] MEM HIT for key %s... (%d cached)", cache_key[:16], len(self._cache))
                    return copy.deepcopy(cached)
                if cached is not None:
                    logger.debug(
                        "[cache] MEM SKIP provider/model mismatch for key %s...",
                        cache_key[:16],
                    )
                self._cache_misses += 1

            # Memory miss — try disk cache
            if self._disk_cache is not None:
                disk_cached = self._disk_cache.get(cache_key, self.provider, self.config.model)
                if disk_cached is not None:
                    self._disk_cache_hits += 1
                    # Promote to memory cache
                    with self._cache_lock:
                        self._cache[cache_key] = copy.deepcopy(disk_cached)
                        while len(self._cache) > self._cache_max_size:
                            self._cache.popitem(last=False)
                    logger.debug("[cache] DISK HIT for key %s...", cache_key[:16])
                    return copy.deepcopy(disk_cached)

        call_id = str(uuid.uuid4())
        start_time = time.perf_counter()
        retry_count = 0
        raw_text = ""
        parsed_data: Dict[str, Any] = {}
        success = False
        error_message: Optional[str] = None
        prompt_tokens = completion_tokens = total_tokens = 0
        # Index of the provider that actually served the request (set on success).
        served_provider_index: Optional[int] = None

        template_id = getattr(template, "template_id", "unknown")
        try:
            (
                raw_text,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                retry_count,
                served_provider_index,
            ) = self._execute_with_retry(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                images=images,
                enable_thinking=enable_thinking,
            )
            parsed_data = _extract_json_from_text(raw_text)
            if response_schema:
                _validate_schema_basic(parsed_data, response_schema)
            success = True
        except PromptRenderError as exc:
            error_message = str(exc)
            logger.error(
                "[vlm_engine:call] PROMPT_RENDER_ERROR | template_id=%s images=%d schema=%s | %s",
                template_id,
                len(images),
                "yes" if response_schema else "no",
                exc,
                exc_info=True,
            )
        except ResponseParseError as exc:
            error_message = str(exc)
            logger.error(
                "[vlm_engine:call] PARSE_ERROR | template_id=%s images=%d schema=%s | %s",
                template_id,
                len(images),
                "yes" if response_schema else "no",
                exc,
                exc_info=True,
            )
        except SchemaValidationError as exc:
            error_message = str(exc)
            raw_text = f"{raw_text}\n\nSchema validation error: {exc}" if raw_text else str(exc)
            logger.error(
                "[vlm_engine:call] SCHEMA_ERROR | template_id=%s images=%d schema=%s | %s",
                template_id,
                len(images),
                "yes" if response_schema else "no",
                exc,
                exc_info=True,
            )
        except Exception as exc:
            # Fatal API errors (quota/auth/all-providers-exhausted/402 payment)
            # must propagate up to stop batch processing.
            if _is_fatal_api_error(exc) or isinstance(exc, AllProvidersExhaustedError):
                raise FatalAPIError(f"API unusable: {exc}") from exc
            # Surface the error in the response so reports can show what failed
            # instead of an empty raw_text snippet.
            error_summary = f"{type(exc).__name__}: {exc}"
            raw_text = error_summary
            error_message = error_summary
            retry_count = getattr(exc, "_retry_count", retry_count)
            logger.error(
                "[vlm_engine:call] UNEXPECTED_ERROR | template_id=%s images=%d schema=%s | %s",
                template_id,
                len(images),
                "yes" if response_schema else "no",
                exc,
                exc_info=True,
            )

        latency_ms = (time.perf_counter() - start_time) * 1000.0

        # Update stats
        with self._cache_lock:
            self._total_calls += 1
            self._total_prompt_tokens += prompt_tokens
            self._total_completion_tokens += completion_tokens
            self._total_tokens += total_tokens
            self._total_latency_ms += latency_ms
            self._total_retries += retry_count
            if not success:
                self._failed_calls += 1

        # Label the response from the provider that actually served the call.
        # Reading self.provider/self.config here would race with a concurrent
        # failover flipping the shared provider index mid-call.
        if served_provider_index is not None:
            served_config = self._providers[served_provider_index]
            response_provider = served_config.provider
            response_model = served_config.model
        else:
            response_provider = self.provider
            response_model = self.config.model

        response = LLMResponse(
            success=success,
            raw_text=raw_text,
            parsed_data=parsed_data,
            model=response_model,
            provider=response_provider,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            retry_count=retry_count,
        )

        # --- Cache store (only successful responses) ---
        if self._cache_enabled and cache_key and success:
            with self._cache_lock:
                self._cache[cache_key] = copy.deepcopy(response)
                # Evict oldest if over capacity
                while len(self._cache) > self._cache_max_size:
                    self._cache.popitem(last=False)
                logger.debug("[cache] MEM STORED key %s... (size=%d)", cache_key[:16], len(self._cache))
            # Also write to disk cache for cross-process sharing
            if self._disk_cache is not None:
                self._disk_cache.set(cache_key, response_provider, response_model, response)
                logger.debug("[cache] DISK STORED key %s...", cache_key[:16])

        return response

    def _execute_once(
        self,
        system_prompt: str,
        user_prompt: str,
        images: List[Any],
        provider_index: Optional[int] = None,
        enable_thinking: Optional[bool] = None,
    ) -> Tuple[str, int, int, int]:
        """Execute a single provider-specific API call (no retry).

        Args:
            system_prompt: Rendered system prompt.
            user_prompt: Rendered user prompt.
            images: List of images.
            provider_index: Provider to dispatch to. Resolved from the shared
                sticky index when omitted; callers doing failover must pass it
                explicitly so a concurrent failover cannot swap provider,
                config, and client mid-call.
            enable_thinking: 仅 OpenAI 兼容(aliyun)分支支持;anthropic/google
                分支不支持该参数,忽略。
        """
        if provider_index is None:
            with self._provider_lock:
                provider_index = self._current_provider_index
        provider = self._providers[provider_index].provider
        config = self._providers[provider_index]
        client = self._clients[provider_index]
        try:
            if provider == "anthropic":
                # enable_thinking 不适用(Anthropic 分支默认已尝试禁用 thinking)。
                _, kwargs = _build_anthropic_payload(
                    system_prompt,
                    user_prompt,
                    images,
                    config.model,
                    config.max_tokens,
                    config.temperature,
                )
                return _call_anthropic(client, kwargs)
            elif provider == "google":
                # enable_thinking 不适用(google.generativeai 无对应参数)。
                contents, kwargs = _build_google_payload(
                    system_prompt,
                    user_prompt,
                    images,
                    config.model,
                    config.max_tokens,
                    config.temperature,
                )
                return _call_google(
                    client.GenerativeModel(config.model),
                    contents,
                    kwargs["generation_config"],
                    timeout=config.timeout,
                )
            elif provider == "aliyun":
                _, kwargs = _build_aliyun_payload(
                    system_prompt,
                    user_prompt,
                    images,
                    config.model,
                    config.max_tokens,
                    config.temperature,
                    enable_thinking=enable_thinking,
                )
                return _call_aliyun(client, kwargs)
            else:
                raise ProviderNotSupportedError(f"Provider {provider} not supported")
        except ProviderNotSupportedError:
            raise
        except Exception as exc:
            logger.error(
                "[vlm_engine:_execute_once] PROVIDER_ERROR | provider=%s model=%s | %s",
                provider,
                config.model,
                exc,
                exc_info=True,
            )
            raise

    def _execute_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        images: List[Any],
        enable_thinking: Optional[bool] = None,
    ) -> Tuple[str, int, int, int, int, int]:
        """Execute the API call with per-provider retry and provider failover.

        Args:
            enable_thinking: 仅 OpenAI 兼容(aliyun)分支支持,原样下传。

        Returns:
            Tuple of (raw_text, prompt_tokens, completion_tokens, total_tokens,
            retry_count, provider_index) where provider_index identifies the
            provider that actually served the request.
        """
        # NOTE: This is the sole retry layer for VLM API calls.
        # PipelineStep no longer performs retries — all retry logic lives here.
        last_error: Optional[Exception] = None
        retry_count = 0
        # The provider index is a local for the rest of this call; the shared
        # sticky index is only read here (under lock) and updated on success.
        with self._provider_lock:
            start_index = self._current_provider_index
        num_providers = len(self._providers)

        for provider_idx in range(start_index, num_providers):
            provider = self._providers[provider_idx].provider
            max_retries = max(1, self._providers[provider_idx].max_retries)
            last_error = None

            for attempt in range(max_retries):
                try:
                    result = self._execute_once(
                        system_prompt,
                        user_prompt,
                        images,
                        provider_index=provider_idx,
                        enable_thinking=enable_thinking,
                    )
                    # Sticky failover: subsequent calls start from the provider
                    # that served this request.
                    with self._provider_lock:
                        self._current_provider_index = provider_idx
                    return (*result, retry_count, provider_idx)
                except Exception as exc:
                    last_error = exc
                    if not _is_retryable_error(exc):
                        # Non-retryable error: decide whether to failover or give up.
                        if is_failover_trigger(exc):
                            if provider_idx < num_providers - 1:
                                next_provider = self._providers[provider_idx + 1].provider
                                logger.error(
                                    "[vlm_engine:_execute_with_retry] FAILOVER | from_provider=%s to_provider=%s reason=%s",
                                    provider,
                                    next_provider,
                                    exc,
                                )
                                break
                            # Failover-trigger error on the LAST provider means
                            # the whole provider chain is down: escalate so the
                            # batch aborts instead of silently reporting empty
                            # results for every event.
                            logger.error(
                                "[vlm_engine:_execute_with_retry] ALL_PROVIDERS_EXHAUSTED | provider=%s | error=%s",
                                provider,
                                exc,
                                exc_info=True,
                            )
                            setattr(last_error, "_retry_count", retry_count)
                            exhausted = AllProvidersExhaustedError(
                                f"All providers exhausted. Last error: {last_error}"
                            )
                            setattr(exhausted, "_retry_count", retry_count)
                            raise exhausted from last_error
                        # Plain client-side error (e.g. HTTP 400): not an outage,
                        # propagate unchanged.
                        logger.error(
                            "[vlm_engine:_execute_with_retry] NON_RETRYABLE | provider=%s attempt=%d/%d | error=%s",
                            provider,
                            attempt + 1,
                            max_retries,
                            exc,
                            exc_info=True,
                        )
                        setattr(last_error, "_retry_count", retry_count)
                        raise last_error
                    if attempt < max_retries - 1:
                        retry_count += 1
                        wait_sec = min(2 ** attempt, 30)
                        logger.error(
                            "[vlm_engine:_execute_with_retry] RETRY | provider=%s attempt=%d/%d wait=%.1fs | error=%s",
                            provider,
                            attempt + 1,
                            max_retries,
                            wait_sec,
                            exc,
                            exc_info=True,
                        )
                        time.sleep(wait_sec)
                    else:
                        break

            # Provider exhausted its retries. Failover if the final error warrants it
            # and another provider is available; otherwise propagate the error.
            if last_error is not None:
                if is_failover_trigger(last_error) and provider_idx < num_providers - 1:
                    next_provider = self._providers[provider_idx + 1].provider
                    logger.error(
                        "[vlm_engine:_execute_with_retry] FAILOVER | from_provider=%s to_provider=%s reason=%s",
                        provider,
                        next_provider,
                        last_error,
                    )
                    continue
                # Retries exhausted on the LAST provider (always a retryable
                # error here): total outage — escalate as fatal so callers can
                # abort the batch instead of emitting all-zero reports.
                if provider_idx == num_providers - 1:
                    logger.error(
                        "[vlm_engine:_execute_with_retry] ALL_PROVIDERS_EXHAUSTED | provider=%s attempts=%d last_error=%s",
                        provider,
                        retry_count,
                        last_error,
                        exc_info=True,
                    )
                    setattr(last_error, "_retry_count", retry_count)
                    exhausted = AllProvidersExhaustedError(
                        f"All providers exhausted. Last error: {last_error}"
                    )
                    setattr(exhausted, "_retry_count", retry_count)
                    raise exhausted from last_error
                logger.error(
                    "[vlm_engine:_execute_with_retry] MAX_RETRIES_EXCEEDED | provider=%s attempts=%d last_error=%s",
                    provider,
                    retry_count,
                    last_error,
                    exc_info=True,
                )
                setattr(last_error, "_retry_count", retry_count)
                raise last_error

        if last_error is not None:
            raise AllProvidersExhaustedError(
                f"All providers exhausted. Last error: {last_error}"
            ) from last_error
        raise RuntimeError("Unknown error during VLM call")

    # ------------------------------------------------------------------
    # Batch call
    # ------------------------------------------------------------------

    def batch_call(
        self,
        requests: List[Dict[str, Any]],
        parallel: bool = False,
        max_workers: int = 4,
    ) -> List[LLMResponse]:
        """Execute multiple VLM calls.

        Args:
            requests: List of request dicts, each containing:
                - template (PromptTemplate)
                - images (List[Any])
                - context_vars (Optional[Dict[str, Any]])
                - response_schema (Optional[Dict[str, Any]])
            parallel: If True, execute calls in parallel using ThreadPoolExecutor.
            max_workers: Maximum number of threads for parallel execution.

        Returns:
            List of LLMResponse objects in the same order as requests.
        """
        if parallel:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _submit(req: Dict[str, Any]) -> LLMResponse:
                return self.call(
                    template=req["template"],
                    images=req.get("images", []),
                    context_vars=req.get("context_vars"),
                    response_schema=req.get("response_schema"),
                )

            responses: List[LLMResponse] = [LLMResponse(success=False)] * len(requests)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_index = {
                    executor.submit(_submit, req): idx
                    for idx, req in enumerate(requests)
                }
                for future in as_completed(future_to_index):
                    idx = future_to_index[future]
                    try:
                        responses[idx] = future.result()
                    except Exception as exc:
                        template_id = getattr(requests[idx].get("template"), "template_id", "unknown")
                        logger.error(
                            "[vlm_engine:batch_call] FUTURE_ERROR | idx=%d template_id=%s | %s",
                            idx,
                            template_id,
                            exc,
                            exc_info=True,
                        )
                        responses[idx] = LLMResponse(
                            success=False,
                            raw_text="",
                            error_message=str(exc),
                        )
            return responses

        # Sequential execution
        results: List[LLMResponse] = []
        for idx, req in enumerate(requests):
            try:
                resp = self.call(
                    template=req["template"],
                    images=req.get("images", []),
                    context_vars=req.get("context_vars"),
                    response_schema=req.get("response_schema"),
                )
                results.append(resp)
            except Exception as exc:
                template_id = getattr(req.get("template"), "template_id", "unknown")
                logger.error(
                    "[vlm_engine:batch_call] CALL_ERROR | idx=%d template_id=%s images=%d schema=%s | %s",
                    idx,
                    template_id,
                    len(req.get("images", [])),
                    "yes" if req.get("response_schema") else "no",
                    exc,
                    exc_info=True,
                )
                results.append(
                    LLMResponse(
                        success=False,
                        raw_text="",
                        error_message=str(exc),
                    )
                )
        return results

    # ------------------------------------------------------------------
    # Usage stats
    # ------------------------------------------------------------------

    def get_usage_stats(self) -> Dict[str, Any]:
        """Return cumulative usage statistics.

        Returns:
            Dictionary with total calls, tokens, latency, retries, and failures.
        """
        total_cache_lookups = self._cache_hits + self._cache_misses
        total_disk_lookups = total_cache_lookups + self._disk_cache_hits
        stats = {
            "provider": self.provider,
            "model": self.config.model,
            "total_calls": self._total_calls,
            "failed_calls": self._failed_calls,
            "total_prompt_tokens": self._total_prompt_tokens,
            "total_completion_tokens": self._total_completion_tokens,
            "total_tokens": self._total_tokens,
            "total_latency_ms": round(self._total_latency_ms, 2),
            "total_retries": self._total_retries,
            "average_latency_ms": round(
                self._total_latency_ms / max(self._total_calls, 1), 2
            ),
            "cache_enabled": self._cache_enabled,
            "cache_size": len(self._cache),
            "cache_max_size": self._cache_max_size,
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "cache_hit_rate": round(
                self._cache_hits / max(total_cache_lookups, 1), 4
            ),
            "disk_cache_hits": self._disk_cache_hits,
            "combined_cache_hit_rate": round(
                (self._cache_hits + self._disk_cache_hits) / max(total_disk_lookups, 1), 4
            ),
        }
        if self._disk_cache is not None:
            stats.update(self._disk_cache.get_stats())
        return stats

    # ------------------------------------------------------------------
    # Audit helper
    # ------------------------------------------------------------------

    def create_call_record(
        self,
        template_id: str,
        response: LLMResponse,
    ) -> LLMCallRecord:
        """Create an audit record from an LLMResponse.

        Args:
            template_id: Identifier of the prompt template used.
            response: The response object returned by call().

        Returns:
            LLMCallRecord suitable for logging in AnalysisContext.
        """
        return LLMCallRecord(
            call_id=str(uuid.uuid4()),
            template_id=template_id,
            model=response.model or self.config.model,
            provider=response.provider or self.provider,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens,
            latency_ms=response.latency_ms,
            success=response.success,
            error_message=(
                None
                if response.success
                else response.error_message or response.raw_text or "Unknown error"
            ),
        )
