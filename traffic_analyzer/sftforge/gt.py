"""SFT Forge 真值解析与预测 diff(纯函数)。

[文件说明]
作用:复用 evaluation.extract_gt_from_filename 从视频文件名前缀解析真值事件
  集合;09(正常)与无法解析的文件名返回 None 由上层跳过;diff_predictions
  给出 missing(真值有而推理无)与 extra(推理有而真值无)两个方向。
上游:sftforge/engine.py、scripts/sft_forge.py。
下游:traffic_analyzer/evaluation/__init__.py。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from traffic_analyzer.evaluation import NORMAL_EVENT_ID, extract_gt_from_filename


def gt_for_video(filename: str, names: Optional[Dict[int, str]] = None) -> Optional[Set[int]]:
    """解析文件名真值;返回 None 表示该视频应跳过(09 正常或无法解析)。"""
    gt = extract_gt_from_filename(filename, names)
    if not gt or NORMAL_EVENT_ID in gt:
        return None
    return gt


def diff_predictions(pred: Set[int], gt: Set[int]) -> Tuple[List[int], List[int]]:
    """(missing, extra):missing=真值有而推理未检出;extra=推理检出而真值无。"""
    return sorted(gt - pred), sorted(pred - gt)
