"""Evidence PUT route (split from the old evidence_api module).

[文件说明]
作用:PUT /api/results/{stem}/evidence。仅允许修改标定多边形
(calibration.emergency_polygon_rel / chevron_polygon_rel)与证据框/标签
(evidence_regions[*].box_rel / .label),其余字段与磁盘版本比对不一致即
422;base_sig 乐观锁(指纹不匹配 → 409 conflict);per-stem 锁内复查同
stem 在跑 infer(409);last_edited_by 只落盘不进响应;tmp+os.replace
原子写(写后 fsync);落盘后看板/视频缓存失效;响应回传新
evidence_sig 供前端下一次保存作 base_sig。
上游:web/evidence/__init__.py(共享 router、JSON IO 与差异比对
helper);web/evidence/locks.py(_put_locks 与 _reject_active_infer)。
下游:web/workspace.py(路径与 stem 校验、缓存失效)、
web/evidence_schema.py(Evidence 请求体模型)。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import HTTPException, Request

from traffic_analyzer.web import workspace as workspace_mod
from traffic_analyzer.web.evidence import (
    _CorruptJsonError,
    _atomic_write_json,
    _file_sig,
    _read_json,
    _strip_editable,
    router,
)
from traffic_analyzer.web.evidence.locks import _put_locks, _reject_active_infer
from traffic_analyzer.web.evidence_schema import Evidence


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

        new_payload = body.model_dump(exclude={"base_sig"})
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

        # 乐观锁:提交时的基准指纹与当前文件不一致 → 他人已改,拒绝覆盖。
        if body.base_sig is not None and body.base_sig != _file_sig(evidence_path):
            raise HTTPException(status_code=409, detail="conflict")
        # 追溯字段只落盘;响应仍返回用户提交的 payload 本身。
        to_write = dict(new_payload)
        to_write["last_edited_by"] = getattr(request.state, "user", "local")
        _atomic_write_json(evidence_path, to_write)
    # 证据落盘 → 看板/视频缓存失效
    workspace_mod.invalidate_caches()
    # 回传写入后的新指纹,前端下一次保存以此为 base_sig,无需再 GET。
    return dict(new_payload, evidence_sig=_file_sig(evidence_path))
