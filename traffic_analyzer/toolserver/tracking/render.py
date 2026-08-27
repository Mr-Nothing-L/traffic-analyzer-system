"""Artifact rendering for suspect tracking — ported/adapted from legacy visualize.py.

[文件说明]
作用:跟踪产物渲染(代码移植自旧 pipeline/visualize.py 并适配归一化坐标):
    - overlay_video:可疑时段轨迹叠加视频(逐帧插值框+轨迹 ID+拖尾+
      时间戳+自检事件帧标记);
    - speed_colored_image:速度染色轨迹叠加图(红=静止/黄=缓行/绿=正常,
      带方向箭头+轨迹 ID);
    - best_frame_crops:每条轨迹取目标最大最清晰的 1-2 张裁剪帧;
    - export_csv:tracks.csv 导出。
上游:windows.py(编排后统一落盘);tests/test_tracking_render.py。
下游:cv2(读视频/写视频/编码 JPEG);models(Track 数据结构与速度阈值)。
"""

from __future__ import annotations

import base64
import csv
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import cv2

from traffic_analyzer.toolserver.tracking.models import (
    STATIC_DISPLACEMENT_RATIO,
    SLOW_SPEED_RATIO,
    Track,
    TrackPoint,
    bbox_center,
    box_diagonal,
)

# 叠加视频/大图的最大宽度(与旧 visualize.py 一致)
MAX_WIDTH = 1280
# 拖尾时长(秒)
TAIL_SECONDS = 2.0
# best-frame 裁剪的外扩比例(对 bbox 边长)
CROP_MARGIN = 0.25
_BEST_FRAMES_PER_TRACK = 2
_JPEG_QUALITY = 85

# 速度染色(BGR):红=静止 / 黄=缓行 / 绿=正常
_SPEED_COLOR = {
    "red": (0, 0, 255),
    "yellow": (0, 255, 255),
    "green": (0, 255, 0),
}


def _track_id_color(tid: int) -> "tuple[int, int, int]":
    """Stable per-id color (legacy visualize.py convention)."""
    return ((tid * 67) % 256, (tid * 131) % 256, (tid * 197) % 256)


def _interp_bbox(
    pts: Sequence[Dict[str, Any]], fi: int, max_gap: int = 20
) -> Optional[List[float]]:
    """帧 fi 处线性插值归一化 bbox;跨断裂或范围外返回 None(legacy 移植)。"""
    prev = nxt = None
    for q in sorted(pts, key=lambda d: d["frame"]):  # type: ignore[arg-type,return-value,type-var]
        if int(q["frame"]) <= fi:
            prev = q
        if int(q["frame"]) >= fi and nxt is None:
            nxt = q
    if prev is None or nxt is None:
        return None
    gap = int(nxt["frame"]) - int(prev["frame"])  # type: ignore[arg-type]
    if gap > max_gap:
        return prev["box"] if fi == int(prev["frame"]) else None  # type: ignore[arg-type]
    if gap == 0:
        return prev["box"]  # type: ignore[return-value]
    r = (fi - int(prev["frame"])) / gap  # type: ignore[arg-type]
    return [a + (b - a) * r for a, b in zip(prev["box"], nxt["box"])]  # type: ignore[index]


def _segment_speed_state(p1: Dict[str, Any], p2: Dict[str, Any]) -> str:
    """相邻两点的局部速度状态:按 bbox 对角线比例归一口径分档。"""
    c1 = bbox_center(p1["box"])  # type: ignore[arg-type]
    c2 = bbox_center(p2["box"])  # type: ignore[arg-type]
    disp = ((c2[0] - c1[0]) ** 2 + (c2[1] - c1[1]) ** 2) ** 0.5
    dt = abs(float(p2.get("timestamp", p1["frame"])) - float(p1.get("timestamp", p1["frame"])))  # noqa: E501
    diag = max(box_diagonal(p2["box"]), 1e-6)  # type: ignore[arg-type]
    speed = disp / dt if dt > 0 else 0.0
    if speed < STATIC_DISPLACEMENT_RATIO * diag:
        return "red"
    if speed < SLOW_SPEED_RATIO * diag:
        return "yellow"
    return "green"


def _as_point_dicts(track: Track) -> List[Dict[str, Any]]:
    return [
        {"frame": p.frame_idx, "timestamp": p.timestamp, "box": p.box}
        for p in track.points
    ]


