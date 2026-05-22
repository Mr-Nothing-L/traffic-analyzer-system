#!/usr/bin/env python3
"""Batch evaluation script for traffic event detection results.

Compares inference reports against ground truth, computing per-event
precision, recall, and F1 score.

Usage:
    python scripts/batch_evaluate.py \
        --video-dir /path/to/videos \
        --report-dir /path/to/reports \
        --output evaluation_report.html

    # With annotation file
    python scripts/batch_evaluate.py \
        --video-dir /path/to/videos \
        --report-dir /path/to/reports \
        --gt-mode annotation_file \
        --annotation-file /path/to/annotations.json \
        --output evaluation_report.html

    # Single-class mode (only evaluate is_active=true events from config)
    python scripts/batch_evaluate.py \
        --video-dir /path/to/videos \
        --report-dir /path/to/reports \
        --single-class \
        --config-dir ./traffic_analyzer/config \
        --output evaluation_report.html
"""

from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import logging
import os
import re
import sys

import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


# ---------------------------------------------------------------------------
# Event metadata (aligned with event_categories.yaml)
# ---------------------------------------------------------------------------
EVENT_NAMES: Dict[int, str] = {
    0: "违法停车",
    1: "应急车道占用",
    2: "交通事故",
    3: "高速公路行人出现",
    4: "摩托车出现",
    5: "严重拥堵",
    6: "道路施工",
    7: "车辆逆行/倒车",
    8: "抛洒物",
    9: "实线变道",
}

NUM_EVENTS = 10

