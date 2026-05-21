"""
LogicEngine module for the traffic analyzer framework.

Executes configurable logic chains defined in YAML for hard-case event
detection. Supports step types: vlm_call, compute, condition, cv_fusion,
loop, and aggregate.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventInstance,
    EventResult,
    LLMResponse,
    LogicChain,
    LogicStep,
    StepType,
)
from traffic_analyzer.utils.tool_call_logger import tool_call, tool_call_nested

logger = logging.getLogger(__name__)


def _parse_scene_tags(scene_description: str) -> Dict[str, str]:
    """Extract structured tags like {类别：内容} from scene_description.

    Supports several bracket styles ({ }, 【 】, [ ]) and delimiters (： : = →).
    Trailing punctuation (。，；！？.,) is stripped from values so that
    '{交通事故：无。}' is parsed as {"交通事故": "无"}.

    Returns a dict mapping tag name to content (empty string if not found).
    """
    pattern = re.compile(
        r"[\{【\[]\s*([^\{\}【】\[\]：:=→]+?)\s*[：:=→]\s*(.*?)[\}】\]]",
        re.DOTALL,
    )
    _PUNCT = "。，；！？.,;!?"
    tags: Dict[str, str] = {}
    for match in pattern.finditer(scene_description):
        key = match.group(1).strip()
        value = match.group(2).strip(_PUNCT).strip()
        if key:
            tags[key] = value
    logger.debug("Parsed scene tags: %s", tags)
    return tags


class LogicEngineError(Exception):
    """Base exception for logic engine errors."""


class StepExecutionError(LogicEngineError):
    """Raised when a logic step fails to execute."""


class VariableNotFoundError(LogicEngineError):
    """Raised when a referenced variable is not found in local_vars."""


# ---------------------------------------------------------------------------
# Variable resolution helpers
# ---------------------------------------------------------------------------


def _resolve_var(path: str, local_vars: Dict[str, Any]) -> Any:
    """Resolve a dotted variable path like 'road_directions.roads.0.normal_direction'.

    Args:
        path: Variable path, optionally wrapped in ${...} or {{...}}.
        local_vars: Current local variables dictionary.

    Returns:
        The resolved value.

    Raises:
        VariableNotFoundError: If the path cannot be resolved.
    """
    # Strip wrappers
    path = path.strip()
    for prefix, suffix in [("${", "}"), ("{{", "}}")]:
        if path.startswith(prefix) and path.endswith(suffix):
            path = path[len(prefix) : -len(suffix)].strip()

    parts = path.split(".")
    value: Any = local_vars
    for part in parts:
        if value is None:
            raise VariableNotFoundError(f"Cannot resolve '{path}': encountered None at '{part}'")
        if isinstance(value, dict):
            if part not in value:
                raise VariableNotFoundError(f"Key '{part}' not found in local_vars for path '{path}'")
            value = value[part]
        elif isinstance(value, list):
            try:
                idx = int(part)
                value = value[idx]
            except (ValueError, IndexError) as exc:
                raise VariableNotFoundError(f"Invalid list index '{part}' in path '{path}': {exc}")
        else:
            try:
                value = getattr(value, part)
            except AttributeError as exc:
                raise VariableNotFoundError(f"Attribute '{part}' not found for path '{path}': {exc}")
    return value


def _resolve_value(value: Any, local_vars: Dict[str, Any]) -> Any:
    """Recursively resolve variable references in a value.

    Strings that look like ${key} or {{key}} are resolved from local_vars.
    Lists and dicts are traversed recursively.
    """
    if isinstance(value, str):
        stripped = value.strip()
        # Single variable reference
        for prefix, suffix in [("${", "}"), ("{{", "}}")]:
            if stripped.startswith(prefix) and stripped.endswith(suffix):
                inner = stripped[len(prefix) : -len(suffix)].strip()
                return _resolve_var(inner, local_vars)
        # String with embedded references - simple substitution
        def _replacer(match: re.Match) -> str:
            key = match.group(1).strip()
            resolved = _resolve_var(key, local_vars)
            return str(resolved) if resolved is not None else ""

        value = re.sub(r"\$\{([^}]+)\}", _replacer, value)
        value = re.sub(r"\{\{([^}]+)\}\}", _replacer, value)
        return value
    if isinstance(value, list):
        return [_resolve_value(v, local_vars) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_value(v, local_vars) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# Safe expression evaluation
# ---------------------------------------------------------------------------


_SAFE_BUILTINS: Dict[str, Any] = {
    "len": len,
    "range": range,
    "enumerate": enumerate,
    "zip": zip,
    "max": max,
    "min": min,
    "sum": sum,
    "abs": abs,
    "round": round,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "list": list,
    "dict": dict,
    "set": set,
    "any": any,
    "all": all,
    "next": next,
    "json": json,
    "re": re,
}


def _safe_eval(expression: str, local_vars: Dict[str, Any]) -> Any:
    """Safely evaluate a Python expression with restricted builtins.

    Args:
        expression: Python expression string.
        local_vars: Variables available in the expression scope.

    Returns:
        The evaluated result.

    Raises:
        StepExecutionError: If evaluation fails.
    """
    try:
        compiled = compile(expression, "<logic_step>", "eval")
        return eval(compiled, {"__builtins__": _SAFE_BUILTINS}, local_vars)
    except Exception as exc:
        raise StepExecutionError(f"Expression evaluation failed: {exc}\nExpression: {expression}")


def _safe_exec(code: str, local_vars: Dict[str, Any]) -> Dict[str, Any]:
    """Safely execute Python code block and return updated locals.

    Args:
        code: Python code string (usually a function definition).
        local_vars: Variables available in the code scope.

    Returns:
        Updated locals dict after execution.
    """
    try:
        exec_globals = {"__builtins__": _SAFE_BUILTINS}
        exec_locals = dict(local_vars)
        # Make local_vars available as globals so functions defined in the code
        # can access them via their __globals__ reference.
        exec_globals.update(local_vars)
        exec(code, exec_globals, exec_locals)
        return exec_locals
    except Exception as exc:
        raise StepExecutionError(f"Code execution failed: {exc}")


# ---------------------------------------------------------------------------
# LogicEngine
# ---------------------------------------------------------------------------


class LogicEngine:
    """Executes configurable logic chains for hard-case event detection.

    Each logic chain is a sequence of steps that may involve VLM calls,
    computations, conditionals, loops, and CV data fusion.
    """

    def __init__(
        self,
        vlm_engine: Any,
        config_manager: Optional[Any] = None,
    ) -> None:
        """Initialize the LogicEngine.

        Args:
            vlm_engine: VLMInferenceEngine instance for vlm_call steps.
            config_manager: Optional ConfigManager for loading templates.
        """
        self.vlm_engine = vlm_engine
        self.config_manager = config_manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(
        self,
        logic_chain: LogicChain,
        context: AnalysisContext,
    ) -> EventResult:
        """Execute a logic chain and return the event result.

        Args:
            logic_chain: The logic chain configuration to execute.
            context: The shared analysis context.

        Returns:
            EventResult containing detection results.
        """
        # Evaluate precondition if set
        if logic_chain.precondition:
            # Parse structured tags from scene_description for easy access
            scene_tags: Dict[str, str] = {}
            if context.scene_understanding and context.scene_understanding.scene_description:
                scene_tags = _parse_scene_tags(context.scene_understanding.scene_description)
            eval_locals = {
                "event_results": {
                    eid: r.model_dump() for eid, r in context.event_results.items()
                },
                "scene_understanding": (
                    context.scene_understanding.model_dump()
                    if context.scene_understanding else {}
                ),
                "scene_tags": scene_tags,
                "video_meta": (
                    context.video_meta.model_dump()
                    if context.video_meta else {}
                ),
                "keyframes": (
                    context.keyframes.model_dump()
                    if context.keyframes else {}
                ),
                "local_vars": context.local_vars,
            }
            try:
                precondition_expr = " ".join(logic_chain.precondition.split())
                precondition_met = bool(
                    eval(precondition_expr, {"__builtins__": {}}, eval_locals)
                )
            except Exception as exc:
                logger.warning(
                    "Precondition evaluation failed for chain '%s': %s",
                    logic_chain.chain_id,
                    exc,
                )
                precondition_met = False

            if not precondition_met:
                logger.info(
                    "Precondition not met for chain '%s', skipping",
                    logic_chain.chain_id,
                )
                # Build a reasoning log even when skipped so users can see WHY
                skip_reason = (
                    f"前置条件不满足，跳过逻辑链执行。"
                    f"precondition=`{logic_chain.precondition}`"
                )
                return EventResult(
                    event_id=logic_chain.target_event_id,
                    event_name=logic_chain.name_zh
                    if logic_chain.name_zh
                    else logic_chain.name,
                    detected=False,
                    confidence=0.0,
                    summary="未触发检测条件（前置条件不满足）",
                    analysis_process=[skip_reason],
                )

        # Seed local_vars with context fields so logic chains can reference
        # ${keyframes}, ${scene_understanding}, ${cv_tracks}, ${video_meta}
        local_vars: Dict[str, Any] = {
            "keyframes": context.keyframes,
            "scene_understanding": context.scene_understanding,
            "cv_tracks": context.cv_tracks,
            "video_meta": context.video_meta,
            **context.local_vars,
        }
        step_index = 0
        steps = logic_chain.steps
        evidence_log: List[str] = []
        steps_total = len(steps)

        logger.info(
            "Executing logic chain '%s' (%d steps)",
            logic_chain.chain_id,
            len(steps),
        )

        with tool_call(
            "reasoning_chain.execute",
            event=logic_chain.chain_id,
            steps=steps_total,
        ) as _parent:
            local_vars["__tool_call_parent__"] = _parent
            local_vars["__tool_call_total__"] = steps_total
            local_vars["__tool_call_idx__"] = 0

            while 0 <= step_index < len(steps):
                step = steps[step_index]
                local_vars["__tool_call_idx__"] = step_index + 1
                try:
                    next_index = self._execute_step(
                        step, steps, local_vars, context, evidence_log
                    )
                    step_index = next_index
                except Exception as exc:
                    logger.error(
                        "Logic chain '%s' failed at step '%s': %s",
                        logic_chain.chain_id,
                        step.step_id,
                        exc,
                    )
                    # Return a failed result rather than crashing the whole pipeline
                    failed_result = EventResult(
                        event_id=logic_chain.target_event_id,
                        event_name=logic_chain.name,
                        detected=False,
                        summary=f"Logic chain execution failed at step {step.step_id}: {exc}",
                        analysis_process=evidence_log,
                    )
                    _parent.result(
                        f"detected={failed_result.detected}, "
                        f"confidence={failed_result.confidence:.2f}"
                    )
                    return failed_result

            # Build final EventResult from local_vars
            final_result = self._build_event_result(logic_chain, local_vars, evidence_log)
            _parent.result(
                f"detected={final_result.detected}, "
                f"confidence={final_result.confidence:.2f}"
            )
            return final_result

    # ------------------------------------------------------------------
    # Step dispatch
    # ------------------------------------------------------------------

    def _execute_step(
        self,
        step: LogicStep,
        all_steps: List[LogicStep],
        local_vars: Dict[str, Any],
        context: AnalysisContext,
        evidence_log: List[str],
    ) -> int:
        """Execute a single step and return the next step index.

        Args:
            step: The current step to execute.
            all_steps: Full list of steps in the chain (for navigation).
            local_vars: Mutable local variable space.
            context: Shared analysis context.
            evidence_log: List to append evidence strings.

        Returns:
            Index of the next step to execute.
        """
        step_type = step.step_type
        logger.debug("Executing step '%s' (%s)", step.step_id, step_type)

        parent = local_vars.get("__tool_call_parent__")
        total = local_vars.get("__tool_call_total__", 0)
        idx = local_vars.get("__tool_call_idx__", 0)
        step_type_name = (
            step_type.value if hasattr(step_type, "value") else str(step_type)
        )
        nested_name = f"{step_type_name}.{step.step_id}"

        if parent is None:
            return self._dispatch_step(
                step, all_steps, local_vars, context, evidence_log
            )

        with tool_call_nested(parent, idx, total, nested_name) as _sub:
            next_idx = self._dispatch_step(
                step, all_steps, local_vars, context, evidence_log
            )
            summary = evidence_log[-1] if evidence_log else "ok"
            _sub.result(summary)
            return next_idx

    def _dispatch_step(
        self,
        step: LogicStep,
        all_steps: List[LogicStep],
        local_vars: Dict[str, Any],
        context: AnalysisContext,
        evidence_log: List[str],
    ) -> int:
        """Internal: original dispatch logic from _execute_step."""
        step_type = step.step_type

        if step_type == StepType.VLM_CALL:
            return self._execute_vlm_step(step, all_steps, local_vars, context, evidence_log)
        if step_type == StepType.COMPUTE:
            return self._execute_compute_step(step, all_steps, local_vars, evidence_log)
        if step_type == StepType.CONDITION:
            return self._execute_condition_step(step, all_steps, local_vars, evidence_log)
        if step_type == StepType.CV_FUSION:
            return self._execute_cv_fusion_step(step, all_steps, local_vars, context, evidence_log)
        if step_type == StepType.LOOP:
            return self._execute_loop_step(step, all_steps, local_vars, context, evidence_log)
        if step_type == StepType.AGGREGATE:
            return self._execute_aggregate_step(step, all_steps, local_vars, evidence_log)

        raise StepExecutionError(f"Unknown step type: {step_type}")

    # ------------------------------------------------------------------
    # VLM call step
    # ------------------------------------------------------------------

    def _execute_vlm_step(
        self,
        step: LogicStep,
        all_steps: List[LogicStep],
        local_vars: Dict[str, Any],
        context: AnalysisContext,
        evidence_log: List[str],
    ) -> int:
        """Execute a VLM call step."""
        if not self.vlm_engine:
            raise StepExecutionError("VLM engine not available for vlm_call step")

        # Resolve prompt template
        template_id = step.prompt_template_id
        if not template_id:
            raise StepExecutionError(f"Step {step.step_id} missing prompt_template_id")

        template = None
        if self.config_manager:
            template = self.config_manager.get_prompt_template(template_id)
        if template is None:
            raise StepExecutionError(f"Prompt template '{template_id}' not found")

        # Resolve context variables
        resolved_context = _resolve_value(
            dict(step.context_vars_mapping), local_vars
        )
        if not isinstance(resolved_context, dict):
            resolved_context = {}

        # Resolve images
        images: List[Any] = []
        if step.input_images:
            resolved_images = _resolve_value(step.input_images, local_vars)
            if isinstance(resolved_images, list):
                # Flatten one level: input_images is a list of expressions, each
                # may resolve to a list of Keyframes (e.g. ["${keyframes.coarse_frames}"]
                # -> [[Keyframe, ...]]). We need [Keyframe, ...].
                for item in resolved_images:
                    if isinstance(item, list):
                        images.extend(item)
                    else:
                        images.append(item)
            else:
                images = [resolved_images]

        # Uniformly select max_frames if needed (before extracting actual image data)
        max_frames = 10
        if (
            self.config_manager is not None
            and self.config_manager._system_config is not None
            and self.config_manager._system_config.vlm_max_frames > 0
        ):
            max_frames = self.config_manager._system_config.vlm_max_frames
        if len(images) > max_frames:
            logger.info(
                "Uniformly selecting %d of %d frames for VLM call (vlm_max_frames)",
                max_frames,
                len(images),
            )
            indices = [int(i * (len(images) - 1) / (max_frames - 1)) for i in range(max_frames)]
            images = [images[i] for i in indices]

        # Filter to actual image data (Keyframe objects have image_data or image_path)
        actual_images: List[Any] = []
        for img in images:
            if hasattr(img, "image_data") and img.image_data is not None:
                actual_images.append(img.image_data)
            elif hasattr(img, "image_path") and img.image_path is not None:
                actual_images.append(img.image_path)
            elif isinstance(img, (str, bytes)):
                actual_images.append(img)

        # Make VLM call
        response: LLMResponse = self.vlm_engine.call(
            template=template,
            images=actual_images,
            context_vars=resolved_context,
            response_schema=step.response_schema,
        )

        # Store result
        result = response.parsed_data if response.success else {"error": response.raw_text}
        if step.output_key:
            local_vars[step.output_key] = result

        evidence_log.append(
            f"Step {step.step_id} (VLM调用): 成功={response.success}, "
            f"tokens={response.total_tokens}, 模型={response.model}"
        )

        return self._next_step_index(step.step_id, all_steps)

    # ------------------------------------------------------------------
    # Compute step
    # ------------------------------------------------------------------

    def _execute_compute_step(
        self,
        step: LogicStep,
        all_steps: List[LogicStep],
        local_vars: Dict[str, Any],
        evidence_log: List[str],
    ) -> int:
        """Execute a computation step.

        Supports three forms of compute_expression:
        1. Single-line expression → evaluated with _safe_eval.
        2. Multi-line function definition (starts with "def ") → executed with
           _safe_exec, then the defined function is auto-discovered and called.
        3. Multi-line code block with top-level ``return`` → executed with
           _safe_exec in a fresh locals dict; the returned value is extracted
           from the locals dict under the magic key ``__return_value__``.
        """
        if not step.compute_expression:
            raise StepExecutionError(f"Step {step.step_id} missing compute_expression")

        expr = step.compute_expression.strip()
        result: Any = None

        if expr.startswith("def "):
            # Multi-line function definition
            exec_locals = _safe_exec(expr, local_vars)
            # Find the defined function and call it with local_vars
            func_name = None
            for name, obj in exec_locals.items():
                if callable(obj) and name != "__builtins__":
                    func_name = name
                    break
            if func_name is None:
                raise StepExecutionError("No function found in compute_expression")
            func = exec_locals[func_name]
            # Try calling with keyword args from local_vars that match func params
            import inspect
            sig = inspect.signature(func)
            kwargs = {}
            for param_name in sig.parameters:
                if param_name in local_vars:
                    kwargs[param_name] = local_vars[param_name]
            result = func(**kwargs)
        elif "\n" in expr or "return " in expr:
            # Multi-line code block (e.g. with imports, loops, if/else, return)
            # Wrap in a function so we can capture the return value reliably.
            wrapped_code = "def __compute_fn__():\n" + "\n".join(
                "    " + line for line in expr.splitlines()
            )
            exec_locals = _safe_exec(wrapped_code, local_vars)
            func = exec_locals.get("__compute_fn__")
            if func is None:
                raise StepExecutionError("Failed to compile compute_expression wrapper")
            result = func()
        else:
            # Single-line expression
            result = _safe_eval(expr, local_vars)

        if step.output_key:
            local_vars[step.output_key] = result

        evidence_log.append(f"Step {step.step_id} (计算): 结果类型={type(result).__name__}")

        return self._next_step_index(step.step_id, all_steps)

    # ------------------------------------------------------------------
    # Condition step
    # ------------------------------------------------------------------

    def _execute_condition_step(
        self,
        step: LogicStep,
        all_steps: List[LogicStep],
        local_vars: Dict[str, Any],
        evidence_log: List[str],
    ) -> int:
        """Execute a conditional branch step."""
        if not step.condition_expression:
            raise StepExecutionError(f"Step {step.step_id} missing condition_expression")

        condition_result = _safe_eval(step.condition_expression, local_vars)
        is_true = bool(condition_result)

        # Log the evaluation so users can see the reasoning even when result is False
        branch = "true" if is_true else "false"
        next_id = step.true_next_step if is_true else step.false_next_step
        evidence_log.append(
            f"Step {step.step_id} (条件判断): 表达式=`{step.condition_expression}` 结果={branch}"
            f" (下一步={next_id})"
        )

        target_step_id = step.true_next_step if is_true else step.false_next_step
        if target_step_id:
            return self._find_step_index(target_step_id, all_steps)

        return self._next_step_index(step.step_id, all_steps)

    # ------------------------------------------------------------------
    # CV fusion step
    # ------------------------------------------------------------------

    def _execute_cv_fusion_step(
        self,
        step: LogicStep,
        all_steps: List[LogicStep],
        local_vars: Dict[str, Any],
        context: AnalysisContext,
        evidence_log: List[str],
    ) -> int:
        """Execute a CV fusion step."""
        # Resolve CV data source
        cv_data = None
        if step.cv_data_source:
            cv_data = _resolve_value(step.cv_data_source, {"cv_tracks": context.cv_tracks, **local_vars})

        # Resolve context mapping
        resolved_context = _resolve_value(
            dict(step.context_vars_mapping), local_vars
        )
        if not isinstance(resolved_context, dict):
            resolved_context = {}

        # Placeholder for actual CV fusion logic
        # In a full implementation, this would call methods on ExternalAdapter
        fusion_result = {
            "cv_data_available": cv_data is not None,
            "fusion_method": step.fusion_method,
            "inputs": resolved_context,
        }

        if step.output_key:
            local_vars[step.output_key] = fusion_result

        evidence_log.append(f"Step {step.step_id} (CV融合): 方法={step.fusion_method}")

        return self._next_step_index(step.step_id, all_steps)

    # ------------------------------------------------------------------
    # Loop step
    # ------------------------------------------------------------------

    def _execute_loop_step(
        self,
        step: LogicStep,
        all_steps: List[LogicStep],
        local_vars: Dict[str, Any],
        context: AnalysisContext,
        evidence_log: List[str],
    ) -> int:
        """Execute a loop step."""
        if not step.loop_over_key:
            raise StepExecutionError(f"Step {step.step_id} missing loop_over_key")

        collection = _resolve_value(step.loop_over_key, local_vars)
        if not isinstance(collection, list):
            collection = [collection] if collection is not None else []

        results: List[Any] = []
        max_iter = min(len(collection), step.max_iterations)

        for idx, item in enumerate(collection[:max_iter]):
            loop_locals = dict(local_vars)
            loop_locals["current_item"] = item
            loop_locals["loop_index"] = idx
            loop_locals["loop_total"] = max_iter

            if step.loop_body_chain_id and self.config_manager:
                sub_chain = self.config_manager.get_logic_chain(step.loop_body_chain_id)
                if sub_chain:
                    sub_result = self.execute(sub_chain, context)
                    results.append(sub_result)
                else:
                    logger.warning("Loop body chain '%s' not found", step.loop_body_chain_id)
            else:
                # Simple loop: just collect items
                results.append(item)

        if step.output_key:
            local_vars[step.output_key] = results

        evidence_log.append(f"Step {step.step_id} (循环): 迭代了 {max_iter} 个元素")

        return self._next_step_index(step.step_id, all_steps)

    # ------------------------------------------------------------------
    # Aggregate step
    # ------------------------------------------------------------------

    def _execute_aggregate_step(
        self,
        step: LogicStep,
        all_steps: List[LogicStep],
        local_vars: Dict[str, Any],
        evidence_log: List[str],
    ) -> int:
        """Execute an aggregate step (builds final output from local_vars)."""
        # Aggregate steps typically just map variables to the output key.
        # Missing variables are silently skipped so chains with conditional
        # branches don't crash at the final aggregation.
        resolved_context: Dict[str, Any] = {}
        for k, v in dict(step.context_vars_mapping).items():
            try:
                resolved_context[k] = _resolve_value(v, local_vars)
            except VariableNotFoundError:
                continue

        if step.output_key:
            local_vars[step.output_key] = resolved_context

        # Build a detailed log showing each mapped key and its value (truncated)
        detail_parts: List[str] = []
        for k, v in resolved_context.items():
            s = str(v)
            if len(s) > 80:
                s = s[:77] + "..."
            detail_parts.append(f"{k}={s}")
        evidence_log.append(
            f"Step {step.step_id} (聚合): 映射了 {len(resolved_context)} 个字段"
            + (" -> " + ", ".join(detail_parts) if detail_parts else "")
        )

        return self._next_step_index(step.step_id, all_steps)

    # ------------------------------------------------------------------
    # Navigation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_step_index(step_id: str, all_steps: List[LogicStep]) -> int:
        """Find the index of a step by its ID."""
        for idx, s in enumerate(all_steps):
            if s.step_id == step_id:
                return idx
        raise StepExecutionError(f"Step ID '{step_id}' not found in chain")

    @staticmethod
    def _next_step_index(current_step_id: str, all_steps: List[LogicStep]) -> int:
        """Return the index of the step after the current one."""
        current_idx = LogicEngine._find_step_index(current_step_id, all_steps)
        return current_idx + 1

    # ------------------------------------------------------------------
    # Result builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_event_result(
        logic_chain: LogicChain,
        local_vars: Dict[str, Any],
        evidence_log: List[str],
    ) -> EventResult:
        """Build an EventResult from the final local variables."""
        event_id = logic_chain.target_event_id
        event_name = logic_chain.name_zh if logic_chain.name_zh else logic_chain.name

        # Try to find result data
        result_data: Any = None
        for key in ["event_result", "final_instances", "instances", "result"]:
            if key in local_vars:
                result_data = local_vars[key]
                break

        detected = False
        instances: List[EventInstance] = []
        confidence = 0.0
        summary = ""
        reasoning = ""

        if isinstance(result_data, dict):
            detected = bool(result_data.get("detected", False))
            confidence = float(result_data.get("confidence", 0.0))
            summary = str(result_data.get("summary", ""))
            reasoning = str(result_data.get("reasoning", ""))
            raw_instances = result_data.get("instances", [])
            if isinstance(raw_instances, list):
                for inst in raw_instances:
                    if isinstance(inst, dict):
                        instances.append(
                            EventInstance(
                                event_id=event_id,
                                event_name=event_name,
                                vehicle_id=str(inst.get("vehicle_id", "")),
                                road_id=inst.get("road_id"),
                                start_time_sec=float(inst.get("start_time_sec", 0.0)),
                                end_time_sec=float(inst.get("end_time_sec", 0.0)),
                                confidence=float(inst.get("confidence", 0.0)),
                                evidence_frames=inst.get("evidence_frames", []),
                                description=str(inst.get("description", "")),
                                reasoning=str(inst.get("reasoning", "")),
                            )
                        )
        elif isinstance(result_data, list):
            # List of instances directly
            for inst in result_data:
                if isinstance(inst, dict):
                    instances.append(
                        EventInstance(
                            event_id=event_id,
                            event_name=event_name,
                            confidence=float(inst.get("confidence", 0.0)),
                            description=str(inst.get("description", "")),
                            reasoning=str(inst.get("reasoning", "")),
                        )
                    )
            detected = len(instances) > 0
            confidence = max((i.confidence for i in instances), default=0.0)

        if not summary:
            summary = (
                f"{'检测到' if detected else '未检测到'} {event_name} "
                f"({len(instances)} 个实例)"
            )

        # Append a final reasoning line so the chain logic is clear even when detected=False
        reasoning_summary = (
            f"最终结果: 是否检测={detected}, 置信度={confidence:.2f}, "
            f"实例数={len(instances)}"
        )
        if instances:
            inst_summaries = [f"{i.description[:40]}..." if len(i.description) > 40 else i.description for i in instances]
            reasoning_summary += f" | 实例: {'; '.join(inst_summaries)}"
        evidence_log.append(reasoning_summary)

        # If the VLM returned a top-level reasoning (e.g. detected=false case),
        # prepend it to the evidence log so it shows up in the report.
        if reasoning and reasoning not in evidence_log:
            evidence_log.append(f"VLM 推理: {reasoning}")

        return EventResult(
            event_id=event_id,
            event_name=event_name,
            detected=detected,
            instances=instances,
            summary=summary,
            confidence=confidence,
            reasoning=reasoning,
            analysis_process=evidence_log,
        )