def overlay_video(
    video_path: Path,
    tracks: Sequence[Track],
    out_path: Path,
    start_frame: Optional[int] = None,
    end_frame: Optional[int] = None,
    fps_hint: Optional[float] = None,
    event_marks: Optional[Dict[int, str]] = None,
) -> Path:
    """叠加标注视频:可疑时段逐帧画框+轨迹 ID+拖尾+时间戳+事件帧标记。

    start/end_frame 缺省覆盖整段视频;事件标记为 {frame_idx: 短标签},
    来自 re-anchor 偏差、瞬移断裂等自检事件,画在该帧左上角。
    """
    cap = cv2.VideoCapture(str(video_path))
    try:
        src_fps = float(cap.get(cv2.CAP_PROP_FPS)) or (fps_hint or 25.0)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        sx = MAX_WIDTH / w if w > MAX_WIDTH else 1.0
        out_w, out_h = int(w * sx), int(h * sx)
        writer = cv2.VideoWriter(
            str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), src_fps, (out_w, out_h)
        )
        if not writer.isOpened():
            raise RuntimeError(f"cv2.VideoWriter failed to open: {out_path}")

        lo = 0 if start_frame is None else max(0, start_frame)
        hi = (total - 1) if end_frame is None else min(total - 1, end_frame)
        tpieces = [
            (t.id, t.description, sorted(_as_point_dicts(t), key=lambda d: d["frame"]))  # type: ignore[arg-type,return-value,operator]
            for t in tracks
            if t.points
        ]
        tail_len = int(src_fps * TAIL_SECONDS)
        fi = lo
        while fi <= hi:
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, frame = cap.read()
            if not ok:
                break
            if sx != 1.0:
                frame = cv2.resize(frame, (out_w, out_h))
            for tid, desc, pts in tpieces:
                bb = _interp_bbox(pts, fi)
                if bb is None:
                    continue
                color = _track_id_color(tid)
                # 拖尾:过去 tail_len 帧内的中心点折线
                if tail_len > 0:
                    tail = [
                        tuple(v * sx for v in bbox_center(q["box"]))
                        for q in pts
                        if fi - tail_len <= int(q["frame"]) <= fi
                    ]
                    for a, b in zip(tail, tail[1:]):
                        cv2.line(
                            frame,
                            (int(a[0]), int(a[1])),
                            (int(b[0]), int(b[1])),
                            color,
                            2,
                        )
                x1, y1, x2, y2 = [int(v * sx) for v in bb]
                ts_s = fi / src_fps if src_fps > 0 else 0.0
                label = f"#{tid} {desc[:12]} {ts_s:.1f}s"
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                cv2.putText(
                    frame,
                    label,
                    (x1, max(y1 - 6, 14)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    color,
                    1,
                    cv2.LINE_AA,
                )
            mark = (event_marks or {}).get(fi)
            if mark:
                cv2.putText(
                    frame,
                    f"[{mark}]",
                    (10, 24),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 200, 255),
                    2,
                    cv2.LINE_AA,
                )
            writer.write(frame)
            fi += 1
        writer.release()
    finally:
        cap.release()
    return out_path


def speed_colored_image(
    video_path: Path,
    tracks: Sequence[Track],
    out_path: Optional[Path] = None,
) -> "tuple[Any, Optional[bytes]]":
    """速度染色轨迹叠加图:红=静止/黄=缓行/绿=正常 + 方向箭头 + 轨迹 ID。

    底图取全部轨迹时间跨度中点的那一帧;返回 (BGR 图像, 可选 jpeg 字节)。
    """
    img, _mid_f, _fps = _colored_canvas(video_path, tracks)
    jpeg = None
    if out_path is not None:
        cv2.imwrite(str(out_path), img)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY])
    if ok:
        jpeg = buf.tobytes()
    return img, jpeg


