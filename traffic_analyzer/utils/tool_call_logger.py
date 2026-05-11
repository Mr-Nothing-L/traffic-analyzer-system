"""Tool-call style INFO logging helpers.

Provides context managers that emit "agent-like" log lines for key
operations in the analysis pipeline, without changing any business
logic. Controlled by the TRAFFIC_ANALYZER_TOOL_LOG_LEVEL env var.

Levels:
- off:    no output
- macro:  top-level only (no nested step[i/N])
- mid:    top-level + nested (default)
- fine:   reserved for future VLM-call-level instrumentation
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


def tool_call_nested(
    parent: ToolCall, idx: int, total: int, name: str, **args: Any
) -> ToolCall:
    """Create a nested ToolCall (rendered indented under parent)."""
    return ToolCall(f"step[{idx}/{total}]: {name}", indent=parent.indent + 2, **args)
