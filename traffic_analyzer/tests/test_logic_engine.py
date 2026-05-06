"""Unit tests for the LogicEngine module."""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from traffic_analyzer.core.logic_engine import (
    LogicEngine,
    LogicEngineError,
    StepExecutionError,
    VariableNotFoundError,
    _resolve_var,
    _resolve_value,
    _safe_eval,
    _safe_exec,
)
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCategory,
    Keyframe,
    KeyframeSequence,
    LLMProviderConfig,
    LLMResponse,
    LogicChain,
    LogicStep,
    PromptTemplate,
    SceneInfo,
    StepType,
    VideoMetadata,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_vlm_engine() -> MagicMock:
    engine = MagicMock()
    engine.call.return_value = LLMResponse(
        success=True,
        raw_text='{"detected": true, "confidence": 0.85}',
        parsed_data={"detected": True, "confidence": 0.85},
        model="test-model",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )
    return engine


@pytest.fixture
def mock_config_manager() -> MagicMock:
    manager = MagicMock()
    manager.get_prompt_template.return_value = PromptTemplate(
        template_id="test_template",
        name="Test Template",
        system_prompt="You are a test assistant.",
        user_prompt="Analyze: {{ target }}",
    )
    return manager


@pytest.fixture
def logic_engine(mock_vlm_engine: MagicMock, mock_config_manager: MagicMock) -> LogicEngine:
    return LogicEngine(vlm_engine=mock_vlm_engine, config_manager=mock_config_manager)


@pytest.fixture
def empty_context() -> AnalysisContext:
    return AnalysisContext(
        video_meta=VideoMetadata(
            file_path="test.mp4",
            file_name="test.mp4",
            duration_sec=10.0,
            fps=10.0,
            total_frames=100,
            width=640,
            height=480,
        ),
        scene_understanding=SceneInfo(),
        keyframes=KeyframeSequence(),
    )


# ---------------------------------------------------------------------------
# Variable resolution
# ---------------------------------------------------------------------------


class TestResolveVar:
    def test_simple_key(self) -> None:
        local_vars = {"foo": "bar"}
        assert _resolve_var("foo", local_vars) == "bar"

    def test_nested_dict(self) -> None:
        local_vars = {"data": {"inner": {"value": 42}}}
        assert _resolve_var("data.inner.value", local_vars) == 42

    def test_list_index(self) -> None:
        local_vars = {"items": [{"name": "first"}, {"name": "second"}]}
        assert _resolve_var("items.0.name", local_vars) == "first"

    def test_object_attribute(self) -> None:
        class Obj:
            x = 10
        local_vars = {"obj": Obj()}
        assert _resolve_var("obj.x", local_vars) == 10

    def test_dollar_brace_syntax(self) -> None:
        local_vars = {"foo": "bar"}
        assert _resolve_var("${foo}", local_vars) == "bar"

    def test_double_brace_syntax(self) -> None:
        local_vars = {"foo": "bar"}
        assert _resolve_var("{{foo}}", local_vars) == "bar"

    def test_missing_key_raises(self) -> None:
        with pytest.raises(VariableNotFoundError):
            _resolve_var("missing", {})


class TestResolveValue:
    def test_string_substitution(self) -> None:
        local_vars = {"name": "World"}
        assert _resolve_value("Hello {{name}}!", local_vars) == "Hello World!"

    def test_dict_resolution(self) -> None:
        local_vars = {"a": 1}
        result = _resolve_value({"key": "${a}"}, local_vars)
        assert result == {"key": 1}

    def test_list_resolution(self) -> None:
        local_vars = {"a": 1, "b": 2}
        result = _resolve_value(["${a}", "${b}"], local_vars)
        assert result == [1, 2]

    def test_passthrough_non_string(self) -> None:
        assert _resolve_value(123, {}) == 123


# ---------------------------------------------------------------------------
# Safe evaluation
# ---------------------------------------------------------------------------


class TestSafeEval:
    def test_simple_math(self) -> None:
        assert _safe_eval("2 + 2", {}) == 4

    def test_variable_access(self) -> None:
        assert _safe_eval("x * 2", {"x": 5}) == 10

    def test_len_function(self) -> None:
        assert _safe_eval("len(items)", {"items": [1, 2, 3]}) == 3

    def test_any_function(self) -> None:
        assert _safe_eval("any([True, False])", {}) is True

    def test_unsafe_import_blocked(self) -> None:
        with pytest.raises(StepExecutionError):
            _safe_eval("__import__('os').system('ls')", {})


class TestSafeExec:
    def test_function_definition(self) -> None:
        code = "def add(a, b):\n    return a + b"
        result = _safe_exec(code, {"a": 1, "b": 2})
        assert "add" in result
        assert result["add"](1, 2) == 3

    def test_variable_modification(self) -> None:
        code = "result = x * 2"
        result = _safe_exec(code, {"x": 5})
        assert result["result"] == 10


# ---------------------------------------------------------------------------
# LogicEngine execution
# ---------------------------------------------------------------------------


