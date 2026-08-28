"""Tracking data structures and per-track numeric profile computation.

[文件说明]
作用:定向跟踪的数据结构(SuspectAnchor 锚点 / TrackPoint 轨迹点 / Track
    轨迹)与数值档案计算:方向角、平均速度、静止时长、bbox 面积趋势、
    环境流速比、轨迹覆盖时长/覆盖率;附互证跑飞规则(档案称静止但轨迹
    总长超阈值→标跑飞)与 side_hint/方向一致性初判(低覆盖时静止结论
    追加证据不足限定)。所有位移阈值按 bbox 对角线比例归一,
    不使用绝对像素。
上游:tracking/__init__.py 再导出;windows.py(编排)、render.py(渲染)、
    tests/test_tracking_models.py。
下游:纯函数与 dataclass,无外部依赖(cv2/numpy 不需要)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --- 阈值(均按 bbox 对角线的比例归一) -----------------------------------

# 整段轨迹总位移 < 0.15 × 对角线 → 判静止
STATIC_DISPLACEMENT_RATIO = 0.15
# 平均速度 < 0.5 × 对角线/秒 → 缓行(介于静止上界与正常之间)
SLOW_SPEED_RATIO = 0.5
# 互证防跑飞:轨迹总长 > 3.0 × 对角线 而档案称静止 → 标跑飞
K_TRAJECTORY_LENGTH_RATIO = 3.0
# 锚点描述中的静止声明关键词(互证规则的"语义声明"口径)
_STATIC_KEYWORDS = ("静止", "违停", "停车", "停在", "static")
# bbox 面积趋势:首尾面积变化 < 15% 视为稳定
TREND_CHANGE_RATIO = 0.15
# 趋势判定平滑:取前/后各 TREND_WINDOW 个点的均值再比
TREND_WINDOW = 5
# 环境流速 ≈ 0 的下限(归一化单位/秒),低于它视为环境静止
FLOW_EPSILON = 1e-3


@dataclass
class SuspectAnchor:
    """Agent 定位的疑似目标锚点(box 为 0-1 归一化 [x1,y1,x2,y2])。"""

    box: List[float]
    timestamp: float
    description: str

    @property
    def diagonal(self) -> float:
        return box_diagonal(self.box)


@dataclass
class TrackPoint:
    """单个轨迹采样点:frame_idx 为原始视频帧号,box 为 0-1 归一化。"""

    frame_idx: int
    timestamp: float
    box: List[float]


@dataclass
class Track:
    """一条完整轨迹(profile 由 compute_profile 填充)。"""

    id: int
    description: str
    points: List[TrackPoint] = field(default_factory=list)
    profile: Dict[str, Any] = field(default_factory=dict)
    side_hint: str = "unknown"
    direction_verdict: str = "未知"


# ---------------------------------------------------------------------------
# 几何小工具
# ---------------------------------------------------------------------------


def box_diagonal(box: Sequence[float]) -> float:
    """Normalized bbox 对角线长度。"""
    return math.hypot(box[2] - box[0], box[3] - box[1])


def bbox_center(box: Sequence[float]) -> Tuple[float, float]:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def point_center(point: TrackPoint) -> Tuple[float, float]:
    return bbox_center(point.box)


def path_length(points: Sequence[TrackPoint]) -> float:
    """归一化中心点折线总长(相邻点直连,不跨断裂——断裂由调用方切分)。"""
    total = 0.0
    for a, b in zip(points, points[1:]):
        ca, cb = point_center(a), point_center(b)
        total += math.hypot(cb[0] - ca[0], cb[1] - ca[1])
    return total


# ---------------------------------------------------------------------------
# 数值档案
# ---------------------------------------------------------------------------


def _direction_deg(start: Tuple[float, float], end: Tuple[float, float]) -> float:
    """位移矢量方向角(图像坐标,x 右为 0°,y 向下增大顺时针,[-180,180])。"""
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return 0.0
    return math.degrees(math.atan2(dy, dx))


def _bbox_trend(points: Sequence[TrackPoint]) -> str:
    """bbox 面积变化趋势:靠近镜头(increasing)/远离(decreasing)/稳定。

    用前后各 TREND_WINDOW 点的平均面积对比,避免单帧抖动。
    """
    n = len(points)
    first = points[: min(TREND_WINDOW, n)]
    last = points[max(0, n - min(TREND_WINDOW, n)) :]

    def _area(pts: Sequence[TrackPoint]) -> float:
        vals = [(p.box[2] - p.box[0]) * (p.box[3] - p.box[1]) for p in pts]
        return sum(vals) / len(vals)

    a_first, a_last = _area(first), _area(last)
    base = max(a_first, 1e-9)
    change = (a_last - a_first) / base
    if change > TREND_CHANGE_RATIO:
        return "increasing"
    if change < -TREND_CHANGE_RATIO:
        return "decreasing"
    return "stable"


def stationary_duration_s(points: Sequence[TrackPoint]) -> float:
    """累计静止时长:相邻点位移 < STATIC_DISPLACEMENT_RATIO × 当时对角线的
    时间片段计入静止(逐段累加,精确到相邻采样点的时长)。"""
    total_s = 0.0
    for a, b in zip(points, points[1:]):
        dt = b.timestamp - a.timestamp
        if dt <= 0:
            continue
        diag = max(box_diagonal(b.box), 1e-6)
        ca, cb = point_center(a), point_center(b)
        disp = math.hypot(cb[0] - ca[0], cb[1] - ca[1])
        if disp < STATIC_DISPLACEMENT_RATIO * diag:
            total_s += dt
    return round(total_s, 3)


def compute_profile(
    track: Track,
    fps: Optional[float] = None,
    env_flow: Optional[float] = None,
    span_s: Optional[float] = None,
) -> Dict[str, Any]:
    """计算一条轨迹的数值档案(全部以归一化坐标/bbox 对角线为单位)。

    Args:
        track: 目标轨迹(至少一个点)。
        fps: 抽帧帧率,未给出时按时间戳差直接算速度(时间戳恒可推导速度)。
        env_flow: 环境流速(参照车中位速度,归一化单位/秒);None 表示无参照。
        span_s: 目标时段总长(秒);给出时输出 coverage = 轨迹覆盖时长/总长。

    Returns:
        dict: direction_deg / avg_speed_norm / stationary_duration_s /
        path_length_norm / bbox_trend / env_flow_ratio / mean_diagonal /
        covered_s / coverage。
    """
    points = track.points
    if not points:
        return {
            "direction_deg": 0.0,
            "avg_speed_norm": 0.0,
            "stationary_duration_s": 0.0,
            "path_length_norm": 0.0,
            "bbox_trend": "stable",
            "env_flow_ratio": None,
            "mean_diagonal": 0.0,
            "covered_s": 0.0,
            "coverage": 0.0,
        }

    start_c = point_center(points[0])
    end_c = point_center(points[-1])
    duration_s = max(points[-1].timestamp - points[0].timestamp, 0.0)
    total_len = path_length(points)
    diags = [box_diagonal(p.box) for p in points]
    mean_diag = sum(diags) / len(diags)

    avg_speed = total_len / duration_s if duration_s > 0 else 0.0
    still_s = stationary_duration_s(points)

    # 环境流速比:比值只在环境流速有意义(> FLOW_EPSILON)时给出;
    # 环境原始流速另存 env_flow_norm 供违停/拥堵分流使用。
    if env_flow is not None and env_flow >= FLOW_EPSILON:
        env_ratio: Optional[float] = round(avg_speed / env_flow, 3)
    else:
        env_ratio = None

    # 轨迹覆盖:covered_s = 首尾点时间跨度;coverage = 覆盖时长/目标时段总长。
    covered_s = round(duration_s, 3)
    if span_s is not None and span_s > 0:
        coverage: Optional[float] = round(min(duration_s / span_s, 1.0), 3)
    else:
        coverage = None

    return {
        "direction_deg": round(_direction_deg(start_c, end_c), 1),
        "avg_speed_norm": round(avg_speed, 4),
        "stationary_duration_s": still_s,
        "path_length_norm": round(total_len, 4),
        "bbox_trend": _bbox_trend(points),
        "env_flow_ratio": env_ratio,
        "env_flow_norm": round(env_flow, 4) if env_flow is not None else None,
        "mean_diagonal": round(mean_diag, 4),
        "covered_s": covered_s,
        "coverage": coverage,
    }


def is_consistent(track: Track) -> Tuple[bool, Optional[str]]:
    """互证规则:档案与轨迹形状矛盾 → 标跑飞。

    两条触发口径(均要求折线总长 > K_TRAJECTORY_LENGTH_RATIO × 对角线):
    - 位置自证:净位移(首尾中心距)< STATIC_DISPLACEMENT_RATIO × 对角线,
      即目标实际上哪儿也没去,却画出了很长的轨迹(绕圈/抖动漂移特征);
    - 语义声明:锚点描述含静止关键词(如"违停/静止/停车"),
      而轨迹总长超过阈值。
    来回大幅跳跃的情况由 stitch.teleport_break 在装配阶段切断。

    Returns:
        (是否可信, 失败原因);可信时原因为 None。
    """
    profile = track.profile
    if not track.points or not profile:
        return True, None
    diags = [box_diagonal(p.box) for p in track.points]
    diag = sum(diags) / len(diags)
    path_len = float(profile.get("path_length_norm") or path_length(track.points))
    threshold = K_TRAJECTORY_LENGTH_RATIO * max(diag, 1e-6)
    if path_len <= threshold:
        return True, None
    c_start = point_center(track.points[0])
    c_end = point_center(track.points[-1])
    net_disp = math.hypot(c_end[0] - c_start[0], c_end[1] - c_start[1])
    reasons = []
    if net_disp < STATIC_DISPLACEMENT_RATIO * max(diag, 1e-6):
        reasons.append("near-zero net displacement")
    if any(kw in (track.description or "") for kw in _STATIC_KEYWORDS):
        reasons.append("description claims stationary")
    if reasons:
        return False, (
            f"profile contradicts path: length {path_len:.3f} exceeds "
            f"{K_TRAJECTORY_LENGTH_RATIO:g}x diagonal ({diag:.3f}) "
            f"({'; '.join(reasons)}); tracking likely drifted"
        )
    return True, None


def classify_motion_state(profile: Dict[str, Any]) -> str:
    """按档案把运动状态分为 red(静止)/yellow(缓行)/green(正常)。

    render.py 速度染色与 JSON 输出共用该口径。
    """
    diag = float(profile.get("mean_diagonal") or 0.0)
    speed = float(profile.get("avg_speed_norm") or 0.0)
    if diag <= 0 or speed < STATIC_DISPLACEMENT_RATIO * max(diag, 1e-6):
        return "red"
    if speed < SLOW_SPEED_RATIO * max(diag, 1e-6):
        return "yellow"
    return "green"


def infer_side_hint(description: str) -> str:
    """从描述猜测目标所在侧(handoff 先验:左=来向,右=去向)。"""
    text = (description or "").lower()
    coming_kw = ("来向", "左", "逆行", "迎面", "对向")
    going_kw = ("去向", "右", "驶离", "远去", "顺向")
    if any(kw in text for kw in coming_kw):
        return "coming"
    if any(kw in text for kw in going_kw):
        return "going"
    return "unknown"


def direction_verdict(
    track: Track,
    fps: Optional[float] = None,
) -> str:
    """方向一致性初判(中文,供 agent 裁决引用)。

    来向目标应 bbox 随时间增大(靠近镜头),去向应缩小;静止目标按
    环境流速分流违停/拥堵(先验:环境流动→违停,环境静止→拥堵)。
    轨迹覆盖率(coverage)< 0.5 时,静止类结论追加「证据不足」限定。
    """
    profile = track.profile
    if not track.points or not profile:
        return "无有效轨迹"
    side = track.side_hint
    trend = str(profile.get("bbox_trend") or "stable")
    diag = float(profile.get("mean_diagonal") or 0.0)
    duration_s = max(track.points[-1].timestamp - track.points[0].timestamp, 0.0)
    speed = float(profile.get("avg_speed_norm") or 0.0)
    cov = profile.get("coverage")
    low_cov = (
        "；证据不足(轨迹仅覆盖 %d%%)" % int(round(float(cov) * 100))
        if isinstance(cov, (int, float)) and cov < 0.5
        else ""
    )

    moving = duration_s > 0 and speed > STATIC_DISPLACEMENT_RATIO * max(diag, 1e-6)
    if not moving:
        env_flow = profile.get("env_flow_norm")
        if env_flow is not None and env_flow >= FLOW_EPSILON:
            return "目标静止且环境正常流动(疑似违停)" + low_cov
        if env_flow is not None and env_flow < FLOW_EPSILON:
            return "目标与环境均近乎静止(疑似拥堵)" + low_cov
        return "基本静止(无环境流速参照)" + low_cov

    expectation_ok = {
        ("coming", "increasing"): True,
        ("going", "decreasing"): True,
        ("coming", "decreasing"): False,
        ("going", "increasing"): False,
    }
    key = (side, trend)
    if key in expectation_ok:
        side_cn = "来向" if side == "coming" else "去向"
        trend_cn = "增大" if trend == "increasing" else "缩小"
        if expectation_ok[key]:
            return f"方向一致({side_cn}目标 bbox{trend_cn},与先验相符)"
        return f"方向相反({side_cn}目标 bbox{trend_cn}),疑似逆行/倒车"
    return "所在侧未知,方向不作判"
