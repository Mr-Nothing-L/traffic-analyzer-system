"""Command-line interface for the traffic analyzer framework."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.models.schemas import SceneInfo
from traffic_analyzer.orchestrator.analysis_orchestrator import AnalysisOrchestrator


def _setup_logging(log_level: str = "INFO") -> None:
    """Configure root logger with coloured stderr output."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _resolve_path(path: str) -> str:
    """Resolve a user-supplied path (expand home, make absolute)."""
    return str(Path(path).expanduser().resolve())


def cmd_analyze(args: argparse.Namespace) -> int:
    """Run the full analysis pipeline."""
    _setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    video_path = _resolve_path(args.video)
    if not Path(video_path).exists():
        logger.error("Video file not found: %s", video_path)
        return 1

    config_dir = _resolve_path(args.config_dir)
    if not Path(config_dir).is_dir():
        logger.error("Config directory not found: %s", config_dir)
        return 1

    output_path = _resolve_path(args.output) if args.output else None

    # Pass --min-frames to the system via environment variables
    if args.min_frames != 30:
        os.environ["SCENE_UNDERSTANDING_MIN_FRAMES"] = str(args.min_frames)
        os.environ["VLM_MAX_FRAMES"] = str(args.min_frames)
        logger.info("Max VLM input frames set to %d (scene_understanding + expert_agent)", args.min_frames)

    # Load external scene understanding if provided
    scene_understanding: Optional[SceneInfo] = None
    if args.scene_understanding:
        su_path = _resolve_path(args.scene_understanding)
        if not Path(su_path).exists():
            logger.error("Scene understanding file not found: %s", su_path)
            return 1
        try:
            with open(su_path, "r", encoding="utf-8") as f:
                scene_understanding = SceneInfo.model_validate(json.load(f))
            logger.info("Loaded external scene understanding from %s", su_path)
        except json.JSONDecodeError as exc:
            logger.error("Invalid JSON in scene understanding file: %s", exc)
            return 1
        except Exception as exc:
            logger.error("Failed to read scene understanding file: %s", exc)
            return 1

    try:
        orchestrator = AnalysisOrchestrator.from_config_dir(config_dir)
        report = orchestrator.analyze(
            video_path,
            scene_understanding=scene_understanding,
        )
    except Exception as exc:
        logger.exception("Analysis failed: %s", exc)
        return 1

    # Serialize report
    fmt = args.format.lower()
    if fmt == "json":
        text = report.model_dump_json(indent=2, ensure_ascii=False)
    elif fmt == "markdown":
        from traffic_analyzer.core.report_generator import ReportGenerator
        text = ReportGenerator().to_markdown(report)
    else:
        logger.error("Unknown output format: %s", fmt)
        return 1

    if output_path:
        Path(output_path).write_text(text, encoding="utf-8")
        logger.info("Report written to %s", output_path)
    else:
        print(text)

    return 0


def cmd_validate_config(args: argparse.Namespace) -> int:
    """Validate configuration files without running analysis."""
    _setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    config_dir = _resolve_path(args.config_dir)
    if not Path(config_dir).is_dir():
        logger.error("Config directory not found: %s", config_dir)
        return 1

    try:
        manager = ConfigManager(config_dir)
        config = manager.load_all()
        categories = manager.get_event_categories()
        logger.info("Loaded %d event categories", len(categories))
        for cat in categories:
            logger.info("  [%d] %s (%s)", cat.event_id, cat.name, cat.detection_mode.value)

        # Cross-reference validation
        errors = manager.validate_config()
        if errors:
            logger.error("Configuration has %d cross-reference error(s):", len(errors))
            for err in errors:
                logger.error("  - %s", err)
            return 1

        logger.info("Configuration is valid.")
        return 0
    except Exception as exc:
        logger.exception("Configuration validation failed: %s", exc)
        return 1


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="traffic-analyzer",
        description="LLM/VLM-based traffic event detection framework.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 1.0.0",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- analyze ---
    analyze_parser = subparsers.add_parser(
        "analyze",
        help="Run analysis on a video file.",
    )
    analyze_parser.add_argument(
        "--video", "-v",
        required=True,
        help="Path to the input video file.",
    )
    analyze_parser.add_argument(
        "--scene-understanding", "-s",
        default=None,
        help="Optional path to external scene understanding JSON file.",
    )
    analyze_parser.add_argument(
        "--config-dir", "-d",
        default="./traffic_analyzer/config",
        help="Path to the configuration directory (default: ./traffic_analyzer/config).",
    )
    analyze_parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output file path. Prints to stdout if omitted.",
    )
    analyze_parser.add_argument(
        "--format", "-f",
        default="json",
        choices=["json", "markdown"],
        help="Output format (default: json).",
    )
    analyze_parser.add_argument(
        "--min-frames", "-m",
        type=int,
        default=30,
        help="Minimum number of frames for scene understanding (default: 30). Lower = faster but less accurate.",
    )
    analyze_parser.set_defaults(func=cmd_analyze)

    # --- validate-config ---
    validate_parser = subparsers.add_parser(
        "validate-config",
        help="Validate configuration files.",
    )
    validate_parser.add_argument(
        "--config-dir", "-d",
        default="./traffic_analyzer/config",
        help="Path to the configuration directory (default: ./traffic_analyzer/config).",
    )
    validate_parser.set_defaults(func=cmd_validate_config)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Main entry point for the CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
