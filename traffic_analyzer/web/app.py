"""FastAPI application factory for the traffic analyzer web UI.

``create_app()`` is referenced by the ``web`` CLI subcommand via uvicorn's
factory mode; a preset workspace is passed through the
``TRAFFIC_ANALYZER_WEB_WORKSPACE`` environment variable (factory mode cannot
forward arguments).

[文件说明]
作用:FastAPI 应用工厂 create_app():装配 workspace/fs/jobs/evidence_api/frames/
video_stream/dashboard/auth/presence/realtime 各路由,提供 /api/expert-phases
专家阶段定义接口,挂载 web/static(/,legacy SPA)与 frontend/dist(/v2,新
前端构建产物,未构建时跳过)并为其禁用缓存,在 no-cache 之后注册
auth middleware(未配置 TRAFFIC_ANALYZER_USERS 时认证完全关闭),注册
lifespan/atexit 钩子以在服务退出时停止所有排队/运行中的分析子进程;lifespan
启动时为 realtime EventBus 绑定事件循环(跨线程 publish 经
loop.call_soon_threadsafe 投递);通过 TRAFFIC_ANALYZER_WEB_WORKSPACE
环境变量接收预设工作区(工厂模式无法转发参数)。
上游:traffic_analyzer/cli.py 的 web 子命令(uvicorn "traffic_analyzer.web.app:create_app")。
下游:web/ 下 workspace、fs、jobs、evidence_api、frames、video_stream、dashboard、
auth、presence、realtime 路由模块;web/static 前端静态文件。
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from traffic_analyzer.web import (
    auth,
    dashboard,
    evidence_api,
    frames,
    fs,
    jobs,
    presence,
    realtime,
    video_stream,
    workspace as workspace_mod,
)

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_REPO_ROOT = Path(__file__).resolve().parents[2]


class _SpaStaticFiles(StaticFiles):
    """StaticFiles + SPA 回退:/v2 下的未知路径(客户端路由深链,如
    /v2/dashboard)回退服务 index.html,真实文件(assets/字体)正常返回。"""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise

# Expert phase definitions served by GET /api/expert-phases (frontend uses
# them to cap the progress climb); shipped next to this module.
_EXPERT_PHASES_JSON = Path(__file__).resolve().parent / "expert_phases.json"
WORKSPACE_ENV_VAR = "TRAFFIC_ANALYZER_WEB_WORKSPACE"


def create_app(workspace: Optional[str] = None) -> FastAPI:
    bus = realtime.EventBus()
    job_manager = jobs.JobManager(bus=bus)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # 跨线程 publish(jobs worker 线程、线程池里的同步端点)经
        # loop.call_soon_threadsafe 投递,需先绑定运行中的事件循环。
        bus.bind_loop(asyncio.get_running_loop())
        yield
        bus.unbind_loop()
        # Uvicorn runs this on SIGINT/SIGTERM (Ctrl+C): stop every queued or
        # running analyze child so no orphan keeps writing analysis/<stem>/.
        job_manager.shutdown()

    app = FastAPI(title="Traffic Analyzer Web UI", lifespan=_lifespan)
    app.state.workspace = workspace_mod.WorkspaceState()
    app.state.jobs = job_manager
    app.state.realtime = bus
    # 认证配置(未配置 TRAFFIC_ANALYZER_USERS 时完全关闭)与在线状态名册。
    app.state.auth = auth.configure()
    app.state.presence = presence.PresenceStore()
    # Fallback for exit paths that skip the lifespan (best-effort; SIGKILL
    # cannot be covered).
    atexit.register(job_manager.shutdown)

    preset = workspace or os.environ.get(WORKSPACE_ENV_VAR)
    if preset:
        path = Path(preset).expanduser().resolve()
        if path.is_dir():
            app.state.workspace.set(path)
        else:
            logger.warning("Preset workspace is not a directory, ignored: %s", preset)

    app.include_router(workspace_mod.router)
    app.include_router(fs.router)
    app.include_router(jobs.router)
    app.include_router(evidence_api.router)
    app.include_router(frames.router)
    app.include_router(video_stream.router)
    app.include_router(dashboard.router)
    app.include_router(auth.router)
    app.include_router(presence.router)
    app.include_router(realtime.router)

    @app.get("/login", include_in_schema=False)
    def login_page() -> Any:
        """登录页(静态文件直出;auth middleware 对此路径豁免)。"""
        return FileResponse(_STATIC_DIR / "login.html")

    @app.get("/api/expert-phases")
    def get_expert_phases() -> Any:
        """Expert phase definitions (JSON file shipped beside this module)."""
        if not _EXPERT_PHASES_JSON.is_file():
            raise HTTPException(status_code=404, detail="expert_phases.json not found")
        try:
            return json.loads(_EXPERT_PHASES_JSON.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=500, detail=f"expert_phases.json unreadable: {exc}"
            )

    @app.middleware("http")
    async def _no_cache_static(request, call_next):
        # The SPA must always revalidate: a stale cached js/main.js/index.html
        # breaks the UI after upgrades (heuristic caching otherwise applies).
        response = await call_next(request)
        # Cover the SPA shell AND all ES modules under /js/ — a stale cached
        # module (e.g. sft.js) silently breaks the UI after upgrades.
        if (
            request.url.path in ("/", "/index.html")
            or request.url.path.startswith("/js/")
            or request.url.path.startswith("/css/")
            or request.url.path.startswith("/fonts/")
            or request.url.path == "/v2"
            or request.url.path.startswith("/v2/")
        ):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # 认证 middleware 在 no-cache 之后注册(后注册者更靠外,先执行):未认证
    # 请求直接 302/401,不走 no-cache 处理;认证关闭时 middleware 只补
    # request.state.user='local'。
    auth.install(app)

    # 绞杀者迁移:v2 前端(Vue 3,frontend/dist)挂 /v2,legacy SPA 继续占 /。
    # /v2 必须先于 / 注册(Starlette 挂载按前缀匹配,/ 会吞掉 /v2)。
    # dev 期未构建(frontend/dist 不存在)时跳过,不炸。
    v2_dist = _REPO_ROOT / "frontend" / "dist"
    if (v2_dist / "index.html").is_file():
        app.mount(
            "/v2",
            _SpaStaticFiles(directory=str(v2_dist), html=True),
            name="static-v2",
        )
    else:
        logger.info("v2 frontend not built, /v2 not mounted: %s", v2_dist)

    # Static frontend (developed in parallel) — must not crash when missing.
    try:
        app.mount(
            "/",
            StaticFiles(directory=str(_STATIC_DIR), html=True, check_dir=False),
            name="static",
        )
    except Exception:
        logger.warning("Static directory unavailable, frontend not served: %s", _STATIC_DIR)

    return app
