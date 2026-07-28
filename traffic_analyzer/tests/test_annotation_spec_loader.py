"""
Unit tests for annotation_spec_loader.py (active-event filtering).

[文件说明]
作用:测试 AnnotationSpecLoader.to_prompt_text 的 active_event_ids 过滤行为——
  传入集合时仅输出激活事件段落(全局标注原则保留),None 时保持旧行为(全量输出)。
上游:pytest 自动发现并执行本文件测试。
下游:traffic_analyzer/utils/annotation_spec_loader.py(被测模块)。
"""

from __future__ import annotations

import yaml

from traffic_analyzer.utils.annotation_spec_loader import AnnotationSpecLoader


def _write_spec(tmp_path, events):
    spec = {
        "annotation_spec": {
            "global_guidelines": ["原则一"],
            "events": events,
        }
    }
    path = tmp_path / "annotation_spec.yaml"
    path.write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
    return str(path)


_EVENT_1 = {
    "event_id": 1,
    "action_label": "违法停车",
    "description": "定义一",
    "boundary_conditions": ["边界一"],
}
_EVENT_7 = {
    "event_id": 7,
    "action_label": "道路施工",
    "description": "定义七",
    "boundary_conditions": ["边界七"],
}


class TestToPromptTextFiltering:
    def test_none_keeps_all_events(self, tmp_path):
        loader = AnnotationSpecLoader(_write_spec(tmp_path, [_EVENT_1, _EVENT_7]))
        text = loader.to_prompt_text()
        assert "事件 1: 违法停车" in text
        assert "事件 7: 道路施工" in text

    def test_filter_excludes_inactive_events(self, tmp_path):
        loader = AnnotationSpecLoader(_write_spec(tmp_path, [_EVENT_1, _EVENT_7]))
        text = loader.to_prompt_text(active_event_ids={7})
        assert "事件 7: 道路施工" in text
        assert "事件 1: 违法停车" not in text
        # 全局标注原则不受过滤影响
        assert "原则一" in text

    def test_empty_active_set_emits_no_event_sections(self, tmp_path):
        loader = AnnotationSpecLoader(_write_spec(tmp_path, [_EVENT_1, _EVENT_7]))
        text = loader.to_prompt_text(active_event_ids=set())
        assert "事件 1: 违法停车" not in text
        assert "事件 7: 道路施工" not in text
        assert "原则一" in text
