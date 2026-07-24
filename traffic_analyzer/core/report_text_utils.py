"""Text cleaning utilities for traffic analysis reports.

Functions in this module are intentionally low-level and stateless.
They operate on raw markdown / JSON strings produced by expert agents
and return sanitized text suitable for inclusion in the final report.

[文件说明]
作用:报告文本清洗工具集(_clean_expert_description 等),剔除专家输出中的代码块与
     JSON 片段,规整 Markdown 表格与标题层级,产出可并入最终报告的纯文本。
上游:core/report_markdown_renderer.py(渲染专家原始分析时调用)。
下游:仅依赖 re/logging 标准库,处理专家 agent 产出的原始 markdown/JSON 字符串。
"""

from __future__ import annotations

import logging
import re
from typing import List

logger = logging.getLogger(__name__)


def _clean_expert_description(text: str) -> str:
    """Remove fenced code blocks and standalone JSON objects from expert text.

    Keeps only natural-language paragraphs.
    """
    try:
        # 1. Strip fenced code blocks (```json ... ``` or ``` ... ```)
        cleaned = re.sub(r"```[a-zA-Z]*\n.*?\n```", "", text, flags=re.DOTALL)
        # Also handle single-backtick fenced blocks that may not have a trailing newline
        cleaned = re.sub(r"```[a-zA-Z]*.*?```", "", cleaned, flags=re.DOTALL)

        # 2. Remove multi-line JSON objects/arrays
        # Detect blocks that start with { or [ and end with } or ],
        # where most lines look like JSON (contain ":", commas, quotes)
        cleaned = _strip_json_blocks(cleaned)

        # 3. Remove standalone JSON objects — lines that are just a JSON dict/array
        lines: List[str] = []
        for line in cleaned.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # Skip lines that look like a complete JSON object/array
            if (stripped.startswith("{") and stripped.endswith("}")) or \
               (stripped.startswith("[") and stripped.endswith("]")):
                continue
            lines.append(line)

        # 4. Collapse multiple blank lines and strip edges
        text_out = "\n".join(lines).strip()
        text_out = re.sub(r"\n{3,}", "\n\n", text_out)

        # 5. Normalize markdown formatting (tables, horizontal rules)
        text_out = _normalize_markdown(text_out)

        # 6. Downgrade markdown headings so they don't exceed parent level (#####)
        text_out = _downgrade_headings(text_out, max_level=5)
        return text_out
    except Exception as exc:
        logger.error(
            "[report_text_utils:_clean_expert_description] CLEAN_ERROR | text_len=%d | %s",
            len(text),
            exc,
            exc_info=True,
        )
        return text


def _strip_json_blocks(text: str) -> str:
    """Remove contiguous blocks that look like JSON objects or arrays."""
    lines = text.splitlines()
    result: List[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        # Detect start of a JSON block
        if stripped.startswith("{") or stripped.startswith("["):
            # Try to find where this JSON block ends
            depth = 0
            in_string = False
            escape_next = False
            block_lines: List[str] = []
            j = i
            while j < len(lines):
                line = lines[j]
                block_lines.append(line)
                for ch in line:
                    if escape_next:
                        escape_next = False
                        continue
                    if ch == "\\":
                        escape_next = True
                        continue
                    if ch == '"' and not in_string:
                        in_string = True
                    elif ch == '"' and in_string:
                        in_string = False
                    elif not in_string:
                        if ch in "{[":
                            depth += 1
                        elif ch in "}]":
                            depth -= 1
                # End of block: depth back to 0 and line ends with } or ]
                if depth == 0 and not in_string:
                    last_stripped = line.strip()
                    if last_stripped.endswith("}") or last_stripped.endswith("]"):
                        break
                j += 1
            else:
                # Block didn't close — treat as regular text
                result.extend(block_lines)
            i = j + 1
            continue
        result.append(lines[i])
        i += 1
    return "\n".join(result)


def _downgrade_headings(text: str, max_level: int) -> str:
    """Downgrade markdown headings (# → #####) so they don't exceed parent level."""
    lines = text.splitlines()
    result: List[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#"):
            # Count leading #
            level = 0
            for ch in stripped:
                if ch == "#":
                    level += 1
                else:
                    break
            if level > 0 and level < max_level:
                # Pad to max_level
                new_line = "#" * max_level + stripped[level:]
                result.append(new_line)
                continue
        result.append(line)
    return "\n".join(result)


def _normalize_markdown(text: str) -> str:
    """Ensure proper markdown spacing around tables and horizontal rules."""
    lines = text.splitlines()
    result: List[str] = []
    prev_was_table = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Detect table row: starts with | and has at least one more |
        is_table = stripped.startswith("|") and stripped.count("|") >= 2

        # Detect horizontal rule
        is_hr = stripped == "---" or stripped == "***" or stripped == "___"

        # Add blank line before table if previous line is not blank and not a table
        if is_table and result and result[-1].strip() and not prev_was_table:
            result.append("")

        # Add blank line before and after horizontal rule
        if is_hr:
            if result and result[-1].strip():
                result.append("")
            result.append(line)
            # Add blank line after if next line exists and is not blank
            if i + 1 < len(lines) and lines[i + 1].strip():
                result.append("")
            prev_was_table = False
            continue

        result.append(line)
        prev_was_table = is_table

    return "\n".join(result)
