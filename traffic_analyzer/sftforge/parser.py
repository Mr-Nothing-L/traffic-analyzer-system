"""SFT Forge 输出解析(纯函数)。

[文件说明]
作用:解析 VLM 正向/纠错响应。主路径为 JSON(parse_forward_json /
  parse_correction_json,与 sft_rewrite.yaml 同模式,端点可靠输出);
  文本 <think>/<answer> 格式(parse_output / parse_event_segment /
  parse_review_verdict)保留为回退——qwen3 端点在 enable_thinking=False 时
  chat template 预填 <think></think>,模型不会输出字面标签,最终标签由
  merger.py 代码重建。文本解析支持按「事件名：」标题切分段落(容错编号/
  加粗标题)、classN 列表提取(半角/全角冒号)。
上游:sftforge/engine.py、merger.py、tests/test_sftforge_parser.py。
下游:core/vlm_response_parser.py(_extract_json_from_text)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from traffic_analyzer.core.vlm_exceptions import ResponseParseError
from traffic_analyzer.core.vlm_response_parser import _extract_json_from_text

# think 段事件段落顺序(标注文档 v4.5 的 action 编号;9=正常占位不出现在样本中)。
DEFAULT_EVENT_ORDER: tuple = (1, 2, 3, 4, 5, 6, 7, 8, 10, 11)

# 复核结论标记(review_extra 模板要求模型最后一行输出)。
_VERDICT_CONFIRM = "复核结论：确认"
_VERDICT_FALSE_POSITIVE = "复核结论：误报"


@dataclass
class ParsedOutput:
    """一条 <think>/<answer> 训练样本输出的结构化视图。

    paragraphs 的值为段落正文(不含「事件名：」前缀)。
    """

    paragraphs: Dict[int, str] = field(default_factory=dict)
    weather: str = "未知"
    time_of_day: str = "未知"
    scene: str = "未知"
    classes: List[int] = field(default_factory=list)


class OutputParseError(ValueError):
    """<think>/<answer> 结构缺失或无法解析。"""


def _extract_tag_body(text: str, tag: str) -> str:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    if not match:
        raise OutputParseError(f"missing <{tag}>...</{tag}> block")
    return match.group(1).strip("\n")


def _split_think_paragraphs(think: str, names: Dict[int, str]) -> Dict[int, str]:
    """按行首「事件名：」标题切分 think 段;标题必须命中已知事件中文名。

    容错:模型偶发输出「4. **事件名**：」式编号/加粗标题,归一后再匹配。
    """
    name_to_id = {v: k for k, v in names.items()}
    paragraphs: Dict[int, List[str]] = {}
    current_id: Optional[int] = None
    for line in think.split("\n"):
        normalized = re.sub(r"^\s*\d+\s*[.、．]\s*", "", line.strip())
        header = re.match(r"^\*{0,2}([^\n：*]{2,15})\*{0,2}：", normalized)
        if header and header.group(1).strip() in name_to_id:
            current_id = name_to_id[header.group(1).strip()]
            paragraphs.setdefault(current_id, [])
            rest = normalized[header.end():].strip()
            if rest:
                paragraphs[current_id].append(rest)
            continue
        if current_id is not None:
            paragraphs[current_id].append(line)
    return {
        eid: "\n".join(lines).strip()
        for eid, lines in paragraphs.items()
        if "\n".join(lines).strip()
    }


def parse_output(text: str, names: Dict[int, str]) -> ParsedOutput:
    """解析完整输出;结构缺失时抛 OutputParseError。

    容错:qwen3 系端点在 enable_thinking=False 时 chat template 会预填
    <think></think>,模型常省略字面标签——此时以首个「天气：」行为界,
    之前为 think 正文、之后为 answer 正文(最终标签由 merger 代码重建)。
    think 正文完全缺失(无任何事件段落)视为解析失败:没有思维链的样本
    不能用于训练。
    """
    if "<think>" in text:
        think = _extract_tag_body(text, "think")
        answer = _extract_tag_body(text, "answer")
    else:
        answer_start = re.search(r"^天气\s*[:：]", text, re.MULTILINE)
        if not answer_start:
            raise OutputParseError("missing answer section (no 天气： line)")
        think = text[: answer_start.start()].strip("\n")
        answer = text[answer_start.start():].strip("\n")
        if "<answer>" in answer:
            answer = _extract_tag_body(answer, "answer")

    out = ParsedOutput(paragraphs=_split_think_paragraphs(think, names))
    for line in answer.split("\n"):
        line = line.strip()
        if not line:
            continue
        class_match = re.match(r"^class(\d+)\s*[:：]", line)
        if class_match:
            eid = int(class_match.group(1))
            if eid not in out.classes:
                out.classes.append(eid)
            continue
        for prefix, attr in (("天气", "weather"), ("时间", "time_of_day"), ("场景", "scene")):
            m = re.match(rf"^{prefix}\s*[:：]\s*(.+)$", line)
            if m:
                setattr(out, attr, m.group(1).strip())
                break
    out.classes.sort()
    if not out.paragraphs:
        raise OutputParseError("think section has no event paragraphs")
    return out


def parse_event_segment(text: str, event_id: int, names: Dict[int, str]) -> str:
    """从纠错响应中提取单个事件的段落正文。

    兼容三种形态:带 <think> 包裹、裸「事件名：...」开头、无标题纯文本。
    返回空串表示提取失败(上层按未修补处理)。
    """
    body = text.strip()
    if not body:
        return ""
    if "<think>" in body:
        try:
            body = _extract_tag_body(body, "think")
        except OutputParseError:
            return ""
    name = names.get(event_id, "")
    paragraphs = _split_think_paragraphs(body, names)
    if event_id in paragraphs:
        return paragraphs[event_id]
    # 无标题纯文本:去掉可能残留的标题前缀后整体作为段落。
    if name and body.startswith(f"{name}："):
        body = body[len(name) + 1:].strip()
    # 复核结论标记行不属于段落正文。
    lines = [
        ln for ln in body.split("\n")
        if not ln.strip().startswith("复核结论")
    ]
    return "\n".join(lines).strip()


def parse_review_verdict(text: str) -> Optional[bool]:
    """复核结论:确认→True(保留),误报→False(剔除),缺失→None(保守保留)。"""
    if _VERDICT_FALSE_POSITIVE in text:
        return False
    if _VERDICT_CONFIRM in text:
        return True
    return None


# ---------------------------------------------------------------------------
# JSON 响应解析(主路径;qwen3 端点可靠输出 JSON,文本标签格式作为回退)
# ---------------------------------------------------------------------------


def _try_extract_json(text: str) -> Optional[Dict[str, Any]]:
    """尽力从响应中提取 JSON 对象;失败返回 None(不抛异常)。"""
    try:
        data = _extract_json_from_text(text)
    except (ResponseParseError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_forward_json(text: str, names: Dict[int, str]) -> ParsedOutput:
    """解析 forward 的 JSON 响应为 ParsedOutput;结构不符时抛 OutputParseError。

    JSON 契约:{weather, time_of_day, scene, event_thoughts:[{event_id,
    present, thinking}]};classes = present=true 的 event_id 列表。
    """
    data = _try_extract_json(text)
    if data is None:
        raise OutputParseError("forward response is not a JSON object")
    thoughts = data.get("event_thoughts")
    if not isinstance(thoughts, list):
        raise OutputParseError("forward JSON missing event_thoughts list")

    out = ParsedOutput()
    for item in thoughts:
        # type(...) is int:JSON true 是 bool(True == 1),不得当作 event 1。
        if not isinstance(item, dict) or type(item.get("event_id")) is not int:
            continue
        eid = item["event_id"]
        thinking = str(item.get("thinking") or "").strip()
        if thinking:
            out.paragraphs[eid] = thinking
        if item.get("present") is True and eid not in out.classes:
            out.classes.append(eid)
    if not out.paragraphs:
        raise OutputParseError("forward JSON has no event paragraphs")
    out.weather = str(data.get("weather") or "").strip() or "未知"
    out.time_of_day = str(data.get("time_of_day") or "").strip() or "未知"
    out.scene = str(data.get("scene") or "").strip() or "未知"
    out.classes.sort()
    return out


def parse_correction_json(text: str) -> Optional[Dict[str, Any]]:
    """解析 correct_missing/review_extra 的 JSON 响应。

    返回 {"thinking": str, "verdict": Optional[str]};非 JSON 或缺 thinking
    时返回 None(上层回退到文本解析)。
    """
    data = _try_extract_json(text)
    if data is None:
        return None
    thinking = str(data.get("thinking") or "").strip()
    if not thinking:
        return None
    verdict = data.get("verdict")
    return {
        "thinking": thinking,
        "verdict": verdict if isinstance(verdict, str) else None,
    }
