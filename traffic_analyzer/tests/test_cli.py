"""Unit tests for traffic_analyzer.cli module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from traffic_analyzer.cli import build_parser, cmd_analyze, cmd_validate_config, main


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_analyze_subcommand_exists(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--help"])

    def test_analyze_requires_video(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["analyze", "--video", "test.mp4"])
        assert args.video == "test.mp4"
        assert args.command == "analyze"

    def test_analyze_default_format_is_json(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["analyze", "--video", "test.mp4"])
        assert args.format == "json"

    def test_analyze_default_config_dir(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["analyze", "--video", "test.mp4"])
        assert args.config_dir == "./traffic_analyzer/config"

    def test_validate_config_subcommand_exists(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["validate-config"])
        assert args.command == "validate-config"

    def test_version_flag(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--version"])
        assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# cmd_analyze tests
# ---------------------------------------------------------------------------


class TestCmdAnalyze:
    @patch("traffic_analyzer.cli.Path.exists")
    @patch("traffic_analyzer.cli.AnalysisOrchestrator")
    def test_analyze_success_json_stdout(
        self,
        mock_orchestrator_cls: MagicMock,
        mock_exists: MagicMock,
    ) -> None:
        mock_exists.return_value = True
        mock_orchestrator = MagicMock()
        mock_orchestrator_cls.from_config_dir.return_value = mock_orchestrator

        from traffic_analyzer.models.schemas import (
            BinaryEncoding,
            Report,
            SceneInfo,
            VideoMetadata,
        )

        report = Report(
            video_info=VideoMetadata(
                file_path="test.mp4",
                file_name="test.mp4",
                duration_sec=10.0,
                fps=30.0,
                total_frames=300,
                width=640,
                height=480,
            ),
            scene_summary=SceneInfo(road_count=2),
            binary_encoding=BinaryEncoding(encoding_string="1_0", detected_events=[0]),
        )
        mock_orchestrator.analyze.return_value = report

        parser = build_parser()
        args = parser.parse_args(["analyze", "--video", "test.mp4"])

        with patch("builtins.print") as mock_print:
            ret = cmd_analyze(args)

        assert ret == 0
        call_args = mock_orchestrator.analyze.call_args[0]
        assert "test.mp4" in call_args[0]
        mock_print.assert_called_once()
        printed = mock_print.call_args[0][0]
        assert "test.mp4" in printed

    @patch("traffic_analyzer.cli.Path.exists")
    @patch("traffic_analyzer.cli.AnalysisOrchestrator")
    def test_analyze_success_markdown_output(
        self,
        mock_orchestrator_cls: MagicMock,
        mock_exists: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_exists.return_value = True
        mock_orchestrator = MagicMock()
        mock_orchestrator_cls.from_config_dir.return_value = mock_orchestrator

        from traffic_analyzer.models.schemas import (
            BinaryEncoding,
            Report,
            SceneInfo,
            VideoMetadata,
        )

        report = Report(
            video_info=VideoMetadata(
                file_path="test.mp4",
                file_name="test.mp4",
                duration_sec=10.0,
                fps=30.0,
                total_frames=300,
                width=640,
                height=480,
            ),
            scene_summary=SceneInfo(road_count=2),
            binary_encoding=BinaryEncoding(encoding_string="1_0", detected_events=[0]),
        )
        mock_orchestrator.analyze.return_value = report

        output_path = str(tmp_path / "report.json")
        parser = build_parser()
        args = parser.parse_args([
            "analyze", "--video", "test.mp4",
            "--format", "markdown",
            "--output", output_path,
        ])

        ret = cmd_analyze(args)
        assert ret == 0
        content = Path(output_path).read_text(encoding="utf-8")
        assert "交通事件分析报告" in content

    @patch("traffic_analyzer.cli.Path.exists")
    def test_analyze_video_not_found(self, mock_exists: MagicMock) -> None:
        mock_exists.return_value = False
        parser = build_parser()
        args = parser.parse_args(["analyze", "--video", "missing.mp4"])
        ret = cmd_analyze(args)
        assert ret == 1

    @patch("traffic_analyzer.cli.Path.exists")
    @patch("traffic_analyzer.cli.AnalysisOrchestrator")
    def test_analyze_orchestrator_failure(
        self,
        mock_orchestrator_cls: MagicMock,
        mock_exists: MagicMock,
    ) -> None:
        mock_exists.return_value = True
        mock_orchestrator_cls.from_config_dir.side_effect = Exception("Config error")
        parser = build_parser()
        args = parser.parse_args(["analyze", "--video", "test.mp4"])
        ret = cmd_analyze(args)
        assert ret == 1


# ---------------------------------------------------------------------------
# cmd_validate_config tests
# ---------------------------------------------------------------------------


class TestCmdValidateConfig:
    @patch("traffic_analyzer.cli.Path.exists")
    @patch("traffic_analyzer.cli.ConfigManager")
    def test_validate_success(self, mock_config_cls: MagicMock, mock_exists: MagicMock) -> None:
        mock_exists.return_value = True
        mock_manager = MagicMock()
        mock_manager.get_event_categories.return_value = [
            MagicMock(event_id=0, name="A", detection_mode=MagicMock(value="expert_agent")),
        ]
        mock_manager.validate_config.return_value = []
        mock_config_cls.return_value = mock_manager

        parser = build_parser()
        args = parser.parse_args(["validate-config"])
        ret = cmd_validate_config(args)
        assert ret == 0
        mock_manager.load_all.assert_called_once()

    @patch("traffic_analyzer.cli.Path.exists")
    def test_validate_config_dir_not_found(self, mock_exists: MagicMock) -> None:
        mock_exists.return_value = False
        parser = build_parser()
        args = parser.parse_args(["validate-config", "--config-dir", "/missing"])
        ret = cmd_validate_config(args)
        assert ret == 1

    @patch("traffic_analyzer.cli.Path.exists")
    @patch("traffic_analyzer.cli.ConfigManager")
    def test_validate_load_failure(self, mock_config_cls: MagicMock, mock_exists: MagicMock) -> None:
        mock_exists.return_value = True
        mock_config_cls.return_value.load_all.side_effect = Exception("Bad YAML")
        parser = build_parser()
        args = parser.parse_args(["validate-config"])
        ret = cmd_validate_config(args)
        assert ret == 1


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_analyze_success(self) -> None:
        with patch("traffic_analyzer.cli.cmd_analyze") as mock_cmd:
            mock_cmd.return_value = 0
            ret = main(["analyze", "--video", "test.mp4"])
            assert ret == 0
            mock_cmd.assert_called_once()

    def test_main_no_args_shows_help(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code != 0
