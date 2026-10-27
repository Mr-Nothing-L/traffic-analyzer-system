"""SFT Forge 结果按工作区持久化(重进面板/服务重启后恢复视图)。

[文件说明]
作用:任务终态(done/failed)后把展示所需数据写入
  ``<workspace>/.agent/sftforge/latest_result.json``(原子写:tmp + replace,
  重新推理的新任务终态后覆盖);GET /api/sftforge/store 读、DELETE 删。
  内容 = job_result_payload 全量(stats 含 corrected/videos 摘要含 final_output
  与 r0_match/jsonl/quarantine)+ 每视频轮次精简信息(round_no/kind/
  event_ids/patched,前端「第 N 轮修正」标注数据源,见
  ForgeResultPanel.patchedRoundsOf)。不做迁移:旧缓存缺失 corrected/
  r0_match 字段时读取方需按缺省容忍。
上游:web/sftforge/service.py(终态落盘)、web/sftforge/routes.py(读写端点)。
下游:仅读写工作区文件系统(.agent/ 目录与会话 sessions.db 同级)。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def store_path(workspace: Path) -> Path:
    return workspace / ".agent" / "sftforge" / "latest_result.json"


def save_result(workspace: Path, payload: Dict[str, Any]) -> None:
    """覆盖写持久化结果;失败仅记日志(持久化是旁路,不影响任务终态)。"""
    path = store_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("[sftforge-web] persist result failed (%s): %s", path, exc)


def load_result(workspace: Path) -> Optional[Dict[str, Any]]:
    """读取持久化结果;文件缺失/损坏返回 None(路由层转 404)。"""
    try:
        data = json.loads(store_path(workspace).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def delete_result(workspace: Path) -> bool:
    """删除持久化结果文件;返回是否真的删掉了文件。"""
    try:
        store_path(workspace).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("[sftforge-web] delete stored result failed: %s", exc)
        return False
