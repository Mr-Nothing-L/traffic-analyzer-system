"""FastAPI application factory for the traffic analyzer web UI.

``create_app()`` is referenced by the ``web`` CLI subcommand via uvicorn's
factory mode; a preset workspace is passed through the
``TRAFFIC_ANALYZER_WEB_WORKSPACE`` environment variable (factory mode cannot
forward arguments).

[文件说明]
作用:FastAPI 应用工厂 create_app():装配 workspace/fs/jobs/evidence_api/frames/
video_stream/dashboard/auth/presence/realtime/llm_settings/chat 各路由,提供 /api/expert-phases
专家阶段定义接口,挂载 frontend/dist(/,Vue 3 SPA 构建产物,未构建时跳过)
并为其禁用缓存,/v2/* 旧书签 301 重定向到对应 / 路径,在 no-cache 之后注册
auth middleware(未配置 TRAFFIC_ANALYZER_USERS 时认证完全关闭),注册
lifespan/atexit 钩子以在服务退出时停止所有排队/运行中的分析子进程;lifespan
启动时为 realtime EventBus 绑定事件循环(跨线程 publish 经
loop.call_soon_threadsafe 投递),并创建 chat 上传目录清理后台任务(随退出
cancel);通过 TRAFFIC_ANALYZER_WEB_WORKSPACE
环境变量接收预设工作区(工厂模式无法转发参数)。
上游:traffic_analyzer/cli.py 的 web 子命令(uvicorn "traffic_analyzer.web.app:create_app")。
下游:web/ 下 workspace、fs、jobs、evidence_api、frames、video_stream、dashboard、
auth、presence、realtime、llm_settings、chat 路由模块;frontend/dist 前端构建产物。
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
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from traffic_analyzer.web import (
    auth,
    chat,
    dashboard,
    evidence_api,
    frames,
    fs,
    jobs,
    llm_settings,
    presence,
    realtime,
    video_stream,
    workspace as workspace_mod,
)
from traffic_analyzer.web.workspace import quick_dirs as workspace_quick_dirs

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _SpaStaticFiles(StaticFiles):
    """StaticFiles + SPA 回退:未知路径(客户端路由深链,如 /dashboard、
    /video/<stem>)回退服务 index.html,真实文件(assets/字体)正常返回。"""

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
        # 快速对话上传目录清理:启动时先立即扫一次,之后每小时一次。
        janitor_task = asyncio.create_task(chat.run_janitor())
        yield
        janitor_task.cancel()
        try:
            await janitor_task
        except asyncio.CancelledError:
            pass
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
    app.include_router(workspace_quick_dirs.router)
    app.include_router(fs.router)
    app.include_router(jobs.router)
    app.include_router(evidence_api.router)
    app.include_router(frames.router)
    app.include_router(video_stream.router)
    app.include_router(dashboard.router)
    app.include_router(llm_settings.router)
    app.include_router(chat.router)
    app.include_router(auth.router)
    app.include_router(presence.router)
    app.include_router(realtime.router)

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
        # The SPA must always revalidate: a stale cached index.html or hashed
        # asset breaks the UI after upgrades (heuristic caching otherwise
        # applies); cover the shell plus /assets/ 与 /fonts/。
        response = await call_next(request)
        if (
            request.url.path in ("/", "/index.html")
            or request.url.path.startswith("/assets/")
            or request.url.path.startswith("/fonts/")
        ):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # 认证 middleware 在 no-cache 之后注册(后注册者更靠外,先执行):未认证
    # 请求直接 302/401,不走 no-cache 处理;认证关闭时 middleware 只补
    # request.state.user='local'。
    auth.install(app)

    # /v2 旧书签 301 重定向到对应 / 路径(必须在 / 挂载之前注册)。
    @app.get("/v2", include_in_schema=False)
    @app.get("/v2/{path:path}", include_in_schema=False)
    def v2_redirect(path: str = "") -> Any:
        target = "/" + path if path else "/"
        return RedirectResponse(target, status_code=301)

    # 新前端(Vue 3,frontend/dist)挂 /,放在所有 /api 路由之后:/ 挂载会
    # 吞掉一切未匹配路径。SPA 深链由 _SpaStaticFiles 回退 index.html。
    # dev 期未构建(frontend/dist 不存在)时跳过,不炸。
    spa_dist = _REPO_ROOT / "frontend" / "dist"
    if (spa_dist / "index.html").is_file():
        app.mount(
            "/",
            _SpaStaticFiles(directory=str(spa_dist), html=True),
            name="static-spa",
        )
    else:
        logger.info("frontend not built, SPA not mounted: %s", spa_dist)

    return app
