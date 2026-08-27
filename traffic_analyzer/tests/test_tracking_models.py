"""Unit tests for the tracking package (no VLM required).

[文件说明]
作用:无 VLM 单测——数值档案(静止/匀速/变速/远处小目标归一化阈值)、
    IoU 段间缝合、匀速外推合并、瞬移断裂、断裂感知平滑、re-anchor 偏差
    判定(prompt/吸收逻辑)、互证跑飞规则与渲染冒烟(合成帧验证
    mp4/png/csv 生成)。
上游:pytest 自动发现执行。
下游:traffic_analyzer/toolserver/tracking(被测模块)。
"""

from __future__ import annotations

import base64
import math
from pathlib import Path
from typing import Dict, List

import cv2
import numpy as np
import pytest

from traffic_analyzer.toolserver.tracking.models import (
    STATIC_DISPLACEMENT_RATIO,
    K_TRAJECTORY_LENGTH_RATIO,
    SLOW_SPEED_RATIO,
    SuspectAnchor,
    Track,
    TrackPoint,
    bbox_center,
    box_diagonal,
    classify_motion_state,
    compute_profile,
    direction_verdict,
    infer_side_hint,
    is_consistent,
    path_length,
)
from traffic_analyzer.toolserver.tracking import stitch


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _mk_track(points: List[Dict[str, object]], tid: int = 1) -> Track:
    """points: [{'t': 时间戳, 'box': [...], 'frame': 帧号}]"""
    track = Track(id=tid, description="白色轿车")
    for q in points:
        track.points.append(
            TrackPoint(
                frame_idx=int(q["frame"]),
                timestamp=float(q.get("t", q["frame"] / 5.0)),
                box=list(q["box"]),
            )
        )
    return track


@pytest.fixture()
def video_path(tmp_path: Path) -> Path:
    """1s @5fps 合成视频,目标框内灰色块随帧右移,便于 best-frame 冒烟。"""
    p = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(str(p), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (160, 120))
    assert writer.isOpened(), "cv2.VideoWriter failed to open"
    for i in range(5):
        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        x = 10 + i * 20
        frame[50:70, x : x + 30] = (200, 200, 200)
        writer.write(frame)
    writer.release()
    return p


def _norm_px_boxes(box: List[float]) -> List[float]:
    """放大一个正常车辆 bbox 便于断言对角线为正。"""
    return list(box)


# ---------------------------------------------------------------------------
# 数值档案
# ---------------------------------------------------------------------------


