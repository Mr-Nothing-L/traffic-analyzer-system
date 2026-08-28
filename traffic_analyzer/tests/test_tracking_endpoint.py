"""Integration tests for POST /tools/track_suspects (mock VLM engine).

[文件说明]
作用:端点集成测试——FastAPI TestClient + mock vlm_engine 成脚本化窗响应:
    契约字段齐全、artifacts 落盘(windows.jsonl/track_overlay.mp4/
    tracks.csv/run.json)、缓存命中第二次不调 VLM(描述不进键)、
    全窗失败 → HTTP 200 + failed:true、越界 video_path 403、时间戳校验 422。
上游:pytest 自动发现执行。
下游:traffic_analyzer/toolserver/server.py(被测端点);
    tracking/windows(编排,mock 引擎驱动)。
"""

from __future__ import annotations

import base64
import csv
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from traffic_analyzer.toolserver import create_app
from traffic_analyzer.toolserver.tracking.windows import (
    REANCHOR_EVERY,
    REANCHOR_MISMATCH_IOU,
)

_FPS = 5.0
_FRAMES = 40  # 8s 视频


def _make_video(path: Path, n: int = _FRAMES, fps: float = _FPS) -> Path:
    """合成灰底视频:一个白色方块从左向右匀速移动(供抽帧与渲染)。"""
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (160, 120))
    assert writer.isOpened(), "cv2.VideoWriter failed to open"
    for i in range(n):
        frame = np.full((120, 160, 3), 60, dtype=np.uint8)
        x = 10 + i * 3
        frame[50:70, x : x + 30] = (220, 220, 220)
        writer.write(frame)
    writer.release()
    return path


def _window_response(target_box_frame0: str) -> Dict[str, Any]:
    """脚本化单窗响应 JSON(目标 A 两帧 + 参照车 1)。

    bbox 为 0-1000 归一化;frame 为窗内局部序号。
    """
    return {
        "targets": [
            {
                "key": "A",
                "found": True,
                "boxes": [
                    {"frame": 0, "bbox": target_box_frame0},
                    {"frame": 1, "bbox": target_box_frame0},
                ],
            }
        ],
        "references": [
            {
                "id": 1,
                "boxes": [
                    {"frame": 0, "bbox": [10, 800, 80, 900]},
                    {"frame": 1, "bbox": [30, 800, 100, 900]},
                ],
            }
        ],
    }


