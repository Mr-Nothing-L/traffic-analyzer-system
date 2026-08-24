#!/usr/bin/env python3
"""agent 代理链路(/api/agent)真实后端端到端冒烟脚本。

用法(项目根目录):
    python3 scripts/e2e_agent_smoke.py                # 默认 127.0.0.1:8608
    python3 scripts/e2e_agent_smoke.py --port 8609 --timeout 300

行为:在 127.0.0.1:<port> 启动真实后端(python3 -m traffic_analyzer web,
工作区 = 项目根;web 启动时会自动 spawn toolserver:8601 与 TS agent:8602,
端口被健康实例占用时按 port_occupied 降级,只要代理能连通下游即视为通过)。
随后验证:
  1. GET  /api/agent/health   → 返回且 agent/toolserver 下游探测健康
  2. POST /api/agent/sessions → {mode:'yolo'} 拿到 sessionId
  3. POST /api/agent/chat     → 有界指令(只调 video_meta 查时长),解析 SSE:
     断言出现 tool_call_start 且工具名 video_meta,最终收到 done
认证处理同 e2e_v2_smoke:config/users.db 已有账号时,自建临时账号
(写入 users.db,结束后删除),TRAFFIC_ANALYZER_SECRET 经环境注入,
POST /api/auth/login 拿 ta_session cookie;库为空(认证关闭)则跳过。
真实调用模型 endpoint,chat 步骤整体超时 --timeout(默认 300s)。
每步打印 PASS/FAIL,任一步失败退出码非零。
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from traffic_analyzer.web import user_store  # noqa: E402

SMOKE_USERNAME = "agent_smoke_user"
SMOKE_PASSWORD = "agent_smoke_pass"
SMOKE_SECRET = "agent-smoke-secret"  # 仅注入测试实例环境,避免写回 config/.env
DEFAULT_VIDEO = "演示区/01-02_Event_129_1755579215119_1.mp4"
CHAT_INPUT = (
    "只做一件事:调用 video_meta 工具获取这个视频的元信息,然后用一句中文告诉我时长。"
    "不要调用其他工具,不要调用 submit_detection。"
)

RESULTS: list = []  # (步骤名, 是否通过, 备注)


def record(name: str, ok: bool, note: str = "") -> bool:
    RESULTS.append((name, ok, note))
    suffix = f" ({note})" if note else ""
    print(f"{'PASS' if ok else 'FAIL'}  {name}{suffix}", flush=True)
    return ok


def api_request(base: str, method: str, path: str, cookie: str = "",
                body: dict = None, timeout: float = 10.0):
    """HTTP 请求;返回 (status, headers, body_bytes)。抛 URLError 于连接失败。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def server_up(base: str) -> bool:
    """就绪探测(同 e2e_v2_smoke):/api/jobs 200(列表)或 401(认证开启)。"""
    try:
        status, _, payload = api_request(base, "GET", "/api/jobs", timeout=2)
        if status == 401:
            return True
        return status == 200 and isinstance(json.loads(payload), list)
    except Exception:
        return False


def start_backend(port: int) -> subprocess.Popen:
    """经 CLI web 子命令启动真实后端;BROWSER=true 抑制自动打开浏览器。"""
    import os

    env = dict(os.environ)
    env["TRAFFIC_ANALYZER_SECRET"] = SMOKE_SECRET
    env["BROWSER"] = "true"  # webbrowser 走 BROWSER 命令,'true' 直接返回
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "traffic_analyzer", "web",
            "--host", "127.0.0.1", "--port", str(port),
            "--workspace", str(REPO_ROOT),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def login(base: str) -> str:
    """POST /api/auth/login,返回 'ta_session=...' cookie 串。"""
    status, headers, payload = api_request(
        base, "POST", "/api/auth/login",
        body={"username": SMOKE_USERNAME, "password": SMOKE_PASSWORD},
    )
    if status != 200:
        raise RuntimeError(f"login failed: {status} {payload[:200]!r}")
    jar = SimpleCookie(headers.get("Set-Cookie", ""))
    morsel = jar.get("ta_session")
    if morsel is None:
        raise RuntimeError("login response missing ta_session cookie")
    return f"ta_session={morsel.value}"


def step_health(base: str, cookie: str) -> bool:
    """GET /api/agent/health:返回且 agent/toolserver 下游探测健康。"""
    try:
        status, _, payload = api_request(base, "GET", "/api/agent/health", cookie=cookie)
    except Exception as e:
        return record("GET /api/agent/health", False, str(e))
    if status != 200:
        return record("GET /api/agent/health", False, f"HTTP {status}: {payload[:200]!r}")
    try:
        data = json.loads(payload)
    except ValueError:
        return record("GET /api/agent/health", False, f"非 JSON 响应: {payload[:200]!r}")
    agent = data.get("agent") or {}
    toolserver = data.get("toolserver") or {}
    runtime = data.get("runtime") or {}
    states = ""
    if runtime:
        states = (f", runtime: agent={runtime.get('agent', {}).get('state')}"
                  f" toolserver={runtime.get('toolserver', {}).get('state')}")
    ok = bool(agent.get("healthy")) and bool(toolserver.get("healthy"))
    return record(
        "GET /api/agent/health", ok,
        f"status={data.get('status')} agent.healthy={agent.get('healthy')}"
        f" toolserver.healthy={toolserver.get('healthy')}{states}")


