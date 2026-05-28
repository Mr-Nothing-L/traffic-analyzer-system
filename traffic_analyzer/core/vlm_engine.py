"""
VLMInferenceEngine module for the traffic analyzer framework.

Provides a unified interface for calling vision-language models across
multiple providers (Anthropic, OpenAI, Google, Aliyun) with prompt
templating, JSON response parsing, schema validation, retry logic,
and usage tracking.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from jinja2 import Template, UndefinedError, StrictUndefined, DebugUndefined

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

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_cache_key(system_prompt: str, user_prompt: str, images: List[Any]) -> str:
    """Compute a deterministic cache key for a VLM call.

    The key is a SHA-256 hex digest of the prompt text combined with
    the raw image data.  This allows identical calls (same prompt +
    same images) to hit the cache even if the caller passes different
    Python object identities.

    Args:
        system_prompt: Rendered system prompt.
        user_prompt: Rendered user prompt.
        images: List of images (PIL Image, bytes, or file paths).

    Returns:
        Hex digest string suitable as a cache key.
    """
    hasher = hashlib.sha256()
    hasher.update((system_prompt or "").encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update((user_prompt or "").encode("utf-8"))

    for img in images:
        hasher.update(b"\x00")
        if isinstance(img, bytes):
            hasher.update(img)
        elif isinstance(img, str):
            try:
                with open(img, "rb") as fh:
                    hasher.update(fh.read())
            except OSError:
                hasher.update(img.encode("utf-8"))
        else:
            # PIL Image or other – convert to PNG bytes
            try:
                from PIL import Image as PILImage
                if isinstance(img, PILImage.Image):
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    hasher.update(buf.getvalue())
                else:
                    hasher.update(str(img).encode("utf-8"))
            except Exception:
                hasher.update(str(img).encode("utf-8"))

    return hasher.hexdigest()


def _encode_image_to_base64(image: Any) -> str:
    """Convert an image to a base64-encoded PNG string.

    Args:
        image: PIL Image, bytes, or file path (str/Path).

    Returns:
        Base64-encoded PNG data URI.
    """
    try:
        try:
            from PIL import Image as PILImage
        except ImportError:  # pragma: no cover
            PILImage = None  # type: ignore[misc,assignment]

        if PILImage is not None and isinstance(image, PILImage.Image):
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            data = buffer.getvalue()
        elif isinstance(image, bytes):
            data = image
        elif isinstance(image, (str,)):
            f = open(image, "rb")
            try:
                data = f.read()
            finally:
                f.close()
        else:
            raise TypeError(
                f"Unsupported image type: {type(image)}. "
                "Expected PIL Image, bytes, or file path."
            )

        b64 = base64.b64encode(data).decode("utf-8")
        return f"data:image/png;base64,{b64}"
    except Exception as exc:
        image_type = type(image).__name__
        logger.error(
            "[vlm_engine:_encode_image_to_base64] ENCODE_FAILED | image_type=%s | %s",
            image_type,
            exc,
            exc_info=True,
        )
        raise


def _extract_json_from_text(text: str) -> Dict[str, Any]:
    """Extract JSON object from text, with fallback to regex.

    Tries strict JSON parsing first, then searches for the first
    JSON object block via regex.

    Args:
        text: Raw text potentially containing JSON.

    Returns:
        Parsed JSON dictionary.

    Raises:
        ResponseParseError: If no valid JSON is found.
    """
    try:
        text = text.strip()
        # Try direct parse first
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
            # VLM sometimes returns a JSON array (e.g. []) instead of an object.
            # If the array contains a dict as its first element, use that.
            if isinstance(result, list) and result and isinstance(result[0], dict):
                return result[0]
        except json.JSONDecodeError:
            pass

        # Try to find a JSON object or array block
        # Look for ```json ... ``` fenced code blocks first
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced:
            try:
                return json.loads(fenced.group(1))
            except json.JSONDecodeError:
                pass

        # Fallback: first top-level { ... } or [ ... ]
        match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(1))
                if isinstance(result, dict):
                    return result
                if isinstance(result, list) and result and isinstance(result[0], dict):
                    return result[0]
            except json.JSONDecodeError as exc:
                raise ResponseParseError(f"Found JSON-like block but failed to parse: {exc}")

        raise ResponseParseError("No JSON object found in response text.")
    except ResponseParseError:
        raise
    except Exception as exc:
        logger.error(
            "[vlm_engine:_extract_json_from_text] PARSE_FAILED | text_len=%d | %s",
            len(text),
            exc,
            exc_info=True,
        )
        raise ResponseParseError(f"JSON extraction failed: {exc}") from exc


def _validate_schema_basic(data: Dict[str, Any], schema: Dict[str, Any]) -> None:
    """Perform basic key-check validation against a JSON schema.

    Currently checks that all top-level 'required' keys are present.

    Args:
        data: Parsed response data.
        schema: JSON schema dict (may contain 'required' list).

    Raises:
        SchemaValidationError: If required keys are missing.
    """
    required = schema.get("required", [])
    missing = [k for k in required if k not in data]
    if missing:
        raise SchemaValidationError(
            f"Schema validation failed: missing required keys {missing}"
        )


# ---------------------------------------------------------------------------
# Provider-specific payload builders / callers
# ---------------------------------------------------------------------------

def _is_image_path(path: str) -> bool:
    """Check if a string looks like an image file path or URL."""
    if not isinstance(path, str):
        return False
    return path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")) or path.startswith(("http://", "https://", "data:image/"))


def _build_anthropic_payload(
    system_prompt: str,
    user_prompt: str,
    images: List[Any],
    model: str,
    max_tokens: int,
    temperature: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build Anthropic message list and kwargs.

    Supports interleaving text labels with images: if an element in *images*
    is a plain string that does not look like an image path, it is inserted
    as a text content block before the subsequent image.
    """
    content: List[Dict[str, Any]] = []
    if user_prompt:
        content.append({"type": "text", "text": user_prompt})
    for img in images:
        if isinstance(img, str) and not _is_image_path(img):
            content.append({"type": "text", "text": img})
            continue
        b64_uri = _encode_image_to_base64(img)
        # Anthropic expects base64 data without the data URI prefix
        b64_data = b64_uri.split(",", 1)[1]
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": b64_data,
                },
            }
        )

    messages: List[Dict[str, Any]] = [{"role": "user", "content": content}]
    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if system_prompt:
        kwargs["system"] = system_prompt
    return messages, kwargs


