"""Far-distance object enhancement detector (thin dispatcher).

This module was split out of :mod:`traffic_analyzer.core.expert_agent`.
After Task F3, it is a thin dispatcher that delegates to three strategy
modules based on ``event_id`` and ``frame_selection``:

- ``far_emergency_lane`` — event_id=2 (emergency lane occupancy)
- ``far_gallery`` — ``frame_selection == "middle"`` (construction/gallery)
- ``far_per_frame`` — generic per-frame ROI detection (everything else)

Shared helpers and JSON schemas live in ``far_shared``.

[文件说明]
作用:远距离目标增强检测器(FarEnhancementDetector),从 expert_agent.py
拆出。对开启 far_object_enhancement 的事件模板执行 ROI 驱动的增强流程。
上游:core/expert_agent.py(ExpertAgent 通过 _far_detector 属性委托调用)。
下游:far_emergency_lane / far_gallery / far_per_frame / far_shared 策略模块。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from traffic_analyzer.core.config_manager import ConfigManager
from traffic_analyzer.core.vlm_engine import VLMInferenceEngine
from traffic_analyzer.models.schemas import (
    AnalysisContext,
    EventCandidate,
    EventCategory,
    PromptTemplate,
)
from traffic_analyzer.utils.progress import get_reporter as _get_progress_reporter

# Strategy modules
from traffic_analyzer.core.far_shared import _EXPERT_RESPONSE_SCHEMA  # noqa: F401 (re-export for backward compat)
from traffic_analyzer.core.far_emergency_lane import detect_emergency_lane_occupancy
from traffic_analyzer.core.far_gallery import detect_gallery
from traffic_analyzer.core.far_per_frame import detect_per_frame

logger = logging.getLogger(__name__)

# Directory where far-distance object composite images are saved.
# Kept relative to the project root so it works across local dev, CI and Docker.
# Artifacts are grouped into a per-video subdirectory named after the video
# stem (see ``_detect_with_far_enhancement``).
_FAR_ENHANCEMENT_OUTPUT_DIR = Path("./output/tmp_img")


class FarEnhancementDetector:
    """Holder for far-distance enhancement dependencies.

    After Task F3 this class is a thin dispatcher: ``_detect_with_far_enhancement``
    delegates to one of three strategy modules based on ``event_id`` and
    ``frame_selection``.
    """

    def __init__(
        self,
        category: EventCategory,
        vlm_engine: VLMInferenceEngine,
        config_manager: ConfigManager,
    ) -> None:
        self.category = category
        self.vlm_engine = vlm_engine
        self.config_manager = config_manager

    def _detect_with_far_enhancement(
        self,
        context: AnalysisContext,
        images: List[Any],
        template: PromptTemplate,
        context_vars: Dict[str, Any],
        default_output_dir: Path = _FAR_ENHANCEMENT_OUTPUT_DIR,
    ) -> Optional[EventCandidate]:
        """Dispatch to the appropriate far-enhancement strategy.

        Branch selection:
        - event_id == 2 → emergency-lane occupancy strategy
        - frame_selection == "middle" → gallery (construction) strategy
        - otherwise → generic per-frame ROI strategy
        """
        if context.video_meta is None:
            logger.warning(
                "[expert_agent:_detect_with_far_enhancement] NO_VIDEO_META | event_id=%d",
                self.category.event_id,
            )
            return None

        logger.info(
            "[expert_agent:_detect_with_far_enhancement] START | event_id=%d event_name=%s frames=%d",
            self.category.event_id,
            self.category.name_zh,
            len(images),
        )
        _get_progress_reporter().phase("evidence")

        far_cfg = template.far_object_enhancement
        roi_template_id = far_cfg.roi_template_id

        try:
            roi_template = self.config_manager.get_prompt_template(roi_template_id)
        except (KeyError, RuntimeError) as exc:
            logger.warning(
                "[expert_agent:_detect_with_far_enhancement] ROI_TEMPLATE_ERROR | event_id=%d | %s",
                self.category.event_id,
                exc,
            )
            return None

        video_stem = Path(context.video_meta.file_path).stem

        # When the orchestrator knows where the report will be written, place
        # composites next to the report and reference them with a relative path
        # so markdown viewers can resolve the image. Otherwise fall back to the
        # project-root default for backward compatibility. Artifacts are always
        # grouped into a per-video subdirectory named after the video stem.
        report_output_dir = getattr(context, "output_dir", None)
        if report_output_dir:
            output_dir = Path(report_output_dir) / "tmp_img" / video_stem
            image_ref_prefix = f"tmp_img/{video_stem}"
        else:
            output_dir = default_output_dir / video_stem
            image_ref_prefix = str(default_output_dir / video_stem)
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.error(
                "[expert_agent:_detect_with_far_enhancement] OUTPUT_DIR_ERROR | event_id=%d path=%s | %s",
                self.category.event_id,
                output_dir,
                exc,
                exc_info=True,
            )
            return None

        # ------------------------------------------------------------------
        # Emergency lane occupancy branch (event_id=2).
        # Must be checked before the generic "middle" gallery branch.
        # ------------------------------------------------------------------
        if self.category.event_id == 2:
            return detect_emergency_lane_occupancy(
                category=self.category,
                vlm_engine=self.vlm_engine,
                config_manager=self.config_manager,
                context=context,
                images=images,
                template=template,
                context_vars=context_vars,
                roi_template=roi_template,
                output_dir=output_dir,
                image_ref_prefix=image_ref_prefix,
                video_stem=video_stem,
                far_cfg=far_cfg,
            )

        # ------------------------------------------------------------------
        # Multi-ROI gallery branch (e.g. event_id=7 road construction).
        # Uses a single middle frame and a gallery of evidence ROIs.
        # ------------------------------------------------------------------
        if far_cfg.frame_selection == "middle":
            return detect_gallery(
                category=self.category,
                vlm_engine=self.vlm_engine,
                config_manager=self.config_manager,
                context=context,
                images=images,
                template=template,
                context_vars=context_vars,
                roi_template=roi_template,
                output_dir=output_dir,
                image_ref_prefix=image_ref_prefix,
                video_stem=video_stem,
                far_cfg=far_cfg,
            )

        # ------------------------------------------------------------------
        # Generic per-frame ROI detection.
        # ------------------------------------------------------------------
        return detect_per_frame(
            category=self.category,
            vlm_engine=self.vlm_engine,
            config_manager=self.config_manager,
            context=context,
            images=images,
            template=template,
            context_vars=context_vars,
            roi_template=roi_template,
            output_dir=output_dir,
            image_ref_prefix=image_ref_prefix,
            video_stem=video_stem,
            far_cfg=far_cfg,
        )