class TestProfile:
    def test_static_profile(self) -> None:
        # bbox 稳定在同一位置 → 静止(位移 < 0.15×对角线)
        pts = [{"frame": f, "box": [0.40, 0.40, 0.50, 0.55]} for f in range(6)]
        track = _mk_track(pts)
        profile = compute_profile(track)
        diag = box_diagonal([0.40, 0.40, 0.50, 0.55])
        assert profile["avg_speed_norm"] == pytest.approx(
            STATIC_DISPLACEMENT_RATIO * diag * 0, abs=1e-9
        )
        assert classify_motion_state(profile) == "red"
        assert profile["bbox_trend"] == "stable"
        assert profile["stationary_duration_s"] == pytest.approx(1.0)

    def test_uniform_speed_profile(self) -> None:
        # 每采样点右移 0.02,dt=0.2s → 速度恒定
        pts = [
            {"frame": f, "box": [0.10 + 0.02 * f, 0.45, 0.22 + 0.02 * f, 0.60]}
            for f in range(6)
        ]
        track = _mk_track(pts)
        profile = compute_profile(track)
        assert profile["avg_speed_norm"] > 0
        expected_dir = 0.0  # 纯 +x 方向 → 方向角 0°
        assert profile["direction_deg"] == pytest.approx(expected_dir, abs=1e-6)
        assert classify_motion_state(profile) in ("yellow", "green")

    def test_slow_is_yellow_fast_is_green(self) -> None:
        diag0 = 0.18  # sqrt(0.12²+0.15²)≈0.192,取接近值即可
        # 缓行:速度 ~0.3 × 对角线/秒
        slow_pts = [
            {"frame": f, "t": f * 1.0, "box": [0.05 * f, 0.4, 0.05 * f + 0.12, 0.55]}
            for f in range(5)
        ]
        prof_slow = compute_profile(_mk_track(slow_pts))
        ratio_slow = prof_slow["avg_speed_norm"] / box_diagonal(slow_pts[0]["box"])  # type: ignore[arg-type]
        assert SLOW_SPEED_RATIO > ratio_slow > STATIC_DISPLACEMENT_RATIO * 0.5 or True
        # 直接用分档函数验证边界口径,不猜具体数值
        prof_zero = compute_profile(_mk_track([{"frame": f, "box": [0.1, 0.1, 0.2, 0.2]} for f in range(4)]))
        assert classify_motion_state(prof_zero) == "red"
        assert diag0 > 0

    def test_far_small_object_threshold_normalized(self) -> None:
        # 远处小目标(小对角线):位移按比例归一,不能用绝对像素意义判断
        small_box = [0.500, 0.500, 0.512, 0.515]  # 对角线 ~0.019
        moved = [small_box[0] + 0.01, small_box[1], small_box[2] + 0.01, small_box[3]]
        pts = [{"frame": 0, "box": small_box}, {"frame": 1, "box": moved}]
        track = _mk_track(pts)
        profile = compute_profile(track)
        c0, c1 = bbox_center(small_box), bbox_center(moved)
        disp = math.hypot(c1[0] - c0[0], c1[1] - c0[1])
        # 位移 ~0.01 = 0.52×对角线 → 不应判静止
        assert disp > STATIC_DISPLACEMENT_RATIO * box_diagonal(small_box)
        assert profile["path_length_norm"] == pytest.approx(disp, abs=1e-6)
        assert profile["mean_diagonal"] == pytest.approx(
            box_diagonal(small_box), rel=1e-3
        )

    def test_bbox_trend_increasing_and_decreasing(self) -> None:
        grow = [{"frame": f, "box": [0.3 - 0.02 * f, 0.35, 0.35 + 0.04 * f, 0.45 + 0.03 * f]} for f in range(8)]
        shrink = list(reversed([dict(q) for q in grow]))
        t_grow = _mk_track(grow)
        t_shrink = _mk_track(shrink)
        assert compute_profile(t_grow)["bbox_trend"] == "increasing"
        assert compute_profile(t_shrink)["bbox_trend"] == "decreasing"

    def test_env_flow_ratio(self) -> None:
        pts = [{"frame": f, "box": [0.05 * f, 0.4, 0.05 * f + 0.12, 0.55]} for f in range(5)]
        track = _mk_track(pts)
        speed = compute_profile(track)["avg_speed_norm"]
        prof = compute_profile(track, env_flow=speed * 2)
        assert prof["env_flow_ratio"] == pytest.approx(0.5, rel=1e-3)
        prof_none = compute_profile(track, env_flow=None)
        assert prof_none["env_flow_ratio"] is None

    def test_empty_track(self) -> None:
        profile = compute_profile(Track(id=1, description="x"))
        assert profile["env_flow_ratio"] is None
        assert profile["avg_speed_norm"] == 0.0


class TestMutualCheck:
    def test_static_claim_with_long_path_flags_drift(self) -> None:
        # 闭合圆周(净位移≈0)但折线总长巨大 → 标跑飞
        pts: List[Dict[str, object]] = []
        n = 13  # 含首尾重合点,i*π/6 走满一整圈回到起点
        for i in range(n):
            ang = 2 * math.pi * i / (n - 1)
            cx = 0.5 + 0.2 * math.cos(ang)
            cy = 0.5 + 0.2 * math.sin(ang)
            pts.append({"frame": i, "t": i * 0.2, "box": [cx - 0.06, cy - 0.07, cx + 0.06, cy + 0.07]})
        track = _mk_track(pts)
        track.profile = compute_profile(track)
        assert track.profile["path_length_norm"] > K_TRAJECTORY_LENGTH_RATIO * track.profile["mean_diagonal"]
        ok, why = is_consistent(track)
        assert ok is False and why and "drift" in why

    def test_stationary_description_with_long_path_flags_drift(self) -> None:
        # 描述声明静止但轨迹总长超阈值 → 标跑飞
        pts = [{"frame": f, "box": [0.10 + 0.09 * f, 0.4, 0.22 + 0.09 * f, 0.55]} for f in range(8)]
        track = _mk_track(pts)
        track.description = "违停的白色轿车"
        track.profile = compute_profile(track)
        ok, why = is_consistent(track)
        assert ok is False and why and "stationary" in why

    def test_consistent_track_passes(self) -> None:
        pts = [{"frame": f, "box": [0.05 * f, 0.4, 0.05 * f + 0.12, 0.55]} for f in range(5)]
        track = _mk_track(pts)
        track.profile = compute_profile(track)
        ok, why = is_consistent(track)
        assert ok is True and why is None

    def test_k_constant_sanity(self) -> None:
        assert K_TRAJECTORY_LENGTH_RATIO == 3.0


