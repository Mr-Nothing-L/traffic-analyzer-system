"""Agent proxy package: reverse proxy from the FastAPI web layer to the TS agent runtime.

[文件说明]
作用:/api/agent 代理包。runtime.py 管理 toolserver(Python 视频工具服务,
8601)与 TS agent 服务(8602)两个子进程的生命周期(startup 拉起、shutdown
SIGTERM→SIGKILL、失败降级);routes.py 提供 /api/agent/health|sessions|chat|
approval 代理路由(SSE 透传、workspaceDir 注入、统一 {error:{code,message}}
错误契约)。本模块聚合导出,保持 ``from traffic_analyzer.web import agentproxy``
与 ``agentproxy.router``/``agentproxy.AgentRuntimeManager`` 的用法。
上游:web/app.py(挂载 router,lifespan 调 start/stop)。
下游:web/agentproxy/runtime.py、web/agentproxy/routes.py。
"""

from __future__ import annotations

from traffic_analyzer.web.agentproxy.routes import router
from traffic_analyzer.web.agentproxy.runtime import (
    ENABLE_ENV_VAR,
    AgentRuntimeManager,
    runtime_enabled,
)

__all__ = [
    "ENABLE_ENV_VAR",
    "AgentRuntimeManager",
    "router",
    "runtime_enabled",
]
