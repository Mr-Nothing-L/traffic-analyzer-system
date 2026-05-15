#!/usr/bin/env python3
"""Batch evaluation script for traffic event detection results.

Compares inference reports against ground truth, computing per-event
precision, recall, and F1 score.

Usage:
    python scripts/batch_evaluate.py \
        --video-dir /path/to/videos \
        --report-dir /path/to/reports \
        --output evaluation_result.json

    # With annotation file
    python scripts/batch_evaluate.py \
        --video-dir /path/to/videos \
        --report-dir /path/to/reports \
        --gt-mode annotation_file \
        --annotation-file /path/to/annotations.json \
        --output evaluation_result.json

    # Single-class mode (only evaluate is_active=true events from config)
    python scripts/batch_evaluate.py \
        --video-dir /path/to/videos \
        --report-dir /path/to/reports \
        --single-class \
        --config-dir ./traffic_analyzer/config \
        --output evaluation_result.json
"""

from __future__ import annotations

import argparse
import csv
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

    Default pattern: numbers before ``_Event_`` are 1-based event IDs.
    Examples:
        ``01-02-07-11_Event_65536_...``  -> {0, 1, 6}
        ``02-04-07-08-10_Event_...``     -> {1, 3, 6, 7, 9}
        ``06_Event_...``                  -> {5}

    The number ``11`` does not map to any valid event (max is 10 -> event 9)
    and is silently ignored.
    """
    # Match the prefix before _Event_
    match = re.match(r"^([\d\-]+)_Event_", filename)
    if not match:
        return set()

    prefix = match.group(1)
    event_ids: Set[int] = set()
    for part in prefix.split("-"):
        part = part.strip()
        if not part.isdigit():
            continue
        num = int(part)
        event_id = num - 1  # 1-based -> 0-based
        if 0 <= event_id < NUM_EVENTS:
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
def print_markdown_table(result: Dict[str, Any]) -> None:
    """Print evaluation results as a markdown table to stdout."""
    print("\n## 交通事件检测评估结果\n")
    print("| 事件ID | 事件名称 | TP | FP | FN | 精确率 | 召回率 | F1分数 |")
    print("|--------|----------|----|----|----|--------|--------|--------|")

    per_event = result["per_event"]
    for eid in range(NUM_EVENTS):
        info = per_event[str(eid)]
        print(
            f"| {eid} | {info['name']} | {info['tp']} | {info['fp']} | {info['fn']} | "
            f"{info['precision']:.4f} | {info['recall']:.4f} | {info['f1']:.4f} |"
        )

    overall = result["overall"]
    print("| **总体** | **宏平均** | - | - | - | "
          f"**{overall['macro_precision']:.4f}** | **{overall['macro_recall']:.4f}** | **{overall['macro_f1']:.4f}** |")

    print(f"\n- 总视频数: {result['total_videos']}")
    print(f"- 已评估视频数: {result['evaluated_videos']}")
    print(f"- 跳过视频数: {result['skipped_videos']}")
    print()


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
        default="evaluation_result.json",
        help="Path to write evaluation JSON (default: evaluation_result.json).",
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
                "video": vn,
                "predicted": sorted(pred),
                "ground_truth": sorted(gt),
                "mask": sorted(mask) if mask else None,
            }
            for vn, pred, gt, mask in zip(video_names, predictions, ground_truths, single_class_masks)
        ],
    }

    # Write JSON output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Evaluation result written to %s", output_path)

    # Print markdown table
    print_markdown_table(result)

    return 0


if __name__ == "__main__":
    sys.exit(main())