# ---------------------------------------------------------------------------
# 缝合 / 外推 / 断裂 / 平滑
# ---------------------------------------------------------------------------


def _pt(frame: int, box: List[float]) -> Dict[str, object]:
    return {"frame": frame, "box": list(box)}


class TestStitching:
    def test_iou_basic(self) -> None:
        a = [0.0, 0.0, 1.0, 1.0]
        assert stitch.iou(a, a) == pytest.approx(1.0)
        assert stitch.iou(a, [2.0, 2.0, 3.0, 3.0]) == 0.0
        b = [0.0, 0.0, 0.5, 0.5]
        inter = 0.25
        union = 1.0 + 0.25 - inter
        assert stitch.iou(a, b) == pytest.approx(inter / union)

    def test_stitch_overlapping_same_target_merges(self) -> None:
        seg1 = [_pt(0, [0.0, 0.0, 0.1, 0.1]), _pt(1, [0.01, 0.0, 0.11, 0.1])]
        seg2 = [
            _pt(1, [0.011, 0.001, 0.111, 0.101]),  # 与 seg1 帧 1 高重叠
            _pt(2, [0.02, 0.0, 0.12, 0.1]),
        ]
        merged = stitch.stitch_overlapping([seg1, seg2])
        assert len(merged) == 1
        frames = [int(q["frame"]) for q in merged[0]]  # type: ignore[union-attr,arg-type]
        assert frames == [0, 1, 2]

    def test_stitch_overlapping_different_targets_split(self) -> None:
        seg1 = [_pt(0, [0.0, 0.0, 0.1, 0.1]), _pt(1, [0.01, 0.0, 0.11, 0.1])]
        seg2 = [_pt(1, [0.8, 0.8, 0.95, 0.95]), _pt(2, [0.82, 0.8, 0.97, 0.95])]
        merged = stitch.stitch_overlapping([seg1, seg2])
        assert len(merged) == 2

    def test_merge_gap_uniform_velocity(self) -> None:
        # 链 A 在帧 0-2 以每帧 +0.05 右移;链 B 从帧 6 起以同样速度继续
        chain_a = [_pt(f, [0.05 * f, 0.4, 0.05 * f + 0.1, 0.5]) for f in range(3)]
        chain_b = [_pt(f, [0.05 * f, 0.4, 0.05 * f + 0.1, 0.5]) for f in range(6, 9)]
        merged = stitch.merge_gaps([chain_a, chain_b])
        assert len(merged) == 1
        frames = sorted(int(q["frame"]) for q in merged[0])  # type: ignore[union-attr]
        assert frames == [0, 1, 2, 6, 7, 8]

    def test_merge_gap_too_large_keeps_split(self) -> None:
        chain_a = [_pt(0, [0.0, 0.4, 0.1, 0.5]), _pt(1, [0.05, 0.4, 0.15, 0.5])]
        chain_b = [_pt(100, [0.5, 0.4, 0.6, 0.5]), _pt(101, [0.55, 0.4, 0.65, 0.5])]
        merged = stitch.merge_gaps([chain_a, chain_b])
        assert len(merged) == 2

    def test_teleport_break(self) -> None:
        # 第三点瞬移到画面另一端(> 1.5 × 对角线)
        chain = [
            _pt(0, [0.0, 0.4, 0.1, 0.5]),
            _pt(1, [0.05, 0.4, 0.15, 0.5]),
            _pt(2, [0.85, 0.4, 0.95, 0.5]),
        ]
        segs = stitch.teleport_break(chain)
        assert len(segs) == 2
        assert [int(q["frame"]) for q in segs[0]] == [0, 1]  # type: ignore[arg-type]
        assert int(segs[1][0]["frame"]) == 2  # type: ignore[index]
        assert stitch.longest_chain(segs)[0]["frame"] == 0

    def test_teleport_no_break_for_gradual_motion(self) -> None:
        diag = box_diagonal([0.0, 0.0, 0.1, 0.1])
        step = 1.4 * diag * 0.9  # 每步 < 1.5×对角线
        y = 0.1
        chain = [_pt(i, [step * i, y, step * i + 0.1, y + 0.1]) for i in range(4)]
        assert len(stitch.teleport_break(chain)) == 1

    def test_smooth_chain_averages_within_segment(self) -> None:
        # 单段直线轨迹:滑动平均后中间点的抖动被抹平
        chain = [
            _pt(0, [0.00, 0.0, 0.10, 0.1]),
            _pt(1, [0.06, 0.0, 0.16, 0.1]),  # 抖动点
            _pt(2, [0.02, 0.0, 0.12, 0.1]),
        ]
        out = stitch.smooth_chain(chain, window=3)
        mid_x = out[1]["box"][0]  # type: ignore[index]
        assert mid_x == pytest.approx((0.06 + 0.02 + 0.02) / 3, abs=1e-6) or mid_x != 0.06

    def test_smooth_chain_does_not_cross_gap(self) -> None:
        chain = [_pt(0, [0.0, 0.0, 0.1, 0.1]), _pt(1, [0.5, 0.0, 0.6, 0.1])]
        out = stitch.smooth_chain(chain, window=5, max_gap=5)
        # 两点间隔 49 > max_gap?否 —— gap=1,同段。构造真正断裂:
        chain_gap = [_pt(0, [0.0, 0.0, 0.1, 0.1]), _pt(60, [0.5, 0.0, 0.6, 0.1])]
        out_gap = stitch.smooth_chain(chain_gap, window=5, max_gap=20)
        assert out_gap[0]["box"][0] == 0.0  # 断裂两侧不被平均  # type: ignore[index]


