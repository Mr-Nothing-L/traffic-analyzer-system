"""Result reading and evidence/SFT editing endpoints.

Reads ``report.md`` / ``<stem>.json`` / ``<stem>_evidence.json`` from
``<workspace>/analysis/<stem>/`` and serves the copied composite images.
The evidence PUT endpoint re-validates the payload against schema v1 and
only allows edits to the user-editable coordinate/label fields:

- ``events[*].calibration.emergency_polygon_rel``
- ``events[*].calibration.chevron_polygon_rel``
- ``events[*].evidence_regions[*].box_rel``
- ``events[*].evidence_regions[*].label``

The SFT PUT endpoint only allows ``description`` / ``action`` /
``event_attributes`` / ``attr_mentions`` edits; attribute values are validated
against the closed enums in ``config/event_options.yaml``, and each declared
mention string must appear in the corresponding event's think-section text.
Any other difference versus the on-disk version is rejected with 422.
For the two optional fields the PUT distinguishes "not submitted" from
"explicit null" (via ``exclude_unset``): an omitted field preserves the
on-disk value as-is (legacy samples stay without the key, annotated samples
keep their annotations); an explicit ``null`` deletes the key.

[文件说明]
作用:结果读取与证据/SFT 编辑接口。GET 读取 <workspace>/analysis/<stem>/ 下的
report.md、<stem>.json(SFT 样本)、<stem>_evidence.json 及图片;evidence PUT 仅允许修改
标定多边形与证据框/标签,SFT PUT 仅允许 description/action/event_attributes/attr_mentions
(event_attributes 按 event_options.yaml 封闭枚举校验;attr_mentions 的每个提及串
必须出现在对应事件的 think 段落正文中),其余字段与磁盘版本比对
不一致即 422;event_attributes/attr_mentions 区分「未提交」(保留磁盘原值,
旧格式样本不新增字段)与「显式 null」(删除该键);写入采用 tmp+os.replace
原子写并按 stem 加锁(409 在跑 infer 检查在锁内复查,消除 TOCTOU)。
上游:web/app.py(挂载路由);web/static 前端(结果查看与标注编辑)。
下游:web/workspace.py(路径与 stem 校验)、web/jobs.py(在跑任务检查)、
config/event_categories.yaml 与 config/event_options.yaml
(/api/config/events 供 SFT 编辑器按事件分框与渲染结构化选项)。
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

from traffic_analyzer.web import workspace as workspace_mod

router = APIRouter()

# Repository root (traffic_analyzer/web/evidence_api.py -> parents[2]).
_EVENT_CATEGORIES_YAML = (
    Path(__file__).resolve().parents[2]
    / "traffic_analyzer"
    / "config"
    / "event_categories.yaml"
)
_EVENT_OPTIONS_YAML = (
    Path(__file__).resolve().parents[2]
    / "traffic_analyzer"
    / "config"
    / "event_options.yaml"
)


def _yaml_mtime_ns(path: Path) -> int:
    """文件 mtime(纳秒);缺失时返回 -1(后续 read_text 仍按原样抛错)。"""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


@lru_cache(maxsize=8)
def _event_options_index_cached(
    path: str, mtime_ns: int
) -> Dict[int, List[Dict[str, Any]]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    index: Dict[int, List[Dict[str, Any]]] = {}
    for ev in data.get("event_options") or []:
        groups = [
            {
                "key": str(g["key"]),
                "label": str(g.get("label") or g["key"]),
                "options": [str(o) for o in g.get("options") or []],
                "required": bool(g.get("required", False)),
                "multi": bool(g.get("multi", False)),
            }
            for g in ev.get("groups") or []
            if "key" in g
        ]
        if "event_id" in ev:
            index[int(ev["event_id"])] = groups
    return index


def _event_options_index() -> Dict[int, List[Dict[str, Any]]]:
    """event_options.yaml 的封闭枚举定义:{event_id: [属性组, ...]}(保持声明顺序)。

    按 (路径, mtime) 缓存:运行中编辑 yaml 后下一次读取自动失效,无需重启。
    """
    return _event_options_index_cached(
        str(_EVENT_OPTIONS_YAML), _yaml_mtime_ns(_EVENT_OPTIONS_YAML)
    )


@lru_cache(maxsize=8)
def _event_name_index_cached(path: str, mtime_ns: int) -> Dict[str, int]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {
        str(cat["name_zh"]): int(cat["event_id"])
        for cat in data.get("event_categories") or []
        if "event_id" in cat and "name_zh" in cat
    }


def _event_name_index() -> Dict[str, int]:
    """事件中文名 → event_id(用于在 description 的 think 段落中定位事件文本)。

    与 _event_options_index 同口径:按 (路径, mtime) 缓存,yaml 变更自动失效。
    """
    return _event_name_index_cached(
        str(_EVENT_CATEGORIES_YAML), _yaml_mtime_ns(_EVENT_CATEGORIES_YAML)
    )


def _think_sections(description: str) -> Dict[int, str]:
    """description 的 <think> 按空行分段,「事件名：」前缀定位各事件段落正文。

    与前端 js/sft.js 的 parseSftDescription 同一口径:重复段落取首段,
    匹配不到事件名的段落忽略。
    """
    sections: Dict[int, str] = {}
    m = re.search(r"<think>([\s\S]*?)</think>", description or "")
    if not m:
        return sections
    names = _event_name_index()
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
    index = _event_options_index()
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
    index = _event_options_index()
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

    @field_validator("attr_mentions")
    @classmethod
    def _check_mentions(
        cls, value: Optional[Dict[str, Dict[str, Any]]], info: ValidationInfo
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        if value is None:
            return value
        # description 在字段顺序上先于 attr_mentions 完成校验,可直接取用
        return _check_attr_mentions(value, str(info.data.get("description") or ""))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON via a same-dir ``.tmp`` file + ``os.replace``.

    A mid-write crash can then only lose the tmp file, never truncate the
    real one (which GETs would otherwise silently show as "无标注").
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


# Per-stem locks closing the concurrent read-compare-write PUT window.
_put_locks: "defaultdict[str, threading.Lock]" = defaultdict(threading.Lock)


def _reject_active_infer(request: Request, stem: str) -> None:
    """409 when a queued/running infer job targets ``stem``.

    The job would overwrite the very files the PUT is editing (PUT-vs-infer
    race), so the edit must wait until the job finishes.
    """
    jobs = getattr(request.app.state, "jobs", None)
    if jobs is None:
        return
    for job in jobs.list_jobs():
        if (
            job.get("kind") == "infer"
            and job.get("stem") == stem
            and job.get("status") in ("queued", "running")
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Inference job #{job.get('id')} for '{stem}' is "
                    f"{job.get('status')}; retry after it finishes"
                ),
            )


_MASK = "__editable__"


def _strip_editable(payload: Dict[str, Any]) -> Dict[str, Any]:
    """JSON-normalized copy with the user-editable fields masked out.

    Two payloads that differ ONLY in editable fields strip to equal dicts.
    """
    copy = json.loads(json.dumps(payload, ensure_ascii=False))
    for event in copy.get("events", []):
        calibration = event.get("calibration")
        if isinstance(calibration, dict):
            calibration["emergency_polygon_rel"] = _MASK
            calibration["chevron_polygon_rel"] = _MASK
        for region in event.get("evidence_regions") or []:
            if isinstance(region, dict):
                region["box_rel"] = _MASK
                region["label"] = _MASK
    return copy


def _strip_sft_editable(payload: Dict[str, Any]) -> Dict[str, Any]:
    """与 ``_strip_editable`` 同理:仅 description / action / event_attributes / attr_mentions 允许不同。"""
    copy = json.loads(json.dumps(payload, ensure_ascii=False))
    copy["description"] = _MASK
    copy["action"] = _MASK
    copy["event_attributes"] = _MASK
    copy["attr_mentions"] = _MASK
    return copy


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/results/{stem}")
def get_results(stem: str, request: Request) -> Dict[str, Any]:
    workspace = workspace_mod.require_workspace(request)
    workspace_mod.validate_stem(stem)
    out_dir = workspace_mod.analysis_dir(workspace, stem)

    report_md: Optional[str] = None
    try:
        report_md = (out_dir / "report.md").read_text(encoding="utf-8")
    except OSError:
        pass

    return {
        "report_md": report_md,
        "sft_label": _read_json(out_dir / f"{stem}.json"),
        "evidence": _read_json(out_dir / f"{stem}_evidence.json"),
    }


@router.get("/api/results/{stem}/images/{name}")
def get_result_image(stem: str, name: str, request: Request) -> FileResponse:
    workspace = workspace_mod.require_workspace(request)
    workspace_mod.validate_stem(stem)
    if not name or name in (".", "..") or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=404, detail="Image not found")
    # 仅服务 images/ 下的文件;tmp_img 等子树一律走 /file?path= 精确路径,
    # 不做 basename 回退搜索,避免索引到工作区里的历史残留文件。
    images_dir = (workspace_mod.analysis_dir(workspace, stem) / "images").resolve()
    candidate = (images_dir / name).resolve()
    if candidate.parent != images_dir or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(candidate)


@router.get("/api/results/{stem}/file")
def get_result_file(stem: str, request: Request, path: str = Query(...)) -> FileResponse:
    """Serve any file under ``analysis/<stem>/`` by its relative path.

    report.md references enhancement images with paths relative to its own
    directory (e.g. ``tmp_img/<stem>/.../02_masks_overlay.jpg``); evidence.json
    references ``images/<name>.jpg``. Both are served here with the path
    strictly confined to the analysis directory.
    """
    workspace = workspace_mod.require_workspace(request)
    workspace_mod.validate_stem(stem)
    parts = Path(path).parts
    if not path or path.startswith("/") or "\\" in path or ".." in parts:
        raise HTTPException(status_code=404, detail="File not found")
    analysis_dir = workspace_mod.analysis_dir(workspace, stem).resolve()
    candidate = (analysis_dir / path).resolve()
    if analysis_dir not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(candidate)


@router.put("/api/results/{stem}/evidence")
def put_evidence(stem: str, body: Evidence, request: Request) -> Dict[str, Any]:
    workspace = workspace_mod.require_workspace(request)
    workspace_mod.validate_stem(stem)
    evidence_path = workspace_mod.analysis_dir(workspace, stem) / f"{stem}_evidence.json"
    with _put_locks[stem]:
        # 锁内复查 409:检查与写文件之间不能再插入新的 infer 任务(TOCTOU)。
        _reject_active_infer(request, stem)
        disk = _read_json(evidence_path)
        if disk is None:
            raise HTTPException(status_code=404, detail="Evidence file not found")

        new_payload = body.model_dump()
        if not isinstance(disk, dict) or _strip_editable(disk) != _strip_editable(
            new_payload
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Only calibration.emergency_polygon_rel, "
                    "calibration.chevron_polygon_rel and "
                    "evidence_regions[*].box_rel/.label may be modified"
                ),
            )

        _atomic_write_json(evidence_path, new_payload)
    return new_payload


@router.get("/api/config/events")
def get_config_events() -> List[Dict[str, Any]]:
    """事件类别配置(供 SFT 编辑器按事件分框),按 event_id 排序。

    每个事件附带 ``options``:event_options.yaml 中定义的结构化属性组
    (封闭枚举,只读选项集);未定义的事件返回空列表。
    """
    try:
        data = (
            yaml.safe_load(_EVENT_CATEGORIES_YAML.read_text(encoding="utf-8")) or {}
        )
        options_index = _event_options_index()
    except (OSError, yaml.YAMLError) as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to load event categories config: {exc}"
        )
    events = [
        {
            "event_id": int(cat["event_id"]),
            "name_zh": str(cat.get("name_zh") or ""),
            "is_active": bool(cat.get("is_active", True)),
            "options": options_index.get(int(cat["event_id"]), []),
        }
        for cat in data.get("event_categories") or []
        if "event_id" in cat and "name_zh" in cat
    ]
    events.sort(key=lambda e: e["event_id"])
    if not events:
        raise HTTPException(
            status_code=500, detail="Event categories config has no valid entries"
        )
    return events


@router.put("/api/results/{stem}/sft")
def put_sft(stem: str, body: SftSample, request: Request) -> Dict[str, Any]:
    workspace = workspace_mod.require_workspace(request)
    workspace_mod.validate_stem(stem)
    sft_path = workspace_mod.analysis_dir(workspace, stem) / f"{stem}.json"
    with _put_locks[stem]:
        # 锁内复查 409:检查与写文件之间不能再插入新的 infer 任务(TOCTOU)。
        _reject_active_infer(request, stem)
        disk = _read_json(sft_path)
        if disk is None:
            raise HTTPException(status_code=404, detail="SFT file not found")

        # exclude_unset 区分「字段未提交」与「显式 null」:
        # - 未提交:保留磁盘现状(旧格式样本不新增字段;已有结构化标注不丢失);
        # - 显式 null:删除该键(显式清除语义,经正常写路径落盘)。
        new_payload = body.model_dump(exclude_unset=True)
        for field in ("event_attributes", "attr_mentions"):
            if field not in new_payload:
                if isinstance(disk, dict) and field in disk:
                    new_payload[field] = disk[field]
            elif new_payload[field] is None:
                del new_payload[field]
        if not isinstance(disk, dict) or _strip_sft_editable(disk) != _strip_sft_editable(
            new_payload
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Only description, action, event_attributes and "
                    "attr_mentions may be modified"
                ),
            )

        _atomic_write_json(sft_path, new_payload)
    return new_payload
