"""Unit tests for the grounding verification step.

Covers :class:`traffic_analyzer.core.grounding_verification.GroundingVerificationStep`:
- 推翻：grounded=false 的阳性事件被 detected=False 且 note/overturned 落盘。
- 保留：grounded=true 不推翻且写入 note。
- 开关关闭：grounding_check_enable=false 时不发起 VLM 调用、结果不变。
- fail-open：VLM 调用抛普通异常时结果不变、不抛出。
- 响应缺失的阳性事件按 grounded=true 处理（不推翻）。

[文件说明]
作用:测试 GroundingVerificationStep 接地校验步骤,覆盖阳性事件推翻/保留、开关关闭、fail-open 等语义。
上游:pytest 自动发现并执行本文件测试。
下游:traffic_analyzer/core/grounding_verification.py(被测模块)。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.grounding_verification import GroundingVerificationStep
from traffic_analyzer.core.sft_label_rewrite import _build_verdicts_json
from traffic_analyzer.core.vlm_exceptions import FatalAPIError
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventInstance,
    EventResult,
    Keyframe,
    KeyframeSequence,
    SystemConfig,
    VideoMetadata,
)

# 全局 event_id = 标注文档 v4.5 的 action 编号（7 = 道路施工，2 = 应急车道占用）。
_NAME_ZH: Dict[int, str] = {
    1: "违法停车",
    2: "应急车道占用",
    7: "道路施工",
}


class _MockResponse:
    """Minimal stand-in for :class:`traffic_analyzer.models.schemas.LLMResponse`."""

    def __init__(self, parsed_data: Dict[str, Any], success: bool = True) -> None:
        self.success = success
        self.parsed_data = parsed_data
        self.raw_text = str(parsed_data)


class _MockVLMEngine:
    """Mock VLM engine that returns a fixed response or raises a fixed error."""

    def __init__(
        self,
        response: Optional[Dict[str, Any]] = None,
        success: bool = True,
        error: Optional[Exception] = None,
    ) -> None:
        self._response = response
        self._success = success
        self._error = error
        self.calls: List[Dict[str, Any]] = []

    def call(
        self,
        template: Any,
        images: List[Any],
        context_vars: Optional[Dict[str, Any]] = None,
        response_schema: Optional[Dict[str, Any]] = None,
    ) -> _MockResponse:
        self.calls.append(
            {
                "template_id": getattr(template, "template_id", "unknown"),
                "images": images,
                "context_vars": context_vars,
                "response_schema": response_schema,
            }
        )
        if self._error is not None:
            raise self._error
        return _MockResponse(self._response or {}, success=self._success)


def _make_event_results(detected_ids: tuple = (7,)) -> Dict[int, EventResult]:
    results: Dict[int, EventResult] = {}
    for eid, name in _NAME_ZH.items():
        detected = eid in detected_ids
        instances = (
            [
                EventInstance(
                    event_id=eid,
                    event_name=name,
                    start_time_sec=1.0,
                    end_time_sec=5.0,
                    description=f"{name}实例描述",
                    reasoning=f"{name}实例推理",
                )
            ]
            if detected
            else []
        )
        results[eid] = EventResult(
            event_id=eid,
            event_name=name,
            detected=detected,
            summary=f"{name}总结",
            instances=instances,
        )
    return results


def _make_resp_data(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"grounding_results": entries}


def _make_context(
    config: SystemConfig,
    detected_ids: tuple = (7,),
    num_frames: int = 6,
) -> AnalysisContext:
    return AnalysisContext(
        config=config,
        video_meta=VideoMetadata(
            file_path="/tmp/test_video.mp4",
            file_name="test_video.mp4",
            duration_sec=float(num_frames),
            fps=1.0,
            total_frames=num_frames,
            width=1280,
            height=720,
        ),
        keyframes=KeyframeSequence(
            coarse_frames=[
                Keyframe(frame_id=i, timestamp_sec=float(i), image_data=b"fake")
                for i in range(num_frames)
            ],
            precision_frames=[],
        ),
        event_results=_make_event_results(detected_ids),
    )


@pytest.fixture
def config_manager() -> ConfigManager:
    manager = ConfigManager("./traffic_analyzer/config")
    manager.load_all()
    return manager


class TestBuildVerdictsJson:
    def test_only_positive_events_serialized(self, config_manager: ConfigManager) -> None:
        """核验 prompt 只包含阳性事件（与 sft_label_rewrite 的字段口径一致）。"""
        verdicts = json.loads(
            _build_verdicts_json(
                _make_event_results(detected_ids=(7,)),
                config_manager.get_event_categories(),
                only_positive=True,
            )
        )

        assert [v["event_id"] for v in verdicts] == [7]
        assert verdicts[0]["event_name"] == "道路施工"
        assert verdicts[0]["summary"] == "道路施工总结"
        assert verdicts[0]["instances"][0]["start_time_sec"] == 1.0


class TestGroundingVerificationStep:
    def test_overturn_ungrounded_positive(self, config_manager: ConfigManager) -> None:
        """(a) grounded=false 的阳性被推翻：detected=False、note/overturned 落盘。"""
        analysis = "查看最外侧实线以外区域，无占用目标可见，结论无法锚定。"
        engine = _MockVLMEngine(
            response=_make_resp_data(
                [{"event_id": 7, "grounded": False, "analysis": analysis}]
            )
        )
        step = GroundingVerificationStep(config_manager, engine)
        context = _make_context(SystemConfig(grounding_check_enable=True))

        records = step._execute(context)

        er = context.event_results[7]
        assert er.detected is False
        assert er.instances == []
        assert er.grounding_overturned is True
        assert er.grounding_note == analysis
        assert "锚定核验推翻" in er.summary
        assert records == [{"event_id": 7, "grounded": False, "analysis": analysis}]
        assert context.local_vars["grounding_verification"] == records

    def test_keep_grounded_positive(self, config_manager: ConfigManager) -> None:
        """(b) grounded=true 不推翻且写入 note。"""
        analysis = "来向一侧可见锥桶与作业区，关键元素齐全，结论可锚定。"
        engine = _MockVLMEngine(
            response=_make_resp_data(
                [{"event_id": 7, "grounded": True, "analysis": analysis}]
            )
        )
        step = GroundingVerificationStep(config_manager, engine)
        context = _make_context(SystemConfig(grounding_check_enable=True))

        step._execute(context)

        er = context.event_results[7]
        assert er.detected is True
        assert er.grounding_overturned is False
        assert er.grounding_note == analysis
        assert len(er.instances) == 1

    def test_call_uses_template_schema_and_positive_verdicts(
        self, config_manager: ConfigManager
    ) -> None:
        """VLM 调用使用 grounding_verification 模板、schema 与仅阳性的 verdicts_json。"""
        engine = _MockVLMEngine(
            response=_make_resp_data(
                [{"event_id": 7, "grounded": True, "analysis": "可锚定。"}]
            )
        )
        step = GroundingVerificationStep(config_manager, engine)
        context = _make_context(SystemConfig(grounding_check_enable=True))

        step._execute(context)

        assert len(engine.calls) == 1
        call = engine.calls[0]
        assert call["template_id"] == "grounding_verification"
        assert call["images"]
        assert call["response_schema"]["required"] == ["grounding_results"]
        verdicts = json.loads(call["context_vars"]["verdicts_json"])
        assert [v["event_id"] for v in verdicts] == [7]
        definitions = json.loads(call["context_vars"]["event_definitions_json"])
        active_count = sum(
            1 for c in config_manager.get_event_categories() if c.is_active
        )
        assert len(definitions) == active_count

    def test_disabled_switch_skips_vlm_call(self, config_manager: ConfigManager) -> None:
        """(c) grounding_check_enable=false：不发起 VLM 调用、结果不变。"""
        engine = _MockVLMEngine(response=_make_resp_data([]))
        step = GroundingVerificationStep(config_manager, engine)
        context = _make_context(SystemConfig(grounding_check_enable=False))

        assert step._execute(context) is None
        assert engine.calls == []
        er = context.event_results[7]
        assert er.detected is True
        assert len(er.instances) == 1
        assert er.grounding_overturned is False
        assert "grounding_verification" not in context.local_vars

    def test_vlm_call_failure_is_fail_open(self, config_manager: ConfigManager) -> None:
        """(d) VLM 调用抛普通异常：结果不变、不抛出。"""
        engine = _MockVLMEngine(error=RuntimeError("API down"))
        step = GroundingVerificationStep(config_manager, engine)
        context = _make_context(SystemConfig(grounding_check_enable=True))

        assert step._execute(context) is None
        er = context.event_results[7]
        assert er.detected is True
        assert len(er.instances) == 1
        assert er.grounding_overturned is False
        assert "grounding_verification" not in context.local_vars

    def test_fatal_api_error_propagates(self, config_manager: ConfigManager) -> None:
        engine = _MockVLMEngine(error=FatalAPIError("quota exhausted"))
        step = GroundingVerificationStep(config_manager, engine)
        context = _make_context(SystemConfig(grounding_check_enable=True))

        with pytest.raises(FatalAPIError):
            step._execute(context)

    def test_missing_positive_treated_as_grounded(
        self, config_manager: ConfigManager
    ) -> None:
        """响应中缺失的阳性 event_id 按 grounded=true 处理（不推翻）。"""
        engine = _MockVLMEngine(
            response=_make_resp_data(
                [{"event_id": 2, "grounded": True, "analysis": "无关条目。"}]
            )
        )
        step = GroundingVerificationStep(config_manager, engine)
        context = _make_context(SystemConfig(grounding_check_enable=True), detected_ids=(2, 7))

        records = step._execute(context)

        assert context.event_results[7].detected is True
        assert context.event_results[7].grounding_overturned is False
        assert records == [
            {"event_id": 2, "grounded": True, "analysis": "无关条目。"},
            {"event_id": 7, "grounded": True, "analysis": ""},
        ]

    def test_grounded_null_treated_as_grounded(
        self, config_manager: ConfigManager
    ) -> None:
        """grounded=null(键存在但值为空)不得推翻真实阳性:按可锚定处理。"""
        engine = _MockVLMEngine(
            response=_make_resp_data(
                [{"event_id": 7, "grounded": None, "analysis": "模型未给出明确结论。"}]
            )
        )
        step = GroundingVerificationStep(config_manager, engine)
        context = _make_context(SystemConfig(grounding_check_enable=True))

        records = step._execute(context)

        er = context.event_results[7]
        assert er.detected is True
        assert er.grounding_overturned is False
        assert len(er.instances) == 1
        assert records == [
            {"event_id": 7, "grounded": True, "analysis": "模型未给出明确结论。"}
        ]

    def test_bool_event_id_not_matched_to_event_1(
        self, config_manager: ConfigManager
    ) -> None:
        """event_id=true(JSON bool,True == 1)不得匹配 event 1:该阳性按
        「响应缺失」处理,不推翻。"""
        engine = _MockVLMEngine(
            response=_make_resp_data(
                [{"event_id": True, "grounded": False, "analysis": "bool id。"}]
            )
        )
        step = GroundingVerificationStep(config_manager, engine)
        context = _make_context(
            SystemConfig(grounding_check_enable=True), detected_ids=(1,)
        )

        records = step._execute(context)

        er = context.event_results[1]
        assert er.detected is True
        assert er.grounding_overturned is False
        assert records == [{"event_id": 1, "grounded": True, "analysis": ""}]

    def test_guard_skips_when_no_positive_events(
        self, config_manager: ConfigManager
    ) -> None:
        engine = _MockVLMEngine(response=_make_resp_data([]))
        step = GroundingVerificationStep(config_manager, engine)
        context = _make_context(SystemConfig(grounding_check_enable=True), detected_ids=())

        assert step._execute(context) is None
        assert engine.calls == []

    def test_guard_skips_when_no_keyframes(self, config_manager: ConfigManager) -> None:
        engine = _MockVLMEngine(response=_make_resp_data([]))
        step = GroundingVerificationStep(config_manager, engine)
        context = _make_context(SystemConfig(grounding_check_enable=True))
        context.keyframes = None

        assert step._execute(context) is None
        assert engine.calls == []