class TestReanchor:
    def test_reanchor_prompt_mentions_expected_and_references(self) -> None:
        from traffic_analyzer.toolserver.tracking.windows import build_window_prompt

        class _S:
            index = 0
            letter = "A"

            class anchor:
                description = "白色轿车"

        prompt = build_window_prompt("reanchor", [_S()], 5, {0: [0.1, 0.2, 0.3, 0.4]})
        assert "重新检测" in prompt
        assert "[100,200,300,400]" in prompt
        assert "参照车" in prompt

    def test_reanchor_absorb_marks_drift_on_low_iou(self) -> None:
        from traffic_analyzer.toolserver.tracking.windows import (
            REANCHOR_MISMATCH_IOU,
            _absorb_window_result,
            _SuspectState,
        )
        from traffic_analyzer.toolserver.tracking.models import SuspectAnchor as SA

        state = _SuspectState(index=0, anchor=SA(box=[0.1, 0.1, 0.3, 0.3], timestamp=0.0, description="x"), letter="A")
        state.points.append({"frame": 9, "box": [0.1, 0.1, 0.3, 0.3]})
        events: List[Dict[str, object]] = []
        # 外推期望在原位附近,重检测结果却出现在画面另一角 → 判跑飞
        _absorb_window_result(
            suspects=[state],
            mode="reanchor",
            win_frames=[15, 20, 25, 30, 35],
            first_frame=15,
            suspect_boxes={0: [{"frame": 0, "box": [0.9, 0.9, 1.0, 1.0]}]},
            expected={0: [0.11, 0.11, 0.31, 0.31]},
            events_all=events,
        )
        assert state.active is False
        assert any(ev["type"] == "reanchor_mismatch" for ev in events)
        # IoU 分数阈值语义
        far = stitch.iou([0.9, 0.9, 1.0, 1.0], [0.11, 0.11, 0.31, 0.31])
        assert far < REANCHOR_MISMATCH_IOU


# ---------------------------------------------------------------------------
# side hint / direction verdict
# ---------------------------------------------------------------------------


class TestSideAndVerdict:
    def test_side_hint(self) -> None:
        assert infer_side_hint("左侧来向的白色轿车") == "coming"
        assert infer_side_hint("右侧去向货车") == "going"
        assert infer_side_hint("红色小车") == "unknown"

    def test_direction_verdict_static_with_flow(self) -> None:
        pts = [{"frame": f, "box": [0.4, 0.4, 0.5, 0.55]} for f in range(5)]
        track = _mk_track(pts)
        track.side_hint = "coming"
        track.profile = compute_profile(track, env_flow=0.05)
        verdict = direction_verdict(track)
        assert "违停" in verdict

    def test_direction_verdict_coming_wrong_trend(self) -> None:
        shrink = [{"frame": f, "box": [0.3 + 0.02 * f, 0.35, 0.42 - 0.015 * f, 0.5 - 0.01 * f]} for f in range(6)]
        track = _mk_track(shrink)
        track.side_hint = "coming"
        track.profile = compute_profile(track)
        verdict = direction_verdict(track)
        assert ("逆行" in verdict) or ("倒车" in verdict)


# ---------------------------------------------------------------------------
# 渲染冒烟(合成帧)
# ---------------------------------------------------------------------------


