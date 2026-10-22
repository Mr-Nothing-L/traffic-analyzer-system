"""sftforge.parser 纯函数单测。

覆盖:<think>/<answer> 结构解析、按事件标题切分段落(含「车辆逆行/倒车」
带斜杠事件名)、classN 列表提取(半角/全角冒号)、纠错响应单事件段落提取、
复核结论解析。
"""

from __future__ import annotations

import pytest

from traffic_analyzer.sftforge.parser import (
    OutputParseError,
    parse_correction_json,
    parse_event_segment,
    parse_forward_json,
    parse_output,
    parse_review_verdict,
)

NAMES = {
    1: "违法停车",
    2: "应急车道占用",
    3: "交通事故",
    4: "高速公路行人出现",
    5: "摩托车出现",
    6: "拥堵",
    7: "道路施工",
    8: "车辆逆行/倒车",
    9: "无异常事件",
    10: "抛洒物",
    11: "实线变道",
}

SAMPLE_OUTPUT = """<think>
违法停车：去向一侧应急车道内停有一辆小型车。观察：画面右侧最外白色实线以外停有一辆白色面包车。分析：连续帧中位置未变，保持静止。结论：构成违法停车。

应急车道占用：去向一侧应急车道内有小型车占用。观察：白色面包车位于实线右侧。分析：帧间位置不变。结论：构成应急车道占用。

交通事故：扫描全画面各车道，未见车辆碰撞、翻车、起火冒烟等事故特征，不符合交通事故的判定条件。

车辆逆行/倒车：观察双向车流方向，均与各自道路正常流向一致。
逐帧对比未见任何车辆反向移动，不构成逆行或倒车。

实线变道：画面中各车辆均在各自车道内行驶，未见跨越实线的变道行为。
</think>

<answer>
天气：晴天
时间：白天
场景：高速公路双向主路场景，无匝道/导流区/收费口，有来向车道与去向车道，车流量大。
最终结论：本视频块检出以下事件。
class1: 违法停车
class2: 应急车道占用
</answer>"""


class TestParseOutput:
    def test_full_sample(self) -> None:
        out = parse_output(SAMPLE_OUTPUT, NAMES)
        assert out.classes == [1, 2]
        assert out.weather == "晴天"
        assert out.time_of_day == "白天"
        assert out.scene.startswith("高速公路双向主路场景")
        assert set(out.paragraphs) == {1, 2, 3, 8, 11}
        assert out.paragraphs[1].startswith("去向一侧应急车道内停有一辆小型车")
        # 带斜杠的事件名也能正确切分;多行段落保留换行
        assert "逐帧对比未见任何车辆反向移动" in out.paragraphs[8]

    def test_missing_think_raises(self) -> None:
        with pytest.raises(OutputParseError):
            parse_output("<answer>\n天气：晴天\n</answer>", NAMES)

    def test_missing_answer_raises(self) -> None:
        with pytest.raises(OutputParseError):
            parse_output("<think>\n违法停车：未发现。\n</think>", NAMES)

    def test_fullwidth_colon_class_lines(self) -> None:
        text = SAMPLE_OUTPUT.replace("class1: 违法停车", "class1：违法停车")
        out = parse_output(text, NAMES)
        assert out.classes == [1, 2]

    def test_untagged_output_tolerated(self) -> None:
        """端点 chat template 预填 think 致模型省略字面标签时,以首个天气行分段。"""
        untagged = (
            SAMPLE_OUTPUT.replace("<think>\n", "")
            .replace("\n</think>\n\n<answer>\n", "\n")
            .replace("\n</answer>", "")
        )
        out = parse_output(untagged, NAMES)
        assert out.classes == [1, 2]
        assert out.paragraphs[1].startswith("去向一侧")
        assert out.weather == "晴天"

    def test_answer_only_response_raises(self) -> None:
        """只有 answer 字段、无任何事件段落的响应不能用于训练(无思维链)。"""
        with pytest.raises(OutputParseError):
            parse_output("天气：晴天\n时间：白天\n场景：高速公路。\n最终结论：本视频块未检出任何事件，交通状况正常。", NAMES)

    def test_numbered_bold_headers_tolerated(self) -> None:
        """模型偶发输出「4. **事件名**：」式编号/加粗标题,归一后照常切分。"""
        text = SAMPLE_OUTPUT.replace(
            "违法停车：去向一侧", "1. **违法停车**：去向一侧"
        ).replace(
            "车辆逆行/倒车：观察双向车流方向", "8.  **车辆逆行/倒车**：观察双向车流方向"
        )
        out = parse_output(text, NAMES)
        assert out.paragraphs[1].startswith("去向一侧")
        assert "逐帧对比" in out.paragraphs[8]

    def test_no_detection_conclusion(self) -> None:
        text = SAMPLE_OUTPUT.replace(
            "最终结论：本视频块检出以下事件。\nclass1: 违法停车\nclass2: 应急车道占用",
            "最终结论：本视频块未检出任何事件，交通状况正常。",
        )
        out = parse_output(text, NAMES)
        assert out.classes == []


