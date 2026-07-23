"""Result reading and evidence/SFT editing endpoints.

Reads ``report.md`` / ``<stem>.json`` / ``<stem>_evidence.json`` from
``<workspace>/analysis/<stem>/`` and serves the copied composite images.
The evidence PUT endpoint re-validates the payload against schema v1 and
only allows edits to the user-editable coordinate/label fields:

- ``events[*].calibration.emergency_polygon_rel``
- ``events[*].calibration.chevron_polygon_rel``
- ``events[*].evidence_regions[*].box_rel``
- ``events[*].evidence_regions[*].label``

The SFT PUT endpoint only allows ``description`` / ``action`` edits.
Any other difference versus the on-disk version is rejected with 422.
"""

from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, field_validator

from traffic_analyzer.web import workspace as workspace_mod

router = APIRouter()

# Repository root (traffic_analyzer/web/evidence_api.py -> parents[2]).
_EVENT_CATEGORIES_YAML = (
    Path(__file__).resolve().parents[2]
    / "traffic_analyzer"
    / "config"
    / "event_categories.yaml"
)


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
# SFT sample (only description / action are user-editable)
# ---------------------------------------------------------------------------

# 标注文档 v4.5 的合法 action 编号(action 9 = 正常占位,不出现)。
_ALLOWED_ACTION_IDS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 10, 11})


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

    @field_validator("action")
    @classmethod
    def _check_action_ids(cls, value: List[int]) -> List[int]:
        if not all(a in _ALLOWED_ACTION_IDS for a in value):
            raise ValueError(
                f"action ids must be a subset of {sorted(_ALLOWED_ACTION_IDS)}"
            )
        return value


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
    """与 ``_strip_editable`` 同理:仅 description / action 允许不同。"""
    copy = json.loads(json.dumps(payload, ensure_ascii=False))
    copy["description"] = _MASK
    copy["action"] = _MASK
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
    _reject_active_infer(request, stem)
    evidence_path = workspace_mod.analysis_dir(workspace, stem) / f"{stem}_evidence.json"
    with _put_locks[stem]:
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
    """事件类别配置(供 SFT 编辑器按事件分框),按 event_id 排序。"""
    try:
        data = (
            yaml.safe_load(_EVENT_CATEGORIES_YAML.read_text(encoding="utf-8")) or {}
        )
    except (OSError, yaml.YAMLError) as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to load event categories config: {exc}"
        )
    events = [
        {
            "event_id": int(cat["event_id"]),
            "name_zh": str(cat.get("name_zh") or ""),
            "is_active": bool(cat.get("is_active", True)),
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
    _reject_active_infer(request, stem)
    sft_path = workspace_mod.analysis_dir(workspace, stem) / f"{stem}.json"
    with _put_locks[stem]:
        disk = _read_json(sft_path)
        if disk is None:
            raise HTTPException(status_code=404, detail="SFT file not found")

        new_payload = body.model_dump()
        if not isinstance(disk, dict) or _strip_sft_editable(disk) != _strip_sft_editable(
            new_payload
        ):
            raise HTTPException(
                status_code=422,
                detail="Only description and action may be modified",
            )

        _atomic_write_json(sft_path, new_payload)
    return new_payload
