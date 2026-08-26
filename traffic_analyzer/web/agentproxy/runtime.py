"""Agent runtime subprocess lifecycle (toolserver + TS agent server).

[文件说明]
作用:agent 运行时子进程生命周期管理(AgentRuntimeManager)。start() 拉起
两个子进程:① Python 视频工具服务 ``python3 -m traffic_analyzer.toolserver
--workspace <当前workspace或项目根> --port 8601``;② TS agent 服务
``npx tsx src/server/main.ts``(cwd=agent/,env 注入 AGENT_PORT=8602 与
TOOLSERVER_URL)。toolserver 持有「允许根」集合,--workspace 只是初始根;
add_workspace_root() 经 POST /config/roots 把新工作区热注册进去(web 层
切换工作区时调用),免重启;注册失败仅记 warning,不抛异常。
restore_workspace() 经 POST agent /workspaces/restore 让 agent server 把
该工作区 <workspace>/.agent/sessions.db 里的历史会话加载进内存索引(agent
重启后内存为空、磁盘数据还在);web 启动时若 agent 是外部已运行实例
(端口占用未 spawn)由 start() 调用一次,自己 spawn 的 agent 改由
AGENT_RESTORE_WORKSPACES 环境变量在进程启动时自行恢复(避免「spawn 后
立即 HTTP 调用撞上子进程尚未 listen」的竞态);工作区切换时由 web 层再
调一次。同为旁路调用,失败仅 warning。端口被占用 / Popen 失败仅记日志
降级(state 记为 port_occupied/failed),不影响 web 其他功能;/api/agent/health 据此与下游
探测报告 unavailable。stop() 对整个进程组 SIGTERM→SIGKILL(子进程经
start_new_session 独立成组;agent 是 npx/tsx 包装器,只杀直接子进程会把
node 孙进程孤儿化)。
AGENT_RUNTIME_ENABLE(默认 true)可整体关闭;AGENT_RUNTIME_AGENT_PORT /
AGENT_RUNTIME_TOOLSERVER_PORT 可覆盖默认端口(多实例并存时避免降级到
外部旧实例)。子进程 stdout 由守护线程逐行 drain 进 logger,避免管道写满
阻塞子进程。
工作区登记表:add_workspace_root()/restore_workspace() 被调用时把规范化
路径追加登记进 config/agent_workspaces.json(JSON 数组,追加序即时间序;
去重、文件不存在自动建、写失败仅 warning),registered_workspaces() 读取
(文件缺失/损坏返回 [] 不炸);routes.py 的 GET /sessions 据此逐个 restore,
聚合全部工作区的历史会话。登记表是与工作区无关的全局状态,定位方式与
config/users.db 一致(包目录推导)。
上游:web/agentproxy/__init__.py(聚合导出);web/app.py(lifespan 调
start/stop);web/agentproxy/routes.py(读取 agent_url/toolserver_url 与
enabled)。
下游:web/jobs/job.py 的 REPO_ROOT;traffic_analyzer toolserver;
agent/src/server/main.ts。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from traffic_analyzer.web.jobs.job import REPO_ROOT

logger = logging.getLogger(__name__)

ENABLE_ENV_VAR = "AGENT_RUNTIME_ENABLE"
AGENT_PORT_ENV_VAR = "AGENT_RUNTIME_AGENT_PORT"
TOOLSERVER_PORT_ENV_VAR = "AGENT_RUNTIME_TOOLSERVER_PORT"
DEFAULT_TOOLSERVER_PORT = 8601
DEFAULT_AGENT_PORT = 8602

# 注册允许根是旁路请求:toolserver 未就绪/旧版本无此端点都不能拖慢调用方。
_REGISTER_ROOT_TIMEOUT = 2.0

_FALSE_VALUES = {"0", "false", "no", "off"}


def runtime_enabled() -> bool:
    """``AGENT_RUNTIME_ENABLE`` 开关:默认 true,0/false/no/off 关闭。"""
    raw = os.environ.get(ENABLE_ENV_VAR)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSE_VALUES


def _port_from_env(name: str, default: int) -> int:
    """读端口覆盖环境变量;非整数仅告警并回落默认值。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("agent runtime: invalid %s=%r, using default %s",
                       name, raw, default)
        return default