def _build_openai_payload(
    system_prompt: str,
    user_prompt: str,
    images: List[Any],
    model: str,
    max_tokens: int,
    temperature: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build OpenAI message list and kwargs.

    Supports interleaving text labels with images (see _build_anthropic_payload).
    """
    content: List[Dict[str, Any]] = []
    if user_prompt:
        content.append({"type": "text", "text": user_prompt})
    for img in images:
        if isinstance(img, str) and not _is_image_path(img):
            content.append({"type": "text", "text": img})
            continue
        b64_uri = _encode_image_to_base64(img)
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": b64_uri, "detail": "auto"},
            }
        )

    messages: List[Dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": content})

    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    return messages, kwargs


def _build_google_payload(
    system_prompt: str,
    user_prompt: str,
    images: List[Any],
    model: str,
    max_tokens: int,
    temperature: float,
) -> Tuple[Any, Dict[str, Any]]:
    """Build Google GenAI content list and kwargs."""
    try:
        from PIL import Image as PILImage
    except ImportError:  # pragma: no cover
        PILImage = None  # type: ignore[misc,assignment]

    contents: List[Any] = []
    if system_prompt:
        contents.append(system_prompt)
    if user_prompt:
        contents.append(user_prompt)
    for img in images:
        if isinstance(img, str) and not _is_image_path(img):
            contents.append(img)
            continue
        if PILImage is not None and isinstance(img, PILImage.Image):
            contents.append(img)
        elif isinstance(img, bytes):
            contents.append(PILImage.open(io.BytesIO(img)) if PILImage else img)
        elif isinstance(img, str):
            contents.append(PILImage.open(img) if PILImage else img)
        else:
            contents.append(img)

    kwargs: Dict[str, Any] = {
        "model": model,
    }
    generation_config: Dict[str, Any] = {
        "max_output_tokens": max_tokens,
        "temperature": temperature,
    }
    kwargs["generation_config"] = generation_config
    return contents, kwargs


def _build_aliyun_payload(
    system_prompt: str,
    user_prompt: str,
    images: List[Any],
    model: str,
    max_tokens: int,
    temperature: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build Aliyun (OpenAI-compatible) message list and kwargs."""
    # Aliyun Qwen-VL supports OpenAI-compatible vision format
    content: List[Dict[str, Any]] = []
    if user_prompt:
        content.append({"type": "text", "text": user_prompt})
    for img in images:
        if isinstance(img, str) and not _is_image_path(img):
            content.append({"type": "text", "text": img})
            continue
        b64_uri = _encode_image_to_base64(img)
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": b64_uri},
            }
        )

    messages: List[Dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": content})

    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    return messages, kwargs


# ---------------------------------------------------------------------------
# Provider-specific callers
# ---------------------------------------------------------------------------

def _call_anthropic(
    client: Any,
    kwargs: Dict[str, Any],
) -> Tuple[str, int, int, int]:
    """Call Anthropic and return (text, prompt_tokens, completion_tokens, total_tokens)."""
    response = client.messages.create(**kwargs)
    text = ""
    if response.content:
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text += block.text
    usage = response.usage
    prompt_tokens = getattr(usage, "input_tokens", 0)
    completion_tokens = getattr(usage, "output_tokens", 0)
    total_tokens = prompt_tokens + completion_tokens
    return text, prompt_tokens, completion_tokens, total_tokens


def _call_anthropic_with_tools(
    client: Any,
    kwargs: Dict[str, Any],
) -> Tuple[str, List[Dict[str, Any]], int, int, int]:
    """
    Call Anthropic with tool support.
    
    Returns:
        (text, tool_use_blocks, prompt_tokens, completion_tokens, total_tokens)
        tool_use_blocks: list of {"name": str, "id": str, "input": dict}
    """
    response = client.messages.create(**kwargs)
    text = ""
    tool_uses: List[Dict[str, Any]] = []
    
    if response.content:
        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text += block.text
            elif block_type == "tool_use":
                tool_uses.append({
                    "name": getattr(block, "name", ""),
                    "id": getattr(block, "id", ""),
                    "input": getattr(block, "input", {}),
                })
    
    usage = response.usage
    prompt_tokens = getattr(usage, "input_tokens", 0)
    completion_tokens = getattr(usage, "output_tokens", 0)
    total_tokens = prompt_tokens + completion_tokens
    return text, tool_uses, prompt_tokens, completion_tokens, total_tokens


def _call_openai(
    client: Any,
    kwargs: Dict[str, Any],
) -> Tuple[str, int, int, int]:
    """Call OpenAI and return (text, prompt_tokens, completion_tokens, total_tokens)."""
    response = client.chat.completions.create(**kwargs)
    text = response.choices[0].message.content or ""
    usage = response.usage
    prompt_tokens = getattr(usage, "prompt_tokens", 0)
    completion_tokens = getattr(usage, "completion_tokens", 0)
    total_tokens = getattr(usage, "total_tokens", prompt_tokens + completion_tokens)
    return text, prompt_tokens, completion_tokens, total_tokens


def _call_google(
    client_model: Any,
    contents: Any,
    generation_config: Dict[str, Any],
) -> Tuple[str, int, int, int]:
    """Call Google GenAI and return (text, prompt_tokens, completion_tokens, total_tokens)."""
    response = client_model.generate_content(
        contents,
        generation_config=generation_config,
    )
    text = ""
    if response.parts:
        for part in response.parts:
            if hasattr(part, "text"):
                text += part.text
    elif hasattr(response, "text"):
        text = response.text

    # Google does not always return token counts; attempt to extract
    usage_metadata = getattr(response, "usage_metadata", None)
    if usage_metadata:
        prompt_tokens = getattr(usage_metadata, "prompt_token_count", 0)
        completion_tokens = getattr(usage_metadata, "candidates_token_count", 0)
        total_tokens = getattr(
            usage_metadata, "total_token_count", prompt_tokens + completion_tokens
        )
    else:
        prompt_tokens = completion_tokens = total_tokens = 0
    return text, prompt_tokens, completion_tokens, total_tokens


def _call_aliyun(
    client: Any,
    kwargs: Dict[str, Any],
) -> Tuple[str, int, int, int]:
    """Call Aliyun via OpenAI-compatible client."""
    # Aliyun uses the same interface as OpenAI
    return _call_openai(client, kwargs)


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

    def __init__(self, config: LLMProviderConfig) -> None:
        """Initialize the engine with provider configuration.

        Args:
            config: Provider configuration including API key, model,
                timeout, and retry settings.

        Raises:
            ProviderNotSupportedError: If the provider is not supported.
        """
        self.config = config
        self.provider = config.provider.lower().strip()
        if self.provider not in self.SUPPORTED_PROVIDERS:
            raise ProviderNotSupportedError(
                f"Provider '{self.provider}' is not supported. "
                f"Supported: {self.SUPPORTED_PROVIDERS}"
            )

        self._client: Optional[Any] = None
        self._init_client()

        # Usage statistics
        self._total_calls: int = 0
        self._total_prompt_tokens: int = 0
        self._total_completion_tokens: int = 0
        self._total_tokens: int = 0
        self._total_latency_ms: float = 0.0
        self._total_retries: int = 0
        self._failed_calls: int = 0

        # Response cache (LRU, bounded by config.cache_max_size)
        self._cache_enabled: bool = getattr(config, "enable_cache", True)
        self._cache_max_size: int = getattr(config, "cache_max_size", 128)
        self._cache: OrderedDict[str, LLMResponse] = OrderedDict()
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        self._cache_lock = threading.Lock()

    def _init_client(self) -> None:
        """Initialize the underlying SDK client based on provider."""
        # Create an http client that bypasses system proxies to avoid
        # socks:// proxy issues (httpx does not support SOCKS by default).
        http_client = httpx.Client(proxy=None, trust_env=False, timeout=self.config.timeout)

        if self.provider == "anthropic":
            kwargs = {"api_key": self.config.api_key, "http_client": http_client}
            if self.config.base_url:
                kwargs["base_url"] = self.config.base_url
            self._client = anthropic.Anthropic(**kwargs)
        elif self.provider == "google":
            import google.generativeai as genai
            genai.configure(api_key=self.config.api_key)
            self._client = genai
        elif self.provider == "aliyun":
            base_url = self.config.base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
            self._client = openai.OpenAI(
                api_key=self.config.api_key,
                base_url=base_url,
                http_client=http_client,
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
    ) -> LLMResponse:
        """Execute a single VLM call.

        Args:
            template: Prompt template to render.
            images: List of images (PIL Image, bytes, or file paths).
            context_vars: Variables for Jinja2 prompt rendering.
            response_schema: Optional JSON schema for basic validation.

        Returns:
            LLMResponse with parsed data, token usage, and latency.
        """
        system_prompt, user_prompt = self.render_prompt(template, context_vars)

        # --- Cache lookup ---
        cache_key = ""
        if self._cache_enabled:
            cache_key = _compute_cache_key(system_prompt, user_prompt, images)
            with self._cache_lock:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    self._cache_hits += 1
                    # Move to end (most recently used)
                    self._cache.move_to_end(cache_key)
                    logger.debug("[cache] HIT for key %s... (%d cached)", cache_key[:16], len(self._cache))
                    return copy.deepcopy(cached)
                self._cache_misses += 1

        call_id = str(uuid.uuid4())
        start_time = time.perf_counter()
        retry_count = 0
        raw_text = ""
        parsed_data: Dict[str, Any] = {}
        success = False
        error_message: Optional[str] = None
        prompt_tokens = completion_tokens = total_tokens = 0

        template_id = getattr(template, "template_id", "unknown")
        try:
            raw_text, prompt_tokens, completion_tokens, total_tokens, retry_count = (
                self._execute_with_retry(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    images=images,
                )
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
            error_message = str(exc)
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

        response = LLMResponse(
            success=success,
            raw_text=raw_text,
            parsed_data=parsed_data,
            model=self.config.model,
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
                logger.debug("[cache] STORED key %s... (size=%d)", cache_key[:16], len(self._cache))

        return response

    def call_with_tools(
        self,
        template: PromptTemplate,
        images: List[Any],
        tool_definitions: List[Dict[str, Any]],
        context_vars: Optional[Dict[str, Any]] = None,
        response_schema: Optional[Dict[str, Any]] = None,
    ) -> Tuple[LLMResponse, List[Dict[str, Any]]]:
        """
        Execute a VLM call with Anthropic Native API tool support.
        
        This method performs a multi-turn conversation:
        1. User message (prompt + images) + tools definition
        2. Assistant message with tool_use blocks (or direct text response)
        3. If tool_use blocks present: User message with tool_result blocks
        4. Assistant message with final analysis
        
        Args:
            template: Prompt template to render.
            images: List of images.
            tool_definitions: List of Anthropic-format tool definitions.
            context_vars: Variables for Jinja2 prompt rendering.
            response_schema: Optional JSON schema for basic validation.
        
        Returns:
            (LLMResponse, tool_use_blocks)
            If tool_use_blocks is non-empty, caller must execute tools and call again.
        """
        if self.provider != "anthropic":
            logger.warning(
                "[vlm_engine:call_with_tools] FALLBACK | provider=%s not anthropic, using string-based tool parsing",
                self.provider,
            )
            # Fallback to regular call
            response = self.call(template, images, context_vars, response_schema)
            return response, []
        
        system_prompt, user_prompt = self.render_prompt(template, context_vars)
        
        # Build initial message list
        _, kwargs = _build_anthropic_payload(
            system_prompt,
            user_prompt,
            images,
            self.config.model,
            self.config.max_tokens,
            self.config.temperature,
        )
        kwargs["tools"] = tool_definitions
        kwargs["tool_choice"] = {"type": "auto"}
        
        call_id = str(uuid.uuid4())
        start_time = time.perf_counter()
        raw_text = ""
        parsed_data: Dict[str, Any] = {}
        success = False
        error_message: Optional[str] = None
        prompt_tokens = completion_tokens = total_tokens = 0
        tool_uses: List[Dict[str, Any]] = []
        
        template_id = getattr(template, "template_id", "unknown")
        try:
            raw_text, tool_uses, prompt_tokens, completion_tokens, total_tokens = (
                _call_anthropic_with_tools(self._client, kwargs)
            )
            
            # If no tool uses, try to parse JSON from text
            if not tool_uses:
                parsed_data = _extract_json_from_text(raw_text)
                if response_schema:
                    _validate_schema_basic(parsed_data, response_schema)
            
            success = True
            logger.info(
                "[vlm_engine:call_with_tools] FIRST_CALL | template_id=%s tool_uses=%d",
                template_id,
                len(tool_uses),
            )
        except Exception as exc:
            error_message = str(exc)
            logger.error(
                "[vlm_engine:call_with_tools] FIRST_CALL_ERROR | template_id=%s | %s",
                template_id,
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
            if not success:
                self._failed_calls += 1
        
        response = LLMResponse(
            success=success,
            raw_text=raw_text,
            parsed_data=parsed_data,
            model=self.config.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            retry_count=0,
        )
        
        return response, tool_uses
    
    def call_with_tool_results(
        self,
        template: PromptTemplate,
        images: List[Any],
        previous_messages: List[Dict[str, Any]],
        tool_results: List[Dict[str, Any]],
        context_vars: Optional[Dict[str, Any]] = None,
        response_schema: Optional[Dict[str, Any]] = None,
    ) -> LLMResponse:
        """
        Continue conversation with tool results.
        
        Args:
            template: Original prompt template (for system prompt).
            images: Original images (not used in second call, but kept for consistency).
            previous_messages: Full message history from first call.
            tool_results: List of {"tool_use_id": str, "content": str}.
            context_vars: Variables for Jinja2 prompt rendering.
            response_schema: Optional JSON schema for basic validation.
        
        Returns:
            LLMResponse with final analysis.
        """
        if self.provider != "anthropic":
            logger.error(
                "[vlm_engine:call_with_tool_results] ERROR | provider=%s not anthropic",
                self.provider,
            )
            return LLMResponse(
                success=False,
                raw_text="",
                parsed_data={},
                model=self.config.model,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                latency_ms=0,
                retry_count=0,
            )
        
        system_prompt, _ = self.render_prompt(template, context_vars)
        
        # Build messages: previous + tool_result
        messages = list(previous_messages)
        
        # Add tool_result message
        tool_content = []
        for result in tool_results:
            tool_content.append({
                "type": "tool_result",
                "tool_use_id": result["tool_use_id"],
                "content": result["content"],
            })
        messages.append({"role": "user", "content": tool_content})
        
        kwargs = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "messages": messages,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        
        call_id = str(uuid.uuid4())
        start_time = time.perf_counter()
        raw_text = ""
        parsed_data: Dict[str, Any] = {}
        success = False
        prompt_tokens = completion_tokens = total_tokens = 0
        
        template_id = getattr(template, "template_id", "unknown")
        try:
            raw_text, _, prompt_tokens, completion_tokens, total_tokens = (
                _call_anthropic_with_tools(self._client, kwargs)
            )
            parsed_data = _extract_json_from_text(raw_text)
            if response_schema:
                _validate_schema_basic(parsed_data, response_schema)
            success = True
            logger.info(
                "[vlm_engine:call_with_tool_results] SECOND_CALL | template_id=%s",
                template_id,
            )
        except Exception as exc:
            logger.error(
                "[vlm_engine:call_with_tool_results] SECOND_CALL_ERROR | template_id=%s | %s",
                template_id,
                exc,
                exc_info=True,
            )
        
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        
        with self._cache_lock:
            self._total_calls += 1
            self._total_prompt_tokens += prompt_tokens
            self._total_completion_tokens += completion_tokens
            self._total_tokens += total_tokens
            self._total_latency_ms += latency_ms
            if not success:
                self._failed_calls += 1
        
        return LLMResponse(
            success=success,
            raw_text=raw_text,
            parsed_data=parsed_data,
            model=self.config.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            retry_count=0,
        )

    def _execute_once(
        self,
        system_prompt: str,
        user_prompt: str,
        images: List[Any],
    ) -> Tuple[str, int, int, int]:
        """Execute a single provider-specific API call (no retry)."""
        try:
            if self.provider == "anthropic":
                _, kwargs = _build_anthropic_payload(
                    system_prompt,
                    user_prompt,
                    images,
                    self.config.model,
                    self.config.max_tokens,
                    self.config.temperature,
                )
                return _call_anthropic(self._client, kwargs)
            elif self.provider == "google":
                contents, kwargs = _build_google_payload(
                    system_prompt,
                    user_prompt,
                    images,
                    self.config.model,
                    self.config.max_tokens,
                    self.config.temperature,
                )
                return _call_google(self._client.GenerativeModel(self.config.model), contents, kwargs)
            elif self.provider == "aliyun":
                _, kwargs = _build_aliyun_payload(
                    system_prompt,
                    user_prompt,
                    images,
                    self.config.model,
                    self.config.max_tokens,
                    self.config.temperature,
                )
                return _call_aliyun(self._client, kwargs)
            else:
                raise ProviderNotSupportedError(f"Provider {self.provider} not supported")
        except ProviderNotSupportedError:
            raise
        except Exception as exc:
            logger.error(
                "[vlm_engine:_execute_once] PROVIDER_ERROR | provider=%s model=%s | %s",
                self.provider,
                self.config.model,
                exc,
                exc_info=True,
            )
            raise

    def _execute_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        images: List[Any],
    ) -> Tuple[str, int, int, int, int]:
        """Execute the provider-specific API call with manual retry logic.

        Returns:
            Tuple of (raw_text, prompt_tokens, completion_tokens, total_tokens, retry_count).
        """
        # NOTE: This is the sole retry layer for VLM API calls.
        # PipelineStep no longer performs retries — all retry logic lives here.
        last_error: Optional[Exception] = None
        retry_count = 0
        max_retries = max(1, self.config.max_retries)

        for attempt in range(max_retries):
            try:
                result = self._execute_once(system_prompt, user_prompt, images)
                return (*result, retry_count)
            except Exception as exc:
                last_error = exc
                if attempt < max_retries - 1:
                    retry_count += 1
                    wait_sec = min(2 ** attempt, 30)
                    logger.error(
                        "[vlm_engine:_execute_with_retry] RETRY | attempt=%d/%d wait=%.1fs | error=%s",
                        attempt + 1,
                        max_retries,
                        wait_sec,
                        exc,
                        exc_info=True,
                    )
                    time.sleep(wait_sec)
                else:
                    break

        if last_error is not None:
            logger.error(
                "[vlm_engine:_execute_with_retry] MAX_RETRIES_EXCEEDED | attempts=%d last_error=%s",
                retry_count,
                last_error,
                exc_info=True,
            )
            setattr(last_error, "_retry_count", retry_count)
            raise last_error
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
        return {
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
        }

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
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens,
            latency_ms=response.latency_ms,
            success=response.success,
            error_message=None if response.success else response.raw_text or "Unknown error",
        )
