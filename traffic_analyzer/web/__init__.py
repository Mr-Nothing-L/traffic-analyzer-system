"""Web UI backend for the traffic analyzer framework.

FastAPI application exposing workspace browsing, inference job management,
result/evidence reading and editing, and frame extraction.
See ``traffic_analyzer.web.app:create_app``.

[文件说明]
作用:web 子包标识与能力说明,不含路由实现(实现见各子模块)。
上游:traffic_analyzer.web.app 及 web/ 各子模块的相互导入。
下游:无代码级依赖。
"""

from __future__ import annotations
