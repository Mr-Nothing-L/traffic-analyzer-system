"""LLM provider view/switch API for the web UI.

[文件说明]
作用:LLM provider 查看/切换路由。GET /api/llm/providers 读取 .env(与
ConfigManager._load_dotenv_values 相同的文件定位顺序,api_key 只返回
掩码)给出 provider 列表(索引 0 = 主用)与 LLM_AUTO_SWITCH 状态;
POST /api/llm/providers 按 active_index 重排或按 new_provider 新增/置顶
provider,用 set_key/unset_key 写回 .env(保留无关行,清理多余索引键与
legacy 键),auto_switch=false 时只保留激活项;响应与 GET 同构。配置对新
分析子进程生效(orchestrator 启动时重新读 .env)。
上游:web/app.py(include_router)、前端 LLM 设置界面。
下游:core/config_manager.py(_build_llm_config_from_env 解析逻辑)、
traffic_analyzer/config/.env 或项目根 .env(python-dotenv set_key/unset_key)。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import dotenv_values, set_key, unset_key
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.models.config import LLMProviderConfig

logger = logging.getLogger(__name__)

router = APIRouter()

# Kept in sync with core/vlm_engine.py SUPPORTED_PROVIDERS (importing that
# module would pull in the anthropic/openai SDKs at module import time).
SUPPORTED_PROVIDERS = ("anthropic", "google", "aliyun")

_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Legacy single-provider keys; cleared on write to avoid dual-write ambiguity.
# LLM_AUTO_SWITCH must NOT be cleared.
_LEGACY_KEYS = ("VLM_PROVIDER", "LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL")

_AUTO_SWITCH_OFF = ("0", "false", "no", "off")


class NewProvider(BaseModel):
    provider: str
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None


class SwitchRequest(BaseModel):
    active_index: Optional[int] = None
    new_provider: Optional[NewProvider] = None
    auto_switch: bool = True


def _locate_env() -> tuple[Path, Dict[str, Optional[str]]]:
    """Locate the .env file, mirroring ConfigManager._load_dotenv_values.

    Returns the path that was actually read (or the default config-dir path
    when neither exists yet) plus its parsed values.
    """
    config_env = _CONFIG_DIR / ".env"
    env = dotenv_values(config_env)
    if env:
        return config_env, env
    root_env = _REPO_ROOT / ".env"
    env = dotenv_values(root_env)
    if env:
        return root_env, env
    return config_env, {}


def _parse_providers(env: Dict[str, Optional[str]]) -> List[Any]:
    """Parse the provider list from a .env dict (same index detection as
    ConfigManager._load_env_llm_providers)."""
    mgr = ConfigManager(str(_CONFIG_DIR))
    indices = set()
    for key in env:
        if key.startswith("LLM_PROVIDER_") and key.endswith("_PROVIDER"):
            try:
                indices.add(int(key[len("LLM_PROVIDER_") : -len("_PROVIDER")]))
            except ValueError:
                pass
    if not indices:
        return [mgr._build_llm_config_from_env(env, prefix=None)]
    return [
        mgr._build_llm_config_from_env(env, prefix=f"LLM_PROVIDER_{i}")
        for i in sorted(indices)
    ]


def _auto_switch_enabled(env: Dict[str, Optional[str]]) -> bool:
    return str(env.get("LLM_AUTO_SWITCH") or "").strip().lower() not in _AUTO_SWITCH_OFF


def _mask_key(key: str) -> str:
    if key and len(key) > 4:
        return "****" + key[-4:]
    return "****"


def _state_response(env_path: Path, env: Dict[str, Optional[str]]) -> Dict[str, Any]:
    providers = _parse_providers(env)
    return {
        "providers": [
            {
                "index": i,
                "provider": cfg.provider,
                "model": cfg.model,
                "base_url": cfg.base_url,
                "api_key_masked": _mask_key(cfg.api_key),
                "has_api_key": bool(cfg.api_key),
            }
            for i, cfg in enumerate(providers)
        ],
        "auto_switch": _auto_switch_enabled(env),
        "env_path": str(env_path),
    }


@router.get("/api/llm/providers")
def get_llm_providers() -> Dict[str, Any]:
    """Current LLM provider list (index 0 = active) and auto-switch state."""
    env_path, env = _locate_env()
    return _state_response(env_path, env)


@router.post("/api/llm/providers")
def switch_llm_provider(req: SwitchRequest) -> Dict[str, Any]:
    """Reorder / add the active LLM provider and persist to .env."""
    if (req.active_index is None) == (req.new_provider is None):
        raise HTTPException(
            status_code=400,
            detail="Exactly one of active_index or new_provider must be given",
        )

    env_path, env = _locate_env()
    providers = _parse_providers(env)

    new_key_override: Optional[str] = None
    if req.new_provider is not None:
        name = req.new_provider.provider.strip().lower()
        if name not in SUPPORTED_PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported provider '{name}'; must be one of {SUPPORTED_PROVIDERS}",
            )
        existing_key = (
            req.new_provider.api_key
            or env.get(f"{name.upper()}_API_KEY")
            or env.get("LLM_API_KEY")
        )
        if not existing_key:
            raise HTTPException(
                status_code=400,
                detail=f"No API key available for provider '{name}'",
            )
        active = LLMProviderConfig(provider=name)
        active.api_key = req.new_provider.api_key or ""
        if req.new_provider.model:
            active.model = req.new_provider.model
        active.base_url = req.new_provider.base_url or None
        new_key_override = req.new_provider.api_key
        ordered = [active] + providers
    else:
        idx = req.active_index
        assert idx is not None
        if idx < 0 or idx >= len(providers):
            raise HTTPException(
                status_code=400,
                detail=f"active_index {idx} out of range (0..{len(providers) - 1})",
            )
        ordered = [providers[idx]] + [p for i, p in enumerate(providers) if i != idx]

    if not req.auto_switch:
        ordered = ordered[:1]

    if not ordered[0].api_key and not env.get(f"{ordered[0].provider.upper()}_API_KEY"):
        raise HTTPException(
            status_code=400,
            detail=f"Active provider '{ordered[0].provider}' has no API key",
        )

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.touch(exist_ok=True)

    # Write the new order as indexed keys.
    for i, cfg in enumerate(ordered):
        prefix = f"LLM_PROVIDER_{i}"
        set_key(str(env_path), f"{prefix}_PROVIDER", cfg.provider)
        if cfg.model:
            set_key(str(env_path), f"{prefix}_MODEL", cfg.model)
        else:
            unset_key(str(env_path), f"{prefix}_MODEL")
        if cfg.base_url:
            set_key(str(env_path), f"{prefix}_BASE_URL", cfg.base_url)
        else:
            unset_key(str(env_path), f"{prefix}_BASE_URL")
        # Carry the provider's existing key to its new index; only the
        # new_provider request may supply a fresh key.
        if i == 0 and new_key_override:
            set_key(str(env_path), f"{prefix}_API_KEY", new_key_override)
        elif cfg.api_key:
            set_key(str(env_path), f"{prefix}_API_KEY", cfg.api_key)
        else:
            unset_key(str(env_path), f"{prefix}_API_KEY")

    # Drop stale indexed keys beyond the new list length (all suffixes).
    stale = set()
    for key in env:
        if key.startswith("LLM_PROVIDER_"):
            rest = key[len("LLM_PROVIDER_") :]
            head, _, _ = rest.partition("_")
            if head.isdigit() and int(head) >= len(ordered):
                stale.add(key)
    for key in stale:
        unset_key(str(env_path), key)

    # Clear legacy single-provider keys to avoid dual-write ambiguity.
    for key in _LEGACY_KEYS:
        unset_key(str(env_path), key)

    set_key(str(env_path), "LLM_AUTO_SWITCH", "1" if req.auto_switch else "0")
    logger.info(
        "LLM provider switched: active=%s auto_switch=%s providers=%d env=%s",
        ordered[0].provider,
        req.auto_switch,
        len(ordered),
        env_path,
    )

    return _state_response(env_path, dotenv_values(env_path))