def step_create_session(base: str, cookie: str):
    """POST /api/agent/sessions {mode:'yolo'} → sessionId(失败返回 None)。"""
    try:
        status, _, payload = api_request(
            base, "POST", "/api/agent/sessions", cookie=cookie, body={"mode": "yolo"})
    except Exception as e:
        record("POST /api/agent/sessions", False, str(e))
        return None
    session_id = None
    try:
        session_id = json.loads(payload).get("sessionId")
    except ValueError:
        pass
    ok = status == 200 and isinstance(session_id, str) and bool(session_id)
    record("POST /api/agent/sessions", ok,
           f"sessionId={session_id}" if ok else f"HTTP {status}: {payload[:200]!r}")
    return session_id if ok else None


def step_chat(base: str, cookie: str, session_id: str, video: str,
              timeout: float) -> None:
    """POST /api/agent/chat 有界指令;解析 SSE,断言 video_meta 调用与 done。"""
    body = json.dumps({
        "sessionId": session_id,
        "input": CHAT_INPUT,
        "videoPath": video,
    }).encode("utf-8")
    req = urllib.request.Request(base + "/api/agent/chat", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    if cookie:
        req.add_header("Cookie", cookie)

    events = []  # (type, payload)
    text_parts: list = []
    deadline = time.time() + timeout
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                record("POST /api/agent/chat(SSE 流)", False,
                       f"HTTP {resp.status}: {resp.read()[:200]!r}")
                return
            buffer = ""
            while time.time() < deadline:
                chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", "replace")
                while "\n\n" in buffer:
                    raw, buffer = buffer.split("\n\n", 1)
                    for line in raw.splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[len("data:"):].strip())
                        except ValueError:
                            continue
                        etype = event.get("type")
                        events.append((etype, event))
                        if etype == "text_delta":
                            text_parts.append(event.get("text", ""))
                    if events and events[-1][0] == "done":
                        break
    except Exception as e:
        record("POST /api/agent/chat(SSE 流)", False, f"{type(e).__name__}: {e}")
        return

    if time.time() >= deadline and not any(t == "done" for t, _ in events):
        record("POST /api/agent/chat(SSE 流)", False,
               f"超时 {timeout}s 未收到 done(已收 {len(events)} 个事件)")
        return

    types = [t for t, _ in events]
    video_meta_calls = [e for t, e in events
                        if t == "tool_call_start"
                        and (e.get("call") or {}).get("name") == "video_meta"]
    record("SSE: tool_call_start(video_meta)", bool(video_meta_calls),
           f"事件序列: {'/'.join(types)}" if types else "未收到任何事件")
    done_events = [e for t, e in events if t == "done"]
    done_ok = bool(done_events) and done_events[-1].get("reason") != "error"
    note = f"reason={done_events[-1].get('reason')}" if done_events else "未收到 done"
    answer = "".join(text_parts).strip().replace("\n", " ")
    if answer:
        note += f"; 模型回答: {answer[:120]}"
    record("SSE: 收到 done", done_ok, note)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8608,
                        help="测试实例端口(默认 %(default)s)")
    parser.add_argument("--video", default=DEFAULT_VIDEO,
                        help="视频路径,相对工作区(默认 %(default)s)")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="chat 步骤整体超时秒数(默认 %(default)s)")
    args = parser.parse_args()

    t_start = time.time()
    base = f"http://127.0.0.1:{args.port}"
    if server_up(base):
        print(f"error: {base} 已被占用,请换 --port", file=sys.stderr)
        return 2
    if not (REPO_ROOT / args.video).is_file():
        print(f"error: 视频不存在:{REPO_ROOT / args.video}", file=sys.stderr)
        return 2

    # 认证处理同 e2e_v2_smoke:库中已有账号才建临时账号并登录;库为空
    # (认证关闭)不动 users.db。
    user_created = False
    if user_store.list_users():
        user_store.remove_user(SMOKE_USERNAME)  # 清理上次异常退出的残留
        if not user_store.add_user(SMOKE_USERNAME, SMOKE_PASSWORD):
            print(f"error: 无法创建临时账号 {SMOKE_USERNAME}", file=sys.stderr)
            return 2
        user_created = True

    cookie = ""
    proc = start_backend(args.port)
    try:
        deadline = time.time() + 60
        while time.time() < deadline and not server_up(base):
            if proc.poll() is not None:
                print("error: 后端进程启动后退出", file=sys.stderr)
                return 2
            time.sleep(0.3)
        if not server_up(base):
            print(f"error: 后端 60s 内未就绪({base})", file=sys.stderr)
            return 2
        print(f"后端已就绪:{base}({time.time() - t_start:.1f}s)", flush=True)

        # 就绪后兜底:若 API 返回 401(env 配置触发认证),补建临时账号登录。
        status, _, _ = api_request(base, "GET", "/api/jobs", timeout=5)
        if status == 401 and not user_created:
            user_store.remove_user(SMOKE_USERNAME)
            if not user_store.add_user(SMOKE_USERNAME, SMOKE_PASSWORD):
                print(f"error: 无法创建临时账号 {SMOKE_USERNAME}", file=sys.stderr)
                return 2
            user_created = True
        if user_created:
            try:
                cookie = login(base)
                record("登录(临时账号)", True)
            except Exception as e:
                record("登录(临时账号)", False, str(e))

        # 下游不通时后续步骤无意义,仅在前一步通过时继续
        session_id = step_create_session(base, cookie) if step_health(base, cookie) else None
        if session_id is not None:
            step_chat(base, cookie, session_id, args.video, args.timeout)
    finally:
        _cleanup(proc, user_created)

    return _report()


def _cleanup(proc: subprocess.Popen, user_created: bool) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    if user_created:
        user_store.remove_user(SMOKE_USERNAME)


def _report() -> int:
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 步通过"
          + (f",失败: {', '.join(r[0] for r in failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
