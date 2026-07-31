"""Tool-call style INFO logging helpers.

Provides context managers that emit "agent-like" log lines for key
operations in the analysis pipeline, without changing any business
logic. Controlled by the TRAFFIC_ANALYZER_TOOL_LOG_LEVEL env var.

Levels:
- off:    no output
- macro:  top-level only (no nested step[i/N])
- mid:    top-level + nested (default)
- fine:   reserved for future VLM-call-level instrumentation

[文件说明]
作用:提供 ``tool_call`` 上下文管理器,为分析流水线关键
    操作输出带缩进、参数与耗时的 INFO 日志行,不改变任何业务逻辑。
上游:orchestrator/analysis_orchestrator.py。
下游:无第三方依赖;输出级别由环境变量 TRAFFIC_ANALYZER_TOOL_LOG_LEVEL
    (off/macro/mid/fine)控制,日志走 logger ``traffic_analyzer.tool_call``。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

LOG = logging.getLogger("traffic_analyzer.tool_call")


def _level() -> str:
    return os.getenv("TRAFFIC_ANALYZER_TOOL_LOG_LEVEL", "mid").lower()


class ToolCall:
    """Context manager that logs a single tool_call line on enter and a
    result line on exit, with automatic timing.

    Use via the ``tool_call(name, **args)`` factory.
    """

    def __init__(self, name: str, *, indent: int = 0, **args: Any) -> None:
        self.name = name
        self.args = args
        self.indent = indent
        self._result: Optional[str] = None
        self._t0: float = 0.0
        self._silenced: bool = False

    def __enter__(self) -> "ToolCall":
        lvl = _level()
        if lvl == "off":
            self._silenced = True
            return self
        if lvl == "macro" and self.indent > 0:
            self._silenced = True
            return self
        self._t0 = time.monotonic()
        args_str = ", ".join(f"{k}={self._fmt(v)}" for k, v in self.args.items())
        LOG.info("%s\U0001F527 tool_call: %s(%s)", " " * self.indent, self.name, args_str)
        return self

    def result(self, summary: str) -> None:
        self._result = summary

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._silenced:
            return False
        elapsed = time.monotonic() - self._t0
        prefix = " " * (self.indent + 2)
        if exc_type is None:
            summary = self._truncate(self._result or "ok")
            LOG.info("%s↳ result: %s | elapsed=%.1fs", prefix, summary, elapsed)
        else:
            LOG.info("%s✗ failed: %s | elapsed=%.1fs", prefix, exc_type.__name__, elapsed)
        return False  # do not suppress exceptions

    @staticmethod
    def _fmt(v: Any) -> str:
        if isinstance(v, str):
            return f"'{v}'"
        if isinstance(v, list) and len(v) > 4:
            return f"[{len(v)} items]"
        return repr(v)

    @staticmethod
    def _truncate(s: str, n: int = 60) -> str:
        return s if len(s) <= n else s[: n - 3] + "..."


def tool_call(name: str, **args: Any) -> ToolCall:
    """Create a top-level ToolCall context manager."""
    return ToolCall(name, **args)
