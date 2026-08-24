"""Quick-chat backend package (per-IP conversational video/image QA).

[文件说明]
作用:快速对话后端包。routes 提供 /api/chat/* 路由(state/upload/source/ask/
history/files);qa 为问答编排(意图分类、上下文组装、流式 failover、画框);
store 为 SQLite 按 IP 存储(config/chat.db);tokens 为上下文长度启发式估计;
janitor 为上传目录定时清理;paths 为共享路径常量。本模块聚合导出 router 与
run_janitor,供 web/app.py 挂载。
上游:web/app.py(include_router + lifespan 后台任务)。
下游:web/chat/ 各子模块。
"""

from __future__ import annotations

from traffic_analyzer.web.chat.janitor import run_janitor
from traffic_analyzer.web.chat.routes import router

__all__ = ["router", "run_janitor"]
