"""
Unit tests for :mod:`traffic_analyzer.core.report_generator`.

Covers:
- Report generation correctness
- JSON serialization round-trip
- Markdown output structure
- Binary encoding construction
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List

import pytest

from traffic_analyzer.core.report_generator import ReportGenerator
from traffic_analyzer.models.schemas import (
    BinaryEncoding,
    ConfidenceLevel,
    EventInstance,
    EventResult,
    Report,
    SceneInfo,
    VideoMetadata,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def video_meta() -> VideoMetadata:
    return VideoMetadata(
        file_path="/data/video_001.mp4",
        file_name="video_001.mp4",
        duration_sec=120.0,
        fps=25.0,
        total_frames=3000,
        width=1920,
        height=1080,
        codec="h264",
        bitrate=4_000_000,
        record_time=datetime(2024, 6, 1, 8, 30, 0),
        camera_id="CAM_01",
    )


@pytest.fixture
def scene_info() -> SceneInfo:
    return SceneInfo(
        road_count=2,
        weather="晴天",
        lighting="良好",
        traffic_density="中等",
        total_vehicles_estimate=45,
        scene_description="双向四车道城市快速路，视线良好。",
        confidence=0.92,
    )


@pytest.fixture
def event_results() -> List[EventResult]:
    return [
        EventResult(
            event_id=0,
            event_name="车辆逆行",
            detected=True,
            instances=[
                EventInstance(
                    event_id=0,
                    event_name="车辆逆行",
                    vehicle_id="V_12",
                    road_id=1,
                    start_time_sec=15.0,
                    end_time_sec=22.0,
                    confidence=0.88,
                    confidence_level=ConfidenceLevel.HIGH,
                    evidence_frames=[375, 400, 425, 550],
                    description="白色轿车在对向车道逆向行驶约 7 秒。",
                    reasoning="车辆轨迹与道路正常方向夹角大于 150 度，且持续多帧。",
                    disposal_suggestion="立即通知交警拦截逆行车辆，避免正面碰撞风险。",
                )
            ],
            summary="检测到 1 起逆行事件。",
            confidence=0.88,
            analysis_process=["提取关键帧", "VLM 识别逆行", "轨迹验证"],
        ),
        EventResult(
            event_id=1,
            event_name="占用应急车道",
            detected=False,
            instances=[],
            summary="未检测到应急车道占用行为。",
            confidence=0.15,
            analysis_process=["提取关键帧", "VLM 识别应急车道"],
        ),
        EventResult(
            event_id=2,
            event_name="超速行驶",
            detected=True,
            instances=[
                EventInstance(
                    event_id=2,
                    event_name="超速行驶",
                    vehicle_id="V_07",
                    road_id=0,
                    start_time_sec=45.0,
                    end_time_sec=55.0,
                    confidence=0.76,
                    confidence_level=ConfidenceLevel.MEDIUM,
                    evidence_frames=[1125, 1150, 1200, 1375],
                    description="黑色 SUV 在限速 80 km/h 路段估算速度约 110 km/h。",
                    reasoning="跨帧位移与标定车道长度比值超出阈值 1.35 倍。",
                    disposal_suggestion="记录超速车辆信息，移交违章处理系统。",
                ),
                EventInstance(
                    event_id=2,
                    event_name="超速行驶",
                    vehicle_id="V_09",
                    road_id=0,
                    start_time_sec=80.0,
                    end_time_sec=88.0,
                    confidence=0.72,
                    confidence_level=ConfidenceLevel.MEDIUM,
                    evidence_frames=[2000, 2050, 2100, 2200],
                    description="银色轿车疑似超速。",
                    reasoning="同上。",
                    disposal_suggestion="记录超速车辆信息，移交违章处理系统。",
                ),
            ],
            summary="检测到 2 起超速事件。",
            confidence=0.76,
            analysis_process=["速度估算", "阈值比对"],
        ),
    ]


@pytest.fixture
def usage_stats() -> Dict[str, Any]:
    return {
        "total_calls": 12,
        "total_tokens": 34_560,
        "total_latency_ms": 8_200.0,
    }


@pytest.fixture
def generator() -> ReportGenerator:
    return ReportGenerator()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_report_structure(
        self,
        generator: ReportGenerator,
        event_results: List[EventResult],
        scene_info: SceneInfo,
        video_meta: VideoMetadata,
        usage_stats: Dict[str, Any],
    ) -> None:
        report = generator.generate(
            event_results=event_results,
            scene_info=scene_info,
            video_meta=video_meta,
            usage_stats=usage_stats,
        )

        assert isinstance(report, Report)
        assert report.video_info == video_meta
        assert report.scene_summary == scene_info
        assert report.event_results == sorted(event_results, key=lambda r: r.event_id)
        assert report.llm_usage_stats == usage_stats
        assert isinstance(report.generated_at, datetime)

    def test_overall_description_override(
        self,
        generator: ReportGenerator,
        event_results: List[EventResult],
        scene_info: SceneInfo,
        video_meta: VideoMetadata,
        usage_stats: Dict[str, Any],
    ) -> None:
        custom_desc = "自定义整体描述"
        report = generator.generate(
            event_results=event_results,
            scene_info=scene_info,
            video_meta=video_meta,
            usage_stats=usage_stats,
            overall_traffic_description=custom_desc,
        )
        assert report.overall_traffic_description == custom_desc

    def test_event_results_sorted(
        self,
        generator: ReportGenerator,
        scene_info: SceneInfo,
        video_meta: VideoMetadata,
        usage_stats: Dict[str, Any],
    ) -> None:
        unsorted = [
            EventResult(event_id=3, event_name="C", detected=False),
            EventResult(event_id=0, event_name="A", detected=True),
            EventResult(event_id=1, event_name="B", detected=False),
        ]
        report = generator.generate(
            event_results=unsorted,
            scene_info=scene_info,
            video_meta=video_meta,
            usage_stats=usage_stats,
        )
        assert [r.event_id for r in report.event_results] == [0, 1, 3]

    def test_empty_events(
        self,
        generator: ReportGenerator,
        scene_info: SceneInfo,
        video_meta: VideoMetadata,
        usage_stats: Dict[str, Any],
    ) -> None:
        report = generator.generate(
            event_results=[],
            scene_info=scene_info,
            video_meta=video_meta,
            usage_stats=usage_stats,
        )
        assert report.binary_encoding.encoding_string == ""
        assert report.binary_encoding.detected_events == []
        assert report.binary_encoding.event_count == 0
        assert "未检测到显著交通事件" in report.overall_traffic_description


class TestToJson:
    def test_json_roundtrip(
        self,
        generator: ReportGenerator,
        event_results: List[EventResult],
        scene_info: SceneInfo,
        video_meta: VideoMetadata,
        usage_stats: Dict[str, Any],
    ) -> None:
        report = generator.generate(
            event_results=event_results,
            scene_info=scene_info,
            video_meta=video_meta,
            usage_stats=usage_stats,
        )
        json_str = generator.to_json(report)
        data = json.loads(json_str)

        assert data["video_info"]["file_name"] == video_meta.file_name
        assert data["scene_summary"]["weather"] == scene_info.weather
        assert len(data["event_results"]) == len(event_results)
        assert data["binary_encoding"]["encoding_string"] == report.binary_encoding.encoding_string


class TestToMarkdown:
    def test_contains_sections(
        self,
        generator: ReportGenerator,
        event_results: List[EventResult],
        scene_info: SceneInfo,
        video_meta: VideoMetadata,
        usage_stats: Dict[str, Any],
    ) -> None:
        report = generator.generate(
            event_results=event_results,
            scene_info=scene_info,
            video_meta=video_meta,
            usage_stats=usage_stats,
        )
        md = generator.to_markdown(report)

        assert "# 交通事件分析报告" in md
        assert "## 视频信息" in md
        assert "## 整体交通态势" in md
        assert "## 事件类别分析" in md
        assert "## 最终分类" in md
        assert "## 处置建议" in md

    def test_video_info_content(
        self,
        generator: ReportGenerator,
        event_results: List[EventResult],
        scene_info: SceneInfo,
        video_meta: VideoMetadata,
        usage_stats: Dict[str, Any],
    ) -> None:
        report = generator.generate(
            event_results=event_results,
            scene_info=scene_info,
            video_meta=video_meta,
            usage_stats=usage_stats,
        )
        md = generator.to_markdown(report)

        assert video_meta.file_name in md
        assert str(video_meta.width) in md
        assert str(video_meta.height) in md
        assert "CAM_01" in md

    def test_event_details_present(
        self,
        generator: ReportGenerator,
        event_results: List[EventResult],
        scene_info: SceneInfo,
        video_meta: VideoMetadata,
        usage_stats: Dict[str, Any],
    ) -> None:
        report = generator.generate(
            event_results=event_results,
            scene_info=scene_info,
            video_meta=video_meta,
            usage_stats=usage_stats,
        )
        md = generator.to_markdown(report)

        assert "车辆逆行" in md
        assert "超速行驶" in md
        assert "占用应急车道" in md
        # Evidence frames
        assert "375" in md
        assert "1125" in md
        # Reasoning
        assert "轨迹验证" in md

    def test_binary_encoding_explanation(
        self,
        generator: ReportGenerator,
        event_results: List[EventResult],
        scene_info: SceneInfo,
        video_meta: VideoMetadata,
        usage_stats: Dict[str, Any],
    ) -> None:
        report = generator.generate(
            event_results=event_results,
            scene_info=scene_info,
            video_meta=video_meta,
            usage_stats=usage_stats,
        )
        md = generator.to_markdown(report)

        assert report.binary_encoding.encoding_string in md
        assert "二进制编码" in md
        assert "1" in md  # at least one detected

    def test_disposal_recommendations(
        self,
        generator: ReportGenerator,
        event_results: List[EventResult],
        scene_info: SceneInfo,
        video_meta: VideoMetadata,
        usage_stats: Dict[str, Any],
    ) -> None:
        report = generator.generate(
            event_results=event_results,
            scene_info=scene_info,
            video_meta=video_meta,
            usage_stats=usage_stats,
        )
        md = generator.to_markdown(report)

        assert "立即通知交警拦截逆行车辆" in md
        assert "记录超速车辆信息" in md

    def test_no_events_empty_state(
        self,
        generator: ReportGenerator,
        scene_info: SceneInfo,
        video_meta: VideoMetadata,
        usage_stats: Dict[str, Any],
    ) -> None:
        report = generator.generate(
            event_results=[],
            scene_info=scene_info,
            video_meta=video_meta,
            usage_stats=usage_stats,
        )
        md = generator.to_markdown(report)

        assert "未检测到任何事件类别" in md
        assert "暂无处置建议" in md


class TestToBinaryEncoding:
    def test_basic_encoding(
        self, generator: ReportGenerator
    ) -> None:
        results = [
            EventResult(event_id=0, event_name="A", detected=True),
            EventResult(event_id=1, event_name="B", detected=False),
            EventResult(event_id=2, event_name="C", detected=True),
        ]
        be = generator.to_binary_encoding(results, total_categories=3)
        assert be.encoding_string == "1_0_1"
        assert be.detected_events == [0, 2]
        assert be.event_count == 2

    def test_larger_total_categories(
        self, generator: ReportGenerator
    ) -> None:
        results = [
            EventResult(event_id=0, event_name="A", detected=True),
        ]
        be = generator.to_binary_encoding(results, total_categories=5)
        assert be.encoding_string == "1_0_0_0_0"
        assert be.detected_events == [0]

    def test_infer_total_categories(
        self, generator: ReportGenerator
    ) -> None:
        results = [
            EventResult(event_id=0, event_name="A", detected=False),
            EventResult(event_id=4, event_name="E", detected=True),
        ]
        be = generator.to_binary_encoding(results, total_categories=0)
        assert be.encoding_string == "0_0_0_0_1"
        assert be.detected_events == [4]

    def test_empty_results(
        self, generator: ReportGenerator
    ) -> None:
        be = generator.to_binary_encoding([], total_categories=0)
        assert be.encoding_string == ""
        assert be.detected_events == []
        assert be.event_count == 0

    def test_all_detected(
        self, generator: ReportGenerator
    ) -> None:
        results = [
            EventResult(event_id=0, event_name="A", detected=True),
            EventResult(event_id=1, event_name="B", detected=True),
        ]
        be = generator.to_binary_encoding(results, total_categories=2)
        assert be.encoding_string == "1_1"
        assert be.detected_events == [0, 1]

    def test_none_detected(
        self, generator: ReportGenerator
    ) -> None:
        results = [
            EventResult(event_id=0, event_name="A", detected=False),
            EventResult(event_id=1, event_name="B", detected=False),
        ]
        be = generator.to_binary_encoding(results, total_categories=2)
        assert be.encoding_string == "0_0"
        assert be.detected_events == []

    def test_returns_binary_encoding_model(
        self, generator: ReportGenerator
    ) -> None:
        results = [EventResult(event_id=0, event_name="A", detected=True)]
        be = generator.to_binary_encoding(results, total_categories=1)
        assert isinstance(be, BinaryEncoding)


class TestDisposalRecommendations:
    def test_fallback_when_no_instance_suggestion(
        self,
        generator: ReportGenerator,
        scene_info: SceneInfo,
        video_meta: VideoMetadata,
        usage_stats: Dict[str, Any],
    ) -> None:
        results = [
            EventResult(
                event_id=0,
                event_name="违法停车",
                detected=True,
                instances=[
                    EventInstance(
                        event_id=0,
                        event_name="违法停车",
                        confidence=0.8,
                        disposal_suggestion="",
                    )
                ],
            )
        ]
        report = generator.generate(
            event_results=results,
            scene_info=scene_info,
            video_meta=video_meta,
            usage_stats=usage_stats,
        )
        assert any("人工复核并记录" in rec for rec in report.disposal_recommendations)

    def test_no_recommendations_when_nothing_detected(
        self,
        generator: ReportGenerator,
        scene_info: SceneInfo,
        video_meta: VideoMetadata,
        usage_stats: Dict[str, Any],
    ) -> None:
        results = [
            EventResult(event_id=0, event_name="A", detected=False),
        ]
        report = generator.generate(
            event_results=results,
            scene_info=scene_info,
            video_meta=video_meta,
            usage_stats=usage_stats,
        )
        assert report.disposal_recommendations == []
