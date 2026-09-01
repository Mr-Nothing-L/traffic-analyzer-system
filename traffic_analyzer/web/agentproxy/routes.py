"""/api/agent reverse-proxy routes to the TS agent server.

[文件说明]
作用:/api/agent 代理路由(前缀 /api/agent),把 web 层请求转发到 TS agent
服务(agent/src/server,默认 http://127.0.0.1:8602):
- GET  /health    聚合 toolserver 与 agent 两个下游 /health,全部 ok 才报
  status:'ok',否则 'unavailable'(runtime 未启动/禁用时同样 unavailable,
  附带 AgentRuntimeManager.snapshot() 进程状态)。
- POST /sessions  透传;body 缺 workspaceDir 时注入 web 层当前工作区路径
  (未选工作区 → 400)。
- GET  /sessions                  纯透传(session 列表);agent server 自行保证
                                  工作区库已恢复,代理层不再做 restore 写副作用。
- GET  /sessions/{id}/history     透传(entries 时间线)。
- POST /sessions/{id}/compact     透传(手动压缩上下文;进行中 → 409)。
- POST /sessions/{id}/recall      透传(撤回某条用户消息及其后内容)。
- POST /sessions/{id}/title       透传(自定义会话标题,空串恢复自动派生)。
- POST /sessions/{id}/mode        透传(切换会话权限模式 manual|auto|yolo)。
- DELETE /sessions/{id}           透传(删除 session)。
- POST /uploads                   对话文件上传(落盘 <workspace>/.agent/uploads/,
                                  见 uploads.py;不透传,web 层本地处理)。
- GET  /uploads/{name}            已上传文件的流式预览(FileResponse,支持 Range)。
- POST /chat      SSE 透传:httpx AsyncClient stream 读下游,
  StreamingResponse 逐块转发(不缓冲);客户端断连时在 generator finally
  里 aclose 下游响应与 client,取消下游请求。
- GET  /sessions/{id}/media/{name}
                  内容寻址媒体图片的二进制透传(缓冲转发,200 原样
                  Content-Type/Cache-Control;不进 JSON 透传表,手写)。
- POST /approval  透传(状态码与 body 原样返回)。
普通透传端点由 _PASS_THROUGH_ROUTES 声明表统一注册;新增 agent 端点通常只需
在表中加一行(配置 method/path/body 策略/是否注入 workspaceDir 等)。
特殊端点(health、uploads、SSE chat)保持手写。
错误契约统一 {error:{code,message}}:下游错误 body 原样透传;连接失败
(ConnectError 等)→ 503 agent_unavailable。
测试可 monkeypatch 本模块的 AsyncClient(注入 MockTransport),不起真实
子进程/服务。
上游:web/agentproxy/__init__.py(聚合导出 router);web/app.py(挂载)。
下游:web/agentproxy/runtime.py(AgentRuntimeManager);agent/src/server。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from httpx import AsyncClient

from traffic_analyzer.web.agentproxy.runtime import AgentRuntimeManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent")

_HEALTH_TIMEOUT = 2.0
# /chat 轮次可能很长:总超时放开,仅保留连接超时。
_CHAT_TIMEOUT = httpx.Timeout(None, connect=10.0)
_JSON_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


def _runtime(request: Request) -> Optional[AgentRuntimeManager]:
    runtime = getattr(request.app.state, "agent_runtime", None)
    return runtime if isinstance(runtime, AgentRuntimeManager) else None


def _unavailable(detail: str = "agent runtime is not running") -> JSONResponse:
    return _error(503, "agent_unavailable", detail)


def _passthrough_error(status_code: int, payload: bytes) -> JSONResponse:
    """下游错误:body 是合法 JSON 则原样透传,否则包成统一错误契约。"""
    try:
        content = json.loads(payload)
    except ValueError:
        content = {
            "error": {
                "code": "upstream_error",
                "message": payload.decode("utf-8", "replace")[:500],
            }
        }
    return JSONResponse(status_code=status_code, content=content)


async def _probe(url: str) -> Dict[str, Any]:
    """探测一个下游 /health;连接失败不抛异常,healthy=False + detail。"""
    try:
        async with AsyncClient(base_url=url, timeout=_HEALTH_TIMEOUT) as client:
            resp = await client.get("/health")
        healthy = resp.status_code == 200
        return {"healthy": healthy, "url": url, "status_code": resp.status_code}
    except httpx.HTTPError as exc:
        return {"healthy": False, "url": url, "detail": str(exc)}


@router.get("/health")
async def agent_health(request: Request) -> Dict[str, Any]:
    """聚合两个下游健康:toolserver 与 agent 都 ok 才报 ok。"""
    runtime = _runtime(request)
    if runtime is None:
        return {
            "status": "unavailable",
            "detail": "agent runtime not started",
            "agent": {"healthy": False},
            "toolserver": {"healthy": False},
        }
    snapshot = runtime.snapshot()
    if not runtime.enabled:
        return {"status": "unavailable", "runtime": snapshot,
                "agent": {"healthy": False}, "toolserver": {"healthy": False}}
    agent_probe, toolserver_probe = await asyncio.gather(
        _probe(runtime.agent_url), _probe(runtime.toolserver_url)
    )
    ok = agent_probe["healthy"] and toolserver_probe["healthy"]
    return {
        "status": "ok" if ok else "unavailable",
        "runtime": snapshot,
        "agent": agent_probe,
        "toolserver": toolserver_probe,
    }


# ---------------------------------------------------------------------------
# 普通透传端点的声明表与统一实现
# ---------------------------------------------------------------------------

_PassThroughBody = Literal["none", "raw", "json"]


@dataclass(frozen=True)
class _PassThroughRoute:
    """一条普通透传路由的声明。

    - methods: FastAPI 方法列表,普通透传均为单方法。
    - path:    代理层与下游使用相同的路径模板(不含 /api/agent 前缀)。
    - body:    "none" 无 body 转发(GET/DELETE/无 body POST);
               "raw"  原样转发 request.body() 字节;
               "json" 解析 JSON 对象后转发(可配合 inject_workspace_dir)。
    - inject_workspace_dir: 仅对 body="json" 生效;body 中缺 workspaceDir 时
               注入当前工作区路径,未选工作区 → 400 no_workspace。
    - description: 端点 docstring。
    """

    methods: Tuple[str, ...]
    path: str
    body: _PassThroughBody = "none"
    inject_workspace_dir: bool = False
    description: str = ""


_PASS_THROUGH_ROUTES: List[_PassThroughRoute] = [
    _PassThroughRoute(
        ("POST",),
        "/sessions",
        body="json",
        inject_workspace_dir=True,
        description="透传 POST /sessions;body 缺 workspaceDir 时注入当前工作区路径。",
    ),
    _PassThroughRoute(
        ("GET",),
        "/sessions",
        description="透传 GET /sessions(session 列表)。agent server 已自行保证工作区库恢复,代理层不再在列表前做 restore 写副作用。",
    ),
    _PassThroughRoute(
        ("GET",),
        "/sessions/{session_id}/history",
        description="透传 GET /sessions/{id}/history(entries 时间线)。",
    ),
    _PassThroughRoute(
        ("DELETE",),
        "/sessions/{session_id}",
        description="透传 DELETE /sessions/{id}(删除 session)。",
    ),
    _PassThroughRoute(
        ("POST",),
        "/sessions/{session_id}/compact",
        description="透传 POST /sessions/{id}/compact(手动压缩上下文,无 body)。",
    ),
    _PassThroughRoute(
        ("POST",),
        "/sessions/{session_id}/recall",
        body="raw",
        description="透传 POST /sessions/{id}/recall(撤回某条用户消息及其后内容)。",
    ),
    _PassThroughRoute(
        ("POST",),
        "/sessions/{session_id}/title",
        body="raw",
        description="透传 POST /sessions/{id}/title(自定义会话标题,空串恢复自动派生)。",
    ),
    _PassThroughRoute(
        ("POST",),
        "/sessions/{session_id}/mode",
        body="raw",
        description="透传 POST /sessions/{id}/mode(切换会话权限模式)。",
    ),
    _PassThroughRoute(
        ("GET",),
        "/sessions/{session_id}/events",
        description="透传 GET /sessions/{id}/events?fromSeq=N(断连/刷新后补齐条目 + inProgress)。",
    ),
    _PassThroughRoute(
        ("POST",),
        "/sessions/{session_id}/cancel",
        description="透传 POST /sessions/{id}/cancel(显式终止进行中轮次,无 body)。",
    ),
    _PassThroughRoute(
        ("POST",),
        "/sessions/{session_id}/steer",
        body="raw",
        description="透传 POST /sessions/{id}/steer(轮次进行中注入用户消息,下一 step 边界生效)。",
    ),
    _PassThroughRoute(
        ("POST",),
        "/approval",
        body="raw",
        description="透传 POST /approval(审批回执)。",
    ),
]


def _extract_path_params(path: str) -> List[str]:
    return [seg[1:-1] for seg in path.split("/") if seg.startswith("{") and seg.endswith("}")]


async def _proxy_passthrough(
    request: Request,
    runtime: AgentRuntimeManager,
    method: str,
    downstream_path: str,
    body: _PassThroughBody,
    inject_workspace_dir: bool,
) -> JSONResponse:
    """统一普通透传实现:下游错误原样,连接失败 503。"""
    try:
        async with AsyncClient(base_url=runtime.agent_url, timeout=_JSON_TIMEOUT) as client:
            if body == "none":
                resp = await client.request(method, downstream_path)
            elif body == "raw":
                content = await request.body()
                resp = await client.request(
                    method,
                    downstream_path,
                    content=content,
                    headers={"Content-Type": "application/json"},
                )
            else:  # "json"
                try:
                    payload = json.loads(await request.body())
                except (json.JSONDecodeError, ValueError):
                    return _error(400, "bad_request", "request body must be a JSON object")
                if not isinstance(payload, dict):
                    return _error(400, "bad_request", "request body must be a JSON object")
                if inject_workspace_dir and not payload.get("workspaceDir"):
                    workspace = request.app.state.workspace.get()
                    if workspace is None:
                        return _error(400, "no_workspace", "No workspace selected")
                    payload["workspaceDir"] = str(workspace)
                resp = await client.request(method, downstream_path, json=payload)
    except httpx.HTTPError as exc:
        logger.warning("agent %s %s unreachable: %s", method, downstream_path, exc)
        return _unavailable(f"agent server unreachable: {exc}")
    return _passthrough_error(resp.status_code, resp.content)


def _build_passthrough_handler(route: _PassThroughRoute) -> Callable[..., Any]:
    """根据声明表条目生成 FastAPI endpoint 函数。"""
    path_params = _extract_path_params(route.path)
    method = route.methods[0]

    async def endpoint(request: Request, **kwargs: str) -> JSONResponse:
        runtime = _runtime(request)
        if runtime is None or not runtime.enabled:
            return _unavailable()
        downstream_path = route.path.format(**kwargs) if kwargs else route.path
        query = f"?{request.url.query}" if request.url.query else ""
        return await _proxy_passthrough(
            request,
            runtime,
            method,
            f"{downstream_path}{query}",
            route.body,
            route.inject_workspace_dir,
        )

    sig_params = [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request)]
    for param_name in path_params:
        sig_params.append(
            inspect.Parameter(param_name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str)
        )
    endpoint.__signature__ = inspect.Signature(sig_params)  # type: ignore[attr-defined]
    endpoint.__name__ = (
        "pt_" + route.path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    )
    endpoint.__doc__ = route.description
    return endpoint


for _route in _PASS_THROUGH_ROUTES:
    router.add_api_route(
        _route.path,
        _build_passthrough_handler(_route),
        methods=list(_route.methods),
        summary=_route.description.split("。")[0] if _route.description else "",
    )


# ---------------------------------------------------------------------------
# GET /sessions/{id}/media/{name} 透传(特殊:二进制响应,不进 JSON 透传表)
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/media/{name}")
async def session_media(session_id: str, name: str, request: Request) -> Any:
    """透传 GET /sessions/{id}/media/{name}:内容寻址的媒体引用图片(二进制)。

    普通 JSON 透传表经 _passthrough_error 复原 body,不适合 image/jpeg;
    这里单独缓冲转发(单图 MB 级,缓冲即可):200 原样回 Content-Type,
    非 200 走统一错误契约,下游不可达 503。
    """
    runtime = _runtime(request)
    if runtime is None or not runtime.enabled:
        return _unavailable()
    client = AsyncClient(base_url=runtime.agent_url, timeout=_JSON_TIMEOUT)
    try:
        downstream = await client.get(f"/sessions/{session_id}/media/{name}")
    except httpx.HTTPError as exc:
        logger.warning("agent media %s/%s unreachable: %s", session_id, name, exc)
        await client.aclose()
        return _unavailable(f"agent server unreachable: {exc}")
    try:
        if downstream.status_code != 200:
            return _passthrough_error(downstream.status_code, downstream.content)
        return Response(
            content=downstream.content,
            media_type=downstream.headers.get("content-type", "application/octet-stream"),
            headers={
                "Cache-Control": downstream.headers.get("cache-control", "no-store"),
            },
        )
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# SSE chat 透传(特殊:流式转发 + 断连取消)
# ---------------------------------------------------------------------------


@router.post("/chat")
async def chat(request: Request) -> Any:
    """透传 POST /chat 的 SSE 流:逐块转发,不缓冲;断连取消下游请求。"""
    runtime = _runtime(request)
    if runtime is None or not runtime.enabled:
        return _unavailable()
    body = await request.body()
    client = AsyncClient(base_url=runtime.agent_url, timeout=_CHAT_TIMEOUT)
    downstream_req = client.build_request(
        "POST", "/chat", content=body, headers={"Content-Type": "application/json"}
    )
    try:
        downstream = await client.send(downstream_req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("agent /chat unreachable: %s", exc)
        return _unavailable(f"agent server unreachable: {exc}")
    if downstream.status_code != 200:
        payload = await downstream.aread()
        await downstream.aclose()
        await client.aclose()
        return _passthrough_error(downstream.status_code, payload)

    async def event_stream() -> Any:
        try:
            async for chunk in downstream.aiter_bytes():
                yield chunk
        finally:
            # 客户端断连(生成器被 cancel)或流正常结束都走到这里:关闭下游
            # 响应即取消下游请求,回收连接与 client。
            await downstream.aclose()
            await client.aclose()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# 上传/预览端点(web 层本地处理,不透传),聚合进同一 /api/agent 前缀。
from traffic_analyzer.web.agentproxy import uploads as _uploads_mod

router.include_router(_uploads_mod.router)
