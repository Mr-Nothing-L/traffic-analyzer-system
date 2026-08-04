"""Result reading and evidence/SFT editing endpoints (split into a package).

Reads ``report.md`` / ``<stem>.json`` / ``<stem>_evidence.json`` from
``<workspace>/analysis/<stem>/`` and serves files under that directory via
``GET /api/results/{stem}/file?path=`` (composite images, tmp_img subtrees).
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

Optimistic locking: ``GET /api/results/{stem}`` returns ``file_sig`` (sha256
of the current SFT json, first 16 hex chars); both PUTs accept ``base_sig``
and reject with ``409 {"detail": "conflict"}`` when it no longer matches the
file on disk. Successful PUTs stamp ``last_edited_by`` (from
``request.state.user``) into the written JSON (disk only, not the response).

[文件说明]
作用:结果读取与证据/SFT 编辑接口包(自原单文件 evidence_api.py 拆分;
老模块路径 traffic_analyzer.web.evidence_api 经同目录 evidence_api.py
跳板的 sys.modules 自我替换指向本包,二者为同一模块对象,既有引用与
测试 monkeypatch 路径不变)。本 __init__ 持有共享 JSON IO/差异比对
helper、event yaml 路径常量与缓存包装(测试 monkeypatch 面)、聚合
router,并 re-export 各子模块名字;locks.py 为 per-stem PUT 锁与在跑
infer 检查;results.py 为 results/file/config GET 路由;evidence_put.py
为证据 PUT 路由;sft_api.py 为 SFT PUT 路由(首次编辑前冻结
<stem>_raw.json)。pydantic 模型在 web/evidence_schema.py,yaml 缓存索引
在 web/event_config.py。GET 读取 <workspace>/analysis/<stem>/ 下的
report.md、<stem>.json(SFT 样本)、<stem>_evidence.json 及图片;evidence
PUT 仅允许修改标定多边形与证据框/标签,SFT PUT 仅允许
description/action/event_attributes/attr_mentions,其余字段与磁盘版本
比对不一致即 422;event_attributes/attr_mentions 区分「未提交」(保留
磁盘原值)与「显式 null」(删除该键);写入采用 tmp+os.replace 原子写
(写后 fsync)并按 stem 加锁(409 在跑 infer 检查在锁内复查,消除
TOCTOU)。_read_json 区分「文件不存在」(GET → null / PUT → 404)与
「文件损坏」(GET → 500 / PUT → 422,绝不静默当作不存在)。
上游:web/app.py(挂载路由);web/static 前端(结果查看与标注编辑)。
下游:web/workspace.py(路径与 stem 校验)、web/jobs(在跑任务检查,
jobs.post_infer 反向复用本包的 _put_locks 与 find_active_infer_job 消除
infer-vs-PUT 的 TOCTOU)、
config/event_categories.yaml 与 config/event_options.yaml
(/api/config/events 供 SFT 编辑器按事件分框与渲染结构化选项)。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from traffic_analyzer.web import event_config

router = APIRouter()

# 路径常量的规范定义在 event_config;在此重新绑定为模块全局,供端点与包装
# 函数读取(测试 monkeypatch evidence_api._EVENT_*_YAML 后即生效——老路径
# traffic_analyzer.web.evidence_api 与本包为同一模块对象,见
# web/evidence_api.py 兼容跳板)。
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


def _file_sig(path: Path) -> Optional[str]:
    """文件内容 sha256 前 16 位(乐观锁指纹);文件不可读 → None。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


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


_MASK = "__editable__"


def _strip_editable(payload: Dict[str, Any]) -> Dict[str, Any]:
    """JSON-normalized copy with the user-editable fields masked out.

    Two payloads that differ ONLY in editable fields strip to equal dicts.
    """
    copy = json.loads(json.dumps(payload, ensure_ascii=False))
    # last_edited_by 是服务端写入的追溯字段,不参与「仅可编辑字段可不同」比对。
    copy.pop("last_edited_by", None)
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
    copy.pop("last_edited_by", None)  # 服务端追溯字段,不参与比对
    copy["description"] = _MASK
    copy["action"] = _MASK
    copy["event_attributes"] = _MASK
    copy["attr_mentions"] = _MASK
    return copy


# ---------------------------------------------------------------------------
# Aggregate re-exports(各子模块的路由挂在上方共享的 router 上)
# ---------------------------------------------------------------------------

from traffic_analyzer.web.evidence import (  # noqa: E402,F401
    evidence_put,
    locks,
    results,
    sft_api,
)
from traffic_analyzer.web.evidence.evidence_put import put_evidence  # noqa: E402,F401
from traffic_analyzer.web.evidence.locks import (  # noqa: E402,F401
    _put_locks,
    _reject_active_infer,
    find_active_infer_job,
)
from traffic_analyzer.web.evidence.results import (  # noqa: E402,F401
    get_config_events,
    get_result_file,
    get_results,
)
from traffic_analyzer.web.evidence.sft_api import put_sft  # noqa: E402,F401

__all__ = [
    "router",
    "find_active_infer_job",
    "get_config_events",
    "get_result_file",
    "get_results",
    "put_evidence",
    "put_sft",
]
