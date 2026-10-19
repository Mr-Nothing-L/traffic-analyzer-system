"""VLM sliding-window orchestration for suspect-target tracking.

[文件说明]
作用:track_suspects 的确定性编排核心(run_tracking 及窗吸收/状态辅助)。
    采样与运动度量在 sampling.py,窗口 prompt 构造与应答解析在
    window_llm.py,场景方位表决与车头朝向旁证在 scene_vote.py,致命异常
    TrackingFailure 在 errors.py(均经本模块 re-export,既有引用面不变)。
    在可疑时段内默认 5fps 抽帧
    (首个传播窗发现高速运动目标自适应升 10fps)、滑窗 5 帧/stride 4 调
    VLM(复用 core/vlm_engine 的 failover + .vlm_cache.db;传播窗关
    thinking 换低延迟,re-anchor 窗保留服务端默认);锚点 {box, timestamp}
    是唯一直接可靠的定位:锚框直接作为传播链初始框,以锚点所在采样窗为
    界先向后(未来)再向前(过去)双向传播,锚点窗做一次重检测校验
    (锚框 vs 重检 IoU,严重不符时记事件但保留锚框;时间戳缺省回退旧式
    t0 正向盲检);窗 prompt 双模式:传播式(目标描述+上一框位+顺带框
    2~3 辆参照车)与 re-anchor 式(每 REANCHOR_EVERY 窗按描述+外推预期
    位置重检测,检测结果与外推期望 IoU < REANCHOR_MISMATCH_IOU 判跑飞);
    参照车位移中位数 → 环境流速;再经 stitch 后处理与渲染产出数值档案
    (含 covered_s/coverage 覆盖率)和产物文件,run.json 记录 stop_reason、
    逐目标 deactivated 与全部 events。
上游:toolserver/server.py(端点调用);tests/test_track_windows.py(mock 引擎)。
下游:models/stitch/render 本包协作;core/vlm_engine(PromptTemplate 渲染入口);
    cv2(抽帧)。
"""

from __future__ import annotations

import bisect
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from traffic_analyzer.models.llm import PromptTemplate
from traffic_analyzer.toolserver.tracking import (
    STATIC_DISPLACEMENT_RATIO,
    SLOW_SPEED_RATIO,
    SuspectAnchor,
    Track,
    TrackPoint,
    box_diagonal,
    bbox_center,
    compute_profile,
    evaluate_direction,
    infer_side_hint,
    is_consistent,
    render_direction,
)
from traffic_analyzer.toolserver.tracking.models import is_clipped_box
from traffic_analyzer.toolserver.tracking import stitch

logger = logging.getLogger(__name__)

# 拆分至子模块的实现经此 re-export:测试与 server 既有引用面(windows.<名字>)不变
from traffic_analyzer.toolserver.tracking.absorb import (
    REANCHOR_EVERY,
    REANCHOR_MISMATCH_IOU,
    STRIDE,
    WINDOW_FRAMES,
    _SuspectState,
    _absorb_window_result,
    _dedupe_points,
    _expected_box,
    _pick_candidate,
    _write_run_snapshot,
)
from traffic_analyzer.toolserver.tracking.artifacts import finalize_tracking_result
from traffic_analyzer.toolserver.tracking.errors import TrackingFailure

# 拆分至子模块的实现经此 re-export:测试与 server 既有引用面(windows.<名字>)不变
from traffic_analyzer.toolserver.tracking.errors import TrackingFailure
from traffic_analyzer.toolserver.tracking.sampling import (
    compute_env_flow,
    disp_per_second,
    extract_window_jpegs,
    read_video_meta,
    sampling_grid,
    should_upgrade_fps,
)
from traffic_analyzer.toolserver.tracking.scene_vote import (
    _as_unknown_scene,
    _build_fallback_rationale,
    _build_scene_rationale,
    _parse_heading,
    _query_heading,
    _query_scene_side,
    _scene_sample_frames,
    _scene_vote_text,
    _vote_scene_side,
    build_scene_prompt,
    parse_scene_response,
)
from traffic_analyzer.toolserver.tracking.window_llm import (
    _collect_boxes,
    _norm_box,
    parse_window_json,
    parse_window_response,
    build_window_prompt,
)

