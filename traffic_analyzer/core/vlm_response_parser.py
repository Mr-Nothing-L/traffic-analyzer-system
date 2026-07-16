"""
JSON response extraction, repair, and schema validation for VLM calls.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from traffic_analyzer.core.vlm_exceptions import (
    ResponseParseError,
    SchemaValidationError,
)

logger = logging.getLogger(__name__)


def _repair_json(text: str) -> str:
    """Fix common VLM JSON syntax errors.

    Handles missing commas between properties and trailing commas.
    """
    # Fix 1: missing comma after } or ] before the next property key
    # e.g.  {"a": 1} "b": 2  ->  {"a": 1}, "b": 2
    text = re.sub(r'([}\]])(\s*)(")', r'\1,\2\3', text)

    # Fix 2: missing comma after a literal value before the next property key
    # e.g.  "a": true "b": false  ->  "a": true, "b": false
    # Matches: string "...", number, true, false, null
    text = re.sub(r'("(?:[^"\\]|\\.)*"|\d+(?:\.\d+)?|true|false|null)(\s+)(")', r'\1,\2\3', text)

    # Fix 3: trailing commas before } or ]
    # e.g.  {"a": 1, }  ->  {"a": 1}
    text = re.sub(r',(\s*[}\]])', r'\1', text)

    return text


def _find_balanced_brace_substrings(text: str) -> List[str]:
    """Return all top-level balanced `{...}` substrings in *text*.

    Uses a simple brace stack so nested objects (e.g. `{"a": {"b": 1}}`)
    are returned as one complete substring instead of being split at the
    first inner `}`.
    """
    results: List[str] = []
    stack: List[str] = []
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if not stack:
                start = i
            stack.append("{")
        elif ch == "}":
            if stack:
                stack.pop()
                if not stack and start >= 0:
                    results.append(text[start : i + 1])
                    start = -1
    return results


def _try_parse_json_candidate(candidate: str) -> Optional[Dict[str, Any]]:
    """Try to parse *candidate* as a JSON object (or object array).

    Also attempts auto-repair of common VLM JSON syntax errors.
    """
    candidate = candidate.strip()
    if not candidate:
        return None

    try:
        result = json.loads(candidate)
        if isinstance(result, dict):
            return result
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return result[0]
    except json.JSONDecodeError:
        pass

    repaired = _repair_json(candidate)
    try:
        result = json.loads(repaired)
        if isinstance(result, dict):
            logger.debug(
                "[vlm_engine:_extract_json_from_text] JSON_REPAIRED | "
                "original_len=%d repaired_len=%d",
                len(candidate),
                len(repaired),
            )
            return result
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return result[0]
    except json.JSONDecodeError:
        pass

    return None


def _extract_json_from_text(text: str) -> Dict[str, Any]:
    """Extract JSON object from text, with fallback to regex.

    Tries strict JSON parsing first, then searches for the first
    JSON object block via regex.  Also attempts to auto-repair common
    VLM JSON syntax errors (missing commas, trailing commas).

    Args:
        text: Raw text potentially containing JSON.

    Returns:
        Parsed JSON dictionary.

    Raises:
        ResponseParseError: If no valid JSON is found.
    """
    try:
        text = text.strip()
        # Try direct parse first
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
            # VLM sometimes returns a JSON array (e.g. []) instead of an object.
            # If the array contains a dict as its first element, use that.
            if isinstance(result, list) and result and isinstance(result[0], dict):
                return result[0]
        except json.JSONDecodeError:
            pass

        # Look for ```json ... ``` fenced code blocks first.
        # Capture the full content between the fences so nested JSON objects
        # (e.g. {"event_results": [{...}]}) are not truncated by a greedy
        # `{.*?}` regex.
        fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.DOTALL)
        if fenced_match:
            fenced_content = fenced_match.group(1).strip()
            # Try parsing the whole fenced content as JSON.
            result = _try_parse_json_candidate(fenced_content)
            if result is not None:
                return result

            # Whole block is not valid JSON; try each balanced object inside.
            for candidate in _find_balanced_brace_substrings(fenced_content):
                result = _try_parse_json_candidate(candidate)
                if result is not None:
                    return result

        # Fallback: find all balanced JSON objects in the full text and merge
        # them if multiple are found (VLM sometimes outputs partial JSONs that
        # should be merged).
        matches = _find_balanced_brace_substrings(text)
        if len(matches) >= 2:
            merged = {}
            for candidate in matches:
                result = _try_parse_json_candidate(candidate)
                if isinstance(result, dict):
                    merged.update(result)
            if merged:
                return merged
        elif len(matches) == 1:
            candidate = matches[0]
            result = _try_parse_json_candidate(candidate)
            if result is not None:
                return result

            # Still failed — raise with the original error for clarity
            try:
                json.loads(candidate)
            except json.JSONDecodeError as exc:
                raise ResponseParseError(f"Found JSON-like block but failed to parse: {exc}")

        raise ResponseParseError("No JSON object found in response text.")
    except ResponseParseError:
        raise
    except Exception as exc:
        logger.error(
            "[vlm_engine:_extract_json_from_text] PARSE_FAILED | text_len=%d | %s",
            len(text),
            exc,
            exc_info=True,
        )
        raise ResponseParseError(f"JSON extraction failed: {exc}") from exc


def _validate_schema_basic(data: Dict[str, Any], schema: Dict[str, Any]) -> None:
    """Perform basic key-check validation against a JSON schema.

    Currently checks that all top-level 'required' keys are present.

    Args:
        data: Parsed response data.
        schema: JSON schema dict (may contain 'required' list).

    Raises:
        SchemaValidationError: If required keys are missing.
    """
    required = schema.get("required", [])
    missing = [k for k in required if k not in data]
    if missing:
        raise SchemaValidationError(
            f"Schema validation failed: missing required keys {missing}"
        )