def _colored_canvas(
    video_path: Path, tracks: Sequence[Track]
) -> "tuple[Any, int, float]":
    """加载底图(轨迹中点帧)并在其上绘制速度染色轨迹;返回 (img, 底图帧号, fps)。"""
    cap = cv2.VideoCapture(str(video_path))
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS)) or 25.0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frames = [p.frame_idx for t in tracks for p in t.points]
        mid_f = int(sum(frames) / len(frames)) if frames else total // 2
        cap.set(cv2.CAP_PROP_POS_FRAMES, min(max(mid_f, 0), max(total - 1, 0)))
        ok, img = cap.read()
        if not ok or img is None:
            raise RuntimeError(f"cannot read frame {mid_f} of {video_path}")
        sx = MAX_WIDTH / w if w > MAX_WIDTH else 1.0
        if sx != 1.0:
            img = cv2.resize(img, (int(w * sx), int(h * sx)))
    finally:
        cap.release()

    for tid, t in [(t.id, t) for t in tracks if len(t.points) >= 2]:
        pts = sorted(_as_point_dicts(t), key=lambda d: d["frame"])
        for i, (p1, p2) in enumerate(zip(pts, pts[1:])):
            state = _segment_speed_state(p1, p2)
            color = _SPEED_COLOR[state]
            c1 = bbox_center(p1["box"])
            c2 = bbox_center(p2["box"])
            pa = (int(c1[0] * img.shape[1]), int(c1[1] * img.shape[0]))
            pb = (int(c2[0] * img.shape[1]), int(c2[1] * img.shape[0]))
            thick = 3 if state != "red" else 4
            cv2.line(img, pa, pb, color, thick)
            # 方向箭头:每隔一段在较长位移上画箭头头部
            if i % 2 == 0 and (abs(pb[0] - pa[0]) + abs(pb[1] - pa[1])) > 8:
                cv2.arrowedLine(img, pa, pb, color, thick, tipLength=0.35)
        last_c = bbox_center(pts[-1]["box"])
        px = (int(last_c[0] * img.shape[1]), int(last_c[1] * img.shape[0]))
        cv2.putText(
            img,
            f"#{tid}",
            (px[0] + 6, max(px[1] - 6, 14)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            _track_id_color(tid),
            2,
            cv2.LINE_AA,
        )
    return img, mid_f, fps


def best_frame_crops(
    video_path: Path,
    track: Track,
    max_frames: int = _BEST_FRAMES_PER_TRACK,
) -> List[Dict[str, str]]:
    """每条轨迹 1-2 张 best-frame crop:选目标 bbox 面积最大(最清晰)的帧。

    Returns:
        [{"timestamp": "12.40", "jpeg_base64": ...}, ...](timestamp 为字符串,
        与 JSON 数值精度一致化处理,四舍五入两位小数)。
    """
    if not track.points:
        return []
    ranked = sorted(track.points, key=lambda p: box_diagonal(p.box), reverse=True)
    chosen: List[TrackPoint] = []
    for p in ranked:
        if len(chosen) >= max_frames:
            break
        if any(abs(p.frame_idx - q.frame_idx) <= 2 for q in chosen):
            continue
        chosen.append(p)

    cap = cv2.VideoCapture(str(video_path))
    out: List[Dict[str, str]] = []
    try:
        for p in sorted(chosen, key=lambda q: q.frame_idx):
            cap.set(cv2.CAP_PROP_POS_FRAMES, p.frame_idx)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            fh, fw = frame.shape[:2]
            x1f, y1f, x2f, y2f = p.box
            mx, my = (x2f - x1f) * CROP_MARGIN, (y2f - y1f) * CROP_MARGIN
            xa = max(0, int((x1f - mx) * fw))
            ya = max(0, int((y1f - my) * fh))
            xb = min(fw, int((x2f + mx) * fw))
            yb = min(fh, int((y2f + my) * fh))
            crop = frame[ya:yb, xa:xb]
            if crop.size == 0:
                continue
            ok, buf = cv2.imencode(
                ".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY]
            )
            if not ok:
                continue
            out.append(
                {
                    "timestamp": f"{p.timestamp:.2f}",
                    "jpeg_base64": base64.b64encode(buf.tobytes()).decode("ascii"),
                }
            )
    finally:
        cap.release()
    return out


def export_csv(tracks: Sequence[Track], out_path: Path, video_name: str) -> Path:
    """tracks.csv:video,track_id,description,frame,time_s,x1,y1,x2,y2。"""
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(
            ["video", "track_id", "description", "frame", "time_s", "x1", "y1", "x2", "y2"]
        )
        for t in tracks:
            for p in t.points:
                wr.writerow(
                    [video_name, t.id, t.description, p.frame_idx, round(p.timestamp, 2)]
                    + [round(v, 4) for v in p.box]
                )
    return out_path
