"""
Unit tests for vlm_response_parser.py.
"""

from __future__ import annotations

import pytest

from traffic_analyzer.core.vlm_exceptions import ResponseParseError
from traffic_analyzer.core.vlm_response_parser import (
    _extract_json_from_text,
    _find_balanced_brace_substrings,
)


def test_direct_parse_dict() -> None:
    result = _extract_json_from_text('{"a": 1}')
    assert result == {"a": 1}


def test_direct_parse_array_returns_first_dict() -> None:
    result = _extract_json_from_text('[{"a": 1}, {"b": 2}]')
    assert result == {"a": 1}


def test_fenced_json_block() -> None:
    text = '```json\n{"detected": true, "count": 2}\n```'
    result = _extract_json_from_text(text)
    assert result == {"detected": True, "count": 2}


def test_fenced_json_nested_object() -> None:
    """Regression test: nested objects inside a fenced block must not be truncated."""
    text = (
        "```json\n"
        '{"event_results": [{"event_id": 0, "detected": true}], "summary": "ok"}\n'
        "```"
    )
    result = _extract_json_from_text(text)
    assert result == {
        "event_results": [{"event_id": 0, "detected": True}],
        "summary": "ok",
    }


def test_fenced_json_with_surrounding_text() -> None:
    text = (
        "Here is the result:\n"
        "```json\n"
        '{"detected": false, "instances": []}\n'
        "```\n"
        "Hope this helps."
    )
    result = _extract_json_from_text(text)
    assert result == {"detected": False, "instances": []}


def test_plain_text_nested_object() -> None:
    text = 'Some text {"outer": {"inner": 1}} more text'
    result = _extract_json_from_text(text)
    assert result == {"outer": {"inner": 1}}


def test_merge_multiple_objects() -> None:
    text = '{"a": 1} {"b": 2}'
    result = _extract_json_from_text(text)
    assert result == {"a": 1, "b": 2}


def test_repair_missing_comma() -> None:
    text = '{"a": 1 "b": 2}'
    result = _extract_json_from_text(text)
    assert result == {"a": 1, "b": 2}


def test_repair_trailing_comma() -> None:
    text = '{"a": 1,}'
    result = _extract_json_from_text(text)
    assert result == {"a": 1}


def test_no_json_raises() -> None:
    with pytest.raises(ResponseParseError):
        _extract_json_from_text("No JSON here.")


def test_find_balanced_brace_substrings_nested() -> None:
    text = 'x {"a": {"b": 1}} y {"c": 2}'
    matches = _find_balanced_brace_substrings(text)
    assert matches == ['{"a": {"b": 1}}', '{"c": 2}']


def test_find_balanced_brace_substrings_unbalanced_ignored() -> None:
    text = '{"a": 1'  # missing closing brace
    matches = _find_balanced_brace_substrings(text)
    assert matches == []
