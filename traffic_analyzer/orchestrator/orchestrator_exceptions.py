"""Exceptions used by the analysis orchestrator.

[文件说明]
作用:定义编排层异常基类 OrchestratorError。
上游:orchestrator/analysis_orchestrator.py(import)、tests/test_orchestrator.py。
下游:无(仅继承 Exception)。
"""


class OrchestratorError(Exception):
    """Base exception for orchestration errors."""
