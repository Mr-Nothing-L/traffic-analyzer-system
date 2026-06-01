#!/usr/bin/env python3
"""Batch inference script for traffic event detection.

Runs the traffic analyzer on all videos in a directory, producing reports
in the requested format (markdown or json).

Usage:
    python scripts/batch_infer.py \
        --video-dir /path/to/videos \
        --output-dir /path/to/reports \
        --format markdown
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _setup_logging(log_level: str = "INFO") -> logging.Logger:
    """Configure root logger and return a named logger."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger("batch_infer")


def _setup_file_logging(log_path: Path, log_level: str = "INFO") -> logging.Logger:
    """Configure a file-only logger for a single video."""
    logger = logging.getLogger(f"batch_infer.{log_path.stem}")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Avoid duplicate handlers on repeated calls
    if logger.handlers:
        logger.handlers.clear()

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    return logger


def _resolve_path(path: str) -> Path:
    """Resolve a user-supplied path (expand home, make absolute)."""
    return Path(path).expanduser().resolve()


def _find_videos(video_dir: Path, extensions: Tuple[str, ...] = (".mp4", ".avi", ".mov", ".mkv", ".wmv")) -> List[Path]:
    """Find all video files in the given directory (non-recursive)."""
    videos = [f for f in video_dir.iterdir() if f.is_file() and f.suffix.lower() in extensions]
    videos.sort()
    return videos


def _build_output_path(video_path: Path, output_dir: Path, fmt: str) -> Path:
    """Build output report path from video path and format."""
    suffix = ".md" if fmt == "markdown" else ".json"
    return output_dir / (video_path.stem + suffix)


