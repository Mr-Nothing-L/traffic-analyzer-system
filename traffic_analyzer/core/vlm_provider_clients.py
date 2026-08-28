"""
Provider-specific payload builders and API callers for VLM inference.

Supports Anthropic, OpenAI, Google GenAI, and Aliyun (OpenAI-compatible).

[文件说明]
作用:各 VLM provider 的请求构造与调用封装。_encode_image_to_base64 将图像
  统一编码为 base64 data URI;_build_*_payload 按 Anthropic / OpenAI /
  Google GenAI / 阿里云(OpenAI 兼容)各自的格式组装消息与参数(支持文本
  标签与图像交错);_call_* 系列发起请求并统一返回
  (text, prompt_tokens, completion_tokens, total_tokens),
  其中 _call_anthropic_with_tools 额外返回 tool_use 块。
上游:core/vlm_engine.py(_execute_once 调用本模块的
  payload 构造与 caller 函数)。
下游:各 provider 的 HTTP API 端点(经 anthropic / openai / google.generativeai
  SDK 访问,API key 与 base_url 来自环境变量配置);PIL(图像处理)。
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any, Dict, List, Optional, Tuple

import anthropic
import openai

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Image encoding helper
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Provider-specific payload builders
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
    enable_thinking: Optional[bool] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build Aliyun (OpenAI-compatible) message list and kwargs.

    enable_thinking 三态:None=不传(服务端默认);True/False 经 extra_body
    注入 chat_template_kwargs.enable_thinking(vLLM 等 OpenAI 兼容后端的
    非标准字段,OpenAI SDK 未收录,必须走 extra_body 才能落到请求 body)。
    """
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
    if enable_thinking is not None:
        kwargs["extra_body"] = {
            "chat_template_kwargs": {"enable_thinking": enable_thinking}
        }
    return messages, kwargs


# ---------------------------------------------------------------------------
# Provider-specific callers
# ---------------------------------------------------------------------------

def _try_anthropic_create(
    client: Any,
    kwargs: Dict[str, Any],
) -> Any:
    """Create an Anthropic message, disabling thinking when supported.

    Some Anthropic-compatible endpoints (and Claude 4.x models) default to
    adaptive/extended thinking, which can consume the entire output token
    budget and leave no parseable text. We therefore attempt to disable
    thinking; if the endpoint rejects that parameter, we fall back to the
    original request without it.
    """
    bad_request_cls = getattr(anthropic, "BadRequestError", Exception)
    kwargs_disabled = {**kwargs, "thinking": {"type": "disabled"}}
    try:
        return client.messages.create(**kwargs_disabled)
    except bad_request_cls as exc:
        error_text = str(exc).lower()
        if "thinking" in error_text:
            logger.warning(
                "Anthropic rejected thinking=disabled (%s); retrying without thinking parameter",
                exc,
            )
            return client.messages.create(**kwargs)
        raise


def _call_anthropic(
    client: Any,
    kwargs: Dict[str, Any],
) -> Tuple[str, int, int, int]:
    """Call Anthropic and return (text, prompt_tokens, completion_tokens, total_tokens)."""
    response = _try_anthropic_create(client, kwargs)
    text_parts: List[str] = []
    thinking_parts: List[str] = []

    if response.content:
        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(block.text)
            elif block_type == "thinking":
                thinking_parts.append(getattr(block, "thinking", ""))

    if text_parts:
        text = "".join(text_parts)
    elif thinking_parts:
        text = (
            "[THINKING_ONLY_RESPONSE]\n"
            + "\n".join(thinking_parts)
            + "\n[END_THINKING_ONLY_RESPONSE]"
        )
        logger.warning(
            "Anthropic response contained only thinking blocks (no text); "
            "stop_reason=%s output_tokens=%s",
            getattr(response, "stop_reason", None),
            getattr(response.usage, "output_tokens", None) if response.usage else None,
        )
    else:
        text = ""

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
    response = _try_anthropic_create(client, kwargs)
    text_parts: List[str] = []
    thinking_parts: List[str] = []
    tool_uses: List[Dict[str, Any]] = []

    if response.content:
        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(block.text)
            elif block_type == "thinking":
                thinking_parts.append(getattr(block, "thinking", ""))
            elif block_type == "tool_use":
                tool_uses.append({
                    "name": getattr(block, "name", ""),
                    "id": getattr(block, "id", ""),
                    "input": getattr(block, "input", {}),
                })

    if text_parts:
        text = "".join(text_parts)
    elif thinking_parts:
        text = (
            "[THINKING_ONLY_RESPONSE]\n"
            + "\n".join(thinking_parts)
            + "\n[END_THINKING_ONLY_RESPONSE]"
        )
        logger.warning(
            "Anthropic response contained only thinking blocks (no text); "
            "stop_reason=%s output_tokens=%s",
            getattr(response, "stop_reason", None),
            getattr(response.usage, "output_tokens", None) if response.usage else None,
        )
    else:
        text = ""

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
    timeout: Optional[float] = None,
) -> Tuple[str, int, int, int]:
    """Call Google GenAI and return (text, prompt_tokens, completion_tokens, total_tokens)."""
    # Align per-request timeout with the other providers (config.timeout)
    request_options = {"timeout": timeout} if timeout is not None else None
    response = client_model.generate_content(
        contents,
        generation_config=generation_config,
        request_options=request_options,
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
