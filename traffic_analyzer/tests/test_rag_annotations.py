"""
Unit tests for rag/annotations.py (标签剥离 / review 缺省 / 文件名时间戳 / site 归一化).

[文件说明]
作用:测试 load_label 的 <think>/<answer> 正文提取与 human_edited / duration_s 判定、
load_review_states 缺文件与缺条目行为、parse_filename_ts 的 13 位 epoch 毫秒解析、
make_site 归一化;全部使用 tmp_path 假数据,无网络。
上游:pytest 自动发现并执行本文件测试。
下游:traffic_analyzer/rag/annotations.py(被测模块)。
"""

from __future__ import annotations

import json

import pytest

from traffic_analyzer.rag.annotations import (
    load_label,
    load_review_states,
    make_site,
    parse_filename_ts,
)

STEM = "01-02_Event_129_1751869790726_1"


def _write_label(workspace, stem=STEM, **overrides):
    data = {
        "action": [2, 3],
        "description": "<think>\n逐帧分析过程。</think>\n<answer>\n最终结论。</answer>",
        "start_timestamp": 0.0,
        "end_timestamp": 19.92,
        "last_edited_at": "2026-08-12T08:17:44+00:00",
        "last_edited_by": "user",
    }
    data.update(overrides)
    d = workspace / "analysis" / stem
    d.mkdir(parents=True)
    (d / f"{stem}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def test_load_label_strips_tags(tmp_path):
    _write_label(tmp_path)
    label = load_label(tmp_path, STEM)
    assert label is not None
    assert label.text == "逐帧分析过程。\n最终结论。"
    assert "<think>" not in label.text and "<answer>" not in label.text
    assert label.events == [2, 3]
    assert label.human_edited is True
    assert label.ann_edited_at == "2026-08-12T08:17:44+00:00"
    assert label.duration_s == pytest.approx(19.92)


def test_load_label_missing_file(tmp_path):
    assert load_label(tmp_path, "no_such_stem") is None


def test_load_label_broken_json(tmp_path):
    d = tmp_path / "analysis" / STEM
    d.mkdir(parents=True)
    (d / f"{STEM}.json").write_text("{not json", encoding="utf-8")
    assert load_label(tmp_path, STEM) is None


def test_load_label_not_human_edited(tmp_path):
    _write_label(tmp_path, last_edited_by=None, last_edited_at=None)
    label = load_label(tmp_path, STEM)
    assert label.human_edited is False
    assert label.ann_edited_at is None


def test_load_label_empty_action_and_missing_timestamps(tmp_path):
    _write_label(tmp_path, action=[], start_timestamp=None, end_timestamp=None)
    label = load_label(tmp_path, STEM)
    assert label.events == []
    assert label.duration_s is None


def test_load_label_missing_description(tmp_path):
    _write_label(tmp_path, description=None)
    label = load_label(tmp_path, STEM)
    assert label.text == ""


def test_load_review_states_missing_file(tmp_path):
    assert load_review_states(tmp_path) == {}


def test_load_review_states_roundtrip(tmp_path):
    (tmp_path / "analysis").mkdir(parents=True)
    states = {STEM: {"status": "confirmed", "updated_at": "2026-08-11", "by": "user"}}
    (tmp_path / "analysis" / "review_states.json").write_text(
        json.dumps(states), encoding="utf-8"
    )
    loaded = load_review_states(tmp_path)
    assert loaded == states
    # 缺条目由调用方按 unconfirmed 处理
    assert loaded.get("other_stem", {}).get("status", "unconfirmed") == "unconfirmed"


def test_parse_filename_ts():
    assert parse_filename_ts(STEM) == pytest.approx(1751869790.726)
    assert parse_filename_ts("01-02_Event_129") is None
    assert parse_filename_ts("Event_123456789012_x") is None  # 12 位不匹配


def test_make_site():
    assert make_site("北京", "G3京台高速-道路 K18+470", "进京", "3") == (
        "北京-G3京台高速-道路 K18+470-进京-3"
    )
    assert make_site(None, "K18+470", None, "3") == "K18+470-3"
    assert make_site(None, None, None, None) is None
    assert make_site("", "", "", "") is None
