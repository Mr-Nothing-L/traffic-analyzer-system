"""SQLite-backed per-IP chat state and message store.

[文件说明]
作用:快速对话的 SQLite 存储(config/chat.db)。chat_state 表按 IP 保存当前
信源(source_kind: workspace_video|upload_video|upload_images,source_ref 为
JSON)与压缩摘要 summary;chat_messages 表按 IP 保存消息(role:
user|assistant|divider,think 为思考链全文,images 为产出图文件名 JSON 数组)。
提供 get_state/set_source/set_summary/add_message/list_messages/clear/
delete_messages_up_to/delete_message_and_reply(撤回:删消息及其后的
assistant 回复);全部参数化 SQL,每函数新建连接(同 web/user_store.py
模式);读操作在库文件不存在时返回空结果,写操作才创建文件。
上游:web/chat/routes.py(状态/历史接口)、web/chat/qa.py(问答读写与压缩)。
下游:仅 SQLite 文件;DB_PATH 常量供测试 monkeypatch。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# traffic_analyzer/web/chat/store.py → parents[2] = traffic_analyzer/。
# 测试 monkeypatch 此常量(或给各函数显式传 db_path)。
DB_PATH = Path(__file__).resolve().parents[2] / "config" / "chat.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_state (
    ip TEXT PRIMARY KEY,
    source_kind TEXT,
    source_ref TEXT,
    summary TEXT,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT,
    role TEXT,
    content TEXT,
    think TEXT,
    images TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_ip ON chat_messages (ip, id)
"""


def _connect(db_path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    return conn


def get_state(
    ip: str, db_path: Optional[Union[str, Path]] = None
) -> Dict[str, Any]:
    """Chat state for ``ip``; empty defaults when no row exists yet."""
    empty: Dict[str, Any] = {
        "source_kind": None,
        "source_ref": None,
        "summary": "",
        "updated_at": None,
    }
    path = Path(db_path) if db_path is not None else Path(DB_PATH)
    if not path.is_file():
        return empty
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT source_kind, source_ref, summary, updated_at"
            " FROM chat_state WHERE ip = ?",
            (ip,),
        ).fetchone()
    if row is None:
        return empty
    try:
        ref = json.loads(row[1]) if row[1] else None
    except ValueError:
        ref = None
    return {
        "source_kind": row[0],
        "source_ref": ref,
        "summary": row[2] or "",
        "updated_at": row[3],
    }


def _upsert_state(
    ip: str,
    db_path: Optional[Union[str, Path]],
    **fields: Any,
) -> None:
    """Insert-or-update selected chat_state columns for ``ip``."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO chat_state (ip, updated_at) VALUES (?, ?)"
            " ON CONFLICT(ip) DO NOTHING",
            (ip, time.time()),
        )
        assignments = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
        conn.execute(
            f"UPDATE chat_state SET {assignments} WHERE ip = ?",
            (*fields.values(), time.time(), ip),
        )


def set_source(
    ip: str,
    kind: str,
    ref_dict: Dict[str, Any],
    db_path: Optional[Union[str, Path]] = None,
) -> None:
    """Set the current source (workspace_video|upload_video|upload_images)."""
    _upsert_state(
        ip, db_path, source_kind=kind, source_ref=json.dumps(ref_dict, ensure_ascii=False)
    )


def set_summary(
    ip: str, summary: str, db_path: Optional[Union[str, Path]] = None
) -> None:
    """Replace the compaction summary for ``ip``."""
    _upsert_state(ip, db_path, summary=summary)


def add_message(
    ip: str,
    role: str,
    content: str,
    think: str = "",
    images: tuple = (),
    db_path: Optional[Union[str, Path]] = None,
) -> int:
    """Append a message and return its row id."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages (ip, role, content, think, images, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (ip, role, content, think, json.dumps(list(images), ensure_ascii=False), time.time()),
        )
        return int(cur.lastrowid)


def list_messages(
    ip: str, db_path: Optional[Union[str, Path]] = None
) -> List[Dict[str, Any]]:
    """All messages for ``ip`` in insertion order (images parsed to list)."""
    path = Path(db_path) if db_path is not None else Path(DB_PATH)
    if not path.is_file():
        return []
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT id, role, content, think, images, created_at"
            " FROM chat_messages WHERE ip = ? ORDER BY id",
            (ip,),
        ).fetchall()
    messages: List[Dict[str, Any]] = []
    for row in rows:
        try:
            images = json.loads(row[4]) if row[4] else []
        except ValueError:
            images = []
        messages.append(
            {
                "id": row[0],
                "role": row[1],
                "content": row[2] or "",
                "think": row[3] or "",
                "images": images,
                "created_at": row[5],
            }
        )
    return messages


def clear(ip: str, db_path: Optional[Union[str, Path]] = None) -> None:
    """Delete all state and messages for ``ip``."""
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM chat_state WHERE ip = ?", (ip,))
        conn.execute("DELETE FROM chat_messages WHERE ip = ?", (ip,))


def delete_messages_up_to(
    ip: str, msg_id: int, db_path: Optional[Union[str, Path]] = None
) -> None:
    """Delete messages with ``id <= msg_id`` (used after compaction)."""
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM chat_messages WHERE ip = ? AND id <= ?", (ip, msg_id)
        )


def delete_message_and_reply(
    ip: str, msg_id: int, db_path: Optional[Union[str, Path]] = None
) -> bool:
    """Delete message ``msg_id`` plus the assistant reply right after it.

    The next message (by id, same ``ip``) is removed only when its role is
    ``assistant``. Returns False when ``msg_id`` does not belong to ``ip``.
    """
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM chat_messages WHERE ip = ? AND id = ?", (ip, msg_id)
        ).fetchone()
        if row is None:
            return False
        nxt = conn.execute(
            "SELECT id, role FROM chat_messages WHERE ip = ? AND id > ?"
            " ORDER BY id LIMIT 1",
            (ip, msg_id),
        ).fetchone()
        ids = [msg_id]
        if nxt is not None and nxt[1] == "assistant":
            ids.append(int(nxt[0]))
        placeholders = ", ".join("?" for _ in ids)
        conn.execute(
            f"DELETE FROM chat_messages WHERE ip = ? AND id IN ({placeholders})",
            (ip, *ids),
        )
        return True
