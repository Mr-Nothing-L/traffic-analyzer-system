"""SFT Forge 段落级合并(纯函数)。

[文件说明]
作用:以正向解析结果为底,应用纠错补丁(段落替换/插入/删除)重建最终
  <think>/<answer> 文本;answer 的 classN 列表按 final_classes 重建,保证
  think 段落与最终结论集合一致。输出格式与训练样本
  lora27b_v15_mnt4096.jsonl 逐字节对齐(段落间空行、</think> 与 <answer>
  之间空行)。
上游:sftforge/engine.py、tests/test_sftforge_merger.py。
下游:sftforge/parser.py(ParsedOutput 结构与段落顺序常量)。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from traffic_analyzer.sftforge.parser import DEFAULT_EVENT_ORDER, ParsedOutput

# 正向响应缺某事件段落时的兜底阴性段(与 sft_label_rewrite 的「未发现。」口径一致)。
_FALLBACK_NEGATIVE = "未发现。"


def merge_output(
    base: ParsedOutput,
    patches: Dict[int, Optional[str]],
    final_classes: Sequence[int],
    names: Dict[int, str],
    event_order: Sequence[int] = DEFAULT_EVENT_ORDER,
) -> str:
    """应用补丁并重建完整输出文本。

    patches:event_id → 新段落正文(str,替换/插入)或 None(删除该段落)。
    final_classes:合并后的最终结论事件集合(决定 answer 的 classN 列表)。
    """
    think_paragraphs: List[str] = []
    for eid in event_order:
        if eid in patches:
            patch = patches[eid]
            if patch is None:
                continue  # 删除
            text = patch
        else:
            text = base.paragraphs.get(eid, "")
        if not text:
            text = _FALLBACK_NEGATIVE
        name = names.get(eid, f"event_{eid}")
        think_paragraphs.append(f"{name}：{text}")

    answer_lines: List[str] = [
        f"天气：{base.weather or '未知'}",
        f"时间：{base.time_of_day or '未知'}",
        f"场景：{base.scene or '未知'}",
    ]
    classes = sorted(final_classes)
    if classes:
        answer_lines.append("最终结论：本视频块检出以下事件。")
        for eid in classes:
            answer_lines.append(f"class{eid}: {names.get(eid, f'event_{eid}')}")
    else:
        answer_lines.append("最终结论：本视频块未检出任何事件，交通状况正常。")

    return (
        "<think>\n"
        + "\n\n".join(think_paragraphs)
        + "\n</think>\n\n<answer>\n"
        + "\n".join(answer_lines)
        + "\n</answer>"
    )
