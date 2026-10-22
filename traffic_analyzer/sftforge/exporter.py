"""SFT Forge 产出落盘。

[文件说明]
作用:构造训练 JSONL 行(与 lora27b_v15_mnt4096.jsonl 完全一致的
  {index, video, output, gt_classes, gt_class_names} 契约)、追加写 JSONL、
  每视频 trace JSON 落盘(供 Web 面板逐轮可视化)、断点续跑的已完成 stem 收集。
上游:scripts/sft_forge.py。
下游:sftforge/models.py(VideoForgeResult)。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set

from traffic_analyzer.sftforge.models import VideoForgeResult

_write_lock = threading.Lock()


def build_train_row(
    index: int,
    video: str,
    output: str,
    gt: Sequence[int],
    names: Mapping[int, str],
) -> Dict[str, object]:
    """训练 JSONL 行;gt_class_names 只含真值事件(与权威样本一致)。"""
    gt_sorted = sorted(gt)
    return {
        "index": index,
        "video": video,
        "output": output,
        "gt_classes": gt_sorted,
        "gt_class_names": {str(eid): names.get(eid, f"event_{eid}") for eid in gt_sorted},
    }


def append_jsonl(row: Mapping[str, object], out_path: Path) -> None:
    """线程安全地追加一行 JSONL。"""
    line = json.dumps(row, ensure_ascii=False) + "\n"
    with _write_lock:
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(line)


def write_trace_json(result: VideoForgeResult, trace_dir: Path) -> Path:
    """每视频 trace JSON:<trace_dir>/<video_stem>.json。"""
    trace_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(result.video).stem
    path = trace_dir / f"{stem}.json"
    path.write_text(
        json.dumps(result.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def collect_done_stems(jsonl_paths: Iterable[Path]) -> Set[str]:
    """断点续跑:从已有 JSONL(主输出 + quarantine)收集已完成视频 stem。"""
    stems: Set[str] = set()
    for path in jsonl_paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                video = row.get("video")
                if isinstance(video, str) and video:
                    stems.add(Path(video).stem)
    return stems
