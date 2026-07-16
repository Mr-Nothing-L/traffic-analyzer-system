"""
Provider-specific payload builders and API callers for VLM inference.

Supports Anthropic, OpenAI, Google GenAI, and Aliyun (OpenAI-compatible).
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
    response = client.messages.create(**kwargs)
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
