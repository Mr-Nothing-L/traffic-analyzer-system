"""SFT export route: single-video cleaned sample as a one-line JSONL attachment.

[文件说明]
作用:GET /api/results/{stem}/sft/export。读取 analysis/<stem>/ 下的 SFT
样本(经 sft_json_path 解析:顶层优先,quarantine 回退——编辑器能打开的
样本就能导出),投影为训练契约字段,以单行 JSONL 文本附件响应,供前端
浏览器下载为 <stem>.jsonl。纯只读端点:不改磁盘、不加锁、不参与乐观锁。
清洗契约:保留 chunk / idx / action / description / start_timestamp /
end_timestamp / chunk_name / event_attributes / attr_mentions / keyframes,
与 core/sft_label_rewrite.build_sample 的返回键集合一致;last_edited_by /
last_edited_at 等 Web 工作台追溯字段一律剥离;旧格式样本缺失的键补
契约默认值,保证输出 schema 稳定(缺 start/end_timestamp 等历史样本
也能产出可训练的一致结构)。
上游:web/evidence/__init__.py(共享 router、_read_json 与 sft_json_path)。
下游:frontend/src/api/results.ts fetchSftJsonl → SFT 标注 tab「导出」按钮
(dirty 时前端禁用,导出的始终是磁盘已保存版本)。
"""

from __future__ import annotations

import json
from typing import Any, Dict
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

    sample = {key: disk.get(key, default) for key, default in _EXPORT_FIELDS.items()}
    line = json.dumps(sample, ensure_ascii=False) + "\n"
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
