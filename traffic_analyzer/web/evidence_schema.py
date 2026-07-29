"""Pydantic models and validators for the evidence / SFT PUT payloads.

[文件说明]
作用:Evidence schema v1(坐标归一化到 [0, 1],extra=forbid)与 SftSample 模型
(自 evidence_api.py 抽出,不含端点与文件 IO)。SFT 校验规则:action 取标注文档
v4.5 的封闭编号集;event_attributes 按 event_options.yaml 封闭枚举严格校验;
attr_mentions 的每个提及串必须出现在对应事件 description 的 think 段落正文中
(model_validator(mode='after'),不再依赖字段声明顺序)。
上游:web/evidence_api.py(PUT 端点的请求体模型)。
下游:web/event_config.py(封闭枚举与事件名索引)。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from traffic_analyzer.web import event_config


def _think_sections(description: str) -> Dict[int, str]:
    """description 的 <think> 按空行分段,「事件名：」前缀定位各事件段落正文。

    与前端 js/sft.js 的 parseSftDescription 同一口径:重复段落取首段,
    匹配不到事件名的段落忽略。
    """
    sections: Dict[int, str] = {}
    m = re.search(r"<think>([\s\S]*?)</think>", description or "")
    if not m:
        return sections
    names = event_config.event_name_index()
    for para in re.split(r"\n\s*\n", m.group(1).strip()):
        p = para.strip()
        pm = re.match(r"^([^：\n]{1,30})：", p)
        if not pm:
            continue
        ev_id = names.get(pm.group(1))
        if ev_id is not None and ev_id not in sections:
            sections[ev_id] = p[pm.end() :].strip()
    return sections


# ---------------------------------------------------------------------------
# Evidence schema v1 (coordinates normalized to [0, 1])
# ---------------------------------------------------------------------------


def _check_normalized(values: List[float], field_name: str) -> None:
    if not all(0.0 <= v <= 1.0 for v in values):
        raise ValueError(f"{field_name} coordinates must be normalized to [0, 1]")


class Calibration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_index: Optional[int] = None
    emergency_polygon_rel: Optional[List[List[float]]] = None
    chevron_polygon_rel: Optional[List[List[float]]] = None

    @field_validator("emergency_polygon_rel", "chevron_polygon_rel")
    @classmethod
    def _check_polygon(cls, value: Optional[List[List[float]]]) -> Optional[List[List[float]]]:
        if value is not None:
            for point in value:
                if len(point) != 2:
                    raise ValueError("polygon points must be [x, y]")
                _check_normalized(point, "polygon")
        return value


class EvidenceRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_index: Optional[int] = None
    box_rel: List[float]
    label: str
    image: Optional[str] = None

    @field_validator("box_rel")
    @classmethod
    def _check_box(cls, value: List[float]) -> List[float]:
        if len(value) != 4:
            raise ValueError("box_rel must be [x1, y1, x2, y2]")
        _check_normalized(value, "box_rel")
        return value


class EventEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: int
    name: str
    detected: bool
    calibration: Calibration
    evidence_regions: List[EvidenceRegion] = []
    gallery_images: List[str] = []


class VideoInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_name: Optional[str] = None
    duration_sec: Optional[float] = None
    fps: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    video: VideoInfo
    events: List[EventEntry] = []

    @field_validator("schema_version")
    @classmethod
    def _check_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"unsupported schema_version: {value}")
        return value


# ---------------------------------------------------------------------------
# SFT sample (only description / action / event_attributes / attr_mentions are user-editable)
# ---------------------------------------------------------------------------

# 标注文档 v4.5 的合法 action 编号(action 9 = 正常占位,不出现)。
_ALLOWED_ACTION_IDS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 10, 11})


def _check_event_attributes(value: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """event_attributes 严格枚举校验:event_id/属性键必须已定义,值必须在封闭选项内。"""
    index = event_config.event_options_index()
    for ev_key, attrs in value.items():
        try:
            ev_id = int(ev_key)
        except (TypeError, ValueError):
            raise ValueError(f"event_attributes: invalid event id {ev_key!r}")
        groups = {g["key"]: g for g in index.get(ev_id) or []}
        if not groups:
            raise ValueError(f"event_attributes: no options defined for event {ev_key!r}")
        if not isinstance(attrs, dict):
            raise ValueError(f"event_attributes[{ev_key!r}] must be an object")
        for key, val in attrs.items():
            group = groups.get(key)
            if group is None:
                raise ValueError(
                    f"event_attributes[{ev_key!r}]: unknown attribute {key!r}"
                )
            allowed = group["options"]
            if group["multi"]:
                if not isinstance(val, list) or not all(
                    isinstance(v, str) and v in allowed for v in val
                ):
                    raise ValueError(
                        f"event_attributes[{ev_key!r}][{key!r}] must be a list "
                        f"within {allowed}"
                    )
            elif val is not None and (not isinstance(val, str) or val not in allowed):
                # 契约允许 null(VLM 看不清时输出 null);非 null 必须命中枚举。
                raise ValueError(
                    f"event_attributes[{ev_key!r}][{key!r}] must be one of {allowed}"
                )
    return value


def _check_attr_mentions(
    value: Dict[str, Dict[str, Any]], description: str
) -> Dict[str, Dict[str, Any]]:
    """attr_mentions 校验:event_id/属性键必须已定义;单选组值为字符串数组(可空),
    多选组值为字符串数组(旧扁平格式)或「选项名 → 字符串数组」嵌套对象(选项名
    必须在该组 options 内);每个提及串必须出现在对应事件的 description think
    段落正文中(与 _strip_editable 同哲学的 best-effort 一致性检查,找不到即拒绝)。"""
    index = event_config.event_options_index()
    sections: Optional[Dict[int, str]] = None  # 按需解析
    for ev_key, groups_map in value.items():
        try:
            ev_id = int(ev_key)
        except (TypeError, ValueError):
            raise ValueError(f"attr_mentions: invalid event id {ev_key!r}")
        groups = {g["key"]: g for g in index.get(ev_id) or []}
        if not groups:
            raise ValueError(f"attr_mentions: no options defined for event {ev_key!r}")
        if not isinstance(groups_map, dict):
            raise ValueError(f"attr_mentions[{ev_key!r}] must be an object")
        # (属性键, 提及串) 统一收集,随后按事件 think 段落做子串校验
        flat: List[Any] = []
        for key, mentions in groups_map.items():
            group = groups.get(key)
            if group is None:
                raise ValueError(
                    f"attr_mentions[{ev_key!r}]: unknown attribute {key!r}"
                )
            if isinstance(mentions, dict):
                # 新格式多选组:嵌套 per-option 绑定(选项名 → 字符串数组)
                if not group["multi"]:
                    raise ValueError(
                        f"attr_mentions[{ev_key!r}][{key!r}] must be an array of strings"
                    )
                for opt, strs in mentions.items():
                    if opt not in group["options"]:
                        raise ValueError(
                            f"attr_mentions[{ev_key!r}][{key!r}]: option {opt!r} "
                            f"not in group options"
                        )
                    if not isinstance(strs, list) or not all(
                        isinstance(s, str) for s in strs
                    ):
                        raise ValueError(
                            f"attr_mentions[{ev_key!r}][{key!r}][{opt!r}] must be "
                            f"an array of strings"
                        )
                    flat.extend((key, s) for s in strs)
            elif isinstance(mentions, list) and all(
                isinstance(s, str) for s in mentions
            ):
                flat.extend((key, s) for s in mentions)
            else:
                raise ValueError(
                    f"attr_mentions[{ev_key!r}][{key!r}] must be an array of strings"
                )
        if flat:
            if sections is None:
                sections = _think_sections(description)
            text = sections.get(ev_id, "")
            for key, s in flat:
                if s not in text:
                    raise ValueError(
                        f"attr_mentions[{ev_key!r}][{key!r}]: mention {s!r} "
                        f"not found in event {ev_id} description think-section"
                    )
    return value


class SftSample(BaseModel):
    """完整 SFT 样本;chunk/idx/时间戳/chunk_name 与磁盘版本不一致时拒绝。"""

    model_config = ConfigDict(extra="forbid")

    chunk: Any
    idx: Any
    action: List[int]
    description: str
    start_timestamp: Any
    end_timestamp: Any
    chunk_name: Any
    event_attributes: Optional[Dict[str, Dict[str, Any]]] = None
    attr_mentions: Optional[Dict[str, Dict[str, Any]]] = None

    @field_validator("action")
    @classmethod
    def _check_action_ids(cls, value: List[int]) -> List[int]:
        if not all(a in _ALLOWED_ACTION_IDS for a in value):
            raise ValueError(
                f"action ids must be a subset of {sorted(_ALLOWED_ACTION_IDS)}"
            )
        return value

    @field_validator("event_attributes")
    @classmethod
    def _check_attrs(
        cls, value: Optional[Dict[str, Dict[str, Any]]]
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        if value is None:
            return value
        return _check_event_attributes(value)

    @model_validator(mode="after")
    def _check_mentions(self) -> "SftSample":
        # after 模式直接读 self.description,不再依赖字段声明顺序。
        if self.attr_mentions is not None:
            _check_attr_mentions(self.attr_mentions, self.description)
        return self
