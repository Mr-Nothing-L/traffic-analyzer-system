"""Agent runtime subprocess lifecycle (toolserver + TS agent server).

[文件说明]
作用:agent 运行时子进程生命周期管理(AgentRuntimeManager)。start() 拉起
两个子进程:① Python 视频工具服务 ``python3 -m traffic_analyzer.toolserver
--workspace <当前workspace或项目根> --port 8601``;② TS agent 服务
``npx tsx src/server/main.ts``(cwd=agent/,env 注入 AGENT_PORT=8602 与
TOOLSERVER_URL)。端口被占用 / Popen 失败仅记日志降级(state 记为
port_occupied/failed),不影响 web 其他功能;/api/agent/health 据此与下游
探测报告 unavailable。stop() 复用 jobs 的终止语义(SIGTERM→SIGKILL)。
AGENT_RUNTIME_ENABLE(默认 true)可整体关闭。子进程 stdout 由守护线程
逐行 drain 进 logger,避免管道写满阻塞子进程。
上游:web/agentproxy/__init__.py(聚合导出);web/app.py(lifespan 调
start/stop);web/agentproxy/routes.py(读取 agent_url/toolserver_url 与
enabled)。
下游:web/jobs/job.py 的 REPO_ROOT/_terminate_proc;traffic_analyzer
toolserver;agent/src/server/main.ts。
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from traffic_analyzer.web.jobs.job import REPO_ROOT, _terminate_proc

logger = logging.getLogger(__name__)

ENABLE_ENV_VAR = "AGENT_RUNTIME_ENABLE"
DEFAULT_TOOLSERVER_PORT = 8601
DEFAULT_AGENT_PORT = 8602

_FALSE_VALUES = {"0", "false", "no", "off"}


def runtime_enabled() -> bool:
    """``AGENT_RUNTIME_ENABLE`` 开关:默认 true,0/false/no/off 关闭。"""
    raw = os.environ.get(ENABLE_ENV_VAR)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSE_VALUES


def _port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """Loopback TCP 探测:能连上即视为端口被占用。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _drain_stdout(proc: subprocess.Popen, name: str) -> None:
    """守护线程:逐行 drain 子进程 stdout 进 logger(防管道写满阻塞)。"""
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            logger.info("[%s] %s", name, line.rstrip("\n"))
    except (OSError, ValueError):  # 管道关闭/进程退出
        pass


class AgentRuntimeManager:
    """toolserver + TS agent server 两个子进程的生命周期。

    ``workspace``: toolserver 的 --workspace(未选工作区时用项目根)。
    ``enabled``: None 时读 AGENT_RUNTIME_ENABLE(默认 true)。
    ``spawn``/``port_probe`` 可注入,便于测试不起真实子进程。
    """

    def __init__(
        self,
        workspace: Optional[Path] = None,
        *,
        enabled: Optional[bool] = None,
        agent_port: int = DEFAULT_AGENT_PORT,
        toolserver_port: int = DEFAULT_TOOLSERVER_PORT,
        repo_root: Path = REPO_ROOT,
        spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
        port_probe: Callable[[int], bool] = _port_in_use,
    ) -> None:
        self._workspace = workspace
        self._enabled = runtime_enabled() if enabled is None else enabled
        self._agent_port = agent_port
        self._toolserver_port = toolserver_port
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
        self._spawn_one(
            "agent",
            ["npx", "tsx", "src/server/main.ts"],
            cwd=self._repo_root / "agent",
            env=agent_env,
            port=self._agent_port,
        )

    def stop(self) -> None:
        """SIGTERM→SIGKILL 终止两个子进程(jobs 的终止语义);可重复调用。

        可能阻塞数秒(等待 SIGTERM 生效),async 调用方应经
        ``asyncio.to_thread`` 执行。
        """
        for name, proc in self._procs.items():
            if proc is not None and proc.poll() is None:
                _terminate_proc(proc)
            if proc is not None and self._states.get(name) == "running":
                self._states[name] = "not_started"
