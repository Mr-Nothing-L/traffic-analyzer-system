"""sftforge.engine 编排单测(fake caller,无网络)。

覆盖:正向全对直接命中、漏检 correct_missing 补段落、误报 review_extra
复核剔除、误报复核确认后保守保留至多轮结束、达 max_rounds 仍不一致→
match=False+quarantine 原因、09 正常视频跳过、forward 解析失败。
"""

from __future__ import annotations

from typing import Dict, List

import pytest

from traffic_analyzer.models.schemas import EventCategory, PromptTemplate
from traffic_analyzer.sftforge.engine import (
    TEMPLATE_CORRECT_MISSING,
    TEMPLATE_FORWARD,
    TEMPLATE_REVIEW_EXTRA,
    SftForgeEngine,
)
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
            user_prompt="复核 {{ event_name }}:{{ event_definition }}|{{ first_round_segment }}",
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


class TestSkip:
    def test_normal_09_skipped(self) -> None:
        caller = FakeCaller([])
        result = _engine(caller).run("09_Event_test_1.mp4")
        assert result.skipped is True
        assert caller.calls == []  # 跳过不发任何 VLM 调用

    def test_unparseable_filename_skipped(self) -> None:
        caller = FakeCaller([])
        result = _engine(caller).run("no_gt_prefix.mp4")
        assert result.skipped is True


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