def _run_single(
    video_path: Path,
    output_path: Path,
    config_dir: Path,
    fmt: str,
    min_frames: int,
    cv_tracks_path: Optional[Path],
    log_path: Optional[Path],
) -> Tuple[str, bool, str]:
    """Run analysis on a single video via subprocess.

    Returns (video_name, success, message) for aggregation by the main process.
    Per-video logs are written to *log_path* when provided.
    """
    cmd = [
        sys.executable, "-m", "traffic_analyzer", "analyze",
        "--video", str(video_path),
        "--config-dir", str(config_dir),
        "--format", fmt,
        "--output", str(output_path),
        "--min-frames", str(min_frames),
    ]
    if cv_tracks_path:
        cmd += ["--cv-tracks", str(cv_tracks_path)]

    # Optional per-video log file
    log_fh = None
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "w", encoding="utf-8")
        log_fh.write(f"[BEGIN] {video_path.name}\n")
        log_fh.write(f"CMD: {' '.join(cmd)}\n")
        log_fh.flush()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=3600,  # 1 hour per video
        )
        if log_fh:
            log_fh.write(f"\n[STDOUT]\n{result.stdout}\n")
            if result.stderr:
                log_fh.write(f"\n[STDERR]\n{result.stderr}\n")
            log_fh.write(f"\n[EXIT CODE] {result.returncode}\n")
            log_fh.flush()

        if result.returncode != 0:
            msg = f"FAILED (exit {result.returncode})"
            if result.stderr:
                msg += f" — {result.stderr[:200]}"
            if log_fh:
                log_fh.write(f"[END] {msg}\n")
            return video_path.name, False, msg

        msg = f"OK -> {output_path.name}"
        if log_fh:
            log_fh.write(f"[END] {msg}\n")
        return video_path.name, True, msg
    except subprocess.TimeoutExpired:
        msg = "FAILED (timeout after 3600s)"
        if log_fh:
            log_fh.write(f"[END] {msg}\n")
        return video_path.name, False, msg
    except Exception as exc:
        msg = f"FAILED ({exc})"
        if log_fh:
            log_fh.write(f"[END] {msg}\n")
        return video_path.name, False, msg
    finally:
        if log_fh:
            log_fh.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog="batch_infer",
        description="Batch inference for traffic event detection.",
    )
    parser.add_argument(
        "--video-dir", "-v",
        required=True,
        help="Input directory containing video files.",
    )
    parser.add_argument(
        "--output-dir", "-o",
        required=True,
        help="Output directory for reports.",
    )
    parser.add_argument(
        "--config-dir", "-c",
        default="./traffic_analyzer/config",
        help="Path to configuration directory (default: ./traffic_analyzer/config).",
    )
    parser.add_argument(
        "--format", "-f",
        default="markdown",
        choices=["markdown", "json"],
        help="Output format (default: markdown).",
    )
    parser.add_argument(
        "--min-frames", "-m",
        type=int,
        default=10,
        help="Max VLM input frames (default: 10).",
    )
    parser.add_argument(
        "--cv-tracks-dir",
        default=None,
        help="Optional directory containing CV track JSON files (named <video_stem>.json).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run analysis even if output already exists.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO).",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=4,
        help="Number of parallel workers (default: 4). Set to 1 for serial execution.",
    )
    parser.add_argument(
        "--log-dir", "-l",
        default=None,
        help="Optional directory for per-video log files.",
    )
    args = parser.parse_args(argv)

    logger = _setup_logging(args.log_level)

    video_dir = _resolve_path(args.video_dir)
    output_dir = _resolve_path(args.output_dir)
    config_dir = _resolve_path(args.config_dir)

    if not video_dir.is_dir():
        logger.error("Video directory not found: %s", video_dir)
        return 1
    if not config_dir.is_dir():
        logger.error("Config directory not found: %s", config_dir)
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)

    videos = _find_videos(video_dir)
    if not videos:
        logger.warning("No video files found in %s", video_dir)
        return 0

    cv_tracks_dir: Optional[Path] = None
    if args.cv_tracks_dir:
        cv_tracks_dir = _resolve_path(args.cv_tracks_dir)
        if not cv_tracks_dir.is_dir():
            logger.warning("CV tracks directory not found: %s", cv_tracks_dir)
            cv_tracks_dir = None

    total = len(videos)
    processed = 0
    failed = 0
    skipped = 0

    logger.info("Found %d video(s) in %s", total, video_dir)
    logger.info("Output directory: %s", output_dir)
    logger.info("Format: %s", args.format)
    logger.info("Workers: %d", args.workers)

    log_dir: Optional[Path] = None
    if args.log_dir:
        log_dir = _resolve_path(args.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Log directory: %s", log_dir)

    # ------------------------------------------------------------------
    # Build task list (skip already-processed unless --force)
    # ------------------------------------------------------------------
    tasks: List[Tuple[Path, Path, Path, str, int, Optional[Path], Optional[Path]]] = []
    for video_path in videos:
        output_path = _build_output_path(video_path, output_dir, args.format)
        if output_path.exists() and not args.force:
            skipped += 1
            continue

        # Look for matching CV tracks file
        cv_tracks_path: Optional[Path] = None
        if cv_tracks_dir:
            candidate = cv_tracks_dir / (video_path.stem + ".json")
            if candidate.exists():
                cv_tracks_path = candidate

        # Per-video log file
        per_video_log: Optional[Path] = None
        if log_dir:
            per_video_log = log_dir / (video_path.stem + ".log")

        tasks.append((
            video_path, output_path, config_dir,
            args.format, args.min_frames, cv_tracks_path, per_video_log,
        ))

    to_process = len(tasks)
    logger.info("Tasks: %d to process, %d skipped (already exists)", to_process, skipped)

    # ------------------------------------------------------------------
    # Execute: serial or parallel
    # ------------------------------------------------------------------
    start_time = time.time()

    if args.workers <= 1 or to_process == 1:
        # Serial execution
        for idx, task in enumerate(tasks, start=1):
            logger.info("[%d/%d] Processing: %s", idx, to_process, task[0].name)
            name, success, msg = _run_single(*task)
            if success:
                processed += 1
                logger.info("[%d/%d] DONE: %s", idx, to_process, msg)
            else:
                failed += 1
                logger.error("[%d/%d] FAILED: %s — %s", idx, to_process, name, msg)
    else:
        # Parallel execution via ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            future_to_name = {
                executor.submit(_run_single, *task): task[0].name
                for task in tasks
            }
            completed = 0
            for future in as_completed(future_to_name):
                completed += 1
                name, success, msg = future.result()
                if success:
                    processed += 1
                    logger.info("[%d/%d] DONE: %s", completed, to_process, msg)
                else:
                    failed += 1
                    logger.error("[%d/%d] FAILED: %s — %s", completed, to_process, name, msg)

    elapsed = time.time() - start_time
    logger.info("=" * 50)
    logger.info("Batch inference complete.")
    logger.info("  Total videos:    %d", total)
    logger.info("  Processed:       %d", processed)
    logger.info("  Skipped:         %d", skipped)
    logger.info("  Failed:          %d", failed)
    logger.info("  Elapsed time:    %.1f s", elapsed)
    logger.info("=" * 50)

    # Print concise summary to stdout as well
    print(f"\n{total}/{total} videos scanned, {processed} processed, {failed} failed, {skipped} skipped")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
