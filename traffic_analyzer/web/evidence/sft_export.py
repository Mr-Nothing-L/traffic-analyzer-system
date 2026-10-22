"""SFT export routes: single-video & batch cleaned samples as JSONL attachments.

[文件说明]
作用:GET /api/results/{stem}/sft/export(单视频)与 POST /api/workspace/
sft/export(勾选集合批量)。读取 analysis/<stem>/ 下的 SFT 样本(经
sft_json_path 解析:顶层优先,quarantine 回退——编辑器能打开的样本就能
导出),投影为训练契约字段,以 JSONL 文本附件响应,供前端浏览器下载。
纯只读端点:不改磁盘、不加锁、不参与乐观锁。
清洗契约:保留 chunk / idx / action / description / start_timestamp /
end_timestamp / chunk_name / event_attributes / attr_mentions / keyframes,
与 core/sft_label_rewrite.build_sample 的返回键集合一致;last_edited_by /
last_edited_at 等 Web 工作台追溯字段一律剥离;旧格式样本缺失的键补
契约默认值,保证输出 schema 稳定(缺 start/end_timestamp 等历史样本
也能产出可训练的一致结构)。单视频与批量共用 project_export_sample,
防两处投影漂移;差异仅在错误语义:单视频对无标注/损坏显式报
404/422,批量跳过并计数(响应头 X-Export-Total / X-Export-Skipped)。
上游:web/evidence/__init__.py(共享 router、_read_json 与 sft_json_path)。
下游:frontend/src/api/results.ts fetchSftJsonl / exportSftBatch →
SFT 标注 tab「导出」按钮与侧栏树工具栏「导出标注」(批量,导出的是
磁盘已保存版本,与 dirty 草稿无关)。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from urllib.parse import quote
from fastapi import HTTPException, Request
from fastapi.responses import Response

from traffic_analyzer.web import workspace as workspace_mod
from traffic_analyzer.web.evidence import (
    _CorruptJsonError,
    _read_json,
    router,
    sft_json_path,
)

# 训练契约字段(键集合与顺序 = core/sft_label_rewrite.build_sample 的返回;
# 默认值亦取自同处:chunk/idx 为流水线固定常量,时间戳缺失按 0.0)。
_EXPORT_FIELDS: Dict[str, Any] = {
    "chunk": "chunk #1",
    "idx": 1,
    "action": [],
    "description": "",
    "start_timestamp": 0.0,
    "end_timestamp": 0.0,
    "chunk_name": "",
    "event_attributes": {},
    "attr_mentions": {},
    "keyframes": [],
}

# 批量导出的勾选集合上限(超出 400:批量端点必须保持请求可预测,
# 不接受任意大的循环;正常勾选集合远小于此)。
MAX_BATCH_EXPORT_STEMS = 1000


def project_export_sample(disk: Dict[str, Any]) -> Dict[str, Any]:
    """磁盘样本 → 训练契约字段投影(单视频与批量导出共用,单一真相源)。

    键集合/顺序/默认值见 _EXPORT_FIELDS;非契约字段(含工作台追溯字段)
    一律剥离,旧格式缺失键补默认值。
    """
    return {key: disk.get(key, default) for key, default in _EXPORT_FIELDS.items()}


def _export_line(out_dir: Path, stem: str) -> Optional[str]:
    """单个样本 → 清洗后的 JSONL 行;无法产出(无标注/损坏/非对象)返回 None。

    批量导出据此跳过并计数;单视频端点需要区分 404/422,故不走本函数。
    """
    try:
        disk = _read_json(sft_json_path(out_dir, stem))
    except _CorruptJsonError:
        return None
    if not isinstance(disk, dict):
        return None
    return json.dumps(project_export_sample(disk), ensure_ascii=False)


class _BatchExportRequest(BaseModel):
    stems: List[str]


@router.get("/api/results/{stem}/sft/export")
def export_sft_jsonl(stem: str, request: Request) -> Response:
    workspace = workspace_mod.require_workspace(request)
    workspace_mod.validate_stem(stem)
    out_dir = workspace_mod.analysis_dir(workspace, stem)
    try:
        disk = _read_json(sft_json_path(out_dir, stem))
    except _CorruptJsonError as exc:
        raise HTTPException(status_code=422, detail=f"SFT file is corrupt: {exc}")
    if disk is None:
        raise HTTPException(status_code=404, detail="SFT file not found")
    if not isinstance(disk, dict):
        raise HTTPException(status_code=422, detail="SFT file is not a sample object")

    line = json.dumps(project_export_sample(disk), ensure_ascii=False) + "\n"
    # stem 允许非 ASCII(validate_stem 只拒路径穿越/控制字符),文件名按
    # RFC 5987 编码;filename= 回退名保证不支持 filename* 的旧端仍能落盘。
    filename = quote(f"{stem}.jsonl", safe="")
    return Response(
        content=line,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f"attachment; filename=\"export.jsonl\"; filename*=UTF-8''{filename}"
        },
    )


@router.post("/api/workspace/sft/export")
def export_sft_batch(body: _BatchExportRequest, request: Request) -> Response:
    """按勾选集合批量导出:合并 JSONL 附件,坏样本跳过并计数。

    每行 = 与单视频导出逐字节同构的清洗训练版样本(共用
    project_export_sample);响应头 X-Export-Total / X-Export-Skipped 供
    前端诚实报告「导出 N 条,跳过 M 条」。
    """
    workspace = workspace_mod.require_workspace(request)
    stems = body.stems
    if not stems:
        raise HTTPException(status_code=400, detail="stems must not be empty")
    if len(stems) > MAX_BATCH_EXPORT_STEMS:
        raise HTTPException(
            status_code=400,
            detail=f"too many stems: {len(stems)} > {MAX_BATCH_EXPORT_STEMS}",
        )

    lines: List[str] = []
    skipped = 0
    for stem in stems:
        workspace_mod.validate_stem(stem)  # 坏 stem:404,与既有端点口径一致
        line = _export_line(workspace_mod.analysis_dir(workspace, stem), stem)
        if line is None:
            skipped += 1
        else:
            lines.append(line)

    content = "".join(line + "\n" for line in lines)
    date_tag = date.today().isoformat()
    ws_name = workspace.name or "workspace"
    filename = quote(f"{ws_name}_{date_tag}_sft.jsonl", safe="")
    return Response(
        content=content,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f"attachment; filename=\"export.jsonl\"; filename*=UTF-8''{filename}",
            "X-Export-Total": str(len(lines)),
            "X-Export-Skipped": str(skipped),
        },
    )
