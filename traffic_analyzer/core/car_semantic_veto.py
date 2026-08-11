"""Car-semantic veto helpers for the far-distance enhancement flow.

Extracted verbatim from :class:`FarEnhancementDetector`
(:mod:`traffic_analyzer.core.expert_agent_far_enhancement`).  These functions
decide whether a positive detection should be suppressed because the classified
target is actually a car / four-wheel vehicle rather than the event category
(pedestrian, non-motor vehicle, ...).  No logic was changed during the move;
only the ``self.`` prefix was dropped and the regex constants were hoisted to
module level.

公共 API
--------
- :func:`is_explicitly_car_reasoning` / :func:`is_explicitly_car_reasoning_for_pedestrian`
  / :func:`is_explicitly_car_reasoning_for_non_motor` -- regex helpers that inspect
  free-text reasoning.
- :func:`select_car_veto_check` -- returns the appropriate helper for an event id.
- :func:`should_veto_as_car` -- structured-field-first veto decision.
- :func:`apply_structured_veto_to_candidate` -- force ``detected=False`` on a candidate.
- :func:`is_no_structure_reasoning` -- detect "no identifiable vehicle structure" reasons.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict

from traffic_analyzer.models.schemas import EventCandidate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Compiled regex constants (moved from FarEnhancementDetector class level).
# ---------------------------------------------------------------------------

_PEDESTRIAN_CAR_RE = re.compile(
    r"(是|为|疑似|像是|类似|看作|判定为|判断为|确认为)(?:一辆|一个|一尊)?(?:白色|黑色|红色|银色|灰色|蓝色|黄色|深色|浅色)?(?:小|大)?(?:轿车|suv|越野车|货车|卡车|厢式货车|面包车|客车|大巴|中巴|公交车|四轮车|四轮机动车|机动车|汽车)",
    re.IGNORECASE,
)
_PEDESTRIAN_CAR_OCCUPANT_RE = re.compile(
    r"(车内人员|车里的人|坐在车内|在车内|驾驶座|副驾驶|车内)",
    re.IGNORECASE,
)
_PEDESTRIAN_NOT_PEDESTRIAN_BUT_CAR_RE = re.compile(
    r"(不是行人|非行人|不是人|不是高速公路行人|不是滞留人员).*?(轿车|suv|货车|卡车|面包车|客车|四轮车|四轮机动车|汽车|机动车)",
    re.IGNORECASE,
)
_NON_MOTOR_POSITIVE_ANCHORS = [
    "摩托车",
    "电动车",
    "非机动车",
    "两轮车",
    "三轮车",
    "两轮",
    "三轮",
    "骑乘者",
    "骑乘姿态",
    "骑手",
    "头盔",
    "车把",
    "脚踏车",
    "自行车",
    "骑乘",
]
_NON_MOTOR_CAR_NEGATION_RE = re.compile(
    r"(?:"
    r"被.*?轿车.*?取代|被.*?汽车.*?取代|"
    r"被.*?轿车.*?遮挡|被.*?汽车.*?遮挡|"
    r"而非轿车|而非汽车|而非机动车|"
    r"而不是轿车|而不是汽车|而不是机动车|"
    r"不是轿车|不是汽车|不是机动车|不是suv|不是面包车|不是货车|不是卡车|"
    r"不是一辆.*?轿车|不是一辆.*?汽车|不是一辆.*?机动车|"
    r"并未被.*?汽车|并未被.*?轿车|"
    r"没有被.*?汽车|没有被.*?轿车|"
    r"并非汽车|并非轿车|并非机动车|"
    r"没有.*?汽车|没有.*?轿车|没有.*?机动车"
    r")",
    re.IGNORECASE,
)
_NON_MOTOR_EXPLICIT_CAR_RE = re.compile(
    r"(?:"
    r"(?:红框内|框内|目标|该目标|主体|对象|这个物体|ROI内)"
    r".*?"
    r"(?:是|为|疑似|像是|类似|看作|判定为|判断为|确认为|属于|归类于)"
    r"(?:一辆|一台|一个|几辆)?"
    r"(?:白色|黑色|红色|银色|灰色|蓝色|黄色|深色|浅色|亮色|暗色)?"
    r"(?:小|大|小型|大型|中型|微型)?"
    r"(?:轿车|suv|越野车|货车|卡车|厢式货车|面包车|客车|大巴|中巴|公交车|四轮车|四轮机动车|机动车|汽车)"
    r")",
    re.IGNORECASE,
)
_NO_STRUCTURE_RE = re.compile(
    r"(?:无|没有)(?:清晰|明确|可辨识|具体|明显)?(?:的)?(?:车辆结构|非机动车结构|摩托车结构|结构|轮廓|特征|证据|"
    r"车轮|车把|车灯|车牌|车架|车身|车座|骑乘者|骑手|头盔|车辆|非机动车|摩托车|两轮|三轮)|"
    r"轮廓不清|无明显轮廓|无明显车辆结构|无明显结构|缺乏(?:清晰|明确|可辨识|具体|明显)?(?:的)?(?:结构|轮廓|特征|证据)|"
    r"无法(?:辨识|识别|辨认|看清|看出)(?:的)?(?:车辆结构|非机动车结构|摩托车结构|结构|轮廓|特征|"
    r"车轮|车把|车灯|车牌|车架|车身|车座|骑乘者|骑手|头盔|车辆|非机动车|摩托车|两轮|三轮)|"
    r"(?:仅|只|仅为)(?:是|为|能看到|是一个|一个|一团|一块)(?:暗斑|黑块|阴影|模糊|色块|点|亮点|反光点|轮廓|黑影)|"
    r"(?:暗斑|黑块|阴影|模糊色块|一团模糊|一个黑点|仅一个亮点|仅一个反光点|模糊轮廓|模糊目标|色块|反光点)",
    re.IGNORECASE,
)


def is_explicitly_car_reasoning(reason: str) -> bool:
    """判断 reason 文本是否明确说明目标是汽车/四轮车。"""
    if not reason:
        return False
    lower = reason.lower()
    car_keywords = [
        "汽车",
        "轿车",
        "suv",
        "货车",
        "客车",
        "面包车",
        "四轮车",
        "四轮机动车",
        "已驶离",
        "vehicle has left",
    ]
    return any(keyword in lower for keyword in car_keywords)


def is_explicitly_car_reasoning_for_pedestrian(reason: str) -> bool:
    """判断 reason 文本是否明确说明红框内目标是汽车/四轮车或车内人员。"""
    if not reason:
        return False
    return bool(
        _PEDESTRIAN_CAR_RE.search(reason)
        or _PEDESTRIAN_CAR_OCCUPANT_RE.search(reason)
        or _PEDESTRIAN_NOT_PEDESTRIAN_BUT_CAR_RE.search(reason)
    )


def is_explicitly_car_reasoning_for_non_motor(reason: str) -> bool:
    """判断 reason 是否明确说红框内目标是汽车/四轮车（非机动车事件专用）。

    对 event_id=5 做更精细的语义判断：
    - 若出现否定/对比/替代语境（“而非汽车”“被轿车取代”），返回 False；
    - 若明确断言“红框内是汽车/轿车/...”，返回 True；
    - 若出现非机动车/摩托车强锚定词且无明确汽车断言，返回 False。
    """
    if not reason:
        return False
    lower = reason.lower()

    # 1. Negation / comparison / replacement contexts mentioning cars.
    if _NON_MOTOR_CAR_NEGATION_RE.search(reason):
        return False

    # 2. Explicit assertion that the target is a car.
    explicit_car_match = _NON_MOTOR_EXPLICIT_CAR_RE.search(reason)
    if explicit_car_match:
        return True

    # 3. Strong positive anchors for non-motor vehicles: never veto.
    if any(anchor in lower for anchor in _NON_MOTOR_POSITIVE_ANCHORS):
        return False

    return False


def select_car_veto_check(event_id: int) -> Callable[[str], bool]:
    """Return the appropriate car-semantic veto function for an event."""
    if event_id == 4:
        return is_explicitly_car_reasoning_for_pedestrian
    if event_id == 5:
        return is_explicitly_car_reasoning_for_non_motor
    return is_explicitly_car_reasoning


def should_veto_as_car(
    parsed: Dict[str, Any],
    text: str,
    event_id: int,
) -> bool:
    """Decide whether to veto the detection because the target is a car.

    Primary path: structured ``is_target_explicitly_four_wheel_vehicle``.
    Fallback path: regex on the free-text reason when the field is missing.
    """
    structured = parsed.get("is_target_explicitly_four_wheel_vehicle")
    if structured is True:
        return True
    if structured is False:
        return False
    # Structured field absent/null: fall back to regex reasoning checks.
    car_check = select_car_veto_check(event_id)
    return bool(car_check(text))


def apply_structured_veto_to_candidate(
    candidate: EventCandidate,
) -> EventCandidate:
    """Force candidate.detected=False when the classifier says it is a car."""
    parsed = candidate.raw_vlm_response
    if not isinstance(parsed, dict):
        return candidate
    is_car = parsed.get("is_target_explicitly_four_wheel_vehicle")
    if is_car is True and candidate.detected:
        logger.info(
            "[expert_agent:_apply_structured_veto_to_candidate] CAR_OVERRIDDEN | "
            "event_id=%d target_type=%s",
            candidate.event_id,
            parsed.get("target_type", ""),
        )
        candidate.detected = False
    candidate.is_target_explicitly_four_wheel_vehicle = is_car
    candidate.target_type = str(parsed.get("target_type", ""))
    return candidate


def is_no_structure_reasoning(reason: str) -> bool:
    """判断 reason 文本是否说明框内没有可辨识的车辆结构证据。"""
    return bool(reason) and bool(_NO_STRUCTURE_RE.search(reason))