class ScriptedEngine:
    """mock VLMInferenceEngine:按脚本逐窗返回 LLMResponse 形状的对象。"""

    def __init__(
        self,
        responses: Optional[List[Dict[str, Any]]] = None,
        raw_text: Optional[str] = None,
        fail_all: bool = False,
        scene_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.responses = responses or []
        self.raw_text = raw_text
        self.fail_all = fail_all
        self.scene_response = scene_response or {
            "median_side": "unknown",
            "per_target": [{"index": i, "side": "unknown", "rationale": ""} for i in range(5)],
        }
        self.calls = 0
        self.window_calls = 0
        self.prompts: List[str] = []

    def call(self, template: Any, images: Any = None, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        self.calls += 1
        self.prompts.append(template.user_prompt)
        if getattr(template, "template_id", None) == "track_suspects_scene_side":
            if self.fail_all:
                return SimpleNamespace(
                    success=False, error_message="provider down", parsed_data={}, raw_text=""
                )
            data = self.scene_response
            text = f"```json\n{json.dumps(data)}\n```"
            return SimpleNamespace(
                success=True, parsed_data=data, raw_text=text, model="mock", provider="mock"
            )
        self.window_calls += 1
        if self.fail_all or self.window_calls > len(self.responses):
            if self.fail_all:
                return SimpleNamespace(
                    success=False, error_message="provider down", parsed_data={}, raw_text=""
                )
            # 脚本耗尽后默认重复最后一个成功响应
        data = self.responses[min(self.window_calls, len(self.responses)) - 1]
        text = f"```json\n{json.dumps(data)}\n```"
        return SimpleNamespace(
            success=True, parsed_data=data, raw_text=text, model="mock", provider="mock"
        )


@pytest.fixture()
def tracked_video(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return _make_video(ws / "clip.mp4")


@pytest.fixture()
def client(tracked_video: Path) -> TestClient:
    app = create_app(tracked_video.parent)
    app.state.tracking_engine = None  # 每个用例自装引擎
    return TestClient(app)


def _post(
    client: TestClient,
    engine: Any,
    video_rel: str = "clip.mp4",
    side: Optional[str] = None,
    time_range: Optional[List[float]] = None,
) -> Any:
    """替换 app 内懒构建函数指向 mock 引擎后请求端点。"""
    client.app.state.tracking_engine = engine  # type: ignore[attr-defined]
    suspect: Dict[str, Any] = {
        "box": {"x1": 0.06, "y1": 0.41, "x2": 0.25, "y2": 0.58},
        "timestamp": 0.2,
        "description": "左侧来向的白色轿车",
    }
    if side is not None:
        suspect["side"] = side
    return client.post(
        "/tools/track_suspects",
        json={
            "video_path": video_rel,
            "suspects": [suspect],
            "time_range": time_range if time_range is not None else [0.0, 6.0],
        },
    )


class TestContract:
    def test_success_contract_fields(
        self, client: TestClient, tracked_video: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine",
            lambda: ScriptedEngine(responses=[_window_response("[100,700,400,950]")]),
        )
        resp = _post(client, client.app.state.tracking_engine)  # type: ignore[attr-defined]
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for key in ("tracks", "annotated_image", "artifacts", "failed", "failure_reason"):
            assert key in body, key
        assert body["failed"] is False and body["failure_reason"] is None
        tracks = body["tracks"]
        assert len(tracks) >= 1
        track = tracks[0]
        for key in ("id", "description", "profile", "side_hint", "direction_verdict", "best_frames"):
            assert key in track, key
        profile = track["profile"]
        for key in (
            "direction_deg",
            "avg_speed_norm",
            "stationary_duration_s",
            "path_length_norm",
            "bbox_trend",
            "env_flow_ratio",
            "mean_diagonal",
        ):
            assert key in profile, key
        # 锚点在左(L=来向关键词)→ side_hint=coming
        assert track["side_hint"] == "coming"
        # best_frames 是 jpeg base64
        if track["best_frames"]:
            raw = base64.b64decode(track["best_frames"][0]["jpeg_base64"])
            assert raw[:2] == b"\xff\xd8"
            float(track["best_frames"][0]["timestamp"])

    def test_timestamps_use_source_frame_space(
        self, client: TestClient, tracked_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回归:源 fps ≠ 采样 fps 时,轨迹点时间戳必须按原始帧号换算。

        25fps 视频按 5fps 采样:采样网格第 1 点 = 源帧 5 = 0.2s;
        若错把网格序号当帧号会得到 1/25=0.04s(原 bug)。
        """
        _make_video(tracked_video.parent / "clip25.mp4", n=50, fps=25.0)
        # mock 重检框与锚框一致(0-1000 归一化)→ 锚点窗校验通过、窗内
        # 重检框被吸收;旧 mock 框与锚框无重叠,在新语义下会被判为锁错
        # 对象而不吸收(双向播种修复的正是该旧路径)。
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine",
            lambda: ScriptedEngine(responses=[_window_response("[60,410,250,580]")]),
        )
        resp = _post(client, client.app.state.tracking_engine, video_rel="clip25.mp4")  # type: ignore[attr-defined]
        assert resp.status_code == 200 and resp.json()["failed"] is False
        csv_path = tracked_video.parent / resp.json()["artifacts"]["csv"]
        rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
        assert rows, "tracks.csv empty"
        times = [float(r["time_s"]) for r in rows]
        frames = [int(r["frame"]) for r in rows]
        for f, ts in zip(frames, times):
            assert ts == pytest.approx(f / 25.0, abs=1e-3), f"frame {f} -> ts {ts}"
        assert times[1] == pytest.approx(0.2, abs=1e-3), times[:3]

    def test_artifacts_laid_out(
        self, client: TestClient, tracked_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine",
            lambda: ScriptedEngine(responses=[_window_response("[100,700,400,950]")]),
        )
        resp = _post(client, client.app.state.tracking_engine)  # type: ignore[attr-defined]
        assert resp.status_code == 200
        body = resp.json()
        art = body["artifacts"]
        # dir 相对允许根,clip/csv 拼接正确
        base_dir = tracked_video.parent / ".agent" / "tracks"
        out_root = base_dir / art["dir"].split(".agent/tracks/", 1)[1].split("/")[0]
        full_dir = tracked_video.parent / art["dir"]
        assert full_dir.is_dir() and str(full_dir).startswith(str(out_root.parent))
        assert art["clip"] and (tracked_video.parent / art["clip"]).is_file()
        assert art["csv"] and (tracked_video.parent / art["csv"]).is_file()
        # debug bundle:每窗一条 jsonl + run 快照
        jsonl = full_dir / "windows.jsonl"
        lines = [json.loads(x) for x in jsonl.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert len(lines) >= 1
        first = lines[0]
        assert first["mode"] == "reanchor"  # 第 0 窗即重检测式
        win_idx = [w["mode"] == "reanchor" for w in lines]
        assert win_idx.index(True) == 0
        every_ok = all(lines[i]["mode"] != "propagate" or i % REANCHOR_EVERY != 0 for i in range(len(lines)))
        assert every_ok
        reanchored = [i for i, w in enumerate(lines) if w["mode"] == "reanchor"]
        assert reanchored[0] == 0
        assert all(i % REANCHOR_EVERY == 0 for i in reanchored)
        assert (full_dir / "tracks.csv").is_file()
        assert (full_dir / "run.json").is_file()
        run = json.loads((full_dir / "run.json").read_text(encoding="utf-8"))
        assert run["sample_fps"] == body["fps_used"]
        del out_root

    def test_cache_hit_skips_vlm_and_ignores_description(
        self, client: TestClient, tracked_video: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = ScriptedEngine(responses=[_window_response("[100,700,400,950]")])
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        tc = client.app.state.tracking_engine  # type: ignore[attr-defined]
        resp1 = _post(client, tc)
        assert resp1.status_code == 200 and resp1.json()["failed"] is False
        calls_after_first = engine.calls
        assert calls_after_first > 0

        # 同锚点同视频但不同描述 → 缓存键不变,VLM 不再被调用
        cache_resp = client.post(
            "/tools/track_suspects",
            json={
                "video_path": "clip.mp4",
                "suspects": [
                    {
                        "box": {"x1": 0.06, "y1": 0.41, "x2": 0.25, "y2": 0.58},
                        "timestamp": 0.2,
                        "description": "喷过漆的蓝色货车(不同描述)",
                    }
                ],
                "time_range": [0.0, 6.0],
            },
        )
        assert cache_resp.status_code == 200 and cache_resp.json()["failed"] is False
        assert engine.calls == calls_after_first

        # 缓存目录落了结果文件
        cache_dir = tracked_video.parent / ".agent" / "tracks" / "_cache"
        assert cache_dir.is_dir() and len(list(cache_dir.glob("*.json"))) == 1


class TestFailurePaths:
    def test_all_windows_fail_returns_200_failed_true(
        self, client: TestClient, tracked_video: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = ScriptedEngine(fail_all=True)
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        resp = _post(client, engine)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["failed"] is True
        assert body["failure_reason"]
        assert "all VLM window calls failed" in body["failure_reason"]
        assert body["tracks"] == []
        # 失败结果不写缓存
        cache_dir = tracked_video.parent / ".agent" / "tracks" / "_cache"
        assert not cache_dir.exists() or not list(cache_dir.glob("*.json"))

    def test_window_jsonl_records_errors_even_when_failing(
        self, client: TestClient, tracked_video: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = ScriptedEngine(fail_all=True)
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        resp = _post(client, engine)
        body = resp.json()
        dirs = list((tracked_video.parent / ".agent" / "tracks").glob("clip/*"))
        assert dirs, "debug bundle dir must exist even on failure"
        jsonl = dirs[0] / "windows.jsonl"
        records = [json.loads(x) for x in jsonl.read_text(encoding="utf-8").splitlines()]
        assert all(r["ok"] is False and r["error"] for r in records)
        del body


class TestGuards:
    def test_outside_root_403(self, client: TestClient) -> None:
        resp = client.post(
            "/tools/track_suspects",
            json={
                "video_path": "/etc/hostname",
                "suspects": [{"box": {"x1": 0, "y1": 0, "x2": 0.5, "y2": 0.5}, "timestamp": 0}],
            },
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "path_outside_workspace"

    def test_missing_video_404(self, client: TestClient) -> None:
        resp = client.post(
            "/tools/track_suspects",
            json={
                "video_path": "nope.mp4",
                "suspects": [{"box": {"x1": 0, "y1": 0, "x2": 0.5, "y2": 0.5}, "timestamp": 0}],
            },
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "video_not_found"

    def test_too_many_suspects_422(self, client: TestClient) -> None:
        suspects = [{"box": {"x1": 0.0, "y1": 0.0, "x2": 0.1, "y2": 0.1}, "timestamp": 0}] * 6
        resp = client.post(
            "/tools/track_suspects", json={"video_path": "clip.mp4", "suspects": suspects}
        )
        assert resp.status_code == 422

    def test_inverted_box_422(self, client: TestClient) -> None:
        resp = client.post(
            "/tools/track_suspects",
            json={
                "video_path": "clip.mp4",
                "suspects": [{"box": {"x1": 0.5, "y1": 0.0, "x2": 0.1, "y2": 0.1}, "timestamp": 0}],
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_request"

    def test_bad_time_range_422(self, client: TestClient) -> None:
        resp = client.post(
            "/tools/track_suspects",
            json={
                "video_path": "clip.mp4",
                "suspects": [{"box": {"x1": 0.0, "y1": 0.0, "x2": 0.2, "y2": 0.2}, "timestamp": 0}],
                "time_range": [1.0],
            },
        )
        assert resp.status_code == 422


class TestReanchorDrift:
    def test_reanchor_loss_deactivates_and_reports_event(
        self, client: TestClient, tracked_video: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """初始窗检出后所有窗都空 → 下一个 re-anchor 窗判失活并上报事件。

        已有的部分轨迹仍按契约返回(failed=false 仅当存在可用轨迹);
        目标失活事实通过 events 上报。
        """
        good = _window_response("[100,700,400,950]")
        empty = {"targets": [], "references": []}
        engine = ScriptedEngine(responses=[good, empty])
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        resp = _post(client, engine)
        assert resp.status_code == 200
        body = resp.json()
        # 首窗的两帧轨迹仍构成一条可用轨迹(未全部丢失)
        assert body["failed"] is False
        assert len(body["tracks"]) == 1
        assert any(
            ev.get("type") == "reanchor_not_found" for ev in body.get("events") or []
        )

    def test_reanchor_far_jump_flags_then_reported_in_events(
        self, client: TestClient, tracked_video: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """第 1 个 re-anchor 正常、之后目标跳到远处 → mismatch 进事件列表。

        注意:窗口 0 是 re-anchor,窗口 REANCHOR_EVERY 也是 re-anchor。
        """
        good = _window_response("[100,700,400,950]")
        drifted_box = "[900,100,999,200]"  # 与外推预期 IoU ≈ 0
        engine = ScriptedEngine(
            responses=[good] * REANCHOR_EVERY + [_window_response(drifted_box)]
        )
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        resp = _post(client, engine)
        assert resp.status_code == 200
        body = resp.json()
        # 目标在下一个 re-anchor 窗跑飞 → 失败兜底或事件记录
        events = body.get("events") or []
        assert any(ev.get("type") == "reanchor_mismatch" for ev in events)
        iou_threshold_ok = REANCHOR_MISMATCH_IOU == 0.3
        assert iou_threshold_ok


class TestAdaptiveFps:
    def test_fast_target_upgrades_to_10fps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """窗内目标位移速率超缓行上限 → 整段切 10fps 并上报 fps_upgrade 事件。"""
        import cv2
        import numpy as np

        from traffic_analyzer.toolserver.tracking import windows as W

        ws = tmp_path / "ws"
        ws.mkdir()
        video = ws / "clip.mp4"
        writer = cv2.VideoWriter(
            str(video), cv2.VideoWriter_fourcc(*"mp4v"), _FPS, (320, 240)
        )
        assert writer.isOpened()
        for i in range(60):
            frame = np.full((240, 320, 3), 60, dtype=np.uint8)
            x = (10 + i * 40) % 320
            frame[100:140, x : x + 50] = (220, 220, 220)
            writer.write(frame)
        writer.release()

        # 响应框每局部帧右移 30(0-1000)→ 速率显著高于缓行阈值
        moving_resp = {
            "targets": [
                {
                    "key": "A",
                    "boxes": [
                        {
                            "frame": j,
                            "bbox": [200 + 60 * j, 400, 320 + 60 * j, 620],
                        }
                        for j in range(4)
                    ],
                }
            ],
            "references": [],
        }

        class FixedRespEngine:
            def __init__(self) -> None:
                self.calls = 0

            def call(self, template: Any = None, images: Any = None, **kw: Any) -> Any:
                from types import SimpleNamespace

                self.calls += 1
                return SimpleNamespace(
                    success=True,
                    parsed_data=moving_resp,
                    raw_text=json.dumps(moving_resp),
                    model="m",
                    provider="p",
                )

        engine = FixedRespEngine()
        from traffic_analyzer.toolserver.tracking.models import SuspectAnchor

        result = W.run_tracking(
            engine,
            video.resolve(),
            [SuspectAnchor(box=[0.1, 0.4, 0.28, 0.62], timestamp=0.2, description="高速小车")],
            time_range=[0.0, 11.0],
            out_dir=None,
        )
        assert result["failed"] is False or "fps_upgrade" in [e["type"] for e in result["events"]]
        assert result["fps_used"] == W.FAST_SAMPLE_FPS
        assert any(e["type"] == "fps_upgrade" for e in result["events"])


class KwargsRecordingEngine(ScriptedEngine):
    """ScriptedEngine + 逐窗记录 engine.call 的 kwargs(验证 thinking 传递口径)。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.call_kwargs: List[Dict[str, Any]] = []

    def call(self, template: Any, images: Any = None, **kwargs: Any) -> Any:
        self.call_kwargs.append(
            {"template_id": getattr(template, "template_id", None), **kwargs}
        )
        return super().call(template, images=images, **kwargs)


class TestThinkingPropagation:
    def test_propagate_windows_disable_thinking_reanchor_untouched(
        self, client: TestClient, tracked_video: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """传播窗以 enable_thinking=False 调引擎;re-anchor 窗不传该参(保留
        服务端默认);run.json 快照记录该口径。"""
        engine = KwargsRecordingEngine(responses=[_window_response("[100,700,400,950]")])
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        resp = _post(client, engine)
        assert resp.status_code == 200, resp.text
        assert resp.json()["failed"] is False
        # 场景判定现在取首/中/尾 3 帧,每帧都以 enable_thinking=False 调用
        scene_kwargs = [
            kw
            for kw in engine.call_kwargs
            if kw.get("template_id") == "track_suspects_scene_side"
        ]
        window_kwargs = [
            kw
            for kw in engine.call_kwargs
            if kw.get("template_id") != "track_suspects_scene_side"
        ]
        assert len(scene_kwargs) == 3
        assert all(kw.get("enable_thinking") is False for kw in scene_kwargs)
        assert len(window_kwargs) >= REANCHOR_EVERY + 1
        for wi, kwargs in enumerate(window_kwargs):
            if wi % REANCHOR_EVERY == 0:
                assert "enable_thinking" not in kwargs  # re-anchor:不传,保留默认
            else:
                assert kwargs.get("enable_thinking") is False

        art = resp.json()["artifacts"]
        run = json.loads(
            (tracked_video.parent / art["dir"] / "run.json").read_text(encoding="utf-8")
        )
        assert run["enable_thinking"] == {"propagate": False, "reanchor": None, "scene_side": False}
        assert run["scene_side"]["frames"] is not None
        assert len(run["scene_side"]["frames"]) == 3


class ProgressiveEngine:
    """按窗序号生成持续移动的脚本化框,避免 5 点滑动平均把运动抹平。"""

    def __init__(self, mode: str = "stable", heading_answer: str = "朝镜头") -> None:
        self.mode = mode
        self.heading_answer = heading_answer
        self.calls = 0
        self.heading_calls = 0
        self.window_calls = 0

    def call(self, template: Any, images: Any = None, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        self.calls += 1
        if getattr(template, "template_id", None) == "vehicle_heading":
            self.heading_calls += 1
            return SimpleNamespace(
                success=True,
                raw_text=self.heading_answer,
                parsed_data={},
                model="mock",
                provider="mock",
            )
        if getattr(template, "template_id", None) == "track_suspects_scene_side":
            data = {"median_side": "unknown", "per_target": [{"index": 0, "side": "unknown", "rationale": ""}]}
            return SimpleNamespace(
                success=True,
                parsed_data=data,
                raw_text=json.dumps(data),
                model="mock",
                provider="mock",
            )
        wi = self.window_calls
        self.window_calls += 1
        boxes: List[Dict[str, Any]] = []
        for f in range(5):
            x = 60 + wi * 80 + f * 20
            if self.mode == "stable":
                y1, y2, w = 410, 580, 190
            elif self.mode == "shrink":
                y1, y2, w = 410 + wi * 10 + f * 3, 580 - wi * 10 - f * 3, 190
            else:  # grow
                y1, y2, w = 410 - wi * 5 - f * 2, 580 + wi * 5 + f * 2, 190
            boxes.append({"frame": f, "bbox": [x, y1, x + w, y2]})
        data = {"targets": [{"key": "A", "found": True, "boxes": boxes}], "references": []}
        return SimpleNamespace(
            success=True,
            parsed_data=data,
            raw_text=f"```json\n{json.dumps(data)}\n```",
            model="mock",
            provider="mock",
        )


class TestHeading:
    _TR_RANGE = [0.0, 4.0]

    def test_heading_accepted_when_two_frames_agree(
        self,
        client: TestClient,
        tracked_video: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = ProgressiveEngine(mode="stable", heading_answer="朝镜头")
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        resp = _post(client, engine, side="coming", time_range=self._TR_RANGE)
        assert resp.status_code == 200, resp.text
        track = resp.json()["tracks"][0]
        assert track["heading"] is not None
        assert track["heading"]["accepted"] == "toward"
        assert track["heading"]["n_total"] >= 1
        assert track["heading"]["n_consistent"] == track["heading"]["n_total"]
        assert "车头朝向" in track["direction_verdict"]

    def test_heading_uncertain_when_frames_disagree(
        self,
        client: TestClient,
        tracked_video: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class DisagreeEngine(ProgressiveEngine):
            def call(self, template: Any, images: Any = None, **kwargs: Any) -> Any:
                from types import SimpleNamespace

                self.calls += 1
                if getattr(template, "template_id", None) == "vehicle_heading":
                    self.heading_calls += 1
                    answer = "朝镜头" if self.heading_calls == 1 else "背镜头"
                    return SimpleNamespace(
                        success=True,
                        raw_text=answer,
                        parsed_data={},
                        model="mock",
                        provider="mock",
                    )
                return super().call(template, images, **kwargs)

        engine = DisagreeEngine(mode="stable")
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        resp = _post(client, engine, side="coming", time_range=self._TR_RANGE)
        assert resp.status_code == 200, resp.text
        track = resp.json()["tracks"][0]
        assert track["heading"]["accepted"] == "unknown"
        assert "难以判断" in track["direction_verdict"]

    def test_heading_dichotomy_wrong_way(
        self,
        client: TestClient,
        tracked_video: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # coming 目标 bbox 缩小(实际远离),车头背镜头 → 与移动方向一致 → 逆行
        engine = ProgressiveEngine(mode="shrink", heading_answer="背镜头")
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        resp = _post(client, engine, side="coming", time_range=self._TR_RANGE)
        assert resp.status_code == 200, resp.text
        track = resp.json()["tracks"][0]
        assert track["heading"]["accepted"] == "away"
        assert "疑似逆行" in track["direction_verdict"]
        assert "车头朝向" in track["direction_verdict"]

    def test_heading_dichotomy_backing(
        self,
        client: TestClient,
        tracked_video: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # coming 目标 bbox 缩小(实际远离),车头朝镜头 → 与移动方向相反 → 倒车
        engine = ProgressiveEngine(mode="shrink", heading_answer="朝镜头")
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        resp = _post(client, engine, side="coming", time_range=self._TR_RANGE)
        assert resp.status_code == 200, resp.text
        track = resp.json()["tracks"][0]
        assert track["heading"]["accepted"] == "toward"
        assert "疑似倒车" in track["direction_verdict"]

    def test_heading_not_triggered_on_consistent_track(
        self,
        client: TestClient,
        tracked_video: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # coming 目标 bbox 增大 → 方向一致,不触发 heading
        engine = ProgressiveEngine(mode="grow", heading_answer="朝镜头")
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        resp = _post(client, engine, side="coming", time_range=self._TR_RANGE)
        assert resp.status_code == 200, resp.text
        track = resp.json()["tracks"][0]
        assert track["heading"] is None
        assert "方向一致" in track["direction_verdict"]

    def test_request_side_overrides_description_hint(
        self,
        client: TestClient,
        tracked_video: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 描述含"左侧来向"会 infer 成 coming,显式 side=going 应覆盖
        engine = ProgressiveEngine(mode="stable", heading_answer="朝镜头")
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        resp = _post(client, engine, side="going", time_range=self._TR_RANGE)
        assert resp.status_code == 200, resp.text
        track = resp.json()["tracks"][0]
        assert track["side_hint"] == "going"
        assert "去向侧" in track["direction_verdict"]


class TestSceneSide:
    _SCENE_COMING_LEFT = {
        "median_side": "left",
        "per_target": [{"index": 0, "side": "coming", "rationale": "在隔离带右侧,朝镜头"}],
    }
    _SCENE_GOING_LEFT = {
        "median_side": "left",
        "per_target": [{"index": 0, "side": "going", "rationale": "在隔离带右侧,背镜头"}],
    }

    def test_scene_side_overrides_anchor_and_records_run(
        self,
        client: TestClient,
        tracked_video: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = ScriptedEngine(
            responses=[_window_response("[100,700,400,950]")],
            scene_response=self._SCENE_COMING_LEFT,
        )
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        # 显式 anchor side=going,但场景判定为 coming → 应被覆盖并记冲突
        resp = _post(client, engine, side="going")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        track = body["tracks"][0]
        assert track["side_hint"] == "coming"
        assert "来向侧" in track["direction_verdict"]
        assert "中央隔离带在画面左侧" in track["direction_verdict"]
        assert any(ev.get("type") == "side_conflict" for ev in body["events"])

        art = body["artifacts"]
        run = json.loads(
            (tracked_video.parent / art["dir"] / "run.json").read_text(encoding="utf-8")
        )
        assert run["scene_side"]["median_side"] == "left"
        assert run["scene_side"]["per_target"][0]["side"] == "coming"
        assert run["scene_side"]["per_target"][0]["source"] == "scene"
        assert run["tracks"][0]["side_source"] == "scene"
        assert "side_rationale" in run["tracks"][0]

    def test_unknown_scene_does_not_override_anchor(
        self,
        client: TestClient,
        tracked_video: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = ScriptedEngine(
            responses=[_window_response("[100,700,400,950]")],
            scene_response={
                "median_side": "unknown",
                "per_target": [{"index": 0, "side": "unknown", "rationale": ""}],
            },
        )
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        resp = _post(client, engine, side="going")
        assert resp.status_code == 200, resp.text
        track = resp.json()["tracks"][0]
        assert track["side_hint"] == "going"
        assert "去向侧" in track["direction_verdict"]
        assert "side_conflict" not in [e.get("type") for e in resp.json()["events"]]

    def test_scene_side_agrees_with_anchor_no_conflict(
        self,
        client: TestClient,
        tracked_video: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine = ScriptedEngine(
            responses=[_window_response("[100,700,400,950]")],
            scene_response=self._SCENE_GOING_LEFT,
        )
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        resp = _post(client, engine, side="going")
        assert resp.status_code == 200, resp.text
        track = resp.json()["tracks"][0]
        assert track["side_hint"] == "going"
        assert "side_conflict" not in [e.get("type") for e in resp.json()["events"]]


class RotatingSceneEngine(ScriptedEngine):
    """ScriptedEngine 变体:每次场景判定返回不同的 scene_responses。"""

    def __init__(
        self,
        scene_responses: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.scene_responses = scene_responses or []
        self._scene_idx = 0

    def call(self, template: Any, images: Any = None, **kwargs: Any) -> Any:
        if getattr(template, "template_id", None) == "track_suspects_scene_side":
            self.calls += 1
            data = (
                self.scene_responses[self._scene_idx % len(self.scene_responses)]
                if self.scene_responses
                else self.scene_response
            )
            self._scene_idx += 1
            text = f"```json\n{json.dumps(data)}\n```"
            return SimpleNamespace(
                success=True, parsed_data=data, raw_text=text, model="mock", provider="mock"
            )
        return super().call(template, images=images, **kwargs)


class TestSceneSideVotingRun:
    _SCENE_COMING = {
        "median_side": "left",
        "per_target": [{"index": 0, "side": "coming", "rationale": "在隔离带右侧,朝镜头"}],
    }
    _SCENE_UNKNOWN = {
        "median_side": "unknown",
        "per_target": [{"index": 0, "side": "unknown", "rationale": ""}],
    }

    def test_run_json_records_votes_and_raw_frames(
        self,
        client: TestClient,
        tracked_video: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """2/3 多数场景判定:run.json 应记录 3 帧原始响应、票数与表决结果。"""
        engine = RotatingSceneEngine(
            responses=[_window_response("[100,700,400,950]")],
            scene_responses=[
                self._SCENE_COMING,
                self._SCENE_COMING,
                self._SCENE_UNKNOWN,
            ],
        )
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        resp = _post(client, engine, side="going")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        track = body["tracks"][0]
        assert track["side_hint"] == "coming"

        art = body["artifacts"]
        run = json.loads(
            (tracked_video.parent / art["dir"] / "run.json").read_text(encoding="utf-8")
        )
        assert "场景方位2/3帧" in run["tracks"][0]["side_rationale"]
        assert len(run["scene_side"]["frames"]) == 3
        pt_votes = run["scene_side"]["votes"]["per_target"]["0"]
        assert pt_votes["coming"] == 2
        assert pt_votes["unknown"] == 1
        assert len(run["scene_side"]["raw_response"]["frames"]) == 3
        assert "side_conflict" in [e["type"] for e in run["events"]]
        assert "scene_side_split" not in [e["type"] for e in run["events"]]

    def test_split_event_emitted_when_frames_disagree(
        self,
        client: TestClient,
        tracked_video: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """coming/going/unknown 分裂 → 目标 side 为 unknown 并记录 split 事件。"""
        engine = RotatingSceneEngine(
            responses=[_window_response("[100,700,400,950]")],
            scene_responses=[
                {
                    "median_side": "left",
                    "per_target": [{"index": 0, "side": "coming", "rationale": ""}],
                },
                {
                    "median_side": "left",
                    "per_target": [{"index": 0, "side": "going", "rationale": ""}],
                },
                {
                    "median_side": "unknown",
                    "per_target": [{"index": 0, "side": "unknown", "rationale": ""}],
                },
            ],
        )
        monkeypatch.setattr(
            "traffic_analyzer.toolserver.server._build_default_engine", lambda: engine
        )
        resp = _post(client, engine, side="going")
        assert resp.status_code == 200, resp.text
        run = json.loads(
            (tracked_video.parent / resp.json()["artifacts"]["dir"] / "run.json").read_text(
                encoding="utf-8"
            )
        )
        assert run["scene_side"]["per_target"][0]["scene_side"] == "unknown"
        assert run["scene_side"]["per_target"][0]["source"] == "anchor"
        assert any(e["type"] == "scene_side_split" for e in run["events"])