class TestLogicEngineExecute:
    def test_empty_chain_returns_undetected(self, logic_engine: LogicEngine, empty_context: AnalysisContext) -> None:
        chain = LogicChain(
            chain_id="test_empty",
            name="Empty Test",
            target_event_id=0,
            steps=[],
        )
        result = logic_engine.execute(chain, empty_context)
        assert result.detected is False
        assert result.event_id == 0

    def test_compute_step(self, logic_engine: LogicEngine, empty_context: AnalysisContext) -> None:
        chain = LogicChain(
            chain_id="test_compute",
            name="Compute Test",
            target_event_id=1,
            steps=[
                LogicStep(
                    step_id="S1",
                    step_type=StepType.COMPUTE,
                    compute_expression="x + 10",
                    output_key="result",
                ),
            ],
        )
        empty_context.set_local("x", 5)
        result = logic_engine.execute(chain, empty_context)
        assert result.detected is False

    def test_condition_true_branch(self, logic_engine: LogicEngine, empty_context: AnalysisContext) -> None:
        chain = LogicChain(
            chain_id="test_condition",
            name="Condition Test",
            target_event_id=2,
            steps=[
                LogicStep(
                    step_id="S1",
                    step_type=StepType.COMPUTE,
                    compute_expression="20",
                    output_key="score",
                ),
                LogicStep(
                    step_id="S2",
                    step_type=StepType.CONDITION,
                    condition_expression="score > 10",
                    true_next_step="S3",
                ),
                LogicStep(
                    step_id="S3",
                    step_type=StepType.COMPUTE,
                    compute_expression="'high'",
                    output_key="level",
                ),
            ],
        )
        result = logic_engine.execute(chain, empty_context)
        assert result.detected is False
        assert any("S3" in entry for entry in result.analysis_process)

    def test_condition_false_branch(self, logic_engine: LogicEngine, empty_context: AnalysisContext) -> None:
        chain = LogicChain(
            chain_id="test_condition_false",
            name="Condition False Test",
            target_event_id=3,
            steps=[
                LogicStep(
                    step_id="S1",
                    step_type=StepType.COMPUTE,
                    compute_expression="5",
                    output_key="score",
                ),
                LogicStep(
                    step_id="S2",
                    step_type=StepType.CONDITION,
                    condition_expression="score > 10",
                    false_next_step="S3",
                ),
                LogicStep(
                    step_id="S3",
                    step_type=StepType.COMPUTE,
                    compute_expression="'low'",
                    output_key="level",
                ),
            ],
        )
        result = logic_engine.execute(chain, empty_context)
        assert any("S3" in entry for entry in result.analysis_process)

    def test_vlm_step(self, logic_engine: LogicEngine, empty_context: AnalysisContext) -> None:
        chain = LogicChain(
            chain_id="test_vlm",
            name="VLM Test",
            target_event_id=4,
            steps=[
                LogicStep(
                    step_id="S1",
                    step_type=StepType.VLM_CALL,
                    prompt_template_id="test_template",
                    context_vars_mapping={"target": "'vehicle'"},
                    output_key="vlm_result",
                ),
            ],
        )
        result = logic_engine.execute(chain, empty_context)
        assert result.detected is False

    def test_loop_step(self, logic_engine: LogicEngine, empty_context: AnalysisContext) -> None:
        chain = LogicChain(
            chain_id="test_loop",
            name="Loop Test",
            target_event_id=5,
            steps=[
                LogicStep(
                    step_id="S1",
                    step_type=StepType.COMPUTE,
                    compute_expression="[1, 2, 3]",
                    output_key="items",
                ),
                LogicStep(
                    step_id="S2",
                    step_type=StepType.LOOP,
                    loop_over_key="items",
                    output_key="processed",
                ),
            ],
        )
        result = logic_engine.execute(chain, empty_context)
        assert result.detected is False

    def test_aggregate_builds_result(self, logic_engine: LogicEngine, empty_context: AnalysisContext) -> None:
        chain = LogicChain(
            chain_id="test_aggregate",
            name="Aggregate Test",
            target_event_id=6,
            steps=[
                LogicStep(
                    step_id="S1",
                    step_type=StepType.COMPUTE,
                    compute_expression="{'detected': True, 'confidence': 0.9, 'instances': []}",
                    output_key="event_result",
                ),
            ],
        )
        result = logic_engine.execute(chain, empty_context)
        assert result.detected is True
        assert result.confidence == 0.9

    def test_compute_with_function(self, logic_engine: LogicEngine, empty_context: AnalysisContext) -> None:
        chain = LogicChain(
            chain_id="test_func",
            name="Function Test",
            target_event_id=7,
            steps=[
                LogicStep(
                    step_id="S1",
                    step_type=StepType.COMPUTE,
                    compute_expression="def calc(items):\n    return sum(items)\n",
                    output_key="total",
                ),
            ],
        )
        empty_context.set_local("items", [1, 2, 3])
        result = logic_engine.execute(chain, empty_context)
        # The function should have been called with available local vars
        assert result.detected is False

    def test_missing_variable_error_handled(self, logic_engine: LogicEngine, empty_context: AnalysisContext) -> None:
        chain = LogicChain(
            chain_id="test_error",
            name="Error Test",
            target_event_id=8,
            steps=[
                LogicStep(
                    step_id="S1",
                    step_type=StepType.COMPUTE,
                    compute_expression="missing_var + 1",
                ),
            ],
        )
        result = logic_engine.execute(chain, empty_context)
        assert result.detected is False
        assert "failed" in result.summary.lower()

    def test_evidence_log_accumulated(self, logic_engine: LogicEngine, empty_context: AnalysisContext) -> None:
        chain = LogicChain(
            chain_id="test_evidence",
            name="Evidence Test",
            target_event_id=9,
            steps=[
                LogicStep(
                    step_id="S1",
                    step_type=StepType.COMPUTE,
                    compute_expression="1 + 1",
                    output_key="a",
                ),
                LogicStep(
                    step_id="S2",
                    step_type=StepType.COMPUTE,
                    compute_expression="a + 1",
                    output_key="b",
                ),
            ],
        )
        result = logic_engine.execute(chain, empty_context)
        assert len(result.analysis_process) == 2
        assert "S1" in result.analysis_process[0]
        assert "S2" in result.analysis_process[1]