# --- 编排参数(计划默认值) -----------------------------------------------

DEFAULT_SAMPLE_FPS = 5.0
FAST_SAMPLE_FPS = 10.0
SPAN_MARGIN_S = 2.0         # 锚点前后扩展的上下文秒数
DEFAULT_SPAN_S = 8.0        # 无 time_range 时锚点之后继续跟踪的时长
MAX_WINDOW_CALLS = 40       # 单次请求 VLM 调用硬上限(控成本/兜底超时)
TRACKING_MAX_WORKERS = 4    # 并发 VLM 调用上限(场景判定帧/车头朝向旁证)

_TEMPLATE_ID = "track_suspects_window"


def _clipped_count(track: Track) -> int:
    """轨迹中贴边(可能被裁断)的采样点数量。"""
    return sum(1 for p in track.points if is_clipped_box(p.box))


# ---------------------------------------------------------------------------
# 主编排
# ---------------------------------------------------------------------------


def run_tracking(
    engine: Any,
    video_path: Path,
    anchors: Sequence[SuspectAnchor],
    time_range: Optional[Sequence[float]] = None,
    out_dir: Optional[Path] = None,
    deadline: Optional[float] = None,
    max_workers: int = TRACKING_MAX_WORKERS,
) -> Dict[str, Any]:
    """执行定向跟踪,返回端点契约所需的载荷(server 补 artifacts 相对路径)。

    运行级问题不抛异常,返回 {"failed": True, "failure_reason": ...};
    只有视频不可读这类致命问题抛 TrackingFailure。

    Args:
        max_workers: 并发 VLM 调用上限(场景 side 判定 3 帧、车头朝向旁证
            按 suspect 并发)。默认 TRACKING_MAX_WORKERS;设为 1 可退回串行。
    """
    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
    meta = read_video_meta(video_path)
    duration = meta["total"] / meta["fps"]

    # --- 可疑时段 ---
    if time_range and len(time_range) == 2:
        t0 = max(0.0, float(min(time_range)))
        t1 = min(duration, float(max(time_range)))
    else:
        anchor_ts = [a.timestamp for a in anchors]
        t0 = max(0.0, min(anchor_ts) - SPAN_MARGIN_S)
        t1 = min(duration, max(anchor_ts) + SPAN_MARGIN_S + DEFAULT_SPAN_S)
    if t1 <= t0:
        return {
            "failed": True,
            "failure_reason": f"empty time range [{t0:.2f},{t1:.2f}]s",
        }

    grid = sampling_grid(meta, t0, t1, DEFAULT_SAMPLE_FPS)
    if len(grid) < 2:
        return {"failed": True, "failure_reason": "fewer than 2 sampled frames"}

    suspects = [
        _SuspectState(index=i, anchor=a, letter=chr(ord("A") + i))
        for i, a in enumerate(anchors)
    ]
    events_all: List[Dict[str, Any]] = []
    ref_windows: List[Tuple[int, List[Dict[str, Any]]]] = []  # (ref_id, 局部帧点)
    windows_log: List[Dict[str, Any]] = []

    fps_used = DEFAULT_SAMPLE_FPS
    fps_upgraded = False
    win_calls = 0
    n_ok_calls = 0
    stop_reason = "completed"

    # --- 锚点播种:锚框直接入链,锚点时刻映射到采样网格 ---
    # (时间戳缺省 → 不播种,回退旧式 t0 正向盲检路径)
    for s in suspects:
        ts = getattr(s.anchor, "timestamp", None)
        if ts is None:
            continue
        s.anchor_grid_index = min(
            range(len(grid)), key=lambda i: abs(grid[i] / meta["fps"] - ts)
        )
        s.anchor_frame = grid[s.anchor_grid_index]
        s.anchor_group = s.anchor_grid_index // STRIDE
        s.points.append({"frame": s.anchor_frame, "box": list(s.anchor.box)})
    anchored_groups = [s.anchor_group for s in suspects if s.anchor_group is not None]
    first_group = min(anchored_groups) if anchored_groups else 0

    # --- 场景级中央隔离带方位判定(首/中/尾 3 帧多数表决,帧间可并发) ---
    scene_sample_frames = _scene_sample_frames(grid, meta, t0, t1)
    scene_frame_index = scene_sample_frames[0] if scene_sample_frames else None
    scene_side_result: Optional[Dict[str, Any]] = None
    scene_frame_results: List[Dict[str, Any]] = []
    if scene_sample_frames:
        scene_jpegs = extract_window_jpegs(video_path, scene_sample_frames)
        if max_workers == 1:
            for jpeg, frame in zip(scene_jpegs, scene_sample_frames):
                scene_frame_results.append(
                    {"frame": frame, "result": _query_scene_side(engine, jpeg, suspects)}
                )
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(_query_scene_side, engine, jpeg, suspects)
                    for jpeg in scene_jpegs
                ]
                scene_frame_results = [
                    {"frame": frame, "result": future.result()}
                    for frame, future in zip(scene_sample_frames, futures)
                ]
    if scene_frame_results:
        scene_side_result = _vote_scene_side(
            scene_frame_results, suspects, events_all
        )
    if scene_side_result is None:
        scene_side_result = _as_unknown_scene(
            suspects, error="sample frames unavailable"
        )

    def _reindex_anchors(new_grid: List[int]) -> int:
        """fps 升档后把锚点定位迁移到新网格,返回新的最早锚点窗组号。"""
        for s in suspects:
            if s.anchor_frame is None:
                continue
            s.anchor_grid_index = min(
                range(len(new_grid)), key=lambda i: abs(new_grid[i] - s.anchor_frame)  # type: ignore[arg-type]
            )
            s.anchor_frame = new_grid[s.anchor_grid_index]
            s.anchor_group = s.anchor_grid_index // STRIDE
        groups_now = [s.anchor_group for s in suspects if s.anchor_group is not None]
        return min(groups_now) if groups_now else 0

    def _participants(group: Optional[int], direction: str) -> List[_SuspectState]:
        """本窗参与目标:剔除失活;前向未到锚点窗的锚定目标不参与
        (防止目标进场前盲检锁错对象)。"""
        out: List[_SuspectState] = []
        for s in suspects:
            if not s.active:
                continue
            if (
                direction == "forward"
                and group is not None
                and s.anchor_group is not None
                and s.anchor_group > group
            ):
                continue
            out.append(s)
        return out

    def _deadline_hit() -> bool:
        return deadline is not None and deadline - _now() <= 0

    def _execute_window(
        win_frames: List[int], direction: str, group: Optional[int] = None
    ) -> Dict[str, Any]:
        """跑一个窗:prompt → VLM → 吸收。

        返回 {"status": "ok"/"skip"/"inactive", "response_ok": bool,
        "mode": 窗模式};skip = 有存活目标但都不属于本窗(静默跳过);
        inactive = 全部目标已失活。窗号/日志/事件/参照框/目标状态经闭包
        变量累积。
        """
        nonlocal win_calls, n_ok_calls
        wi = win_calls
        participants = _participants(group, direction)
        if not participants:
            if not any(s.active for s in suspects):
                return {"status": "inactive", "response_ok": False, "mode": "propagate"}
            # 有存活目标但都不属于本窗(如锚点窗在更后面):静默跳过
            return {"status": "skip", "response_ok": False, "mode": "propagate"}
        validations = {
            s.index: list(s.anchor.box)
            for s in participants
            if direction == "forward"
            and s.anchor_group == group
            and not s.anchor_validated
        }
        mode = (
            "reanchor"
            if (validations or wi % REANCHOR_EVERY == 0)
            else "propagate"
        )
        expected = {s.index: _expected_box(s, direction) for s in participants}
        prompt = build_window_prompt(mode, participants, len(win_frames), expected)
        images = extract_window_jpegs(video_path, win_frames)

        suspect_parsed: Dict[int, List[Dict[str, Any]]] = {}
        refs_parsed: Dict[int, List[Dict[str, Any]]] = {}
        err_msg: Optional[str] = None
        response_ok = False
        if len(images) < 2:
            err_msg = f"only {len(images)} frames readable in window {wi}"
        else:
            template = PromptTemplate(
                template_id=_TEMPLATE_ID,
                name="track_suspects window",
                user_prompt=prompt,
            )
            try:
                win_calls += 1
                call_kwargs: Dict[str, Any] = {"template": template, "images": images}
                if mode == "propagate":
                    # 传播窗是封闭感知任务(接框),关 thinking 换低延迟。
                    call_kwargs["enable_thinking"] = False
                # re-anchor 窗使用引擎默认 reasoning_effort(medium),不额外覆盖。
                resp = engine.call(**call_kwargs)
                if getattr(resp, "success", False):
                    response_ok = True
                    n_ok_calls += 1
                    suspect_parsed, refs_parsed = parse_window_response(
                        resp, participants, len(win_frames)
                    )
                else:
                    err_msg = (
                        getattr(resp, "error_message", None) or "vlm call failed"
                    )
            except Exception as exc:  # 网络/配额等:该窗按无响应处理
                logger.warning("[track_suspects] window %d vlm failed: %s", wi, exc)
                err_msg = f"vlm call error: {exc}"

        deactivated_now: List[Dict[str, Any]] = []
        if response_ok:
            before = {s.index: s.deactivated for s in participants}
            _absorb_window_result(
                suspects=participants,
                mode=mode,
                win_frames=win_frames,
                first_frame=int(win_frames[0]),
                suspect_boxes=suspect_parsed,
                expected=expected,
                events_all=events_all,
                direction=direction,
                anchor_validations=validations or None,
                window=wi,
            )
            deactivated_now = [
                {"index": s.index, "letter": s.letter, **(s.deactivated or {})}
                for s in participants
                if s.deactivated is not None and s.deactivated is not before[s.index]
            ]
            for rid, boxes in refs_parsed.items():
                if boxes:
                    # 参照框同样按 win_frames 查表换算到原始帧号
                    # (否则各窗局部 0-4 帧互相污染)
                    ref_windows.append(
                        (
                            rid,
                            [
                                {
                                    "frame": int(win_frames[int(q["frame"])])
                                    if 0 <= int(q["frame"]) < len(win_frames)
                                    else int(win_frames[-1]),
                                    "box": q["box"],
                                }
                                for q in boxes
                            ],
                        )
                    )
        # 锚点窗执行过即消耗校验机会(该窗不重跑,失败窗不重试)
        for s in participants:
            if s.index in validations:
                s.anchor_validated = True

        record = {
            "window": wi,
            "mode": mode,
            "direction": direction,
            "frames": list(win_frames),
            "timestamps": [round(f / meta["fps"], 3) for f in win_frames],
            "request_prompt": prompt,
            "response": {
                "targets": {str(k): v for k, v in suspect_parsed.items()},
                "references": {str(k): v for k, v in refs_parsed.items()},
            },
            "error": err_msg,
            "ok": response_ok,
            "deactivated": deactivated_now,
        }
        windows_log.append(record)
        if out_dir is not None:
            with open(out_dir / "windows.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {"status": "ok", "response_ok": response_ok, "mode": mode}

    # --- 正向传播:锚点窗 → 时段末端 ---
    pos = first_group * STRIDE
    while pos < len(grid):
        if _deadline_hit():
            stop_reason = "timeout"
            break
        if win_calls >= MAX_WINDOW_CALLS:
            stop_reason = "max_calls"
            break
        if not any(s.active for s in suspects):
            stop_reason = "all_inactive"
            break
        win_frames = grid[pos : pos + WINDOW_FRAMES]
        if not win_frames:
            break
        res = _execute_window(win_frames, "forward", group=pos // STRIDE)
        if res["status"] == "skip":
            pos += STRIDE
            continue
        if res["status"] != "ok":
            stop_reason = "all_inactive" if res["status"] == "inactive" else "timeout"
            break

        # 自适应升帧率:首个传播窗发现高速目标即整段改用 10fps 接续
        if res["response_ok"] and not fps_upgraded and res["mode"] == "propagate":
            upgraded = False
            for s in suspects:
                recent = s.points[-WINDOW_FRAMES:]
                if len(recent) >= 2 and should_upgrade_fps(recent, meta["fps"]):
                    upgraded = True
                    break
            if upgraded:
                fps_upgraded = True
                fps_used = FAST_SAMPLE_FPS
                new_grid = sampling_grid(meta, t0, t1, FAST_SAMPLE_FPS)
                boundary = win_frames[-1]
                bi = min(bisect.bisect_left(new_grid, boundary), len(new_grid) - 1)
                grid = new_grid
                first_group = _reindex_anchors(grid)
                # 对齐到组边界继续:重叠帧吸收时去重,不产生覆盖空洞
                pos = max((bi // STRIDE) * STRIDE, 0)
                events_all.append(
                    {
                        "type": "fps_upgrade",
                        "frame": int(boundary),
                        "label": f"{fps_used:g}fps",
                    }
                )
                continue

        pos += STRIDE

    # --- 反向传播:锚点窗之前 → 时段起点(无锚点时间则无反向窗) ---
    end_idx = first_group * STRIDE
    while end_idx > 0:
        if _deadline_hit():
            stop_reason = "timeout"
            break
        if win_calls >= MAX_WINDOW_CALLS:
            stop_reason = "max_calls"
            break
        if not any(s.active for s in suspects):
            stop_reason = "all_inactive"
            break
        lo = max(end_idx - (WINDOW_FRAMES - 1), 0)
        res = _execute_window(grid[lo : end_idx + 1], "backward")
        if res["status"] == "skip":
            end_idx -= STRIDE
            continue
        if res["status"] != "ok":
            stop_reason = "all_inactive" if res["status"] == "inactive" else "timeout"
            break
        end_idx -= STRIDE

    # --- 超时:契约保持 failed:true,但落 run.json 便于观测 ---
    if stop_reason == "timeout":
        if out_dir is not None:
            _write_run_snapshot(
                out_dir, video_path, anchors, time_range, fps_used, None,
                tracks=[], events=events_all, stop_reason=stop_reason,
                suspects=suspects,
                scene_side_result=scene_side_result,
                scene_frame_index=scene_frame_index,
                scene_sample_frames=scene_sample_frames,
            )
        return {"failed": True, "failure_reason": "tracking timed out"}

    # --- 全部窗口失败 ---
    if n_ok_calls == 0:
        reason = windows_log[-1].get("error") if windows_log else "vlm unavailable"
        if out_dir is not None:
            _write_run_snapshot(
                out_dir, video_path, anchors, time_range, fps_used, None,
                tracks=[], events=events_all, stop_reason=stop_reason,
                suspects=suspects,
                scene_side_result=scene_side_result,
                scene_frame_index=scene_frame_index,
                scene_sample_frames=scene_sample_frames,
            )
        return {"failed": True, "failure_reason": f"all VLM window calls failed ({reason})"}

    # --- 参照车缝合与环境流速 ---
    ref_tracks = (
        stitch.stitch_overlapping([list(seg) for _, seg in ref_windows])
        if ref_windows
        else []
    )
    env_flow = compute_env_flow(ref_tracks, meta["fps"], fps_used)

    # --- 轨迹装配:去重 → 瞬移断裂 → 取主链 → 平滑 → 档案/互证 ---
    span_s = max(t1 - t0, 0.0)  # 目标时段总长(coverage 分母)
    tracks: List[Track] = []
    dropped: List[str] = []

    # 第一阶段:装配 Track 并一次性评估方向(状态机只算一次)
    pending: List[Tuple[_SuspectState, Track, DirectionAssessment]] = []
    for s in suspects:
        ordered = _dedupe_points(s.points)
        if not ordered:
            dropped.append(f"suspect {s.letter}({s.anchor.description}): never located")
            continue
        chains = stitch.teleport_break(ordered)
        if len(chains) > 1:
            split_at = int(chains[1][0]["frame"])
            s.events.append({"type": "teleport_split", "frame": split_at})
            events_all.append({"type": "teleport_split", "frame": split_at, "label": "split"})
        main = stitch.longest_chain(chains)
        smoothed = stitch.smooth_chain(main)
        track = Track(
            id=0,  # 占位;通过互证后再按 tracks 顺序赋真实 id
            description=s.anchor.description,
            points=[
                TrackPoint(
                    frame_idx=int(q["frame"]),
                    timestamp=round(int(q["frame"]) / meta["fps"], 3),
                    box=q["box"],
                )
                for q in smoothed
            ],
        )
        track.profile = compute_profile(
            track, fps=fps_used, env_flow=env_flow, span_s=span_s
        )
        # side 来源优先级:场景判定 > 锚点 side > 描述关键词嗅探
        anchor_side = getattr(s.anchor, "side", None) or "unknown"
        scene_target = (scene_side_result or {}).get("per_target", {}).get(
            s.index, {}
        )
        scene_side = scene_target.get("side", "unknown")
        scene_median = (scene_side_result or {}).get("median_side", "unknown")

        if scene_side != "unknown":
            track.side_hint = scene_side
            track.side_source = "scene"
            vote_text = _scene_vote_text(
                (scene_side_result or {})
                .get("votes", {})
                .get("per_target", {})
                .get(s.index, {}),
                scene_side,
            )
            base_rationale = _build_scene_rationale(
                scene_median, scene_side, scene_target.get("rationale")
            )
            track.side_rationale = f"场景方位{vote_text}。" + (base_rationale or "")
            if anchor_side != "unknown" and anchor_side != scene_side:
                events_all.append(
                    {
                        "type": "side_conflict",
                        "frame": scene_frame_index,
                        "index": s.index,
                        "anchor_side": anchor_side,
                        "scene_side": scene_side,
                    }
                )
        elif anchor_side != "unknown":
            track.side_hint = anchor_side
            track.side_source = "anchor"
            track.side_rationale = _build_fallback_rationale(anchor_side, "anchor")
        else:
            inferred = infer_side_hint(track.description)
            track.side_hint = inferred
            track.side_source = "infer" if inferred != "unknown" else "unknown"
            track.side_rationale = (
                _build_fallback_rationale(inferred, "infer")
                if inferred != "unknown"
                else None
            )

        # 回写 suspect 状态,便于 run.json 记录每个目标最终 side 与来源
        s.final_side = track.side_hint
        s.side_source = track.side_source
        s.side_rationale = track.side_rationale

        # 贴边/裁断信息入档案(取最简:计数 + 帧号列表)
        track.profile["clipped_count"] = _clipped_count(track)
        track.profile["clipped_frames"] = [
            p.frame_idx for p in track.points if is_clipped_box(p.box)
        ]

        assessment = evaluate_direction(track)
        pending.append((s, track, assessment))

    # 第二阶段:按 suspect 并发请求车头朝向旁证(仅 needs_heading 的目标)
    def _fetch_heading(
        item: Tuple[_SuspectState, Track, DirectionAssessment]
    ) -> Optional[Dict[str, Any]]:
        _s, track, assessment = item
        if assessment.needs_heading:
            return _query_heading(engine, video_path, track)
        return None

    if max_workers == 1 or len(pending) <= 1:
        headings: List[Optional[Dict[str, Any]]] = [
            _fetch_heading(item) for item in pending
        ]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            headings = list(executor.map(_fetch_heading, pending))

    # 第三阶段:渲染 verdict 并做互证跑飞检查
    for (s, track, assessment), heading_result in zip(pending, headings):
        track.heading = heading_result
        track.direction_verdict = render_direction(assessment, heading=heading_result)
        ok, why = is_consistent(track)
        if not ok:
            dropped.append(f"suspect {s.letter}({s.anchor.description}): {why}")
            events_all.append(
                {"type": "mutual_check_fail", "frame": track.points[0].frame_idx, "label": "drift"}
            )
            continue
        track.id = len(tracks) + 1
        tracks.append(track)

    return finalize_tracking_result(
        video_path=video_path,
        anchors=anchors,
        time_range=time_range,
        out_dir=out_dir,
        fps_used=fps_used,
        env_flow=env_flow,
        stop_reason=stop_reason,
        events_all=events_all,
        dropped=dropped,
        suspects=suspects,
        scene_side_result=scene_side_result,
        scene_frame_index=scene_frame_index,
        scene_sample_frames=scene_sample_frames,
        tracks=tracks,
        grid=grid,
        meta=meta,
    )


def _now() -> float:
    """与 server 侧 time.monotonic() 同一单调时钟(deadline 用)。"""
    return time.monotonic()
