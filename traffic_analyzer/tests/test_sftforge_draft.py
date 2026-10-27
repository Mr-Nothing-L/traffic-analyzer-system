"""sftforge.draft PipelineDraftProvider 单测(fake orchestrator,无网络/无真实管线)。

覆盖:成功路径(读样本 JSON → DraftResult)、预筛拒绝、改写隔离、样本缺失、
env 复原、进度 JSONL tailer 转发 on_step。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from traffic_analyzer.contracts import sft_json, sft_quarantine_json
from traffic_analyzer.sftforge.draft import DraftError, PipelineDraftProvider
from traffic_analyzer.utils.progress import PROGRESS_FILE_ENV


def _sample(description: str = "<think>\n</think>\n<answer>x</answer>", action=(2, 1)) -> str:
    return json.dumps(
        {"chunk": "chunk #1", "idx": 1, "action": list(action), "description": description},
        ensure_ascii=False,
    )


def _factory_ok(report: object = None, sample: str | None = None, quarantine: bool = False):
    """构造 fake orchestrator 工厂:analyze 时向 SFT_LABEL_OUTPUT_DIR 写样本。"""
    def factory(config_dir: str):
        class _FakeOrchestrator:
            def analyze(self, video_path: str) -> object:
                out_dir = Path(os.environ["SFT_LABEL_OUTPUT_DIR"])
                if quarantine:
                    target = sft_quarantine_json(out_dir, Path(video_path).stem)
                else:
                    target = sft_json(out_dir, Path(video_path).stem)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(sample if sample is not None else _sample(), encoding="utf-8")
                return report or SimpleNamespace(rejected=False)
        return _FakeOrchestrator()
    return factory


@pytest.fixture()
def video(tmp_path):
    path = tmp_path / "01-02_Event_test_1.mp4"
    path.write_bytes(b"fake")
    return path


class TestDraftSuccess:
    def test_reads_sample_into_draft_result(self, video) -> None:
        provider = PipelineDraftProvider(orchestrator_factory=_factory_ok())
        result = provider(video)
        assert result.output == "<think>\n</think>\n<answer>x</answer>"
        assert result.classes == [1, 2]  # 排序
        assert result.via == "pipeline"

    def test_env_restored_after_run(self, video) -> None:
        os.environ.pop("SFT_LABEL_ENABLE", None)
        provider = PipelineDraftProvider(orchestrator_factory=_factory_ok())
        provider(video)
        assert os.environ.get("SFT_LABEL_ENABLE") is None
        assert os.environ.get("SFT_LABEL_OUTPUT_DIR") is None
        assert os.environ.get(PROGRESS_FILE_ENV) is None

    def test_progress_events_forwarded(self, video) -> None:
        def factory(config_dir: str):
            class _FakeOrchestrator:
                def analyze(self, video_path: str) -> object:
                    progress = Path(os.environ[PROGRESS_FILE_ENV])
                    progress.write_text(
                        json.dumps({"type": "step", "step": 1, "total": 4, "name": "视频预处理"})
                        + "\n"
                        + json.dumps({"type": "lane_done", "lane": "违法停车", "result": "detected"})
                        + "\n",
                        encoding="utf-8",
                    )
                    out_dir = Path(os.environ["SFT_LABEL_OUTPUT_DIR"])
                    target = sft_json(out_dir, Path(video_path).stem)
                    target.write_text(_sample(), encoding="utf-8")
                    return SimpleNamespace(rejected=False)
            return _FakeOrchestrator()

        steps: list = []
        provider = PipelineDraftProvider(orchestrator_factory=factory)
        provider(video, on_step=steps.append)
        types = [s["type"] for s in steps]
        assert "step" in types and "lane_done" in types


class TestDraftFailure:
    def test_prefilter_reject(self, video) -> None:
        provider = PipelineDraftProvider(
            orchestrator_factory=_factory_ok(
                report=SimpleNamespace(rejected=True, reject_reason="时长 < 5s")
            )
        )
        with pytest.raises(DraftError, match="prefilter_reject"):
            provider(video)

    def test_quarantined_sample(self, video) -> None:
        provider = PipelineDraftProvider(
            orchestrator_factory=_factory_ok(quarantine=True)
        )
        with pytest.raises(DraftError, match="pipeline_quarantine"):
            provider(video)

    def test_missing_sample(self, video) -> None:
        def factory(config_dir: str):
            return SimpleNamespace(analyze=lambda vp: SimpleNamespace(rejected=False))
        provider = PipelineDraftProvider(orchestrator_factory=factory)
        with pytest.raises(DraftError, match="pipeline_no_sample"):
            provider(video)

    def test_empty_description(self, video) -> None:
        provider = PipelineDraftProvider(
            orchestrator_factory=_factory_ok(sample=_sample(description="  "))
        )
        with pytest.raises(DraftError, match="pipeline_empty_description"):
            provider(video)
