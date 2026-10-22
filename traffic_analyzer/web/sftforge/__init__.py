"""SFT Forge web API 包(多轮纠错 SFT label 生成的可视化后端)。

[文件说明]
作用:web 层 SFT Forge 包。service.py 为 ForgeJobManager(后台线程执行
  引擎、事件存储/回放式订阅)与 build_engine 真实引擎装配点;routes.py
  为 /api/sftforge/jobs 三个端点;本模块聚合导出,保持
  ``from traffic_analyzer.web import sftforge`` 与 ``sftforge.router``/
  ``sftforge.ForgeJobManager``/``sftforge.build_engine`` 的用法
  (build_engine 经包命名空间导出,测试 monkeypatch
  traffic_analyzer.web.sftforge.build_engine 即生效)。
上游:web/app.py(挂载 router,app.state.sftforge = ForgeJobManager)。
下游:web/sftforge/service.py、web/sftforge/routes.py。
"""

from __future__ import annotations

from traffic_analyzer.web.sftforge.routes import router
from traffic_analyzer.web.sftforge.service import (
    ForgeJob,
    ForgeJobManager,
    build_engine,
)


__all__ = [
    "ForgeJob",
    "ForgeJobManager",
    "build_engine",
    "router",
]
