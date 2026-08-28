"""Tests for anchor-seeded bidirectional tracking (锚点双向播种).

[文件说明]
作用:锚点播种语义的行为测试——锚点 {box, timestamp} 是唯一直接可靠的
    定位:轨迹从锚点所在窗向过去/未来双向传播(首窗即锚点窗,不再从 t0
    盲检)、锚点窗重检测校验严重不符时保留锚框、coverage/covered_s 档案、
    低覆盖静止结论限定语、stop_reason/deactivated 可观测、时间戳缺省回退
    旧式 t0 正向。全部用脚本化 mock 引擎,不依赖真实 VLM。
上游:pytest 自动发现执行。
下游:traffic_analyzer/toolserver/tracking/windows、models(被测模块)。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence

import cv2
import numpy as np
import pytest

from traffic_analyzer.toolserver.tracking import windows as W
from traffic_analyzer.toolserver.tracking.models import (
    SuspectAnchor,
    Track,
    TrackPoint,
    compute_profile,
    direction_verdict,
)

_ANCHOR_BBOX = "[400,400,500,500]"  # 0-1000,归一化 [0.4,0.4,0.5,0.5]
_ANCHOR_BOX = [0.4, 0.4, 0.5, 0.5]
_FAR_BBOX = "[950,50,999,100]"  # 与锚框 IoU≈0 的远处干扰框


def _make_video(path: Path, n: int = 75, fps: float = 5.0) -> Path:
    """合成灰底视频(内容无关,引擎为脚本化 mock,仅供抽帧/渲染)。"""
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (160, 120))
    assert writer.isOpened(), "cv2.VideoWriter failed to open"
    for _ in range(n):
        writer.write(np.full((120, 160, 3), 60, dtype=np.uint8))
    writer.release()
    return path


def _resp(bbox: str, frames: Sequence[int] = range(5)) -> Dict[str, Any]:
    """单目标 A 在给定局部帧上的固定框响应(0-1000 归一化)。"""
    return {
        "targets": [
            {"key": "A", "found": True, "boxes": [{"frame": j, "bbox": bbox} for j in frames]}
        ],
        "references": [],
    }


def _empty_resp() -> Dict[str, Any]:
    return {"targets": [], "references": []}


class IndexedEngine:
    """按窗序号脚本化响应的 mock 引擎(记录 prompt 供断言)。

    prompt 感知:只回报 prompt 中被点名跟踪的目标(真实模型只会追踪
    prompt 里列出的目标,不会回报未要求的目标)。
    """

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self.responses = responses
        self.calls = 0
        self.prompts: List[str] = []

    def call(self, template: Any, images: Any = None, **kwargs: Any) -> Any:
        import re

        letters = set(re.findall(r"目标([A-Z]):", template.user_prompt))
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        self.prompts.append(template.user_prompt)
        data = self.responses[idx]
        filtered = {
            "targets": [
                t
                for t in data.get("targets", [])
                if str(t.get("key", "")).upper() in letters
            ],
            "references": data.get("references", []),
        }
        return SimpleNamespace(
            success=True,
            parsed_data=filtered,
            raw_text=json.dumps(filtered),
            model="mock",
            provider="mock",
        )


def _anchor(ts: Optional[float] = 6.0, box: Optional[List[float]] = None) -> SuspectAnchor:
    return SuspectAnchor(
        box=list(box if box is not None else _ANCHOR_BOX),
        timestamp=ts,  # type: ignore[arg-type]
        description="路口中央的白色轿车",
    )


def _read_jsonl(out_dir: Path) -> List[Dict[str, Any]]:
    return [
        json.loads(x)
        for x in (out_dir / "windows.jsonl").read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]


def _read_run(out_dir: Path) -> Dict[str, Any]:
    return json.loads((out_dir / "run.json").read_text(encoding="utf-8"))


@pytest.fixture()
def video(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return _make_video(ws / "clip.mp4")


# ---------------------------------------------------------------------------
# A. 锚点双向播种
# ---------------------------------------------------------------------------


class TestBidirectionalSeeding:
    def test_track_spans_both_sides_of_mid_anchor(
        self, video: Path, tmp_path: Path
    ) -> None:
        """锚点在时段中段:轨迹必须跨过锚点前后(向过去+未来双向覆盖)。"""
        out_dir = tmp_path / "out"
        engine = IndexedEngine([_resp(_ANCHOR_BBOX)])
        result = W.run_tracking(
            engine, video, [_anchor(6.0)], time_range=[0.0, 12.0], out_dir=out_dir
        )
        assert result["failed"] is False, result["failure_reason"]
        assert len(result["tracks"]) == 1
        records = _read_jsonl(out_dir)
        # 首个执行窗即锚点窗(锚点帧 30 = 6.0s × 5fps),不是 t0 盲检
        first = records[0]
        assert first["direction"] == "forward"
        assert first["frames"][0] == 28 and 30 in first["frames"]
        assert first["mode"] == "reanchor"
        assert "预期位置" in first["request_prompt"]
        assert _ANCHOR_BBOX in first["request_prompt"]
        # 末尾存在反向窗,一路铺回时段起点(帧 0)
        backwards = [r for r in records if r["direction"] == "backward"]
        assert backwards, "no backward windows executed"
        assert records[-1]["direction"] == "backward"
        assert 0 in records[-1]["frames"]
        # 轨迹时间跨度跨过锚点两侧
        run = _read_run(out_dir)
        assert run["tracks"], "track missing"
        profile = run["tracks"][0]["profile"]
        assert profile["covered_s"] == pytest.approx(12.0, abs=0.3)
        assert profile["coverage"] == pytest.approx(1.0, abs=0.05)

    def test_first_window_never_blind_scans_from_t0(self, video: Path, tmp_path: Path) -> None:
        """锚点前时段不参与任何 prompt(目标未进场也不能锁错对象)。"""
        out_dir = tmp_path / "out"
        engine = IndexedEngine([_resp(_ANCHOR_BBOX)])
        result = W.run_tracking(
            engine, video, [_anchor(6.0)], time_range=[0.0, 12.0], out_dir=out_dir
        )
        assert result["failed"] is False
        records = _read_jsonl(out_dir)
        forward_records = [r for r in records if r["direction"] == "forward"]
        # 正向窗全部从锚点窗起;锚点帧(30)之前的帧只出现在反向窗
        assert all(r["frames"][0] >= 28 for r in forward_records)
        before_anchor_forward = [
            f for r in forward_records for f in r["frames"] if f < 28
        ]
        assert not before_anchor_forward

    def test_anchor_validation_mismatch_keeps_anchor_box(
        self, video: Path, tmp_path: Path
    ) -> None:
        """锚点窗重检与锚框严重不符:记事件但保留锚框、不失活。"""
        out_dir = tmp_path / "out"
        engine = IndexedEngine([_resp(_FAR_BBOX), _empty_resp()])
        result = W.run_tracking(
            engine, video, [_anchor(6.0)], time_range=[0.0, 12.0], out_dir=out_dir
        )
        assert result["failed"] is False
        events = result["events"]
        mismatch = [e for e in events if e["type"] == "reanchor_mismatch"]
        assert mismatch and mismatch[0].get("kept_anchor") is True
        # 后续窗全空 → 直到 re-anchor 周期才判失活;轨迹就是锚框本身
        assert len(result["tracks"]) == 1
        assert result["tracks"][0]["profile"]["mean_diagonal"] > 0
        run = _read_run(out_dir)
        assert run["suspects"][0]["deactivated"]["reason"] == "reanchor_not_found"

    def test_multiple_anchors_seed_independently(self, video: Path, tmp_path: Path) -> None:
        """两个锚点各自独立播种(共享窗,各自从自己的锚框入链)。"""
        out_dir = tmp_path / "out"
        engine = IndexedEngine(
            [
                {
                    "targets": [
                        {"key": "A", "found": True, "boxes": [{"frame": j, "bbox": "[100,400,200,500]"} for j in range(5)]},
                        {"key": "B", "found": True, "boxes": [{"frame": j, "bbox": "[600,400,700,500]"} for j in range(5)]},
                    ],
                    "references": [],
                }
            ]
        )
        anchors = [
            SuspectAnchor(box=[0.1, 0.4, 0.2, 0.5], timestamp=2.0, description="甲车"),
            SuspectAnchor(box=[0.6, 0.4, 0.7, 0.5], timestamp=8.0, description="乙车"),
        ]
        result = W.run_tracking(
            engine, video, anchors, time_range=[0.0, 12.0], out_dir=out_dir
        )
        assert result["failed"] is False, result["failure_reason"]
        assert len(result["tracks"]) == 2
        run = _read_run(out_dir)
        # 锚点 A(t=2s → 帧 10)先校验;锚点 B(t=8s → 帧 40)到窗才校验
        anchor_frames = {s["letter"]: s["anchor_frame"] for s in run["suspects"]}
        assert anchor_frames == {"A": 10, "B": 40}
        # B 未到锚点窗前不参与 prompt(A 单独出现,无 B 的预期位置)
        first_prompt = _read_jsonl(out_dir)[0]["request_prompt"]
        assert "目标A" in first_prompt and "目标B" not in first_prompt
        b_join = next(
            r for r in _read_jsonl(out_dir) if "目标B" in r["request_prompt"]
        )
        assert 40 in b_join["frames"]

    def test_missing_timestamp_falls_back_to_legacy_forward(
        self, video: Path, tmp_path: Path
    ) -> None:
        """时间戳缺省:回退旧式 t0 正向,不播种锚框、无反向窗。"""
        out_dir = tmp_path / "out"
        engine = IndexedEngine([_resp("[100,100,200,200]")])
        result = W.run_tracking(
            engine, video, [_anchor(None)], time_range=[0.0, 6.0], out_dir=out_dir
        )
        assert result["failed"] is False
        records = _read_jsonl(out_dir)
        assert records[0]["frames"][0] == 0  # 从 t0 起扫
        assert all(r["direction"] == "forward" for r in records)
        run = _read_run(out_dir)
        assert run["suspects"][0]["anchor_frame"] is None
        assert run["stop_reason"] == "completed"
        # 轨迹框来自窗口检出(锚框从未入链:锚框 [0.4,...] ≠ 检出 [0.1,...])
        assert result["tracks"], "legacy path should still produce a track"


# ---------------------------------------------------------------------------
# B. coverage / covered_s
# ---------------------------------------------------------------------------


def _mk_track(points: List[Dict[str, Any]], tid: int = 1) -> Track:
    track = Track(id=tid, description="白色轿车")
    for q in points:
        track.points.append(
            TrackPoint(
                frame_idx=int(q["frame"]),
                timestamp=float(q["t"]),
                box=list(q["box"]),
            )
        )
    return track


class TestCoverageProfile:
    def test_covered_s_and_coverage(self) -> None:
        pts = [{"frame": f, "t": f * 0.2, "box": [0.4, 0.4, 0.5, 0.5]} for f in range(5)]
        profile = compute_profile(_mk_track(pts), span_s=4.0)
        assert profile["covered_s"] == pytest.approx(0.8)
        assert profile["coverage"] == pytest.approx(0.2)

    def test_coverage_clamped_to_one(self) -> None:
        pts = [{"frame": f, "t": f * 1.0, "box": [0.4, 0.4, 0.5, 0.5]} for f in range(4)]
        profile = compute_profile(_mk_track(pts), span_s=2.0)
        assert profile["coverage"] == 1.0

    def test_no_span_means_coverage_none(self) -> None:
        pts = [{"frame": f, "t": f * 0.2, "box": [0.4, 0.4, 0.5, 0.5]} for f in range(5)]
        profile = compute_profile(_mk_track(pts))
        assert profile["covered_s"] == pytest.approx(0.8)
        assert profile["coverage"] is None

    def test_empty_track_coverage_zero(self) -> None:
        profile = compute_profile(Track(id=1, description="x"))
        assert profile["covered_s"] == 0.0
        assert profile["coverage"] == 0.0


class TestVerdictCoverageQualifier:
    def _static_track(self, span_s: Optional[float]) -> Track:
        pts = [{"frame": f, "t": f * 0.2, "box": [0.4, 0.4, 0.5, 0.55]} for f in range(5)]
        track = _mk_track(pts)
        track.profile = compute_profile(track, env_flow=0.05, span_s=span_s)
        return track

    def test_low_coverage_appends_insufficient_evidence(self) -> None:
        verdict = direction_verdict(self._static_track(span_s=4.0))
        assert "违停" in verdict
        assert "证据不足" in verdict
        assert "轨迹仅覆盖 20%" in verdict

    def test_sufficient_coverage_has_no_qualifier(self) -> None:
        verdict = direction_verdict(self._static_track(span_s=1.0))
        assert "违停" in verdict
        assert "证据不足" not in verdict


# ---------------------------------------------------------------------------
# C. stop_reason / deactivated
# ---------------------------------------------------------------------------


class TestStopReasonAndDeactivated:
    def test_completed_run_stop_reason(self, video: Path, tmp_path: Path) -> None:
        out_dir = tmp_path / "out"
        result = W.run_tracking(
            engine=IndexedEngine([_resp(_ANCHOR_BBOX)]),
            video_path=video,
            anchors=[_anchor(6.0)],
            time_range=[0.0, 12.0],
            out_dir=out_dir,
        )
        assert result["failed"] is False
        run = _read_run(out_dir)
        assert run["stop_reason"] == "completed"
        assert run["suspects"][0]["active"] is True
        assert run["suspects"][0]["deactivated"] is None

    def test_reanchor_mismatch_records_deactivated_and_stop_reason(
        self, video: Path, tmp_path: Path
    ) -> None:
        """锚点窗通过后,第 5 窗(re-anchor 周期)跳变 → 失活原因+窗号落盘。"""
        out_dir = tmp_path / "out"
        engine = IndexedEngine(
            [_resp(_ANCHOR_BBOX)] * 5 + [_resp(_FAR_BBOX)] + [_resp(_ANCHOR_BBOX)]
        )
        result = W.run_tracking(
            engine, video, [_anchor(6.0)], time_range=[0.0, 12.0], out_dir=out_dir
        )
        assert result["failed"] is False  # 锚点+前 4 窗轨迹仍可用
        assert len(result["tracks"]) == 1
        assert any(e["type"] == "reanchor_mismatch" for e in result["events"])
        run = _read_run(out_dir)
        assert run["stop_reason"] == "all_inactive"
        deact = run["suspects"][0]["deactivated"]
        assert deact["reason"] == "reanchor_mismatch"
        assert deact["window"] == 5
        # re-anchor 事件写入 run.json(旧版遗漏修复)
        assert any(e["type"] == "reanchor_mismatch" for e in run["events"])
        # windows.jsonl 对应窗记录失活明细
        records = _read_jsonl(out_dir)
        win5 = next(r for r in records if r["window"] == 5)
        assert win5["deactivated"] == [
            {"index": 0, "letter": "A", "reason": "reanchor_mismatch", "window": 5, "iou": 0.0}
        ]

    def test_max_calls_stop_reason(
        self, video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MAX_WINDOW_CALLS 打满 → stop_reason=max_calls 且照常落 run.json。"""
        monkeypatch.setattr(W, "MAX_WINDOW_CALLS", 3)
        out_dir = tmp_path / "out"
        result = W.run_tracking(
            engine=IndexedEngine([_resp(_ANCHOR_BBOX)]),
            video_path=video,
            anchors=[_anchor(6.0)],
            time_range=[0.0, 12.0],
            out_dir=out_dir,
        )
        assert result["failed"] is False
        assert result["tracks"]
        run = _read_run(out_dir)
        assert run["stop_reason"] == "max_calls"
