"""Suspect-target tracking package for /tools/track_suspects.

[文件说明]
作用:定向跟踪子系统的实现包。windows.run_tracking 编排 VLM 滑窗跟踪,
    models 定义锚点/轨迹数据结构与数值档案计算,stitch/render/cache 分别
    提供轨迹缝合、产物渲染与磁盘结果缓存;server.py 的 /tools/track_suspects
    是唯一对外入口。
上游:toolserver/server.py(端点)。
下游:tracking/models.py(数据结构与档案)、stitch.py(缝合/外推/平滑)、
    render.py(mp4/png/csv 产物)、windows.py(VLM 窗编排,复用 core/vlm_engine)、
    cache.py(结果缓存,.agent/tracks/_cache/)。
"""

from traffic_analyzer.toolserver.tracking.models import (
    STATIC_DISPLACEMENT_RATIO,
    SLOW_SPEED_RATIO,
    SuspectAnchor,
    Track,
    TrackPoint,
    bbox_center,
    box_diagonal,
    compute_profile,
    direction_verdict,
    evaluate_direction,
    infer_side_hint,
    is_consistent,
    render_direction,
)

__all__ = [
    "STATIC_DISPLACEMENT_RATIO",
    "SLOW_SPEED_RATIO",
    "SuspectAnchor",
    "Track",
    "TrackPoint",
    "bbox_center",
    "box_diagonal",
    "compute_profile",
    "direction_verdict",
    "evaluate_direction",
    "infer_side_hint",
    "is_consistent",
    "render_direction",
]
