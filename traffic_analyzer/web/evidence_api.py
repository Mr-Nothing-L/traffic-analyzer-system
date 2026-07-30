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
作用:结果读取与证据/SFT 编辑接口(端点 + 原子写 + per-stem PUT 锁;pydantic
模型在 web/evidence_schema.py,yaml 缓存索引在 web/event_config.py)。GET 读取
<workspace>/analysis/<stem>/ 下的 report.md、<stem>.json(SFT 样本)、
<stem>_evidence.json 及图片;evidence PUT 仅允许修改标定多边形与证据框/标签,
SFT PUT 仅允许 description/action/event_attributes/attr_mentions,其余字段与
磁盘版本比对不一致即 422;首次 SFT 编辑落盘前把原始输出冻结为 <stem>_raw.json
(shutil.copy,已存在则不覆盖;重推理成功由 jobs 删除);event_attributes/attr_mentions
区分「未提交」(保留
磁盘原值)与「显式 null」(删除该键);写入采用 tmp+os.replace 原子写(写后
fsync)并按 stem 加锁(409 在跑 infer 检查在锁内复查,消除 TOCTOU)。
_read_json 区分「文件不存在」(GET → null / PUT → 404)与「文件损坏」
(GET → 500 / PUT → 422,绝不静默当作不存在)。
上游:web/app.py(挂载路由);web/static 前端(结果查看与标注编辑)。
下游:web/workspace.py(路径与 stem 校验)、web/jobs.py(在跑任务检查,
jobs.post_infer 反向复用本模块的 _put_locks 消除 infer-vs-PUT 的 TOCTOU)、
config/event_categories.yaml 与 config/event_options.yaml
(/api/config/events 供 SFT 编辑器按事件分框与渲染结构化选项)。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from traffic_analyzer.web import event_config
from traffic_analyzer.web import workspace as workspace_mod
from traffic_analyzer.web.evidence_schema import Evidence, SftSample

router = APIRouter()

# 路径常量的规范定义在 event_config;在此重新绑定为模块全局,供端点与包装
# 函数读取(测试 monkeypatch evidence_api._EVENT_*_YAML 后即生效)。
_EVENT_CATEGORIES_YAML = event_config._EVENT_CATEGORIES_YAML
_EVENT_OPTIONS_YAML = event_config._EVENT_OPTIONS_YAML


def _event_options_index() -> Dict[int, List[Dict[str, Any]]]:
    """event_options.yaml 封闭枚举索引(读本模块的路径常量,monkeypatch 友好)。"""
    return event_config.event_options_index(_EVENT_OPTIONS_YAML)


def _event_name_index() -> Dict[str, int]:
    """事件中文名 → event_id(读本模块的路径常量,monkeypatch 友好)。"""
    return event_config.event_name_index(_EVENT_CATEGORIES_YAML)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CorruptJsonError(ValueError):
    """analysis JSON 文件存在但解析失败(区别于「文件不存在」)。"""


def _read_json(path: Path) -> Optional[Any]:
    """Read a JSON file: ``None`` when missing, raise on corrupt content.

    损坏(半写/截断)与不存在必须区分开:调用方据此返回 500/422,而不是
    静默当成「无标注」(GET null / PUT 404)。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise _CorruptJsonError(f"{path.name}: {exc}") from exc


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON via a same-dir ``.tmp`` file + ``os.replace``.

    A mid-write crash can then only lose the tmp file, never truncate the
    real one (which GETs would otherwise silently show as "无标注").
    fsync before the replace: the renamed data is durable, not just queued
    in the page cache.
    """
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# Per-stem locks closing the concurrent read-compare-write PUT window.
# jobs.post_infer 提交同 stem 的 infer 前也持有同一把锁(锁顺序统一为
# _put_locks[stem] → JobManager._lock,反向路径不存在,不会死锁)。
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

    try:
        sft_label = _read_json(out_dir / f"{stem}.json")
        evidence = _read_json(out_dir / f"{stem}_evidence.json")
    except _CorruptJsonError as exc:
        raise HTTPException(
            status_code=500, detail=f"Corrupt analysis JSON for '{stem}': {exc}"
        )
    return {
        "report_md": report_md,
        "sft_label": sft_label,
        "evidence": evidence,
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
        try:
            disk = _read_json(evidence_path)
        except _CorruptJsonError as exc:
            # 损坏 ≠ 不存在:无法与损坏基线做差异比对,明确报 422。
            raise HTTPException(
                status_code=422, detail=f"Existing evidence file is corrupt: {exc}"
            )
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
        try:
            disk = _read_json(sft_path)
        except _CorruptJsonError as exc:
            # 损坏 ≠ 不存在:明确报 422,不能静默 404 诱导前端以为「无标注」。
            raise HTTPException(
                status_code=422, detail=f"Existing SFT file is corrupt: {exc}"
            )
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

        # 原始输出冻结:首次人工编辑落盘前,把推理原始输出复制为
        # <stem>_raw.json(dashboard 据此计算 edited/edit_missing/edit_extra);
        # 已存在则不覆盖(保持「首次编辑前的原始输出」语义);重推理成功时由
        # jobs 删除该快照。
        raw_path = sft_path.with_name(f"{stem}_raw.json")
        if not raw_path.exists():
            shutil.copy(sft_path, raw_path)
        _atomic_write_json(sft_path, new_payload)
    return new_payload
