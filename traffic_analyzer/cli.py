"""Command-line interface for the traffic analyzer framework.

[文件说明]
作用:CLI 入口,提供 analyze / validate-config / web 三个子命令。analyze 将
--min-frames、--sft-label(置 SFT_LABEL_ENABLE)、--sft-output-dir 等参数转为环境变量后,
驱动 AnalysisOrchestrator 完成视频分析并输出 json/markdown 报告;validate-config 校验配置
交叉引用;web 经 uvicorn 工厂模式启动 web/app.py 的 create_app。
上游:traffic_analyzer/__main__.py(python -m traffic_analyzer);scripts/infer.sh、
scripts/analyze.sh、scripts/batch_infer.py、web/jobs.py 的子进程命令。
下游:core/config_manager.py、orchestrator/analysis_orchestrator.py、
core/report_generator.py(markdown 输出)、web/app.py(web 子命令)、config/ 配置目录。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import List, Optional, Sequence

from traffic_analyzer import __version__
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
    if args.min_frames is not None:
        os.environ["SCENE_UNDERSTANDING_MIN_FRAMES"] = str(args.min_frames)
        os.environ["VLM_MAX_FRAMES"] = str(args.min_frames)
        logger.info("Max VLM input frames set to %d (scene_understanding + expert_agent)", args.min_frames)

    # Pass --sft-label / --sft-output-dir to the system via environment variables
    if args.sft_label:
        os.environ["SFT_LABEL_ENABLE"] = "true"
        logger.info("SFT label rewrite enabled")
    if args.sft_output_dir is not None:
        os.environ["SFT_LABEL_OUTPUT_DIR"] = args.sft_output_dir
        logger.info("SFT label output directory set to %s", args.sft_output_dir)

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
        report_output_dir = str(Path(output_path).parent) if output_path else None
        report = orchestrator.analyze(
            video_path,
            scene_understanding=scene_understanding,
            output_dir=report_output_dir,
        )
    except Exception as exc:
        logger.exception("Analysis failed: %s", exc)
        return 1

    # Prefilter rejection — don't save any output file
    if report.rejected:
        logger.info(
            "Video rejected by prefilter: %s | reason=%s | no report saved",
            video_path,
            report.reject_reason,
        )
        return 2

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


def cmd_web(args: argparse.Namespace) -> int:
    """Launch the web UI server (uvicorn + FastAPI backend)."""
    _setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    try:
        import uvicorn
    except ImportError:
        logger.error("The web UI requires extra dependencies: pip install fastapi uvicorn")
        return 1

    if args.workspace is not None:
        workspace = _resolve_path(args.workspace)
        if not Path(workspace).is_dir():
            logger.error("Workspace directory not found: %s", workspace)
            return 1
        # Factory mode cannot forward arguments — pass via environment.
        os.environ["TRAFFIC_ANALYZER_WEB_WORKSPACE"] = workspace
        logger.info("Workspace preset to %s", workspace)

    url = f"http://{args.host}:{args.port}"

    # 浏览器自动打开改为 opt-in:显式 BROWSER=true 才打开(默认无头,
    # 测试/部署/后台运行不再每次弹标签页)。
    if os.environ.get("BROWSER", "").lower() in ("1", "true", "yes"):

        def _open_browser() -> None:
            time.sleep(1.0)
            try:
                webbrowser.open(url)
            except Exception as exc:
                logger.warning("Could not open browser: %s", exc)

        threading.Thread(target=_open_browser, daemon=True).start()

    logger.info("Starting web UI at %s", url)
    uvicorn.run(
        "traffic_analyzer.web.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="traffic-analyzer",
        description="LLM/VLM-based traffic event detection framework.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
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
        default=None,
        help="Minimum number of frames for scene understanding (default: 10). Lower = faster but less accurate.",
    )
    analyze_parser.add_argument(
        "--sft-label",
        action="store_true",
        help="Enable SFT label rewrite: export one SFT training sample per video after adjudication.",
    )
    analyze_parser.add_argument(
        "--sft-output-dir",
        default=None,
        help="Output directory for SFT label samples (default: output/sft_labels).",
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

    # --- web ---
    web_parser = subparsers.add_parser(
        "web",
        help="Launch the web UI server.",
    )
    web_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default: 127.0.0.1).",
    )
    web_parser.add_argument(
        "--port",
        type=int,
        default=8600,
        help="Bind port (default: 8600).",
    )
    web_parser.add_argument(
        "--workspace", "-w",
        default=None,
        help="Optional workspace directory to preselect.",
    )
    web_parser.set_defaults(func=cmd_web)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Main entry point for the CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
