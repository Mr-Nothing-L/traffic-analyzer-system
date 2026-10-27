"""sftforge.merger / sftforge.gt 纯函数单测。

merger:段落插入/替换/删除、answer classN 列表重建、无检出结论、输出格式与
训练样本对齐(段落间空行、</think> 与 <answer> 之间空行)。
gt:文件名真值解析(09 跳过、无法解析跳过)与 missing/extra diff。
"""

from __future__ import annotations

from traffic_analyzer.sftforge.gt import diff_predictions, gt_for_video
from traffic_analyzer.sftforge.merger import merge_output
from traffic_analyzer.sftforge.parser import parse_output

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

ORDER = (1, 2, 3, 4, 5, 6, 7, 8, 10, 11)

BASE = parse_output(
    """<think>
违法停车：未发现违法停车，各车道车辆均在行驶。

应急车道占用：扫描应急车道，未见车辆占用。
</think>

<answer>
天气：晴天
时间：白天
场景：高速公路双向主路场景。
最终结论：本视频块未检出任何事件，交通状况正常。
</answer>""",
    NAMES,
)


class TestMergeOutput:
    def test_insert_missing_paragraph_and_class(self) -> None:
        merged = merge_output(
            BASE,
            {1: "观察：应急车道内停有一辆白色面包车。结论：构成违法停车。"},
            [1],
            NAMES,
            ORDER,
        )
        out = parse_output(merged, NAMES)
        assert out.classes == [1]
        assert out.paragraphs[1].startswith("观察：应急车道内停有一辆白色面包车")
        # 原有段落保留,未提及事件以兜底阴性段补齐,think 始终 10 段
        assert out.paragraphs[2].startswith("扫描应急车道")
        assert len(out.paragraphs) == len(ORDER)

    def test_replace_paragraph_on_false_positive(self) -> None:
        merged = merge_output(
            BASE,
            {2: "经复核，画面内容不满足应急车道占用的判定条件，原检出为误报。"},
            [],
            NAMES,
            ORDER,
        )
        out = parse_output(merged, NAMES)
        assert out.classes == []
        assert "误报" in out.paragraphs[2]
        assert "最终结论：本视频块未检出任何事件，交通状况正常。" in merged

    def test_delete_paragraph(self) -> None:
        merged = merge_output(BASE, {2: None}, [], NAMES, ORDER)
        out = parse_output(merged, NAMES)
        assert 2 not in out.paragraphs
        assert 1 in out.paragraphs

    def test_format_matches_training_sample(self) -> None:
        merged = merge_output(BASE, {}, [], NAMES, ORDER)
        assert merged.startswith("<think>\n违法停车：")
        assert "\n\n应急车道占用：" in merged  # 段落间空行
        assert "\n</think>\n\n<answer>\n" in merged  # think/answer 之间空行
        assert merged.endswith("\n</answer>")
        assert "天气：晴天\n时间：白天\n场景：" in merged

    def test_answer_class_lines_sorted_with_names(self) -> None:
        merged = merge_output(BASE, {}, [11, 2, 1], NAMES, ORDER)
        assert "class1: 违法停车\nclass2: 应急车道占用\nclass11: 实线变道" in merged


class TestGt:
    def test_standard_prefix(self) -> None:
        assert gt_for_video("01-02_Event_129_1755579215119_1.mp4", NAMES) == {1, 2}
        assert gt_for_video("01-02-04-05-06-11_Event_257_1751848076373_1.mp4", NAMES) == {
            1, 2, 4, 5, 6, 11,
        }

    def test_normal_09_is_valid(self) -> None:
        assert gt_for_video("09_Event_2048_xxx.mp4", NAMES) == {9}

    def test_normal_mixed_with_event_is_invalid(self) -> None:
        assert gt_for_video("01-09_Event_2048_xxx.mp4", NAMES) is None

    def test_unparseable_skipped(self) -> None:
        assert gt_for_video("random_name.mp4", NAMES) is None

    def test_diff(self) -> None:
        assert diff_predictions({1, 3}, {1, 2}) == ([2], [3])
        assert diff_predictions({1, 2}, {1, 2}) == ([], [])

    def test_diff_normal_uses_empty_event_set(self) -> None:
        assert diff_predictions(set(), {9}) == ([], [])
        assert diff_predictions({9}, {9}) == ([], [])
        assert diff_predictions({1, 2}, {9}) == ([], [1, 2])
        assert diff_predictions({1, 9}, {9}) == ([], [1])
