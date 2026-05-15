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
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


def _setup_logging(log_level: str = "INFO") -> logging.Logger:
    """Configure root logger and return a named logger."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger("batch_infer")


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
    logger: logging.Logger,
) -> bool:
    """Run analysis on a single video via subprocess. Returns True on success."""
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

    logger.debug("Running: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=3600,  # 1 hour per video
        )
        if result.returncode != 0:
            logger.error("Analysis failed for %s (exit %d)", video_path.name, result.returncode)
            if result.stderr:
                logger.error("stderr: %s", result.stderr[:500])
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("Analysis timed out for %s", video_path.name)
        return False
    except Exception as exc:
        logger.error("Analysis error for %s: %s", video_path.name, exc)
        return False


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
        default=30,
        help="Max VLM input frames (default: 30).",
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

    start_time = time.time()

    for idx, video_path in enumerate(videos, start=1):
        output_path = _build_output_path(video_path, output_dir, args.format)

        if output_path.exists() and not args.force:
            logger.info("[%d/%d] SKIP (exists): %s", idx, total, video_path.name)
            skipped += 1
            continue

        # Look for matching CV tracks file
        cv_tracks_path: Optional[Path] = None
        if cv_tracks_dir:
            candidate = cv_tracks_dir / (video_path.stem + ".json")
            if candidate.exists():
                cv_tracks_path = candidate

        logger.info("[%d/%d] Processing: %s", idx, total, video_path.name)
        success = _run_single(
            video_path=video_path,
            output_path=output_path,
            config_dir=config_dir,
            fmt=args.format,
            min_frames=args.min_frames,
            cv_tracks_path=cv_tracks_path,
            logger=logger,
        )
        if success:
            processed += 1
            logger.info("[%d/%d] DONE: %s -> %s", idx, total, video_path.name, output_path.name)
        else:
            failed += 1
            logger.error("[%d/%d] FAILED: %s", idx, total, video_path.name)

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
