"""Result reading and event-config GET routes (split from evidence_api).

[文件说明]
作用:GET /api/results/{stem}(report.md + SFT json + evidence json +
file_sig/evidence_sig 乐观锁指纹)、GET /api/results/{stem}/file(按相对
路径提供 analysis/<stem>/ 下的文件,路径严格限制在该目录内)、
GET /api/config/events(事件类别 + event_options.yaml 封闭枚举选项,供
SFT 编辑器按事件分框与渲染结构化选项)。event yaml 路径常量经包命名
空间延迟查找(_pkg._EVENT_*_YAML):_pkg 与老路径
traffic_analyzer.web.evidence_api 为同一模块对象,测试 monkeypatch
evidence_api._EVENT_*_YAML 后即生效。
上游:web/evidence/__init__.py(共享 router 与 JSON IO helper)。
下游:web/workspace.py(路径与 stem 校验)、web/event_config.py(yaml
缓存索引,经 _pkg 的包装函数读取)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse

from traffic_analyzer.web import evidence as _pkg
from traffic_analyzer.web import workspace as workspace_mod
from traffic_analyzer.web.evidence import (
    _CorruptJsonError,
    _file_sig,
    _read_json,
    router,
)


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
        # 乐观锁指纹:当前 SFT json 内容 sha256 前 16;PUT 回传 base_sig 做冲突检测。
        "file_sig": _file_sig(out_dir / f"{stem}.json"),
        # 证据文件指纹:证据 PUT 的 base_sig 以此为准(与 SFT 分开,互不误伤)。
        "evidence_sig": _file_sig(out_dir / f"{stem}_evidence.json"),
    }


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


@router.get("/api/config/events")
def get_config_events() -> List[Dict[str, Any]]:
    """事件类别配置(供 SFT 编辑器按事件分框),按 event_id 排序。

    每个事件附带 ``options``:event_options.yaml 中定义的结构化属性组
    (封闭枚举,只读选项集);未定义的事件返回空列表。
    """
    try:
        data = (
            yaml.safe_load(_pkg._EVENT_CATEGORIES_YAML.read_text(encoding="utf-8")) or {}
        )
        options_index = _pkg._event_options_index()
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
