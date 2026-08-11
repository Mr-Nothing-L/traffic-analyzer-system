"""
Scene understanding models for the traffic analyzer framework.

[文件说明]
作用:定义场景理解模型 SceneInfo(道路数量/天气/车流密度及行人、非机动车、抛洒物存在标志)。
上游:models/schemas.py、models/context.py、models/report.py 引用;由 core/pipeline_steps.py 场景理解步骤填充,被 orchestrator/analysis_orchestrator.py、core/report_generator.py 消费。
下游:pydantic。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class SceneInfo(BaseModel):
    """Global scene understanding result from VLM."""
    road_count: int = 0
    weather: str = "unknown"
    lighting: str = "unknown"
    traffic_density: str = "unknown"
    total_vehicles_estimate: int = 0
    scene_description: str = ""
    confidence: float = 0.0
