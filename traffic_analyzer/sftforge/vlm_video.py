"""SFT Forge 视频直传 VLM 调用层。

[文件说明]
作用:VideoVLMCaller 以 OpenAI 兼容 chat/completions + video_url dataURI
  调用远程 VL 端点(user content = [video_url, text]);复用现有 provider
  配置体系(LLMProviderConfig 列表)、重试/failover 错误分类
  (vlm_error_classifier)与 DiskCache 磁盘缓存(cache key 含视频字节
  SHA-256,跨进程断点续跑可命中)。
  端点实测(probe_sft_forge_video.py):qwen3.8 思考模型需
  enable_thinking=False,否则隐藏 reasoning 会烧穿 max_tokens 返回空内容。
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
from typing import List, Optional, Union

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

logger = logging.getLogger(__name__)

_OPENAI_COMPATIBLE_PROVIDERS = ("aliyun", "openai")


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

    ``caller(system_prompt, user_prompt, video_path) -> str``
    """

    def __init__(
        self,
        providers: List[LLMProviderConfig],
        disk_cache_path: Optional[str] = None,
    ) -> None:
        if not providers:
            raise ProviderNotSupportedError("至少需要一个 provider 配置")
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

    def __call__(self, system_prompt: str, user_prompt: str, video_path: Union[str, Path]) -> str:
        """对视频发一次 chat 请求,返回响应文本(带缓存与重试/failover)。"""
        video_bytes = Path(video_path).read_bytes()
        video_sha = hashlib.sha256(video_bytes).hexdigest()
        b64 = base64.b64encode(video_bytes).decode()
        data_uri = f"data:video/mp4;base64,{b64}"

        # cache key 含视频字节 SHA-256:同视频同 prompt 跨进程命中,
        # 视频内容变化(重编码/替换)自动失效。
        cache_key = hashlib.sha256(
            f"sftforge-video:{video_sha}\nsys:{system_prompt}\nuser:{user_prompt}".encode()
        ).hexdigest()
        if self._disk_cache is not None:
            for cfg in self._providers:
                cached = self._disk_cache.get(cache_key, cfg.provider, cfg.model)
                if cached is not None:
                    logger.debug("[VideoVLMCaller] DISK HIT key=%s...", cache_key[:16])
                    return cached.raw_text

        text, served_idx = self._execute_with_failover(
            system_prompt, user_prompt, data_uri
        )
        if self._disk_cache is not None:
            cfg = self._providers[served_idx]
            self._disk_cache.set(
                cache_key,
                cfg.provider,
                cfg.model,
                LLMResponse(success=True, raw_text=text, model=cfg.model, provider=cfg.provider),
            )
        return text

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _execute_once(
        self, provider_idx: int, system_prompt: str, user_prompt: str, data_uri: str
    ) -> str:
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
        create_kwargs = dict(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            temperature=cfg.temperature,
            messages=messages,
            # qwen3 思考模型:关闭隐藏 reasoning,防止烧穿 max_tokens 返回空内容
            # (probe 实测,见模块 docstring)。
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        # 空响应多为端点一次性抖动(隐藏 reasoning 耗尽 token 等),裸 RuntimeError
        # 不可重试会直接进 quarantine,故在调用内做 1 次有界即时重试。
        for empty_attempt in range(2):
            response = self._clients[provider_idx].chat.completions.create(**create_kwargs)
            text = ""
            if response.choices:
                text = (response.choices[0].message.content or "").strip()
            if text:
                return text
            logger.warning(
                "[VideoVLMCaller] EMPTY_RESPONSE | provider=%s model=%s attempt=%d/2",
                cfg.provider, cfg.model, empty_attempt + 1,
            )
        raise RuntimeError(
            f"empty response content (provider={cfg.provider} model={cfg.model})"
        )

    def _execute_with_failover(
        self, system_prompt: str, user_prompt: str, data_uri: str
    ) -> tuple:
        """逐 provider 重试 + failover;语义对齐 VLMInferenceEngine._execute_with_retry。

        全停型错误(failover-trigger:401/配额/权限/5xx)在最后一个 provider 上
        升级为 AllProvidersExhaustedError 终止批处理——若按普通错误上抛,整批
        会被静默 quarantine 且断点续跑永久跳过这些视频。
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
                        provider_idx, system_prompt, user_prompt, data_uri
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
                        time.sleep(wait_sec)

            if last_error is not None:
                if is_failover_trigger(last_error) and provider_idx < num_providers - 1:
                    logger.error(
                        "[VideoVLMCaller] FAILOVER | from=%s to=%s reason=%s",
                        cfg.provider,
                        self._providers[provider_idx + 1].provider,
                        last_error,
                    )
                    continue
                if provider_idx == num_providers - 1:
                    raise AllProvidersExhaustedError(
                        f"All providers exhausted. Last error: {last_error}"
                    ) from last_error
                raise last_error

        # 控制流不可达:末位 provider 迭代必然 return 或 raise,
        # 非末位 continue 后必进入下一迭代。
        raise AssertionError("unreachable: failover loop exhausted without result")
