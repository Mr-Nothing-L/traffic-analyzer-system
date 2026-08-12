"""SFT PUT route (split from the old evidence_api module).

[文件说明]
作用:PUT /api/results/{stem}/sft。仅允许修改 description / action /
event_attributes / attr_mentions,其余字段与磁盘版本比对不一致即 422;
event_attributes/attr_mentions 区分「未提交」(exclude_unset,保留磁盘
原值)与「显式 null」(删除该键);base_sig 乐观锁(指纹不匹配 → 409
conflict);per-stem 锁内复查同 stem 在跑 infer(409);首次人工编辑落盘
前把推理原始输出冻结为 <stem>_raw.json(shutil.copy,已存在则不覆盖,
dashboard 据此计算 edited/edit_missing/edit_extra;重推理成功由 jobs
删除);last_edited_by 只落盘不进响应;tmp+os.replace 原子写(写后
fsync);落盘后看板/视频缓存失效;响应回传新 file_sig 供前端下一次
保存作 base_sig。
上游:web/evidence/__init__.py(共享 router、JSON IO 与差异比对
helper);web/evidence/locks.py(_put_locks 与 _reject_active_infer)。
下游:web/workspace.py(路径与 stem 校验、缓存失效)、
web/evidence_schema.py(SftSample 请求体模型与封闭枚举校验)。
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import HTTPException, Request

from traffic_analyzer.web import workspace as workspace_mod
from traffic_analyzer.web.evidence import (
    _CorruptJsonError,
    _atomic_write_json,
    _file_sig,
    _read_json,
    _strip_sft_editable,
    router,
)
from traffic_analyzer.web.evidence.locks import _put_locks, _reject_active_infer
from traffic_analyzer.web.evidence_schema import SftSample


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
        new_payload = body.model_dump(exclude_unset=True, exclude={"base_sig"})
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

        # 乐观锁:提交时的基准指纹与当前文件不一致 → 他人已改,拒绝覆盖。
        if body.base_sig is not None and body.base_sig != _file_sig(sft_path):
            raise HTTPException(status_code=409, detail="conflict")

        # 原始输出冻结:首次人工编辑落盘前,把推理原始输出复制为
        # <stem>_raw.json(dashboard 据此计算 edited/edit_missing/edit_extra);
        # 已存在则不覆盖(保持「首次编辑前的原始输出」语义);重推理成功时由
        # jobs 删除该快照。
        raw_path = sft_path.with_name(f"{stem}_raw.json")
        if not raw_path.exists():
            shutil.copy(sft_path, raw_path)
        # 追溯字段只落盘;响应仍返回用户提交的 payload 本身。
        to_write = dict(new_payload)
        to_write["last_edited_by"] = getattr(request.state, "user", "local")
        to_write["last_edited_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(sft_path, to_write)
    # SFT 落盘 → 看板/视频缓存失效(pred_ids / edited / has_results 可能变化)
    workspace_mod.invalidate_caches()
    # 回传写入后的新指纹,前端下一次保存以此为 base_sig,无需再 GET。
    return dict(new_payload, file_sig=_file_sig(sft_path))
