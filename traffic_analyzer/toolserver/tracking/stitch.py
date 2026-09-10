"""Track stitching helpers ported from the legacy pipeline (给房工急活20260820).

[文件说明]
作用:轨迹段后处理四件套(代码抄自旧 pipeline 并适配本包,不跨目录 import):
    - segment-stitch/track.py 的 ``_iou`` 重叠帧 IoU 段间缝合;
      < 1.5 × bbox 对角线);
    - teleport_break:相邻点中心跳变 > 1.5 × 对角线立即断裂(计划新增);
    - smooth/smooth.py 的断裂感知居中滑动平均(窗 5,gap > 20 不跨)。
    全部函数操作普通点字典 {"frame": int, "box": [x1,y1,x2,y2]}(0-1 归一化),
    与 Track 数据结构解耦,便于单测。
上游:windows.py(窗编排调用);tests/test_tracking_stitch.py。
下游:纯计算,无 IO。
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

# 与旧 track.py 一致的重叠帧匹配 IoU 阈值
IOU_TH = 0.25
# merge_tracks.py 的允许最大间隔(原始帧数)
MAX_GAP_FRAMES = 20
# 合并距离阈值(× 对角线;旧代码像素空间额外 max(d,1px),归一化空间不需要)
# 瞬移断裂阈值(× 对角线)
TELEPORT_RATIO = 1.5
# smooth.py 滑动平均窗口与断裂判定
SMOOTH_WINDOW = 5
SMOOTH_MAX_GAP = MAX_GAP_FRAMES


def iou(a: List[float], b: List[float]) -> float:
    """IoU of two [x1,y1,x2,y2] boxes — verbatim port of legacy _iou."""
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)

    def area(r: List[float]) -> float:
        return max(0, r[2] - r[0]) * max(0, r[3] - r[1])

    union = area(a) + area(b) - inter
    return inter / union if union > 0 else 0.0


def _center(bb: List[float]) -> "tuple[float, float]":
    return ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)


def _diag(bb: List[float]) -> float:
    return math.hypot(bb[2] - bb[0], bb[3] - bb[1])


Point = Dict[str, object]
Segment = List[Point]


def stitch_overlapping(segments: List[Segment], iou_th: float = IOU_TH) -> List[Segment]:
    """Legacy IoU segment stitching, generalized to normalized coordinates.

    segments 为按时间顺序排列的窗内局部轨迹(各含若干 {frame, box});
    窗间有 stride=4 的重叠帧,同一目标在重叠帧上 box 高度相似。
    依次把每段并入与它在重叠帧上 IoU 最高的既有轨迹,否则新建一条,
    返回去重排序后的全局轨迹列表(旧 track.py extract_tracks 主循环同构)。
    """
    stitched: List[Segment] = []
    for seg in segments:
        best: Optional[Segment] = None
        best_iou = iou_th
        for gt in stitched:
            gmap = {q["frame"]: q["box"] for q in gt}  # type: ignore[index]
            for p in seg:
                if p["frame"] in gmap:
                    v = iou(p["box"], gmap[p["frame"]])  # type: ignore[arg-type]
                    if v > best_iou:
                        best, best_iou = gt, v
        if best is None:
            best = []
            stitched.append(best)
        have = {q["frame"] for q in best}
        for p in seg:
            if p["frame"] not in have:
                best.append(p)
    out = []
    for gt in stitched:
        gt.sort(key=lambda q: q["frame"])  # type: ignore[arg-type,return-value,operator]
        out.append(gt)
    return [gt for gt in out if gt]


def teleport_break(chain: Segment, ratio: float = TELEPORT_RATIO) -> List[Segment]:
    """瞬移断裂:相邻帧中心点位移 > ratio × max(两点对角线) 处切开。

    正常抽帧网格下(VLM 隔帧可见)目标移动有限;跳变即身份混淆信号。
    返回按断裂位置切出的连续子链列表(至少一段)。
    """
    if not chain:
        return []
    segs: List[Segment] = [[chain[0]]]
    for prev, cur in zip(chain, chain[1:]):
        c_prev = _center(prev["box"])  # type: ignore[arg-type]
        c_cur = _center(cur["box"])  # type: ignore[arg-type]
        disp = math.dist(c_prev, c_cur)
        th = ratio * max(_diag(prev["box"]), _diag(cur["box"]))  # type: ignore[arg-type]
        if disp > th:
            segs.append([cur])
        else:
            segs[-1].append(cur)
    return segs


def longest_chain(chains: List[Segment]) -> Segment:
    """取最长链(瞬移断裂后的保留策略:主轨迹为最长的连续段)。"""
    if not chains:
        return []
    return max(chains, key=len)


def _smooth_segment(seg: Segment, window: int) -> Segment:
    """smooth.py 的段内居中滑动平均(浮点坐标,不做整型舍入)。"""
    half = window // 2
    out: Segment = []
    for i, q in enumerate(seg):
        win = seg[max(0, i - half) : i + half + 1]
        box = [
            sum(float(p["box"][k]) for p in win) / len(win)  # type: ignore[arg-type]
            for k in range(4)
        ]
        out.append({"frame": q["frame"], "box": box})
    return out


def smooth_chain(
    points: Segment, window: int = SMOOTH_WINDOW, max_gap: int = SMOOTH_MAX_GAP
) -> Segment:
    """smooth.py 的断裂感知滑动平均:gap > max_gap 处分段,不跨断裂平滑。

    返回新列表(frame 保持不变,box 取窗内均值)。
    """
    if not points:
        return []
    ordered = sorted(points, key=lambda q: int(q["frame"]))  # type: ignore[arg-type,return-value,operator]
    segs: List[List[Point]] = []
    cur: List[Point] = []
    for q in ordered:
        if cur and int(q["frame"]) - int(cur[-1]["frame"]) > max_gap:  # type: ignore[arg-type]
            segs.append(cur)
            cur = []
        cur.append(q)
    if cur:
        segs.append(cur)
    out: Segment = []
    for seg in segs:
        out.extend(_smooth_segment(seg, window))
    return out