def _port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """Loopback TCP 探测:能连上即视为端口被占用。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _post_workspace_root(toolserver_url: str, path: Path) -> None:
    """POST /config/roots 的实际 HTTP 调用(独立出来便于测试替换)。"""
    httpx.post(
        f"{toolserver_url}/config/roots",
        json={"path": str(path)},
        timeout=_REGISTER_ROOT_TIMEOUT,
    )


def _post_workspace_restore(agent_url: str, path: Path) -> None:
    """POST /workspaces/restore 的实际 HTTP 调用(独立出来便于测试替换)。"""
    httpx.post(
        f"{agent_url}/workspaces/restore",
        json={"workspaceDir": str(path)},
        timeout=_REGISTER_ROOT_TIMEOUT,
    )


# 工作区登记表:与工作区无关的全局状态,定位方式同 config/users.db。
# JSON 数组,元素为规范化后的工作区绝对路径,追加序即时间序。
REGISTRY_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "agent_workspaces.json"
)
_registry_lock = threading.Lock()


def registered_workspaces() -> List[str]:
    """读取登记表(规范化工作区路径列表);文件缺失/损坏返回 [],不抛异常。"""
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logger.warning("agent runtime: workspace registry unreadable: %s", exc)
        return []
    if not isinstance(data, list):
        logger.warning("agent runtime: workspace registry is not a list, ignoring")
        return []
    return [p for p in data if isinstance(p, str)]


def _record_workspace(path: Path) -> None:
    """把规范化工作区路径追加进登记表(去重、文件不存在自动建)。

    与下游 HTTP 调用一样是旁路动作:写失败仅记 warning,不影响调用方。
    """
    resolved = str(Path(path).expanduser().resolve())
    with _registry_lock:
        try:
            entries = registered_workspaces()
            if resolved in entries:
                return
            entries.append(resolved)
            REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
            REGISTRY_PATH.write_text(
                json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "agent runtime: failed to record workspace %s: %s", resolved, exc
            )


def _drain_stdout(proc: subprocess.Popen, name: str) -> None:
    """守护线程:逐行 drain 子进程 stdout 进 logger(防管道写满阻塞)。"""
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            logger.info("[%s] %s", name, line.rstrip("\n"))
    except (OSError, ValueError):  # 管道关闭/进程退出
        pass


def _terminate_proc_group(proc: subprocess.Popen, timeout: float = 3.0) -> None:
    """对整个进程组 SIGTERM→SIGKILL(jobs._terminate_proc 的进程组变体)。

    子进程 spawn 时用 start_new_session 独立成组,killpg 连带 npx/tsx
    包装器下的 node 孙进程一起终止,避免孤儿化。
    """
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return  # 竞态:poll 之后进程刚好退出
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # SIGKILL 后仍不退出(如 D 状态):不再无限阻塞调用方。
        logger.warning("pid %s did not exit after SIGKILL", proc.pid)


class AgentRuntimeManager:
    """toolserver + TS agent server 两个子进程的生命周期。

    ``workspace``: toolserver 的 --workspace(未选工作区时用项目根)。
    ``enabled``: None 时读 AGENT_RUNTIME_ENABLE(默认 true)。
    ``agent_port``/``toolserver_port``: None 时读对应环境变量,缺省 8602/8601。
    ``spawn``/``port_probe`` 可注入,便于测试不起真实子进程。
    """

    def __init__(
        self,
        workspace: Optional[Path] = None,
        *,
        enabled: Optional[bool] = None,
        agent_port: Optional[int] = None,
        toolserver_port: Optional[int] = None,
        repo_root: Path = REPO_ROOT,
        spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
        port_probe: Callable[[int], bool] = _port_in_use,
    ) -> None:
        self._workspace = workspace
        self._enabled = runtime_enabled() if enabled is None else enabled
        self._agent_port = (
            _port_from_env(AGENT_PORT_ENV_VAR, DEFAULT_AGENT_PORT)
            if agent_port is None else agent_port
        )
        self._toolserver_port = (
            _port_from_env(TOOLSERVER_PORT_ENV_VAR, DEFAULT_TOOLSERVER_PORT)
            if toolserver_port is None else toolserver_port
        )
        self._repo_root = Path(repo_root)
        self._spawn = spawn
        self._port_probe = port_probe
        # state: not_started | running | failed | port_occupied | disabled
        self._procs: Dict[str, Optional[subprocess.Popen]] = {
            "toolserver": None,
            "agent": None,
        }
        self._states: Dict[str, str] = {
            "toolserver": "not_started",
            "agent": "not_started",
        }

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def agent_url(self) -> str:
        return f"http://127.0.0.1:{self._agent_port}"

    @property
    def toolserver_url(self) -> str:
        return f"http://127.0.0.1:{self._toolserver_port}"

    def snapshot(self) -> Dict[str, Any]:
        """进程状态快照(供 /api/agent/health 附带报告)。"""
        return {
            "enabled": self._enabled,
            "toolserver": {"state": self._states["toolserver"], "port": self._toolserver_port},
            "agent": {"state": self._states["agent"], "port": self._agent_port},
        }

    def _spawn_one(
        self,
        name: str,
        argv: List[str],
        *,
        cwd: Path,
        env: Optional[Dict[str, str]] = None,
        port: int,
    ) -> None:
        if self._port_probe(port):
            # 端口被占用(可能是上次残留或外部进程):不抢占,记日志降级。
            logger.warning(
                "agent runtime: port %s already in use, not spawning %s", port, name
            )
            self._states[name] = "port_occupied"
            return
        try:
            proc = self._spawn(
                argv,
                cwd=str(cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                # 独立 session:子进程命运由我们控制(stop),不随终端进程组。
                start_new_session=True,
            )
        except OSError as exc:
            logger.warning("agent runtime: failed to spawn %s: %s", name, exc)
            self._states[name] = "failed"
            return
        self._procs[name] = proc
        self._states[name] = "running"
        threading.Thread(
            target=_drain_stdout,
            args=(proc, name),
            daemon=True,
            name=f"agent-runtime-{name}-stdout",
        ).start()
        logger.info("agent runtime: spawned %s (pid %s, port %s)", name, proc.pid, port)

    def add_workspace_root(self, path: Path) -> None:
        """把一个工作区目录热注册进 toolserver 的允许根集合。

        失败(toolserver 未就绪、旧版本无 /config/roots、超时等)仅记
        warning:注册是旁路优化,不影响工作区切换本身。
        """
        _record_workspace(path)
        if not self._enabled:
            return
        try:
            _post_workspace_root(self.toolserver_url, Path(path))
        except Exception as exc:  # noqa: BLE001 - 注册失败降级为告警
            logger.warning(
                "agent runtime: failed to register workspace root %s: %s",
                path, exc,
            )

    def restore_workspace(self, path: Path) -> None:
        """让 agent server 把该工作区的磁盘历史会话加载进内存索引。

        自己 spawn 的 agent 由 AGENT_RESTORE_WORKSPACES 在启动时自行恢复
        (见 start());本方法用于外部已运行实例(端口占用)与工作区切换。
        与 add_workspace_root 同为旁路调用:失败(agent 未就绪、旧版本无此
        端点、超时等)仅记 warning,不影响调用方。
        """
        _record_workspace(path)
        if not self._enabled:
            return
        try:
            _post_workspace_restore(self.agent_url, Path(path))
        except Exception as exc:  # noqa: BLE001 - 恢复失败降级为告警
            logger.warning(
                "agent runtime: failed to restore workspace %s: %s",
                path, exc,
            )

    def start(self) -> None:
        """拉起 toolserver 与 agent 服务;任一失败仅降级,不抛异常。"""
        if not self._enabled:
            logger.info("agent runtime disabled (%s)", ENABLE_ENV_VAR)
            self._states["toolserver"] = "disabled"
            self._states["agent"] = "disabled"
            return
        workspace = self._workspace or self._repo_root
        self._spawn_one(
            "toolserver",
            [
                sys.executable, "-m", "traffic_analyzer.toolserver",
                "--workspace", str(workspace),
                "--port", str(self._toolserver_port),
            ],
            cwd=self._repo_root,
            env=None,
            port=self._toolserver_port,
        )
        agent_env = dict(os.environ)
        agent_env["AGENT_PORT"] = str(self._agent_port)
        agent_env["TOOLSERVER_URL"] = self.toolserver_url
        # 让 agent 进程自己于启动时恢复当前工作区的磁盘历史会话:spawn 后
        # 立即 HTTP restore 会撞上「子进程尚未 listen」的竞态(连接被拒绝,
        # 仅 warning 不重试),导致重启后历史会话列表为空、history 404。
        agent_env["AGENT_RESTORE_WORKSPACES"] = str(workspace)
        self._spawn_one(
            "agent",
            ["npx", "tsx", "src/server/main.ts"],
            cwd=self._repo_root / "agent",
            env=agent_env,
            port=self._agent_port,
        )
        # toolserver 的 --workspace 只是初始根;把当前工作区热注册进去,
        # 之后切换工作区由 web 层调 add_workspace_root 追加,免重启。
        self.add_workspace_root(workspace)
        # 端口被外部已运行的 agent 实例占用时,该实例没有我们的
        # AGENT_RESTORE_WORKSPACES,经 HTTP 补一次 restore(实例在听,
        # 无启动竞态)。自己 spawn 的已由上面的 env 覆盖,不重复调。
        if self._states["agent"] == "port_occupied":
            self.restore_workspace(workspace)

    def stop(self) -> None:
        """对两个子进程的进程组 SIGTERM→SIGKILL;可重复调用。

        可能阻塞数秒(等待 SIGTERM 生效),async 调用方应经
        ``asyncio.to_thread`` 执行。
        """
        for name, proc in self._procs.items():
            if proc is not None and proc.poll() is None:
                _terminate_proc_group(proc)
            if proc is not None and self._states.get(name) == "running":
                self._states[name] = "not_started"
