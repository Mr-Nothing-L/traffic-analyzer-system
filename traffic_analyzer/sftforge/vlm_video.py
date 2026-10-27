"""SFT Forge 视频直传 VLM 调用层。

[文件说明]
作用:VideoVLMCaller 以 OpenAI 兼容 chat/completions + video_url dataURI
  调用远程 VL 端点(user content = [video_url, text]);复用现有 provider
  配置体系(LLMProviderConfig 列表)、重试/failover 错误分类
  (vlm_error_classifier)与 DiskCache 磁盘缓存(cache key 含视频字节
  SHA-256,跨进程断点续跑可命中)。
  端点实测(probe_sft_forge_video.py):qwen3.8 为思考模型。thinking 档位
  经 chat_template_kwargs 注入:off=enable_thinking False;low/medium/high
  =enable_thinking True + reasoning_effort(high 映射 vLLM 的 xhigh),
  开启时 max_tokens 抬到 >=8192 防隐藏 reasoning 烧穿预算,并捕获响应的
  reasoning_content 供 Web 面板展示(不进训练 label)。
上游:sftforge/engine.py(caller 注入)、scripts/sft_forge.py(CLI 构造)。
下游:core/vlm_error_classifier.py、core/vlm_cache.py(DiskCache)、
  core/config_manager.py(provider 解析)、models/schemas.py(LLMResponse)。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from pathlib import Path
from typing import Callable, List, Optional, Union

import httpx
import openai

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.vlm_cache import DiskCache
from traffic_analyzer.core.vlm_error_classifier import (
    _is_retryable_error,
    is_failover_trigger,
)
from traffic_analyzer.core.vlm_exceptions import (
    AllProvidersExhaustedError,
    ProviderNotSupportedError,
)
from traffic_analyzer.models.schemas import LLMProviderConfig, LLMResponse
from traffic_analyzer.sftforge.models import CallerResult

logger = logging.getLogger(__name__)

_OPENAI_COMPATIBLE_PROVIDERS = ("aliyun", "openai")

# thinking 档位 → vLLM chat_template_kwargs.reasoning_effort
# (档位名对齐 agent/src/llm/provider.ts:服务端支持 low/medium/xhigh)。
_THINKING_MODES = ("off", "low", "medium", "high")
_REASONING_EFFORT = {"low": "low", "medium": "medium", "high": "xhigh"}
# thinking 开启时的 max_tokens 下限:隐藏 reasoning 也消耗预算
# (probe 实测缺省 4096 会被烧穿返回空内容)。
_THINKING_MIN_MAX_TOKENS = 8192


def load_providers(
    env_file: Optional[Union[str, Path]] = None,
    config_dir: Union[str, Path] = "traffic_analyzer/config",
) -> List[LLMProviderConfig]:
    """加载 provider 配置列表(优先级顺序)。

    env_file 给出时从该 .env 所在配置目录解析(文件名必须为 .env,以便复用
    ConfigManager 的 provider 解析,避免私钥/密钥进入 os.environ);否则从
    config_dir 加载。worktree 运行时用 env_file 指向主仓
    traffic_analyzer/config/.env。
    """
    if env_file is not None:
        env_path = Path(env_file)
        if env_path.name != ".env":
            raise ValueError(
                f"env_file 必须指向名为 .env 的文件,收到: {env_path}"
            )
        manager = ConfigManager(config_dir=str(env_path.parent))
    else:
        manager = ConfigManager(config_dir=str(config_dir))
    manager.load_all()
    providers = manager.get_llm_providers()
    if not providers:
        raise RuntimeError("没有可用的 LLM provider 配置")
    return providers


class VideoVLMCaller:
    """视频直传 chat caller;签名与 engine 注入协议一致:

    ``caller(system_prompt, user_prompt, video_path) -> CallerResult``
    """

    def __init__(
        self,
        providers: List[LLMProviderConfig],
        disk_cache_path: Optional[str] = None,
        thinking: str = "medium",
    ) -> None:
        if not providers:
            raise ProviderNotSupportedError("至少需要一个 provider 配置")
        thinking = thinking.lower().strip()
        if thinking not in _THINKING_MODES:
            raise ValueError(
                f"thinking 必须为 {_THINKING_MODES} 之一,收到: {thinking}"
            )
        self._thinking = thinking
        self._providers: List[LLMProviderConfig] = []
        self._clients: List[openai.OpenAI] = []
        for cfg in providers:
            normalized = cfg.provider.lower().strip()
            if normalized not in _OPENAI_COMPATIBLE_PROVIDERS:
                raise ProviderNotSupportedError(
                    f"视频直传仅支持 OpenAI 兼容端点,provider={normalized} 不支持"
                )
            self._providers.append(cfg.model_copy(update={"provider": normalized}))
            self._clients.append(
                openai.OpenAI(
                    api_key=cfg.api_key,
                    base_url=cfg.base_url,
                    http_client=httpx.Client(
                        proxy=None, trust_env=False, timeout=cfg.timeout
                    ),
                )
            )

        self._disk_cache: Optional[DiskCache] = None
        cache_path = disk_cache_path or self._providers[0].disk_cache_path
        if cache_path:
            try:
                self._disk_cache = DiskCache(
                    db_path=cache_path,
                    max_entries=self._providers[0].disk_cache_max_entries,
                )
                logger.info("[VideoVLMCaller] Disk cache enabled: %s", cache_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[VideoVLMCaller] Disk cache init failed: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def __call__(
        self,
        system_prompt: str,
        user_prompt: str,
        video_path: Union[str, Path],
        on_delta: Optional[Callable[[str, str], None]] = None,
    ) -> CallerResult:
        """对视频发一次 chat 请求,返回 CallerResult(带缓存与重试/failover)。

        on_delta(field, text):给出时走 stream=True 流式调用,逐 chunk 回调
        reasoning/response 增量(field ∈ reasoning/response);重试/failover
        重新开始前回调 field=reset(前端应清空该轮流式缓冲)。
        """
        video_bytes = Path(video_path).read_bytes()
        video_sha = hashlib.sha256(video_bytes).hexdigest()
        b64 = base64.b64encode(video_bytes).decode()
        data_uri = f"data:video/mp4;base64,{b64}"

        # cache key 含视频字节 SHA-256 + thinking 档位:同视频同 prompt 同档位
        # 跨进程命中;视频内容变化或档位切换自动失效。
        cache_key = hashlib.sha256(
            f"sftforge-video:{video_sha}\nthinking:{self._thinking}\nsys:{system_prompt}\nuser:{user_prompt}".encode()
        ).hexdigest()
        if self._disk_cache is not None:
            for cfg in self._providers:
                cached = self._disk_cache.get(cache_key, cfg.provider, cfg.model)
                if cached is not None:
                    logger.debug("[VideoVLMCaller] DISK HIT key=%s...", cache_key[:16])
                    return CallerResult(
                        text=cached.raw_text,
                        reasoning=str(cached.parsed_data.get("reasoning", "")),
                    )

        (text, reasoning), served_idx = self._execute_with_failover(
            system_prompt, user_prompt, data_uri, on_delta
        )
        if self._disk_cache is not None:
            cfg = self._providers[served_idx]
            self._disk_cache.set(
                cache_key,
                cfg.provider,
                cfg.model,
                LLMResponse(
                    success=True,
                    raw_text=text,
                    parsed_data={"reasoning": reasoning},
                    model=cfg.model,
                    provider=cfg.provider,
                ),
            )
        return CallerResult(text=text, reasoning=reasoning)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _execute_once(
        self,
        provider_idx: int,
        system_prompt: str,
        user_prompt: str,
        data_uri: str,
        on_delta: Optional[Callable[[str, str], None]] = None,
    ) -> tuple:
        """单次调用,返回 (text, reasoning);reasoning 为隐藏通道文本(可空)。

        on_delta 给出时走 stream=True 流式:逐 chunk 回调 reasoning/response
        增量,聚合后返回;空响应重试前回调 reset。
        """
        cfg = self._providers[provider_idx]
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "video_url", "video_url": {"url": data_uri}},
                    {"type": "text", "text": user_prompt},
                ],
            }
        )
        if self._thinking == "off":
            template_kwargs: dict = {"enable_thinking": False}
            max_tokens = cfg.max_tokens
        else:
            template_kwargs = {
                "enable_thinking": True,
                "reasoning_effort": _REASONING_EFFORT[self._thinking],
            }
            # 隐藏 reasoning 同吃 max_tokens,抬下限防烧穿(见模块 docstring)
            max_tokens = max(cfg.max_tokens, _THINKING_MIN_MAX_TOKENS)
        create_kwargs = dict(
            model=cfg.model,
            max_tokens=max_tokens,
            temperature=cfg.temperature,
            messages=messages,
            extra_body={"chat_template_kwargs": template_kwargs},
        )
        # 空响应多为端点一次性抖动(隐藏 reasoning 耗尽 token 等),裸 RuntimeError
        # 不可重试会直接进 quarantine,故在调用内做 1 次有界即时重试。
        for empty_attempt in range(2):
            if empty_attempt > 0 and on_delta is not None:
                on_delta("reset", "")
            if on_delta is not None:
                text, reasoning = self._stream_once(
                    provider_idx, dict(create_kwargs, stream=True), on_delta
                )
            else:
                response = self._clients[provider_idx].chat.completions.create(**create_kwargs)
                text, reasoning = "", ""
                if response.choices:
                    message = response.choices[0].message
                    text = (message.content or "").strip()
                    # vLLM qwen3 思考模型经 reasoning_content 返回隐藏通道文本
                    reasoning = (getattr(message, "reasoning_content", None) or "").strip()
            if text:
                return text, reasoning
            logger.warning(
                "[VideoVLMCaller] EMPTY_RESPONSE | provider=%s model=%s attempt=%d/2",
                cfg.provider, cfg.model, empty_attempt + 1,
            )
        raise RuntimeError(
            f"empty response content (provider={cfg.provider} model={cfg.model})"
        )

    def _stream_once(
        self,
        provider_idx: int,
        create_kwargs: dict,
        on_delta: Callable[[str, str], None],
    ) -> tuple:
        """流式调用:逐 chunk 回调增量并聚合 (text, reasoning)。"""
        stream = self._clients[provider_idx].chat.completions.create(**create_kwargs)
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            reasoning_delta = (getattr(delta, "reasoning_content", None) or "")
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)
                on_delta("reasoning", reasoning_delta)
            content_delta = (getattr(delta, "content", None) or "")
            if content_delta:
                text_parts.append(content_delta)
                on_delta("response", content_delta)
        return "".join(text_parts).strip(), "".join(reasoning_parts).strip()

    def _execute_with_failover(
        self,
        system_prompt: str,
        user_prompt: str,
        data_uri: str,
        on_delta: Optional[Callable[[str, str], None]] = None,
    ) -> tuple:
        """逐 provider 重试 + failover;语义对齐 VLMInferenceEngine._execute_with_retry。

        全停型错误(failover-trigger:401/配额/权限/5xx)在最后一个 provider 上
        升级为 AllProvidersExhaustedError 终止批处理——若按普通错误上抛,整批
        会被静默 quarantine 且断点续跑永久跳过这些视频。failover 切换 provider
        前回调 reset(前一 provider 的流式部分输出作废)。
        """
        last_error: Optional[Exception] = None
        num_providers = len(self._providers)

        for provider_idx in range(num_providers):
            cfg = self._providers[provider_idx]
            max_retries = max(1, cfg.max_retries)
            last_error = None

            for attempt in range(max_retries):
                try:
                    return self._execute_once(
                        provider_idx, system_prompt, user_prompt, data_uri, on_delta
                    ), provider_idx
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if not _is_retryable_error(exc):
                        if is_failover_trigger(exc):
                            if provider_idx < num_providers - 1:
                                logger.error(
                                    "[VideoVLMCaller] FAILOVER | from=%s to=%s reason=%s",
                                    cfg.provider,
                                    self._providers[provider_idx + 1].provider,
                                    exc,
                                )
                                if on_delta is not None:
                                    on_delta("reset", "")
                                break
                            # 全停型错误发生在最后一个 provider:升级终止批处理
                            # (对齐 vlm_engine.py:698-707)。
                            raise AllProvidersExhaustedError(
                                f"All providers exhausted. Last error: {exc}"
                            ) from exc
                        raise
                    if attempt < max_retries - 1:
                        wait_sec = min(2 ** attempt, 30)
                        logger.error(
                            "[VideoVLMCaller] RETRY | provider=%s attempt=%d/%d wait=%.1fs | %s",
                            cfg.provider,
                            attempt + 1,
                            max_retries,
                            wait_sec,
                            exc,
                        )
                        if on_delta is not None:
                            on_delta("reset", "")
                        time.sleep(wait_sec)

            if last_error is not None:
                if is_failover_trigger(last_error) and provider_idx < num_providers - 1:
                    logger.error(
                        "[VideoVLMCaller] FAILOVER | from=%s to=%s reason=%s",
                        cfg.provider,
                        self._providers[provider_idx + 1].provider,
                        last_error,
                    )
                    if on_delta is not None:
                        on_delta("reset", "")
                    continue
                if provider_idx == num_providers - 1:
                    raise AllProvidersExhaustedError(
                        f"All providers exhausted. Last error: {last_error}"
                    ) from last_error
                raise last_error

        # 控制流不可达:末位 provider 迭代必然 return 或 raise,
        # 非末位 continue 后必进入下一迭代。
        raise AssertionError("unreachable: failover loop exhausted without result")
