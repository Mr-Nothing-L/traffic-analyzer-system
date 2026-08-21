"""LLM provider view/switch API for the web UI (action-style endpoints).

[文件说明]
作用:LLM provider 查看/编辑路由。GET /api/llm/providers 读取 .env(与
ConfigManager._load_dotenv_values 相同的文件定位顺序,api_key 只返回中间
打码)给出 provider 行列表(index 0 = 主用,含 enabled)与 LLM_AUTO_SWITCH
状态;POST /api/llm/providers/save(追加或覆盖一行,api_key 未传则沿用)、
/delete(删行并重排)、/active(把某行置为主用)、/settings(只写
LLM_AUTO_SWITCH)四个动作端点操作内存行列表后由 _write_providers 整体写回
.env(set_key/unset_key,清多余 LLM_PROVIDER_{j}_* 与 legacy 五键,保留
LLM_AUTO_SWITCH 与无关行,每行写 LLM_PROVIDER_{i}_ENABLED);响应统一与 GET
同构。配置对新分析子进程生效(orchestrator 启动时重新读 .env)。
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

_FLAG_OFF = ("0", "false", "no", "off")


class SaveRequest(BaseModel):
    index: Optional[int] = None  # null -> append; i -> overwrite row i
    provider: str
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    enabled: bool = True


class IndexRequest(BaseModel):
    index: int


class SettingsRequest(BaseModel):
    auto_switch: bool


def _flag_enabled(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() not in _FLAG_OFF


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


def _read_rows(env: Dict[str, Optional[str]]) -> List[Dict[str, Any]]:
    """Parse provider rows (with enabled flag) from a .env dict.

    Index detection mirrors ConfigManager._load_env_llm_providers; field
    parsing reuses ConfigManager._build_llm_config_from_env so provider-level
    fallbacks (``{PROVIDER}_API_KEY`` etc.) resolve the same way.
    """
    mgr = ConfigManager(str(_CONFIG_DIR))
    indices = set()
    for key in env:
        if key.startswith("LLM_PROVIDER_") and key.endswith("_PROVIDER"):
            try:
                indices.add(int(key[len("LLM_PROVIDER_") : -len("_PROVIDER")]))
            except ValueError:
                pass
    rows: List[Dict[str, Any]] = []
    if not indices:
        # Legacy single-provider form; without any legacy provider key the
        # list is genuinely empty (e.g. all rows deleted) -- do not fabricate
        # a phantom default provider.
        if env.get("VLM_PROVIDER") or env.get("LLM_PROVIDER"):
            cfg = mgr._build_llm_config_from_env(env, prefix=None)
            rows.append(_row_from_config(cfg, enabled=True))
    else:
        for i in sorted(indices):
            cfg = mgr._build_llm_config_from_env(env, prefix=f"LLM_PROVIDER_{i}")
            rows.append(
                _row_from_config(
                    cfg, enabled=_flag_enabled(env.get(f"LLM_PROVIDER_{i}_ENABLED"))
                )
            )
    return rows


def _row_from_config(cfg: Any, enabled: bool) -> Dict[str, Any]:
    return {
        "provider": cfg.provider,
        "model": cfg.model,
        "base_url": cfg.base_url,
        "api_key": cfg.api_key,
        "enabled": enabled,
    }


def _mask_key(key: str) -> str:
    if key and len(key) > 8:
        return key[:4] + "****" + key[-4:]
    return "****"


def _write_providers(
    env_path: Path, rows: List[Dict[str, Any]], auto_switch: bool
) -> None:
    """Rewrite the whole provider list (plus LLM_AUTO_SWITCH) into .env.

    Each row's effective key travels with it to its new index; stale indexed
    keys beyond the new length (all suffixes) and the legacy single-provider
    keys are removed; unrelated lines are preserved.
    """
    old_env = dotenv_values(env_path)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.touch(exist_ok=True)

    for i, row in enumerate(rows):
        prefix = f"LLM_PROVIDER_{i}"
        set_key(str(env_path), f"{prefix}_PROVIDER", row["provider"])
        if row.get("model"):
            set_key(str(env_path), f"{prefix}_MODEL", row["model"])
        else:
            unset_key(str(env_path), f"{prefix}_MODEL")
        if row.get("base_url"):
            set_key(str(env_path), f"{prefix}_BASE_URL", row["base_url"])
        else:
            unset_key(str(env_path), f"{prefix}_BASE_URL")
        if row.get("api_key"):
            set_key(str(env_path), f"{prefix}_API_KEY", row["api_key"])
        else:
            unset_key(str(env_path), f"{prefix}_API_KEY")
        set_key(str(env_path), f"{prefix}_ENABLED", "1" if row["enabled"] else "0")

    # Drop stale indexed keys beyond the new list length (all suffixes).
    for key in old_env:
        if key.startswith("LLM_PROVIDER_"):
            head, _, _ = key[len("LLM_PROVIDER_") :].partition("_")
            if head.isdigit() and int(head) >= len(rows):
                unset_key(str(env_path), key)

    # Clear legacy single-provider keys to avoid dual-write ambiguity.
    for key in _LEGACY_KEYS:
        unset_key(str(env_path), key)

    set_key(str(env_path), "LLM_AUTO_SWITCH", "1" if auto_switch else "0")


def _state_response(env_path: Path, env: Dict[str, Optional[str]]) -> Dict[str, Any]:
    rows = _read_rows(env)
    return {
        "providers": [
            {
                "index": i,
                "provider": row["provider"],
                "model": row["model"],
                "base_url": row["base_url"],
                "api_key_masked": _mask_key(row["api_key"]),
                "has_api_key": bool(row["api_key"]),
                "enabled": row["enabled"],
            }
            for i, row in enumerate(rows)
        ],
        "auto_switch": _flag_enabled(env.get("LLM_AUTO_SWITCH")),
        "env_path": str(env_path),
    }


def _check_index(index: int, rows: List[Dict[str, Any]]) -> None:
    if index < 0 or index >= len(rows):
        raise HTTPException(
            status_code=400,
            detail=f"index {index} out of range (0..{len(rows) - 1})",
        )


@router.get("/api/llm/providers")
def get_llm_providers() -> Dict[str, Any]:
    """Current LLM provider list (index 0 = active) and auto-switch state."""
    env_path, env = _locate_env()
    return _state_response(env_path, env)


@router.post("/api/llm/providers/save")
def save_llm_provider(req: SaveRequest) -> Dict[str, Any]:
    """Append (index=null) or overwrite (index=i) a provider row."""
    name = req.provider.strip().lower()
    if name not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported provider '{name}'; must be one of {SUPPORTED_PROVIDERS}",
        )

    env_path, env = _locate_env()
    rows = _read_rows(env)

    existing_key = ""
    if req.index is not None:
        _check_index(req.index, rows)
        existing_key = rows[req.index]["api_key"]

    # Key resolution: request > existing row > provider-specific/generic env.
    api_key = (
        (req.api_key or "").strip()
        or existing_key
        or env.get(f"{name.upper()}_API_KEY")
        or env.get("LLM_API_KEY")
        or ""
    )
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail=f"No API key available for provider '{name}'",
        )

    row = {
        "provider": name,
        # Overwrite semantics: omitted model/base_url keep the existing row's
        # values (the UI toggles `enabled` without resending them).
        "model": (req.model or "").strip() or (rows[req.index]["model"] if req.index is not None else None),
        "base_url": (req.base_url or "").strip() or (rows[req.index]["base_url"] if req.index is not None else None),
        "api_key": api_key,
        "enabled": req.enabled,
    }
    if req.index is None:
        rows.append(row)
    else:
        rows[req.index] = row

    _write_providers(env_path, rows, _flag_enabled(env.get("LLM_AUTO_SWITCH")))
    logger.info(
        "LLM provider saved: index=%s provider=%s enabled=%s env=%s",
        req.index if req.index is not None else len(rows) - 1,
        name,
        req.enabled,
        env_path,
    )
    return _state_response(env_path, dotenv_values(env_path))


@router.post("/api/llm/providers/delete")
def delete_llm_provider(req: IndexRequest) -> Dict[str, Any]:
    """Delete a provider row; following rows shift up (empty list allowed)."""
    env_path, env = _locate_env()
    rows = _read_rows(env)
    _check_index(req.index, rows)
    removed = rows.pop(req.index)
    _write_providers(env_path, rows, _flag_enabled(env.get("LLM_AUTO_SWITCH")))
    logger.info(
        "LLM provider deleted: index=%d provider=%s remaining=%d env=%s",
        req.index,
        removed["provider"],
        len(rows),
        env_path,
    )
    return _state_response(env_path, dotenv_values(env_path))


@router.post("/api/llm/providers/active")
def activate_llm_provider(req: IndexRequest) -> Dict[str, Any]:
    """Move the given row to index 0 (primary)."""
    env_path, env = _locate_env()
    rows = _read_rows(env)
    _check_index(req.index, rows)
    rows.insert(0, rows.pop(req.index))
    _write_providers(env_path, rows, _flag_enabled(env.get("LLM_AUTO_SWITCH")))
    logger.info(
        "LLM provider activated: index=%d provider=%s env=%s",
        req.index,
        rows[0]["provider"],
        env_path,
    )
    return _state_response(env_path, dotenv_values(env_path))


@router.post("/api/llm/providers/settings")
def save_llm_settings(req: SettingsRequest) -> Dict[str, Any]:
    """Only flip LLM_AUTO_SWITCH; provider rows are untouched."""
    env_path, env = _locate_env()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.touch(exist_ok=True)
    set_key(str(env_path), "LLM_AUTO_SWITCH", "1" if req.auto_switch else "0")
    logger.info("LLM auto_switch=%s env=%s", req.auto_switch, env_path)
    return _state_response(env_path, dotenv_values(env_path))
