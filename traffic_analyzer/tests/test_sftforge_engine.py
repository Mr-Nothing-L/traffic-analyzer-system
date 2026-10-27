"""sftforge.engine 编排单测(fake caller,无网络)。

覆盖:正向全对直接命中、漏检 correct_missing 补段落、误报 review_extra
复核剔除、误报复核确认后保守保留至多轮结束、达 max_rounds 仍不一致→
match=False+quarantine 原因、09 正常真值直接一致/误报剔除/确认后隔离、
真实 YAML 的正常与普通复核 Prompt 分支、forward 解析失败、
r0_match 字段(R0 直接一致/纠错后一致/失败保持 None)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import pytest
import yaml

from traffic_analyzer.core.vlm_engine import VLMInferenceEngine
from traffic_analyzer.models.schemas import EventCategory, PromptTemplate
from traffic_analyzer.sftforge.engine import (
    TEMPLATE_CORRECT_MISSING,
    TEMPLATE_FORWARD,
    TEMPLATE_REVIEW_EXTRA,
    SftForgeEngine,
)
from traffic_analyzer.sftforge.models import CallerResult
from traffic_analyzer.sftforge.draft import DraftResult, DraftError
from traffic_analyzer.sftforge.parser import parse_output

NAMES = {
    1: "违法停车",
    2: "应急车道占用",
    9: "无异常事件",
}


def _categories() -> List[EventCategory]:
    return [
        EventCategory(
            event_id=1, event_code="A", name="Illegal Parking",
            name_zh="违法停车", description="机动车静止停放", definition="违停判定定义",
        ),
        EventCategory(
            event_id=2, event_code="B", name="Emergency Lane Occupancy",
            name_zh="应急车道占用", description="占用应急车道", definition="占用判定定义",
        ),
    ]


def _templates() -> Dict[str, PromptTemplate]:
    return {
        TEMPLATE_FORWARD: PromptTemplate(
            template_id=TEMPLATE_FORWARD, name="fwd",
            user_prompt="正向 {{ event_definitions }}",
        ),
        TEMPLATE_CORRECT_MISSING: PromptTemplate(
            template_id=TEMPLATE_CORRECT_MISSING, name="cm",
            user_prompt="补 {{ event_name }}:{{ event_definition }}",
        ),
        TEMPLATE_REVIEW_EXTRA: PromptTemplate(
            template_id=TEMPLATE_REVIEW_EXTRA, name="re",
            user_prompt="复核 {{ event_name }}:{{ event_definition }}|{{ first_round_segment }}|正常={{ normal_ground_truth }}",
        ),
    }


def _make_output(paragraphs: Dict[int, str], classes: List[int]) -> str:
    think = "\n\n".join(f"{NAMES[e]}：{t}" for e, t in paragraphs.items())
    lines = ["天气：晴天", "时间：白天", "场景：高速公路双向主路场景。"]
    if classes:
        lines.append("最终结论：本视频块检出以下事件。")
        lines += [f"class{e}: {NAMES[e]}" for e in sorted(classes)]
    else:
        lines.append("最终结论：本视频块未检出任何事件，交通状况正常。")
    return f"<think>\n{think}\n</think>\n\n<answer>\n" + "\n".join(lines) + "\n</answer>"


class FakeCaller:
    """按队列返回响应并记录调用;队列耗尽或遇到 Exception 实例时抛出。"""

    def __init__(self, responses: List[object]) -> None:
        self._responses = list(responses)
        self.calls: List[tuple] = []

    def __call__(self, system: str, user: str, video_path) -> str:
        self.calls.append((system, user, str(video_path)))
        assert self._responses, f"unexpected call: {user[:40]}"
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]


def _engine(caller: FakeCaller, max_rounds: int = 2) -> SftForgeEngine:
    return SftForgeEngine(
        caller=caller,
        templates=_templates(),
        categories=_categories(),
        max_rounds=max_rounds,
        names=NAMES,
    )


class TestForwardAllCorrect:
    def test_match_without_correction(self) -> None:
        raw = _make_output({1: "观察：面包车静止。结论：构成违法停车。", 2: "观察：面包车占用。结论：构成占用。"}, [1, 2])
        caller = FakeCaller([raw])
        result = _engine(caller).run("01-02_Event_test_1.mp4")
        assert result.match is True
        assert result.skipped is False
        assert result.gt == [1, 2]
        # 最终输出由 merger 代码重建为规范标签结构,段落正文保持模型原文
        out = parse_output(result.final_output, NAMES)
        assert out.classes == [1, 2]
        assert result.final_output.startswith("<think>\n违法停车：观察：面包车静止。")
        assert "\n</think>\n\n<answer>\n" in result.final_output
        assert len(result.rounds) == 1
        assert result.rounds[0].kind == "forward"
        assert result.rounds[0].round_no == 0


class TestCallerResultReasoning:
    """caller 返回 CallerResult 时,reasoning 落进 RoundTrace;裸 str 兼容。"""

    def test_reasoning_recorded_on_trace(self) -> None:
        raw = _make_output({1: "观察：静止。", 2: "观察：占用。"}, [1, 2])
        caller = FakeCaller([CallerResult(text=raw, reasoning="隐藏推理:先扫应急车道…")])
        result = _engine(caller).run("01-02_Event_test_1.mp4")
        trace = result.rounds[0]
        assert trace.reasoning == "隐藏推理:先扫应急车道…"
        assert trace.response == raw

    def test_plain_str_caller_leaves_reasoning_empty(self) -> None:
        raw = _make_output({1: "观察：静止。", 2: "观察：占用。"}, [1, 2])
        caller = FakeCaller([raw])
        result = _engine(caller).run("01-02_Event_test_1.mp4")
        assert result.rounds[0].reasoning == ""


class TestOnDeltaHook:
    """on_delta 钩子:绑定 video/round_no/kind 上下文透传给 caller。"""

    def test_deltas_carry_round_context(self) -> None:
        raw = _make_output({1: "观察：静止。", 2: "观察：占用。"}, [1, 2])

        def caller(system: str, user: str, video_path, on_delta=None):
            assert on_delta is not None
            on_delta("reasoning", "思")
            on_delta("response", "答")
            return raw

        deltas: List[tuple] = []
        engine = SftForgeEngine(
            caller=caller,
            templates=_templates(),
            categories=_categories(),
            max_rounds=2,
            names=NAMES,
            on_delta=lambda info, field, text: deltas.append((info, field, text)),
        )
        result = engine.run("01-02_Event_test_1.mp4")
        assert result.match is True
        assert [(d[1], d[2]) for d in deltas] == [("reasoning", "思"), ("response", "答")]
        info = deltas[0][0]
        assert info["round_no"] == 0 and info["kind"] == "forward"
        assert str(info["video"]).endswith("01-02_Event_test_1.mp4")


class TestDraftProviderMode:
    """draft_provider 注入时,R0 走批量链路初稿(不调 forward caller)。"""

    def _engine_with_draft(self, draft_provider, on_draft_step=None) -> SftForgeEngine:
        # FakeCaller 空队列:任何 forward 调用都会断言失败,证明 R0 未走 caller
        return SftForgeEngine(
            caller=FakeCaller([]),
            templates=_templates(),
            categories=_categories(),
            max_rounds=2,
            names=NAMES,
            draft_provider=draft_provider,
            on_draft_step=on_draft_step,
        )

    def test_draft_text_used_as_forward(self) -> None:
        raw = _make_output({1: "观察：静止。", 2: "观察：占用。"}, [1, 2])
        draft = DraftResult(output=raw, classes=[1, 2])
        result = self._engine_with_draft(lambda vp, on_step=None: draft).run(
            "01-02_Event_test_1.mp4"
        )
        assert result.match is True
        assert result.rounds[0].kind == "forward"
        assert "批量推理链路" in result.rounds[0].prompt
        assert result.rounds[0].response == raw

    def test_draft_failure_marks_reason(self) -> None:
        def failing(vp, on_step=None):
            raise DraftError("pipeline_quarantine: 隔离")

        result = self._engine_with_draft(failing).run("01-02_Event_test_1.mp4")
        assert result.match is False
        assert result.reason == "draft_failed"
        assert "pipeline_quarantine" in result.rounds[0].response

    def test_on_draft_step_receives_video(self) -> None:
        raw = _make_output({1: "观察：静止。", 2: "观察：占用。"}, [1, 2])
        steps: List[dict] = []

        def provider(vp, on_step=None):
            assert on_step is not None
            on_step({"type": "step", "step": 1, "total": 4, "name": "视频预处理"})
            return DraftResult(output=raw, classes=[1, 2])

        engine = self._engine_with_draft(provider, on_draft_step=steps.append)
        engine.run("01-02_Event_test_1.mp4")
        assert steps[0]["type"] == "step"
        assert str(steps[0]["video"]).endswith("01-02_Event_test_1.mp4")


class TestMissingCorrection:
    def test_missing_event_patched(self) -> None:
        forward = _make_output(
            {1: "观察：面包车静止。结论：构成违法停车。", 2: "扫描应急车道，未见占用。"},
            [1],
        )
        correction = "应急车道占用：观察：一辆白色面包车静止于应急车道。分析：帧间位置不变。结论：构成应急车道占用。"
        caller = FakeCaller([forward, correction])
        result = _engine(caller).run("01-02_Event_test_1.mp4")

        assert result.match is True
        assert [t.kind for t in result.rounds] == ["forward", "correct_missing"]
        trace = result.rounds[1]
        assert trace.round_no == 1 and trace.event_ids == [2] and trace.patched is True

        out = parse_output(result.final_output, NAMES)
        assert out.classes == [1, 2]
        assert out.paragraphs[2].startswith("观察：一辆白色面包车静止于应急车道")
        assert "class2: 应急车道占用" in result.final_output

    def test_correction_prompt_carries_event_info(self) -> None:
        forward = _make_output({1: "观察：静止。", 2: "未见占用。"}, [1])
        caller = FakeCaller([forward, "应急车道占用：观察：……。结论：构成占用。"])
        _engine(caller).run("01-02_Event_test_1.mp4")
        cm_user_prompt = caller.calls[1][1]
        assert "应急车道占用" in cm_user_prompt and "占用判定定义" in cm_user_prompt
        # 正向 prompt 不得注入真值信息(盲推)
        assert "真值" not in caller.calls[0][1]


class TestExtraReview:
    def test_false_positive_dropped(self) -> None:
        forward = _make_output(
            {1: "观察：面包车静止。结论：构成违法停车。", 2: "观察：疑似占用。结论：构成占用。"},
            [1, 2],
        )
        review = "应急车道占用：经复核，该车位于行车道内而非应急车道，原检出为误报。\n复核结论：误报"
        caller = FakeCaller([forward, review])
        result = _engine(caller).run("01_Event_test_1.mp4")

        assert result.match is True
        assert [t.kind for t in result.rounds] == ["forward", "review_extra"]
        assert result.rounds[1].patched is True
        out = parse_output(result.final_output, NAMES)
        assert out.classes == [1]
        # 误报段落被否定段替换(think 仍保留该事件段,而非整段删除)
        assert "误报" in out.paragraphs[2]
        assert "class2" not in result.final_output

    def test_review_confirmed_kept_then_quarantine(self) -> None:
        forward = _make_output(
            {1: "观察：静止。", 2: "观察：疑似占用。结论：构成占用。"}, [1, 2]
        )
        review = "应急车道占用：观察：……。结论：复核后仍构成占用。\n复核结论：确认"
        caller = FakeCaller([forward, review])
        result = _engine(caller).run("01_Event_test_1.mp4")

        # 复核确认=保守保留;该轮补丁为空,继续重试不会有进展,提前终止并隔离
        assert result.match is False
        assert "extra=[2]" in (result.reason or "")
        assert [t.kind for t in result.rounds] == ["forward", "review_extra"]
        assert result.rounds[1].patched is False


class TestQuarantine:
    def test_max_rounds_exhausted(self) -> None:
        forward = _make_output({1: "观察：静止。", 2: "未见占用。"}, [1])
        # correct_missing 返回空响应 → 无法提取段落 → 补丁为空,终止纠错
        caller = FakeCaller([forward, ""])
        result = _engine(caller).run("01-02_Event_test_1.mp4")
        assert result.match is False
        assert "missing=[2]" in (result.reason or "")

    def test_forward_parse_failed(self) -> None:
        caller = FakeCaller(["这不是合法的样本输出"])
        result = _engine(caller).run("01-02_Event_test_1.mp4")
        assert result.match is False
        assert (result.reason or "").startswith("forward_parse_failed")

    def test_caller_exception_recorded_in_trace(self) -> None:
        forward = _make_output({1: "观察：静止。", 2: "未见占用。"}, [1])
        caller = FakeCaller([forward, RuntimeError("boom"), RuntimeError("boom")])
        result = _engine(caller).run("01-02_Event_test_1.mp4")
        assert result.match is False
        assert "[ERROR] RuntimeError" in result.rounds[1].response

    def test_all_providers_exhausted_reraised(self) -> None:
        """全停型错误必须上抛终止批处理,不得被吞成单视频 quarantine。"""
        from traffic_analyzer.core.vlm_exceptions import AllProvidersExhaustedError

        caller = FakeCaller([AllProvidersExhaustedError("all down")])
        with pytest.raises(AllProvidersExhaustedError):
            _engine(caller).run("01-02_Event_test_1.mp4")


def _render_actual_review_prompt(normal_ground_truth: bool) -> str:
    prompt_path = Path(__file__).resolve().parents[1] / "config" / "prompts" / "sft_forge.yaml"
    data = yaml.safe_load(prompt_path.read_text(encoding="utf-8"))
    raw = next(t for t in data["prompt_templates"] if t["template_id"] == TEMPLATE_REVIEW_EXTRA)
    _, user = VLMInferenceEngine.render_prompt(
        PromptTemplate(**raw),
        {
            "event_id": 1,
            "event_name": "违法停车",
            "event_definition": "机动车静止停放",
            "first_round_segment": "观察：疑似违停。",
            "normal_ground_truth": normal_ground_truth,
        },
    )
    return user


class TestNormalGroundTruth:
    def test_normal_review_prompt_branch(self) -> None:
        user = _render_actual_review_prompt(True)
        assert "class09 正常" in user
        assert "正常样本复核要求" in user
        assert "证据不足或不完整时输出 false_positive" in user

    def test_regular_review_prompt_unchanged(self) -> None:
        user = _render_actual_review_prompt(False)
        assert "人工标注真值中不存在该类事件" in user
        assert "正常样本复核要求" not in user
        assert "class09 正常" not in user

    def test_normal_09_match_without_correction(self) -> None:
        raw = _make_output(
            {1: "扫描各车道，车辆均持续行驶。", 2: "扫描应急车道，未见占用。"}, []
        )
        caller = FakeCaller([raw])
        result = _engine(caller).run("09_Event_test_1.mp4")

        assert result.match is True
        assert result.skipped is False
        assert result.gt == [9]
        assert [t.kind for t in result.rounds] == ["forward"]
        out = parse_output(result.final_output, NAMES)
        assert out.classes == []
        assert "未检出任何事件，交通状况正常" in result.final_output

    def test_normal_09_false_positive_dropped(self) -> None:
        forward = _make_output(
            {1: "观察：面包车静止。结论：构成违法停车。", 2: "扫描应急车道，未见占用。"},
            [1],
        )
        review = '{"verdict": "false_positive", "thinking": "经复核，该车持续行驶，原违停检出为误报。"}'
        caller = FakeCaller([forward, review])
        result = _engine(caller).run("09_Event_test_1.mp4")

        assert result.match is True
        assert result.skipped is False
        assert result.gt == [9]
        assert [t.kind for t in result.rounds] == ["forward", "review_extra"]
        out = parse_output(result.final_output, NAMES)
        assert out.classes == []
        assert "误报" in out.paragraphs[1]
        assert "未检出任何事件，交通状况正常" in result.final_output
        assert "正常=True" in caller.calls[1][1]

    def test_normal_09_confirmed_extra_quarantined(self) -> None:
        forward = _make_output(
            {1: "观察：面包车静止。结论：构成违法停车。", 2: "扫描应急车道，未见占用。"},
            [1],
        )
        review = '{"verdict": "confirm", "thinking": "观察：面包车持续静止。结论：复核后仍构成违法停车。"}'
        caller = FakeCaller([forward, review])
        result = _engine(caller).run("09_Event_test_1.mp4")

        assert result.match is False
        assert result.skipped is False
        assert result.gt == [9]
        assert "extra=[1]" in (result.reason or "")
        assert [t.kind for t in result.rounds] == ["forward", "review_extra"]


class TestSkip:
    def test_unparseable_filename_skipped(self) -> None:
        caller = FakeCaller([])
        result = _engine(caller).run("no_gt_prefix.mp4")
        assert result.skipped is True


class TestR0Match:
    """r0_match:区分「直接一致」(True)与「修改后一致」(False);失败/跳过为 None。"""

    def test_r0_match_true_when_forward_matches(self) -> None:
        raw = _make_output({1: "观察：静止。", 2: "观察：占用。"}, [1, 2])
        caller = FakeCaller([raw])
        result = _engine(caller).run("01-02_Event_test_1.mp4")
        assert result.match is True
        assert result.r0_match is True

    def test_r0_match_false_when_corrected_to_match(self) -> None:
        forward = _make_output({1: "观察：静止。", 2: "未见占用。"}, [1])
        correction = "应急车道占用：观察：一辆白色面包车静止于应急车道。结论：构成占用。"
        caller = FakeCaller([forward, correction])
        result = _engine(caller).run("01-02_Event_test_1.mp4")
        assert result.match is True
        assert result.r0_match is False

    def test_r0_match_false_when_final_mismatch(self) -> None:
        forward = _make_output({1: "观察：静止。", 2: "未见占用。"}, [1])
        # correct_missing 返回空 → 无法补齐,终不一致
        caller = FakeCaller([forward, ""])
        result = _engine(caller).run("01-02_Event_test_1.mp4")
        assert result.match is False
        assert result.r0_match is False

    def test_r0_match_none_on_forward_call_failure(self) -> None:
        caller = FakeCaller([RuntimeError("boom")])
        result = _engine(caller).run("01-02_Event_test_1.mp4")
        assert result.reason == "forward_call_failed"
        assert result.r0_match is None

    def test_r0_match_none_on_forward_parse_failure(self) -> None:
        caller = FakeCaller(["这不是合法的样本输出"])
        result = _engine(caller).run("01-02_Event_test_1.mp4")
        assert result.r0_match is None

    def test_r0_match_none_on_draft_failure(self) -> None:
        def failing(vp, on_step=None):
            raise DraftError("pipeline_quarantine: 隔离")

        engine = SftForgeEngine(
            caller=FakeCaller([]),
            templates=_templates(),
            categories=_categories(),
            max_rounds=2,
            names=NAMES,
            draft_provider=failing,
        )
        result = engine.run("01-02_Event_test_1.mp4")
        assert result.reason == "draft_failed"
        assert result.r0_match is None

    def test_r0_match_none_when_skipped(self) -> None:
        result = _engine(FakeCaller([])).run("no_gt_prefix.mp4")
        assert result.skipped is True
        assert result.r0_match is None


# ---------------------------------------------------------------------------
# JSON 主路径(端点实际输出形态;文本格式仅作回退,上方用例已覆盖)
# ---------------------------------------------------------------------------


def _make_forward_json(paragraphs: Dict[int, str], classes: List[int]) -> str:
    import json

    return json.dumps(
        {
            "weather": "晴天",
            "time_of_day": "白天",
            "scene": "高速公路双向主路场景。",
            "event_thoughts": [
                {"event_id": e, "present": e in classes, "thinking": t}
                for e, t in paragraphs.items()
            ],
        },
        ensure_ascii=False,
    )


class TestJsonPath:
    def test_forward_json_all_correct(self) -> None:
        raw = _make_forward_json(
            {1: "观察：面包车静止。结论：构成违法停车。", 2: "观察：面包车占用。结论：构成占用。"},
            [1, 2],
        )
        caller = FakeCaller([raw])
        result = _engine(caller).run("01-02_Event_test_1.mp4")
        assert result.match is True
        out = parse_output(result.final_output, NAMES)
        assert out.classes == [1, 2]
        assert out.weather == "晴天"
        assert result.final_output.startswith("<think>\n违法停车：观察：面包车静止。")

    def test_missing_correction_json(self) -> None:
        forward = _make_forward_json({1: "观察：静止。", 2: "未见占用。"}, [1])
        correction = '{"thinking": "观察：一辆白色面包车静止于应急车道。结论：构成应急车道占用。"}'
        caller = FakeCaller([forward, correction])
        result = _engine(caller).run("01-02_Event_test_1.mp4")
        assert result.match is True
        assert result.rounds[1].patched is True
        assert "class2: 应急车道占用" in result.final_output

    def test_review_extra_json_false_positive(self) -> None:
        forward = _make_forward_json(
            {1: "观察：静止。", 2: "观察：疑似占用。结论：构成占用。"}, [1, 2]
        )
        review = '{"verdict": "false_positive", "thinking": "经复核，该车位于行车道内，原检出为误报。"}'
        caller = FakeCaller([forward, review])
        result = _engine(caller).run("01_Event_test_1.mp4")
        assert result.match is True
        out = parse_output(result.final_output, NAMES)
        assert out.classes == [1]
        assert "误报" in out.paragraphs[2]

    def test_review_extra_json_confirm_kept(self) -> None:
        forward = _make_forward_json(
            {1: "观察：静止。", 2: "观察：疑似占用。结论：构成占用。"}, [1, 2]
        )
        review = '{"verdict": "confirm", "thinking": "复核后仍构成占用。"}'
        caller = FakeCaller([forward, review])
        result = _engine(caller).run("01_Event_test_1.mp4")
        assert result.match is False
        assert "extra=[2]" in (result.reason or "")
        assert result.rounds[1].patched is False
