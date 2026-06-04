"""
工具注册表集成

将项目中已有的工具注册到 ToolRouter。
提供默认的 router 实例供上层使用。
"""

from __future__ import annotations

import logging
from typing import Optional

from .tool_router import ToolRouter

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 默认 Router 实例 (懒加载)
# ──────────────────────────────────────────────

_default_router: Optional[ToolRouter] = None


def get_default_router() -> ToolRouter:
    """
    获取默认的 ToolRouter 实例 (单例)。

    首次调用时会自动注册所有内置工具。
    """
    global _default_router
    if _default_router is None:
        _default_router = create_router()
        logger.info(f"默认 ToolRouter 创建完成，注册工具: {_default_router.list_tools()}")
    return _default_router


def create_router() -> ToolRouter:
    """
    创建新的 ToolRouter 并注册所有工具。

    返回全新的实例 (非单例)。
    """
    router = ToolRouter()

    # TODO: 注册工具
    # _register_scene_understanding_tool(router)
    # _register_video_preprocessing_tool(router)

    return router


# ──────────────────────────────────────────────
# 快捷函数
# ──────────────────────────────────────────────

def execute_tool(request_json: str) -> str:
    """
    快捷函数: 解析 JSON 请求，执行工具，返回 JSON 结果。

    Args:
        request_json: 工具调用请求的 JSON 字符串

    Returns:
        ToolResponse 的 JSON 字符串
    """
    router = get_default_router()
    response = router.route(request_json)
    return response.to_json()