class TestParseEventSegment:
    def test_with_think_wrapper(self) -> None:
        resp = "<think>\n应急车道占用：观察：一辆白色面包车静止于应急车道。结论：构成占用。\n</think>"
        seg = parse_event_segment(resp, 2, NAMES)
        assert seg.startswith("观察：一辆白色面包车")

    def test_bare_header(self) -> None:
        resp = "应急车道占用：观察：一辆白色面包车静止于应急车道。结论：构成占用。"
        seg = parse_event_segment(resp, 2, NAMES)
        assert seg.startswith("观察：")

    def test_plain_text_and_verdict_line_stripped(self) -> None:
        resp = "经复核，画面中车辆均在行驶，原检出为误报。\n复核结论：误报"
        seg = parse_event_segment(resp, 2, NAMES)
        assert "复核结论" not in seg
        assert "误报" in seg

    def test_empty_response(self) -> None:
        assert parse_event_segment("", 2, NAMES) == ""


class TestParseReviewVerdict:
    def test_confirm(self) -> None:
        assert parse_review_verdict("……\n复核结论：确认") is True

    def test_false_positive(self) -> None:
        assert parse_review_verdict("……\n复核结论：误报") is False

    def test_missing_is_none(self) -> None:
        assert parse_review_verdict("没有结论行的响应") is None


class TestParseForwardJson:
    def test_full_json(self) -> None:
        import json

        raw = json.dumps(
            {
                "weather": "晴天",
                "time_of_day": "白天",
                "scene": "高速公路双向主路场景。",
                "event_thoughts": [
                    {"event_id": 1, "present": True, "thinking": "观察：面包车静止。"},
                    {"event_id": 2, "present": False, "thinking": "未见占用。"},
                ],
            },
            ensure_ascii=False,
        )
        out = parse_forward_json(raw, NAMES)
        assert out.classes == [1]
        assert out.paragraphs == {1: "观察：面包车静止。", 2: "未见占用。"}
        assert out.weather == "晴天"

    def test_json_inside_markdown_fence(self) -> None:
        raw = '```json\n{"weather": "阴天", "event_thoughts": [{"event_id": 1, "present": false, "thinking": "未发现。"}]}\n```'
        out = parse_forward_json(raw, NAMES)
        assert out.classes == []
        assert out.paragraphs[1] == "未发现。"

    def test_non_json_raises(self) -> None:
        with pytest.raises(OutputParseError):
            parse_forward_json("纯文本响应", NAMES)

    def test_empty_thoughts_raises(self) -> None:
        with pytest.raises(OutputParseError):
            parse_forward_json('{"event_thoughts": []}', NAMES)


class TestParseCorrectionJson:
    def test_thinking_only(self) -> None:
        data = parse_correction_json('{"thinking": "观察：……。结论：构成占用。"}')
        assert data == {"thinking": "观察：……。结论：构成占用。", "verdict": None}

    def test_with_verdict(self) -> None:
        data = parse_correction_json('{"verdict": "false_positive", "thinking": "误报。"}')
        assert data["verdict"] == "false_positive"
        assert data["thinking"] == "误报。"

    def test_non_json_returns_none(self) -> None:
        assert parse_correction_json("应急车道占用：纯文本段落") is None
