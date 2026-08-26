"""/api/agent reverse-proxy routes to the TS agent server.

[文件说明]
作用:/api/agent 代理路由(前缀 /api/agent),把 web 层请求转发到 TS agent
服务(agent/src/server,默认 http://127.0.0.1:8602):
- GET  /health    聚合 toolserver 与 agent 两个下游 /health,全部 ok 才报
  status:'ok',否则 'unavailable'(runtime 未启动/禁用时同样 unavailable,
  附带 AgentRuntimeManager.snapshot() 进程状态)。
- POST /sessions  透传;body 缺 workspaceDir 时注入 web 层当前工作区路径
  (未选工作区 → 400)。
- GET  /sessions                  透传(session 列表);转发前对登记表
                                  (runtime.registered_workspaces)里每个工作区并发
                                  POST /workspaces/restore(幂等,已打开是快路径),
                                  聚合全部工作区的历史会话;单个失败仅 warning 跳过。
- GET  /sessions/{id}/history     透传(entries 时间线)。
- POST /sessions/{id}/compact     透传(手动压缩上下文;进行中 → 409)。
- POST /sessions/{id}/recall      透传(撤回某条用户消息及其后内容)。
- POST /sessions/{id}/mode        透传(切换会话权限模式 manual|auto|yolo)。
- DELETE /sessions/{id}           透传(删除 session)。
- POST /uploads                   对话文件上传(落盘 <workspace>/.agent/uploads/,
                                  见 uploads.py;不透传,web 层本地处理)。
- GET  /uploads/{name}            已上传文件的流式预览(FileResponse,支持 Range)。
- POST /chat      SSE 透传:httpx AsyncClient stream 读下游,
  StreamingResponse 逐块转发(不缓冲);客户端断连时在 generator finally
  里 aclose 下游响应与 client,取消下游请求。
- POST /approval  透传(状态码与 body 原样返回)。
错误契约统一 {error:{code,message}}:下游错误 body 原样透传;连接失败
(ConnectError 等)→ 503 agent_unavailable。
测试可 monkeypatch 本模块的 AsyncClient(注入 MockTransport),不起真实
子进程/服务。
上游:web/agentproxy/__init__.py(聚合导出 router);web/app.py(挂载)。
下游:web/agentproxy/runtime.py(AgentRuntimeManager);agent/src/server。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional, Tuple

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from httpx import AsyncClient

from traffic_analyzer.web.agentproxy.runtime import (
    AgentRuntimeManager,
    registered_workspaces,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent")

_HEALTH_TIMEOUT = 2.0
# 逐工作区 restore 的超时:已打开的工作区是快路径,收紧避免拖慢列表。
_RESTORE_TIMEOUT = httpx.Timeout(2.0, connect=2.0)
# 登记表追加序即时间序;很大时只恢复最近 N 个,保持列表延迟可接受。
_RESTORE_RECENT_LIMIT = 20
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


async def _simple_passthrough(
    runtime: AgentRuntimeManager, method: str, path: str
) -> JSONResponse:
    """无 body 的 JSON 透传(GET/DELETE):下游错误原样,连接失败 503。"""
    try:
        async with AsyncClient(base_url=runtime.agent_url, timeout=_JSON_TIMEOUT) as client:
            resp = await client.request(method, path)
    except httpx.HTTPError as exc:
        logger.warning("agent %s %s unreachable: %s", method, path, exc)
        return _unavailable(f"agent server unreachable: {exc}")
    return _passthrough_error(resp.status_code, resp.content)


@router.post("/sessions")
async def create_session(request: Request) -> JSONResponse:
    """透传 POST /sessions;body 缺 workspaceDir 时注入当前工作区路径。"""
    runtime = _runtime(request)
    if runtime is None or not runtime.enabled:
        return _unavailable()
    try:
        payload: Dict[str, Any] = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _error(400, "bad_request", "request body must be a JSON object")
    if not isinstance(payload, dict):
        return _error(400, "bad_request", "request body must be a JSON object")
    if not payload.get("workspaceDir"):
        workspace = request.app.state.workspace.get()
        if workspace is None:
            return _error(400, "no_workspace", "No workspace selected")
        payload["workspaceDir"] = str(workspace)
    try:
        async with AsyncClient(base_url=runtime.agent_url, timeout=_JSON_TIMEOUT) as client:
            resp = await client.post("/sessions", json=payload)
    except httpx.HTTPError as exc:
        logger.warning("agent /sessions unreachable: %s", exc)
        return _unavailable(f"agent server unreachable: {exc}")
    return _passthrough_error(resp.status_code, resp.content)


async def _restore_registered_workspaces(runtime: AgentRuntimeManager) -> None:
    """对登记表里每个工作区调 agent /workspaces/restore(幂等,已打开即返回)。

    并发 gather;单个失败或下游 4xx/5xx(如目录已删)仅 warning 跳过,
    不影响会话列表透传。登记表很大时只处理最近 _RESTORE_RECENT_LIMIT 个。
    """
    entries = registered_workspaces()[-_RESTORE_RECENT_LIMIT:]
    if not entries:
        return

    async def _restore_one(path: str) -> None:
        try:
            async with AsyncClient(
                base_url=runtime.agent_url, timeout=_RESTORE_TIMEOUT
            ) as client:
                resp = await client.post(
                    "/workspaces/restore", json={"workspaceDir": path}
                )
            if resp.status_code >= 400:
                logger.warning(
                    "agent restore workspace %s -> %s, skipped",
                    path, resp.status_code,
                )
        except httpx.HTTPError as exc:
            logger.warning("agent restore workspace %s failed: %s", path, exc)

    await asyncio.gather(*(_restore_one(p) for p in entries))


@router.get("/sessions")
async def list_sessions(request: Request) -> JSONResponse:
    """透传 GET /sessions(session 列表);先逐个 restore 登记表工作区,
    让 agent server 把全部工作区的磁盘历史会话加载进内存索引。"""
    runtime = _runtime(request)
    if runtime is None or not runtime.enabled:
        return _unavailable()
    await _restore_registered_workspaces(runtime)
    return await _simple_passthrough(runtime, "GET", "/sessions")


@router.get("/sessions/{session_id}/history")
async def session_history(session_id: str, request: Request) -> JSONResponse:
    """透传 GET /sessions/{id}/history(entries 时间线)。"""
    runtime = _runtime(request)
    if runtime is None or not runtime.enabled:
        return _unavailable()
    return await _simple_passthrough(
        runtime, "GET", f"/sessions/{session_id}/history"
    )


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request) -> JSONResponse:
    """透传 DELETE /sessions/{id}(删除 session)。"""
    runtime = _runtime(request)
    if runtime is None or not runtime.enabled:
        return _unavailable()
    return await _simple_passthrough(runtime, "DELETE", f"/sessions/{session_id}")


@router.post("/sessions/{session_id}/compact")
async def compact_session(session_id: str, request: Request) -> JSONResponse:
    """透传 POST /sessions/{id}/compact(手动压缩上下文,无 body)。"""
    runtime = _runtime(request)
    if runtime is None or not runtime.enabled:
        return _unavailable()
    try:
        async with AsyncClient(base_url=runtime.agent_url, timeout=_JSON_TIMEOUT) as client:
            resp = await client.post(f"/sessions/{session_id}/compact")
    except httpx.HTTPError as exc:
        logger.warning("agent /sessions/%s/compact unreachable: %s", session_id, exc)
        return _unavailable(f"agent server unreachable: {exc}")
    return _passthrough_error(resp.status_code, resp.content)


@router.post("/sessions/{session_id}/recall")
async def recall_session(session_id: str, request: Request) -> JSONResponse:
    """透传 POST /sessions/{id}/recall(撤回某条用户消息及其后内容)。"""
    runtime = _runtime(request)
    if runtime is None or not runtime.enabled:
        return _unavailable()
    body = await request.body()
    try:
        async with AsyncClient(base_url=runtime.agent_url, timeout=_JSON_TIMEOUT) as client:
            resp = await client.post(
                f"/sessions/{session_id}/recall",
                content=body,
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        logger.warning("agent /sessions/%s/recall unreachable: %s", session_id, exc)
        return _unavailable(f"agent server unreachable: {exc}")
    return _passthrough_error(resp.status_code, resp.content)


@router.post("/sessions/{session_id}/mode")
async def set_session_mode(session_id: str, request: Request) -> JSONResponse:
    """透传 POST /sessions/{id}/mode(切换会话权限模式)。"""
    runtime = _runtime(request)
    if runtime is None or not runtime.enabled:
        return _unavailable()
    body = await request.body()
    try:
        async with AsyncClient(base_url=runtime.agent_url, timeout=_JSON_TIMEOUT) as client:
            resp = await client.post(
                f"/sessions/{session_id}/mode",
                content=body,
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        logger.warning("agent /sessions/%s/mode unreachable: %s", session_id, exc)
        return _unavailable(f"agent server unreachable: {exc}")
    return _passthrough_error(resp.status_code, resp.content)


@router.get("/sessions/{session_id}/events")
async def session_events(session_id: str, request: Request) -> JSONResponse:
    """透传 GET /sessions/{id}/events?fromSeq=N(断连/刷新后补齐条目 + inProgress)。"""
    runtime = _runtime(request)
    if runtime is None or not runtime.enabled:
        return _unavailable()
    query = f"?{request.url.query}" if request.url.query else ""
    return await _simple_passthrough(
        runtime, "GET", f"/sessions/{session_id}/events{query}"
    )


@router.post("/sessions/{session_id}/cancel")
async def cancel_session_turn(session_id: str, request: Request) -> JSONResponse:
    """透传 POST /sessions/{id}/cancel(显式终止进行中轮次,无 body)。"""
    runtime = _runtime(request)
    if runtime is None or not runtime.enabled:
        return _unavailable()
    try:
        async with AsyncClient(base_url=runtime.agent_url, timeout=_JSON_TIMEOUT) as client:
            resp = await client.post(f"/sessions/{session_id}/cancel")
    except httpx.HTTPError as exc:
        logger.warning("agent /sessions/%s/cancel unreachable: %s", session_id, exc)
        return _unavailable(f"agent server unreachable: {exc}")
    return _passthrough_error(resp.status_code, resp.content)


@router.post("/sessions/{session_id}/steer")
async def steer_session(session_id: str, request: Request) -> JSONResponse:
    """透传 POST /sessions/{id}/steer(轮次进行中注入用户消息,下一 step 边界生效)。"""
    runtime = _runtime(request)
    if runtime is None or not runtime.enabled:
        return _unavailable()
    body = await request.body()
    try:
        async with AsyncClient(base_url=runtime.agent_url, timeout=_JSON_TIMEOUT) as client:
            resp = await client.post(
                f"/sessions/{session_id}/steer",
                content=body,
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        logger.warning("agent /sessions/%s/steer unreachable: %s", session_id, exc)
        return _unavailable(f"agent server unreachable: {exc}")
    return _passthrough_error(resp.status_code, resp.content)


@router.post("/approval")
async def approval(request: Request) -> JSONResponse:
    """透传 POST /approval(审批回执)。"""
    runtime = _runtime(request)
    if runtime is None or not runtime.enabled:
        return _unavailable()
    body = await request.body()
    try:
        async with AsyncClient(base_url=runtime.agent_url, timeout=_JSON_TIMEOUT) as client:
            resp = await client.post(
                "/approval", content=body, headers={"Content-Type": "application/json"}
            )
    except httpx.HTTPError as exc:
        logger.warning("agent /approval unreachable: %s", exc)
        return _unavailable(f"agent server unreachable: {exc}")
    return _passthrough_error(resp.status_code, resp.content)


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