# Action ID (filename prefix number) -> event_id mapping
# Based on annotation document v4.5:
#   1=违法停车->0, 2=应急车道占用->1, 3=交通事故->2, 4=行人出现->3,
#   5=摩托车出现->4, 6=严重拥堵->5, 7=道路施工->6, 8=车辆逆行/倒车->7,
#   9=Normal (skip), 10=抛洒物->8, 11=实线变道->9
ACTION_TO_EVENT_ID = {
    1: 0, 2: 1, 3: 2, 4: 3, 5: 4,
    6: 5, 7: 6, 8: 7,
    10: 8, 11: 9,
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _setup_logging(log_level: str = "INFO") -> logging.Logger:
    """Configure root logger."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger("batch_evaluate")


# ---------------------------------------------------------------------------
# Ground truth extraction
# ---------------------------------------------------------------------------
def extract_gt_from_filename(filename: str) -> Set[int]:
    """Extract ground-truth event IDs from a video filename.

    Default pattern: numbers before ``_Event_`` are action IDs from the
    annotation document (v4.5).  They map to event_ids via ACTION_TO_EVENT_ID.
    Examples:
        ``01-02-07-11_Event_65536_...``  -> {0, 1, 6, 9}
        ``02-04-07-08-10_Event_...``     -> {1, 3, 6, 7, 8}
        ``06_Event_...``                  -> {5}

    Action ID ``9`` (Normal) and any unknown number are silently skipped.

    Supports two filename patterns:
        ``01-02-08_Event_xxx_...``  -> standard format
        ``01-02-08_20260514-...``    -> date-stamp format (no _Event_)
    """
    # Try standard pattern first: prefix before _Event_
    match = re.match(r"^([\d\-]+)_Event_", filename)
    if match:
        prefix = match.group(1)
    else:
        # Fallback: leading digit-dash prefix before any _ that is NOT _Event_
        # Handles date-stamp filenames like 01-02-08_20260514-173730_前半段.mp4
        match = re.match(r"^([\d\-]+)_(?!Event_)", filename)
        if not match:
            return set()
        prefix = match.group(1)
    event_ids: Set[int] = set()
    for part in prefix.split("-"):
        part = part.strip()
        if not part.isdigit():
            continue
        num = int(part)
        event_id = ACTION_TO_EVENT_ID.get(num)
        if event_id is not None:
            event_ids.add(event_id)
    return event_ids


def load_annotations_json(path: Path) -> Dict[str, Set[int]]:
    """Load annotations from a JSON file.

    Expected format (either flat or nested):
        {
            "video_name.mp4": [0, 2, 7],
            ...
        }
    or
        {
            "video_name.mp4": {"events": [0, 2, 7]},
            ...
        }
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    result: Dict[str, Set[int]] = {}
    for key, value in data.items():
        if isinstance(value, list):
            result[key] = {int(e) for e in value if isinstance(e, int) and 0 <= e < NUM_EVENTS}
        elif isinstance(value, dict):
            events = value.get("events", value.get("event_ids", value.get("labels", [])))
            result[key] = {int(e) for e in events if isinstance(e, int) and 0 <= e < NUM_EVENTS}
        else:
            result[key] = set()
    return result


def load_annotations_csv(path: Path) -> Dict[str, Set[int]]:
    """Load annotations from a CSV file.

    Expected columns (header required):
        video_name,event_ids
    where event_ids is a comma-separated list of integers.
    """
    result: Dict[str, Set[int]] = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            video_name = row.get("video_name", row.get("filename", row.get("file", ""))).strip()
            event_ids_str = row.get("event_ids", row.get("labels", row.get("events", ""))).strip()
            if not video_name:
                continue
            event_ids: Set[int] = set()
            for part in event_ids_str.split(","):
                part = part.strip()
                if part.isdigit():
                    eid = int(part)
                    if 0 <= eid < NUM_EVENTS:
                        event_ids.add(eid)
            result[video_name] = event_ids
    return result


def load_annotations(path: Path) -> Dict[str, Set[int]]:
    """Load annotations from JSON or CSV file."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        return load_annotations_json(path)
    elif suffix in (".csv", ".tsv"):
        return load_annotations_csv(path)
    else:
        raise ValueError(f"Unsupported annotation file format: {suffix}")


# ---------------------------------------------------------------------------
# Prediction extraction from reports
# ---------------------------------------------------------------------------
def extract_pred_from_markdown(text: str) -> Set[int]:
    """Extract predicted event IDs from a Markdown report.

    Looks for:
    1. Binary encoding: ``**二进制编码**: `{0_0_0_0_0_0_1_0_0}` ``
    2. Per-event detection lines: ``- **是否检测到**: 是`` / ``否``
    """
    detected: Set[int] = set()

    # Try binary encoding first (most reliable)
    enc_match = re.search(r"\*\*二进制编码\*\*:\s*`\{([^}]+)\}`", text)
    if enc_match:
        encoding = enc_match.group(1)
        bits = encoding.split("_")
        for eid, bit in enumerate(bits):
            if bit == "1":
                detected.add(eid)
        return detected

    # Fallback: scan per-event sections
    # Pattern: "### [icon] 事件 {id}: {name}"
    event_sections = re.finditer(
        r"###\s*[✅❌]\s*事件\s*(\d+):\s*[^\n]*\n.*?-\s*\*\*是否检测到\*\*:\s*([是|否])",
        text,
        re.DOTALL,
    )
    for m in event_sections:
        eid = int(m.group(1))
        detected_flag = m.group(2).strip() == "是"
        if detected_flag and 0 <= eid < NUM_EVENTS:
            detected.add(eid)

    return detected


def extract_pred_from_json(text: str) -> Set[int]:
    """Extract predicted event IDs from a JSON report."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return set()

    # Try binary_encoding first
    be = data.get("binary_encoding", {})
    if be and "detected_events" in be:
        return {int(e) for e in be["detected_events"] if 0 <= int(e) < NUM_EVENTS}

    # Fallback: scan event_results
    detected: Set[int] = set()
    for result in data.get("event_results", []):
        if result.get("detected", False):
            eid = result.get("event_id")
            if eid is not None and 0 <= eid < NUM_EVENTS:
                detected.add(eid)
    return detected


def extract_pred(report_path: Path) -> Set[int]:
    """Extract predicted event IDs from a report file (markdown or json)."""
    text = report_path.read_text(encoding="utf-8")
    suffix = report_path.suffix.lower()
    if suffix == ".json":
        return extract_pred_from_json(text)
    else:
        return extract_pred_from_markdown(text)


# ---------------------------------------------------------------------------
# Active-event loading from config
# ---------------------------------------------------------------------------
def load_active_events_from_config(config_dir: Path) -> Set[int]:
    """Load is_active=true event IDs from event_categories.yaml.

    Parameters
    ----------
    config_dir:
        Directory containing event_categories.yaml.

    Returns
    -------
        Set of event IDs where is_active is true.
    """
    config_path = config_dir / "event_categories.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"event_categories.yaml not found: {config_path}")

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    active_events: Set[int] = set()
    for cat in data.get("event_categories", []):
        if cat.get("is_active", True):
            active_events.add(int(cat["event_id"]))
    return active_events


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------
def compute_metrics(
    predictions: List[Set[int]],
    ground_truths: List[Set[int]],
    single_class_masks: Optional[List[Optional[Set[int]]]] = None,
) -> Tuple[Dict[str, Any], int, int]:
    """Compute per-event and overall metrics.

    Parameters
    ----------
    predictions:
        List of predicted event sets (one per video).
    ground_truths:
        List of ground-truth event sets (one per video).
    single_class_masks:
        Optional list of event masks. When an entry is a set, only those
        event IDs are evaluated for that video; all others are forced to false.
        When an entry is None, all events are evaluated.

    Returns
    -------
    (per_event_dict, evaluated_count, skipped_count)
    """
    # Per-event counters
    tp = [0] * NUM_EVENTS
    fp = [0] * NUM_EVENTS
    fn = [0] * NUM_EVENTS
    gt_count = [0] * NUM_EVENTS

    evaluated = 0
    skipped = 0

    for pred, gt, mask in zip(
        predictions, ground_truths, single_class_masks or [None] * len(predictions)
    ):
        if mask is not None:
            # Only evaluate events in the mask
            events_to_eval = mask
        else:
            events_to_eval = set(range(NUM_EVENTS))

        if not events_to_eval:
            skipped += 1
            continue

        evaluated += 1
        for eid in events_to_eval:
            p = eid in pred
            g = eid in gt
            if g:
                gt_count[eid] += 1
            if p and g:
                tp[eid] += 1
            elif p and not g:
                fp[eid] += 1
            elif not p and g:
                fn[eid] += 1
            # TN is implicit (not counted)

    per_event: Dict[str, Any] = {}
    for eid in range(NUM_EVENTS):
        precision = tp[eid] / (tp[eid] + fp[eid]) if (tp[eid] + fp[eid]) > 0 else 0.0
        recall = tp[eid] / (tp[eid] + fn[eid]) if (tp[eid] + fn[eid]) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        per_event[str(eid)] = {
            "name": EVENT_NAMES.get(eid, f"事件{eid}"),
            "gt_count": gt_count[eid],
            "tp": tp[eid],
            "fp": fp[eid],
            "fn": fn[eid],
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        }

    # Macro averages (over all 10 events)
    macro_precision = sum(per_event[str(eid)]["precision"] for eid in range(NUM_EVENTS)) / NUM_EVENTS
    macro_recall = sum(per_event[str(eid)]["recall"] for eid in range(NUM_EVENTS)) / NUM_EVENTS
    macro_f1 = sum(per_event[str(eid)]["f1"] for eid in range(NUM_EVENTS)) / NUM_EVENTS

    overall = {
        "macro_precision": round(macro_precision, 4),
        "macro_recall": round(macro_recall, 4),
        "macro_f1": round(macro_f1, 4),
    }

    return {"per_event": per_event, "overall": overall}, evaluated, skipped


# ---------------------------------------------------------------------------
# Markdown table output
# ---------------------------------------------------------------------------
def format_markdown_table(result: Dict[str, Any]) -> str:
    """Format evaluation results as a markdown table string."""
    lines: List[str] = []
    lines.append("## 交通事件检测评估结果\n")
    lines.append("| 事件ID | 事件名称 | 总数 | TP | FP | FN | 精确率 | 召回率 | F1分数 |")
    lines.append("|--------|----------|------|----|----|----|--------|--------|--------|")

    per_event = result["per_event"]
    for eid in range(NUM_EVENTS):
        info = per_event[str(eid)]
        lines.append(
            f"| {eid} | {info['name']} | {info['gt_count']} | {info['tp']} | {info['fp']} | {info['fn']} | "
            f"{info['precision']:.4f} | {info['recall']:.4f} | {info['f1']:.4f} |"
        )

    overall = result["overall"]
    lines.append(
        "| **总体** | **宏平均** | - | - | - | - | "
        f"**{overall['macro_precision']:.4f}** | **{overall['macro_recall']:.4f}** | **{overall['macro_f1']:.4f}** |"
    )

    lines.append("")
    lines.append(f"- 总视频数: {result['total_videos']}")
    lines.append(f"- 已评估视频数: {result['evaluated_videos']}")
    lines.append(f"- 跳过视频数: {result['skipped_videos']}")
    lines.append("")

    # Add per-video detail table if available
    per_video = result.get("per_video")
    if per_video:
        lines.append(format_per_video_table(per_video))

    return "\n".join(lines)


def format_per_video_table(per_video: List[Dict[str, Any]]) -> str:
    """Format per-video details as a markdown table with clickable file links."""
    lines: List[str] = []
    lines.append("## 逐视频详细结果\n")
    lines.append("| 序号 | 视频文件 | 预测结果 | Ground Truth | 报告文件 | 是否正确 |")
    lines.append("|------|----------|----------|--------------|----------|----------|")

    for idx, pv in enumerate(per_video, start=1):
        video_path = pv.get("video_path", "")
        report_path = pv.get("report_path", "")
        video_name = pv.get("video", "")
        report_name = Path(report_path).name if report_path else ""

        # Clickable links using file:// protocol
        video_link = f"[{video_name}](file://{video_path})" if video_path else video_name
        report_link = f"[{report_name}](file://{report_path})" if report_path else report_name

        pred_names = ", ".join(pv.get("predicted_names", [])) or "无"
        gt_names = ", ".join(pv.get("ground_truth_names", [])) or "无"
        correct_mark = "✅" if pv.get("is_correct", False) else "❌"

        lines.append(
            f"| {idx} | {video_link} | {pred_names} | {gt_names} | {report_link} | {correct_mark} |"
        )

    lines.append("")
    return "\n".join(lines)


def print_markdown_table(result: Dict[str, Any]) -> None:
    """Print evaluation results as a markdown table to stdout."""
    print(format_markdown_table(result))


# ---------------------------------------------------------------------------
# HTML report output
# ---------------------------------------------------------------------------
def _ids_to_names(ids: List[int], event_names: Dict[int, str]) -> str:
    if not ids:
        return "无"
    return ", ".join(event_names.get(eid, f"事件{eid}") for eid in sorted(ids))


def format_html_report(
    result: Dict[str, Any],
    video_paths: Dict[str, str],
    report_paths: Dict[str, str],
    report_contents: Dict[str, str],
) -> str:
    """Build an interactive HTML report with inline embedded markdown content.

    All video and report paths use file:// absolute URLs.
    Markdown report contents are base64-encoded and embedded directly in the
    HTML to avoid CORS issues when opening via file:// protocol.
    """
    per_event = result["per_event"]
    overall = result["overall"]
    per_video = result.get("per_video", [])

    # Summary table rows
    summary_rows = []
    for eid in range(NUM_EVENTS):
        ev = per_event[str(eid)]
        summary_rows.append(
            f"<tr>"
            f"<td>{eid}</td>"
            f"<td>{html.escape(ev['name'])}</td>"
            f"<td>{ev['gt_count']}</td>"
            f"<td>{ev['tp']}</td>"
            f"<td>{ev['fp']}</td>"
            f"<td>{ev['fn']}</td>"
            f"<td>{ev['precision']:.4f}</td>"
            f"<td>{ev['recall']:.4f}</td>"
            f"<td>{ev['f1']:.4f}</td>"
            f"</tr>"
        )

    # Detail table rows + inline report data
    detail_rows = []
    report_data_js = []
    for i, row in enumerate(per_video, 1):
        video_name = row["video"]
        pred = row.get("predicted", [])
        gt = row.get("ground_truth", [])
        ok = row.get("is_correct", False)
        pred_text = _ids_to_names(pred, EVENT_NAMES)
        gt_text = _ids_to_names(gt, EVENT_NAMES)

        video_href = video_paths.get(video_name, "")
        report_href = report_paths.get(video_name, "")
        row_class = "row-ok" if ok else "row-fail"
        status = "✅" if ok else "❌"

        detail_rows.append(
            f'<tr class="{row_class}" data-video="{html.escape(video_href, quote=True)}" '
            f'data-report="{html.escape(report_href, quote=True)}">'
            f"<td>{i}</td>"
            f'<td><a class="link-video" href="{html.escape(video_href)}">'
            f"{html.escape(video_name)}</a></td>"
            f"<td>{html.escape(pred_text)}</td>"
            f"<td>{html.escape(gt_text)}</td>"
            f'<td><a class="link-report" href="{html.escape(report_href)}">'
            f"{html.escape(Path(report_href).name if report_href else '')}</a></td>"
            f'<td class="status">{status}</td>'
            f"</tr>"
        )

        # Embed report content as base64-encoded data
        md_content = report_contents.get(video_name, "")
        md_b64 = base64.b64encode(md_content.encode("utf-8")).decode("ascii")
        report_data_js.append(f'    "{html.escape(video_name)}": "{md_b64}"')

    report_data_js_str = ",\n".join(report_data_js)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>交通事件检测评估报告</title>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <style>
    :root {{
      --bg: #0f1419;
      --surface: #1a2332;
      --border: #2d3a4f;
      --text: #e7ecf3;
      --muted: #8b9cb3;
      --accent: #3b82f6;
      --ok: #22c55e;
      --fail: #ef4444;
      --ok-bg: rgba(34, 197, 94, 0.12);
      --fail-bg: rgba(239, 68, 68, 0.1);
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ height: 100%; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}
    header {{
      flex-shrink: 0;
      padding: 1rem 1.25rem;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      z-index: 10;
    }}
    h1 {{ margin: 0 0 0.35rem; font-size: 1.25rem; }}
    .meta {{ color: var(--muted); font-size: 0.85rem; }}
    .meta span {{ margin-right: 1rem; }}
    .layout {{
      flex: 1;
      display: flex;
      min-height: 0;
      overflow: hidden;
    }}
    .layout-left {{
      flex: 0 0 58%;
      max-width: 58%;
      overflow-y: auto;
      padding: 1rem 1.25rem 1.5rem;
      border-right: 1px solid var(--border);
    }}
    .layout-right {{
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
      background: var(--surface);
      position: sticky;
      top: 0;
      height: 100%;
      overflow: hidden;
    }}
    h2 {{ font-size: 1.05rem; margin: 1.25rem 0 0.65rem; color: var(--accent); }}
    h2:first-child {{ margin-top: 0; }}
    .toolbar {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      align-items: center;
      margin-bottom: 0.75rem;
    }}
    .toolbar label {{ color: var(--muted); font-size: 0.85rem; }}
    .toolbar button, .panel-tabs button {{
      background: var(--bg);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 0.35rem 0.75rem;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.85rem;
    }}
    .toolbar button:hover, .toolbar button.active,
    .panel-tabs button:hover, .panel-tabs button.active {{
      border-color: var(--accent);
      background: rgba(59, 130, 246, 0.15);
    }}
    .table-wrap {{
      overflow-x: auto;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
    }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
    th, td {{ padding: 0.45rem 0.6rem; text-align: left; border-bottom: 1px solid var(--border); }}
    th {{
      background: #243044;
      font-weight: 600;
      white-space: nowrap;
      position: sticky;
      top: 0;
      z-index: 1;
    }}
    #detail-table tbody tr:hover {{ background: rgba(59, 130, 246, 0.08); cursor: pointer; }}
    tr.row-ok {{ background: var(--ok-bg); }}
    tr.row-fail {{ background: var(--fail-bg); }}
    tr.hidden {{ display: none; }}
    tr.selected {{ outline: 2px solid var(--accent); outline-offset: -2px; }}
    .status {{ font-size: 1.1rem; text-align: center; }}
    a {{ color: #60a5fa; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    tfoot td {{ font-weight: 600; background: #243044; }}
    .panel-tabs {{
      flex-shrink: 0;
      display: flex;
      gap: 0.5rem;
      padding: 0.65rem 0.75rem;
      border-bottom: 1px solid var(--border);
    }}
    .panel-body {{
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}
    .panel-video-wrap {{
      flex-shrink: 0;
      padding: 0 0.75rem 0.5rem;
      border-bottom: 1px solid var(--border);
    }}
    .panel-body.tab-report-focus .panel-video-wrap {{
      flex-shrink: 1;
      max-height: 28vh;
    }}
    .panel-body.tab-report-focus .panel-video-wrap #player {{
      min-height: 160px;
      height: 24vh;
      max-height: 28vh;
    }}
    .panel-body.tab-video-focus .panel-report-wrap {{
      max-height: 35vh;
    }}
    #player-title {{
      font-size: 0.8rem;
      color: var(--muted);
      margin-bottom: 0.35rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    #player-title strong {{ color: var(--text); }}
    #player {{
      width: 100%;
      height: min(52vh, calc(100vh - 12rem));
      min-height: 280px;
      max-height: 58vh;
      object-fit: contain;
      background: #000;
      border-radius: 6px;
      display: block;
    }}
    .panel-report-wrap {{
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
      padding: 0 0.75rem 0.75rem;
      overflow: hidden;
    }}
    #report-title {{
      flex-shrink: 0;
      font-size: 0.85rem;
      font-weight: 600;
      color: var(--text);
      margin: 0 0 0.5rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    #report-preview {{
      flex: 1;
      overflow-y: auto;
      padding: 0.75rem 1rem;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      font-size: 0.88rem;
    }}
    #report-preview.loading, #report-preview.error {{
      color: var(--muted);
      font-style: italic;
    }}
    #report-preview.error {{ color: #fca5a5; }}
    #report-preview .md-content h1, #report-preview .md-content h2,
    #report-preview .md-content h3 {{
      color: var(--text);
      margin: 1rem 0 0.5rem;
      line-height: 1.3;
    }}
    #report-preview .md-content h1 {{ font-size: 1.25rem; border-bottom: 1px solid var(--border); padding-bottom: 0.35rem; }}
    #report-preview .md-content h2 {{ font-size: 1.1rem; color: var(--accent); }}
    #report-preview .md-content h3 {{ font-size: 1rem; }}
    #report-preview .md-content p, #report-preview .md-content li {{
      color: var(--text);
    }}
    #report-preview .md-content a {{ color: #60a5fa; }}
    #report-preview .md-content code {{
      background: #243044;
      padding: 0.15em 0.4em;
      border-radius: 4px;
      font-size: 0.9em;
    }}
    #report-preview .md-content pre {{
      background: #0d1117;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.75rem 1rem;
      overflow-x: auto;
    }}
    #report-preview .md-content pre code {{
      background: none;
      padding: 0;
    }}
    #report-preview .md-content table {{
      width: 100%;
      border-collapse: collapse;
      margin: 0.75rem 0;
      font-size: 0.85rem;
    }}
    #report-preview .md-content th, #report-preview .md-content td {{
      border: 1px solid var(--border);
      padding: 0.4rem 0.55rem;
    }}
    #report-preview .md-content th {{
      background: #243044;
    }}
    #report-preview .md-content blockquote {{
      border-left: 3px solid var(--accent);
      margin: 0.5rem 0;
      padding-left: 1rem;
      color: var(--muted);
    }}
    #report-preview .md-content ul, #report-preview .md-content ol {{
      padding-left: 1.5rem;
    }}
    .hint {{
      margin-top: 1rem;
      padding: 0.75rem 1rem;
      background: rgba(59, 130, 246, 0.1);
      border: 1px solid var(--border);
      border-radius: 8px;
      font-size: 0.78rem;
      color: var(--muted);
    }}
    .hint code {{ color: #93c5fd; }}
    @media (max-width: 1023px) {{
      body {{ overflow: auto; height: auto; }}
      .layout {{ flex-direction: column; overflow: visible; }}
      .layout-left {{
        flex: none;
        max-width: none;
        overflow: visible;
        border-right: none;
        border-bottom: 1px solid var(--border);
      }}
      .layout-right {{
        position: relative;
        height: auto;
        min-height: 420px;
      }}
      #player {{
        min-height: 240px;
        height: 45vh;
        max-height: none;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>交通事件检测评估结果</h1>
    <div class="meta">
      <span>总视频数: {result['total_videos']}</span>
      <span>已评估: {result['evaluated_videos']}</span>
      <span>跳过: {result['skipped_videos']}</span>
      <span>宏平均 F1: <strong>{overall['macro_f1']:.4f}</strong></span>
    </div>
  </header>
  <div class="layout">
    <main class="layout-left">
      <h2>按事件类别统计</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>事件ID</th><th>事件名称</th><th>总数</th><th>TP</th><th>FP</th><th>FN</th>
              <th>精确率</th><th>召回率</th><th>F1</th>
            </tr>
          </thead>
          <tbody>
            {''.join(summary_rows)}
          </tbody>
          <tfoot>
            <tr>
              <td colspan="2">总体（宏平均）</td>
              <td>-</td><td>-</td><td>-</td><td>-</td>
              <td>{overall['macro_precision']:.4f}</td>
              <td>{overall['macro_recall']:.4f}</td>
              <td><strong>{overall['macro_f1']:.4f}</strong></td>
            </tr>
          </tfoot>
        </table>
      </div>

      <h2>逐视频详细结果</h2>
      <div class="toolbar">
        <label>筛选：</label>
        <button type="button" class="filter-btn active" data-filter="all">全部</button>
        <button type="button" class="filter-btn" data-filter="ok">仅正确 ✅</button>
        <button type="button" class="filter-btn" data-filter="fail">仅错误 ❌</button>
      </div>
      <div class="table-wrap" id="detail-table-wrap">
        <table id="detail-table">
          <thead>
            <tr>
              <th>序号</th><th>视频文件</th><th>预测结果</th><th>Ground Truth</th>
              <th>报告文件</th><th>是否正确</th>
            </tr>
          </thead>
          <tbody>
            {''.join(detail_rows)}
          </tbody>
        </table>
      </div>
      <p class="hint">
        点击表格行播放视频并在右侧预览 Markdown 报告；点击报告链接切换到报告预览。
        所有报告内容已嵌入本 HTML 文件中，无需 HTTP 服务即可预览。
      </p>
    </main>

    <aside class="layout-right" id="preview-panel">
      <div class="panel-tabs">
        <button type="button" class="tab-btn active" data-tab="video">视频</button>
        <button type="button" class="tab-btn" data-tab="report">报告</button>
      </div>
      <div class="panel-body tab-video-focus" id="panel-body">
        <div class="panel-video-wrap" id="pane-video">
          <div id="player-title">当前：<strong id="now-playing">（点击表格中的视频）</strong></div>
          <video id="player" controls preload="metadata"></video>
        </div>
        <div class="panel-report-wrap" id="pane-report">
          <h3 id="report-title">报告预览</h3>
          <div id="report-preview">
            <span class="placeholder">点击表格中的报告文件链接以预览</span>
          </div>
        </div>
      </div>
    </aside>
  </div>

  <script>
    const REPORT_DATA = {{
{report_data_js_str}
    }};

    const player = document.getElementById('player');
    const nowPlaying = document.getElementById('now-playing');
    const reportTitle = document.getElementById('report-title');
    const reportPreview = document.getElementById('report-preview');
    const tabBtns = document.querySelectorAll('.tab-btn');
    const panelBody = document.getElementById('panel-body');
    const paneVideo = document.getElementById('pane-video');
    const paneReport = document.getElementById('pane-report');
    let selectedRow = null;

    function setActiveTab(tab) {{
      tabBtns.forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
      panelBody.classList.toggle('tab-video-focus', tab === 'video');
      panelBody.classList.toggle('tab-report-focus', tab === 'report');
      const target = tab === 'video' ? paneVideo : paneReport;
      target.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
    }}

    tabBtns.forEach(btn => {{
      btn.addEventListener('click', () => setActiveTab(btn.dataset.tab));
    }});

    function playVideo(href, label) {{
      const url = encodeURI(href).replace(/#/g, '%23');
      // Use <source> element to properly set video type for MP4 files
      player.innerHTML = '';
      const source = document.createElement('source');
      source.src = url;
      source.type = 'video/mp4';
      player.appendChild(source);
      player.load();
      player.play().catch(() => {{}});
      nowPlaying.textContent = label || href;
      setActiveTab('video');
    }}

    function selectRow(tr) {{
      if (selectedRow) selectedRow.classList.remove('selected');
      selectedRow = tr;
      tr.classList.add('selected');
    }}

    function loadReport(videoName, label) {{
      reportTitle.textContent = label || videoName;
      reportPreview.className = 'loading';
      reportPreview.innerHTML = '加载中…';
      setActiveTab('report');

      const mdB64 = REPORT_DATA[videoName];
      if (!mdB64) {{
        reportPreview.className = 'error';
        reportPreview.textContent = '未找到该视频的报告内容';
        return;
      }}

      try {{
        // Proper UTF-8 decoding: atob() returns Latin-1 bytes; decode them as UTF-8
        const bytes = Uint8Array.from(atob(mdB64), c => c.charCodeAt(0));
        const md = new TextDecoder('utf-8').decode(bytes);
        if (typeof marked !== 'undefined') {{
          marked.setOptions({{ breaks: true, gfm: true }});
          reportPreview.className = '';
          reportPreview.innerHTML = '<div class="md-content">' + marked.parse(md) + '</div>';
        }} else {{
          reportPreview.className = 'error';
          reportPreview.textContent = 'marked.js 未加载，无法渲染 Markdown';
        }}
      }} catch (err) {{
        reportPreview.className = 'error';
        reportPreview.textContent = '解码报告内容失败: ' + String(err);
      }}
    }}

    document.querySelectorAll('#detail-table tbody tr').forEach(tr => {{
      tr.addEventListener('click', (e) => {{
        if (e.target.closest('a.link-report')) return;
        const video = tr.dataset.video;
        const label = tr.querySelector('.link-video')?.textContent || video;
        const videoName = tr.querySelector('.link-video')?.textContent || '';
        selectRow(tr);
        playVideo(video, label);
      }});
    }});

    document.querySelectorAll('a.link-video').forEach(a => {{
      a.addEventListener('click', (e) => {{
        e.preventDefault();
        e.stopPropagation();
        const tr = a.closest('tr');
        selectRow(tr);
        playVideo(a.getAttribute('href'), a.textContent);
      }});
    }});

    document.querySelectorAll('a.link-report').forEach(a => {{
      a.addEventListener('click', (e) => {{
        e.preventDefault();
        e.stopPropagation();
        const tr = a.closest('tr');
        const videoName = tr.querySelector('.link-video')?.textContent || '';
        selectRow(tr);
        loadReport(videoName, a.textContent);
      }});
    }});

    document.querySelectorAll('.filter-btn').forEach(btn => {{
      btn.addEventListener('click', () => {{
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const f = btn.dataset.filter;
        document.querySelectorAll('#detail-table tbody tr').forEach(tr => {{
          const ok = tr.classList.contains('row-ok');
          const show = f === 'all' || (f === 'ok' && ok) || (f === 'fail' && !ok);
          tr.classList.toggle('hidden', !show);
        }});
      }});
    }});

    setActiveTab('video');
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog="batch_evaluate",
        description="Batch evaluation for traffic event detection results.",
    )
    parser.add_argument(
        "--video-dir", "-v",
        required=True,
        help="Directory containing video files (for ground-truth extraction).",
    )
    parser.add_argument(
        "--report-dir", "-r",
        required=True,
        help="Directory containing inference reports.",
    )
    parser.add_argument(
        "--output",
        default="evaluation_report.html",
        help=(
            "Output file path. Format is auto-detected from extension: "
            ".html -> interactive HTML report (default), "
            ".md -> Markdown table, "
            ".json -> JSON data."
        ),
    )
    parser.add_argument(
        "--gt-mode",
        default="filename",
        choices=["filename", "annotation_file"],
        help="Ground-truth extraction mode (default: filename).",
    )
    parser.add_argument(
        "--annotation-file",
        default=None,
        help="Path to annotation JSON/CSV file (required when gt-mode=annotation_file).",
    )
    parser.add_argument(
        "--single-class",
        action="store_true",
        help=(
            "Single-class mode: only evaluate events with is_active=true in "
            "event_categories.yaml. All inactive events are excluded from metrics."
        ),
    )
    parser.add_argument(
        "--config-dir", "-c",
        default="./traffic_analyzer/config",
        help=(
            "Path to configuration directory containing event_categories.yaml "
            "(default: ./traffic_analyzer/config). Used with --single-class."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO).",
    )
    args = parser.parse_args(argv)

    logger = _setup_logging(args.log_level)

    # Load active events for single-class mode
    active_event_ids: Optional[Set[int]] = None
    if args.single_class:
        config_dir = Path(args.config_dir).expanduser().resolve()
        try:
            active_event_ids = load_active_events_from_config(config_dir)
            logger.info(
                "Single-class mode: evaluating %d active events from config: %s",
                len(active_event_ids),
                sorted(active_event_ids),
            )
        except FileNotFoundError as exc:
            logger.error("%s", exc)
            return 1

    video_dir = Path(args.video_dir).expanduser().resolve()
    report_dir = Path(args.report_dir).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()

    if not video_dir.is_dir():
        logger.error("Video directory not found: %s", video_dir)
        return 1
    if not report_dir.is_dir():
        logger.error("Report directory not found: %s", report_dir)
        return 1

    # Load annotations if needed
    annotations: Optional[Dict[str, Set[int]]] = None
    if args.gt_mode == "annotation_file":
        if not args.annotation_file:
            logger.error("--annotation-file is required when --gt-mode=annotation_file")
            return 1
        annotation_path = Path(args.annotation_file).expanduser().resolve()
        if not annotation_path.exists():
            logger.error("Annotation file not found: %s", annotation_path)
            return 1
        annotations = load_annotations(annotation_path)
        logger.info("Loaded annotations for %d videos from %s", len(annotations), annotation_path)

    # Find all reports
    report_paths = sorted(
        p for p in report_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".md", ".json")
    )
    if not report_paths:
        logger.error("No report files (.md or .json) found in %s", report_dir)
        return 1

    logger.info("Found %d report(s) in %s", len(report_paths), report_dir)

    predictions: List[Set[int]] = []
    ground_truths: List[Set[int]] = []
    single_class_masks: List[Optional[Set[int]]] = []
    video_names: List[str] = []
    skipped_videos: List[str] = []
    # Extra per-video data for markdown report (not added to JSON to keep it serializable)
    per_video_extra: List[Dict[str, Any]] = []
    # For HTML report: absolute file:// paths and embedded report contents
    video_paths_map: Dict[str, str] = {}
    report_paths_map: Dict[str, str] = {}
    report_contents_map: Dict[str, str] = {}

    for report_path in report_paths:
        # Determine corresponding video name
        # Report stem may be same as video stem (e.g., video.mp4 -> video.md)
        report_stem = report_path.stem

        # Try to find matching video
        video_path: Optional[Path] = None
        for ext in (".mp4", ".avi", ".mov", ".mkv", ".wmv"):
            candidate = video_dir / (report_stem + ext)
            if candidate.exists():
                video_path = candidate
                break

        # Also try stripping common suffixes from report name
        if video_path is None:
            for suffix in ("_report", "_tool_call_report", "_tool_call_report_v2"):
                if report_stem.endswith(suffix):
                    base = report_stem[: -len(suffix)]
                    for ext in (".mp4", ".avi", ".mov", ".mkv", ".wmv"):
                        candidate = video_dir / (base + ext)
                        if candidate.exists():
                            video_path = candidate
                            break
                    if video_path:
                        break

        if video_path is None:
            logger.warning("No matching video found for report: %s", report_path.name)
            skipped_videos.append(report_path.name)
            continue

        # Extract prediction
        pred = extract_pred(report_path)

        # Extract ground truth
        if annotations is not None:
            gt = annotations.get(video_path.name, set())
            if video_path.name not in annotations:
                logger.warning("No annotation for video: %s", video_path.name)
        else:
            gt = extract_gt_from_filename(video_path.name)

        # Single-class mask (from config is_active)
        mask: Optional[Set[int]] = None
        if args.single_class:
            mask = active_event_ids

        predictions.append(pred)
        ground_truths.append(gt)
        single_class_masks.append(mask)
        video_names.append(video_path.name)

        # Build per-video extra data for markdown (with absolute paths and names)
        predicted_names = [EVENT_NAMES.get(eid, f"事件{eid}") for eid in sorted(pred)]
        ground_truth_names = [EVENT_NAMES.get(eid, f"事件{eid}") for eid in sorted(gt)]
        is_correct = pred == gt

        video_abs = str(video_path.resolve())
        report_abs = str(report_path.resolve())

        # Use relative paths for HTML so the report works when packaged and shared
        try:
            html_base_dir = output_path.parent.resolve()
            video_rel = video_path.resolve().relative_to(html_base_dir)
            report_rel = report_path.resolve().relative_to(html_base_dir)
            video_uri = str(video_rel)
            report_uri = str(report_rel)
        except ValueError:
            # Fallback to absolute file:// URIs when paths are on different filesystems
            video_uri = f"file://{video_abs}"
            report_uri = f"file://{report_abs}"

        # Read report content for HTML embedding
        try:
            report_text = report_path.read_text(encoding="utf-8")
        except Exception:
            report_text = ""

        video_paths_map[video_path.name] = video_uri
        report_paths_map[video_path.name] = report_uri
        report_contents_map[video_path.name] = report_text

        per_video_extra.append({
            "video": video_path.name,
            "video_path": video_abs,
            "report_path": report_abs,
            "predicted": sorted(pred),
            "ground_truth": sorted(gt),
            "predicted_names": predicted_names,
            "ground_truth_names": ground_truth_names,
            "is_correct": is_correct,
            "mask": sorted(mask) if mask else None,
        })

        logger.debug("%s: pred=%s, gt=%s", video_path.name, pred, gt)

    total_videos = len(report_paths)
    evaluated_videos = len(predictions)
    skipped_count = total_videos - evaluated_videos

    if evaluated_videos == 0:
        logger.error("No videos could be evaluated.")
        return 1

    # Compute metrics
    metrics, eval_count, skip_count = compute_metrics(
        predictions, ground_truths, single_class_masks
    )

    result = {
        **metrics,
        "total_videos": total_videos,
        "evaluated_videos": evaluated_videos,
        "skipped_videos": skipped_count,
        "single_class_mode": args.single_class,
        "gt_mode": args.gt_mode,
        "per_video": [
            {
                "video": pv["video"],
                "predicted": pv["predicted"],
                "ground_truth": pv["ground_truth"],
                "mask": pv["mask"],
            }
            for pv in per_video_extra
        ],
    }

    # Write output (format determined by file extension)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = output_path.suffix.lower()
    if suffix == ".md":
        # For markdown output, include per-video extra data (paths, names, correctness)
        md_result = {**result, "per_video": per_video_extra}
        output_path.write_text(format_markdown_table(md_result), encoding="utf-8")
    elif suffix == ".html":
        # For HTML output, build interactive report with inline embedded data
        html_result = {**result, "per_video": per_video_extra}
        html_content = format_html_report(
            html_result, video_paths_map, report_paths_map, report_contents_map
        )
        output_path.write_text(html_content, encoding="utf-8")
    else:
        output_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    logger.info("Evaluation result written to %s", output_path)

    # Print markdown table (with extra per-video data)
    md_result = {**result, "per_video": per_video_extra}
    print_markdown_table(md_result)

    return 0


if __name__ == "__main__":
    sys.exit(main())
