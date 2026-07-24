"""Far-distance non-motor vehicle ROI enlargement and composition utilities.

This module is now a thin backward-compatibility wrapper.  The implementation
has been split into focused submodules:

* :mod:`traffic_analyzer.utils.bbox_geometry` - bbox math helpers
* :mod:`traffic_analyzer.utils.image_drawing` - image loading / drawing primitives
* :mod:`traffic_analyzer.utils.roi_composite` - single-ROI enhancement composite
* :mod:`traffic_analyzer.utils.roi_motion` - ROI motion scoring & comparison
* :mod:`traffic_analyzer.utils.construction_evidence_gallery` - multi-ROI gallery

Existing callers can continue to import names directly from this module.

[文件说明]
作用:远距离非机动目标 ROI 增强工具的向后兼容再导出层,本身不含实现,
    仅聚合并 re-export 拆分后的五个子模块中的公共名称(见 ``__all__``)。
上游:core/expert_agent_far_enhancement.py(远距离小目标增强与运动判定流程)。
下游:utils/bbox_geometry.py、utils/image_drawing.py、utils/roi_composite.py、
    utils/roi_motion.py、utils/construction_evidence_gallery.py。
"""

from __future__ import annotations

from .bbox_geometry import (
    compute_bbox_area_px,
    compute_bbox_aspect_ratio,
    compute_enlarged_bbox,
    is_bbox_aspect_valid,
    is_bbox_aspect_valid_for_non_motor,
    is_bbox_large_enough,
    _norm_to_px,
)
from .construction_evidence_gallery import (
    create_multi_roi_gallery,
    _compute_grid_layout,
    _CONSTRUCTION_TAG_COLORS,
    _resize_crop_to_fill,
)
from .image_drawing import (
    load_image,
    _draw_crosshair,
    _draw_text_with_background,
    _load_scaled_font,
)
from .roi_composite import (
    create_composite,
    _assemble_side_by_side,
    _load_crop,
    _resize_crop,
)
from .roi_motion import (
    compute_roi_motion_score,
    create_motion_comparison_composite,
    _draw_panel_annotations,
    _ZERO_MOTION_SCORE,
)

__all__ = [
    "compute_enlarged_bbox",
    "compute_bbox_aspect_ratio",
    "is_bbox_aspect_valid",
    "is_bbox_aspect_valid_for_non_motor",
    "compute_bbox_area_px",
    "is_bbox_large_enough",
    "load_image",
    "create_composite",
    "create_motion_comparison_composite",
    "compute_roi_motion_score",
    "create_multi_roi_gallery",
]