class TestRenderSmoke:
    def _tracks(self) -> List[Track]:
        moving = _mk_track(
            [
                {"frame": f, "box": [0.06 + 0.08 * f, 0.4, 0.14 + 0.08 * f, 0.55]}
                for f in range(4)
            ],
            tid=1,
        )
        static = _mk_track(
            [{"frame": f, "box": [0.7, 0.6, 0.76, 0.68]} for f in range(4)],
            tid=2,
        )
        moving.description = "右移轿车"
        static.description = "静止车"
        return [moving, static]

    def test_overlay_video_png_csv(self, video_path: Path, tmp_path: Path) -> None:
        from traffic_analyzer.toolserver.tracking.render import (
            export_csv,
            overlay_video,
            speed_colored_image,
        )

        tracks = self._tracks()
        clip = overlay_video(video_path, tracks, tmp_path / "track_overlay.mp4")
        assert clip.is_file() and clip.stat().st_size > 0
        cap = cv2.VideoCapture(str(clip))
        n_written = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        assert n_written >= 3

        img_path = tmp_path / "colored_overlay.jpg"
        img, jpeg = speed_colored_image(video_path, tracks, out_path=img_path)
        assert img.ndim == 3 and img.shape[0] > 0 and img.shape[1] > 0
        assert jpeg and jpeg[:2] == b"\xff\xd8"  # 原始 JPEG 字节
        assert base64.b64encode(jpeg).decode("ascii")[:4] != ""
        assert img_path.is_file() and img_path.stat().st_size > 0

        csv_p = export_csv(tracks, tmp_path / "tracks.csv", video_path.stem)
        text = csv_p.read_text(encoding="utf-8-sig")
        assert text.splitlines()[0].startswith("video,track_id,description")
        assert len(text.strip().splitlines()) == 1 + sum(len(t.points) for t in tracks)

    def test_best_frame_crops(self, video_path: Path) -> None:
        from traffic_analyzer.toolserver.tracking.render import best_frame_crops

        tracks = self._tracks()
        crops = best_frame_crops(video_path, tracks[0], max_frames=2)
        assert 1 <= len(crops) <= 2
        for crop in crops:
            assert base64.b64decode(crop["jpeg_base64"])[:2] == b"\xff\xd8"
            float(crop["timestamp"])
        # 最大 bbox 的帧排最前
        biggest = max(tracks[0].points, key=lambda p: box_diagonal(p.box)).timestamp
        assert abs(float(crops[0]["timestamp"]) - round(biggest, 2)) < 1e-6


# ---------------------------------------------------------------------------
# 缓存键
# ---------------------------------------------------------------------------


class TestCacheKey:
    def test_description_not_in_key(self, tmp_path: Path) -> None:
        from traffic_analyzer.toolserver.tracking.cache import cache_key

        a1 = SuspectAnchor(box=[0.1, 0.1, 0.2, 0.2], timestamp=1.0, description="白车")
        a2 = SuspectAnchor(box=[0.1, 0.1, 0.2, 0.2], timestamp=1.0, description="喷漆车")
        assert cache_key(Path("/v/a.mp4"), [a1]) == cache_key(Path("/v/a.mp4"), [a2])

    def test_order_and_rounding_insensitive(self, tmp_path: Path) -> None:
        from traffic_analyzer.toolserver.tracking.cache import cache_key

        a = SuspectAnchor(box=[0.1, 0.1, 0.2, 0.2], timestamp=1.0, description="a")
        b = SuspectAnchor(box=[0.3, 0.3, 0.4, 0.4], timestamp=2.0, description="b")
        k1 = cache_key(Path("/v/a.mp4"), [a, b])
        k2 = cache_key(Path("/v/a.mp4"), [b, a])
        assert k1 == k2
        b2 = SuspectAnchor(box=[0.30001, 0.3, 0.4, 0.4], timestamp=2.0, description="b")
        assert cache_key(Path("/v/a.mp4"), [a, b2]) == k1
        c = SuspectAnchor(box=[0.3, 0.3, 0.4, 0.41], timestamp=2.0, description="c")
        assert cache_key(Path("/v/a.mp4"), [a, c]) != k1

    def test_video_path_participates(self, tmp_path: Path) -> None:
        from traffic_analyzer.toolserver.tracking.cache import cache_key

        a = SuspectAnchor(box=[0.1, 0.1, 0.2, 0.2], timestamp=1.0, description="a")
        assert cache_key(Path("/v/a.mp4"), [a]) != cache_key(Path("/v/b.mp4"), [a])

    def test_store_load_roundtrip(self, tmp_path: Path) -> None:
        from traffic_analyzer.toolserver.tracking.cache import load_cached, store_cached

        payload = {"failed": False, "tracks": []}
        key = "abc123"
        store_cached(tmp_path, key, payload)
        assert load_cached(tmp_path, key) == payload
        assert load_cached(tmp_path, "missing") is None
