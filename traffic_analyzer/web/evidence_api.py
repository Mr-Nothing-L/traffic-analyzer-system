"""Result reading and evidence editing endpoints.

Reads ``report.md`` / ``<stem>.json`` / ``<stem>_evidence.json`` from
``<workspace>/analysis/<stem>/`` and serves the copied composite images.
The evidence PUT endpoint re-validates the payload against schema v1 and
only allows edits to the user-editable coordinate/label fields:

- ``events[*].calibration.emergency_polygon_rel``
- ``events[*].calibration.chevron_polygon_rel``
- ``events[*].evidence_regions[*].box_rel``
- ``events[*].evidence_regions[*].label``

Any other difference versus the on-disk version is rejected with 422.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, field_validator

from traffic_analyzer.web import workspace as workspace_mod

router = APIRouter()


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
# Helpers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


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
    images_dir = (workspace_mod.analysis_dir(workspace, stem) / "images").resolve()
    candidate = (images_dir / name).resolve()
    if candidate.parent != images_dir or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(candidate)


@router.put("/api/results/{stem}/evidence")
def put_evidence(stem: str, body: Evidence, request: Request) -> Dict[str, Any]:
    workspace = workspace_mod.require_workspace(request)
    workspace_mod.validate_stem(stem)
    evidence_path = workspace_mod.analysis_dir(workspace, stem) / f"{stem}_evidence.json"
    disk = _read_json(evidence_path)
    if disk is None:
        raise HTTPException(status_code=404, detail="Evidence file not found")

    new_payload = body.model_dump()
    if _strip_editable(disk) != _strip_editable(new_payload):
        raise HTTPException(
            status_code=422,
            detail=(
                "Only calibration.emergency_polygon_rel, "
                "calibration.chevron_polygon_rel and "
                "evidence_regions[*].box_rel/.label may be modified"
            ),
        )

    evidence_path.write_text(
        json.dumps(new_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return new_payload
