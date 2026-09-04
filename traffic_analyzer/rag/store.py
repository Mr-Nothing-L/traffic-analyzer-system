"""RAG 向量库:sqlite 存储 + numpy float32 暴力余弦检索。

[文件说明]
作用:向量与元数据持久化到 <workspace>/.agent/rag/vectors.db(目录自动创建);
records 表存视频/标注向量(float32 BLOB)与审核/站点/时间字段,meta 表存索引级元信息;
search 支持 video / annotation / hybrid(alpha 加权)三种字段及事件、审核态过滤。
上游:scripts/build_rag_index.py(写入)、scripts/rag_search.py(查询)。
下游:sqlite3(本地文件)。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import numpy as np

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    video_path TEXT PRIMARY KEY,
    video_vec BLOB,
    ann_vec BLOB,
    events TEXT,
    has_annotation INT,
    human_edited INT,
    review_status TEXT,
    road TEXT,
    stake TEXT,
    direction TEXT,
    camera TEXT,
    site TEXT,
    start_ts REAL,
    duration_s REAL,
    ann_edited_at TEXT,
    embedded_at REAL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _to_blob(vec) -> bytes | None:
    if vec is None:
        return None
    return np.asarray(vec, dtype=np.float32).tobytes()


def _from_blob(blob: bytes | None) -> np.ndarray | None:
    if blob is None:
        return None
    return np.frombuffer(blob, dtype=np.float32)


def _cosine(q: np.ndarray, v: np.ndarray) -> float:
    denom = float(np.linalg.norm(q) * np.linalg.norm(v))
    if denom == 0.0:
        return 0.0
    return float(np.dot(q, v) / denom)


class RagStore:
    """<workspace>/.agent/rag/vectors.db 的读写封装。"""

    def __init__(self, workspace) -> None:
        self.workspace = Path(workspace)
        rag_dir = self.workspace / ".agent" / "rag"
        rag_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = rag_dir / "vectors.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "RagStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 写入 ----

    def upsert_record(
        self,
        video_path: str,
        *,
        video_vec=None,
        ann_vec=None,
        events: list | None = None,
        has_annotation: int = 0,
        human_edited: int = 0,
        review_status: str = "unconfirmed",
        road: str | None = None,
        stake: str | None = None,
        direction: str | None = None,
        camera: str | None = None,
        site: str | None = None,
        start_ts: float | None = None,
        duration_s: float | None = None,
        ann_edited_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO records
               (video_path, video_vec, ann_vec, events, has_annotation, human_edited,
                review_status, road, stake, direction, camera, site,
                start_ts, duration_s, ann_edited_at, embedded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                video_path,
                _to_blob(video_vec),
                _to_blob(ann_vec),
                json.dumps(events or [], ensure_ascii=False),
                int(has_annotation),
                int(human_edited),
                review_status,
                road,
                stake,
                direction,
                camera,
                site,
                start_ts,
                duration_s,
                ann_edited_at,
                time.time(),
            ),
        )
        self.conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value))
        )
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # ---- 读取 ----

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d.pop("video_vec", None)
        d.pop("ann_vec", None)
        try:
            d["events"] = json.loads(d.get("events") or "[]")
        except json.JSONDecodeError:
            d["events"] = []
        return d

    def records(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM records").fetchall()
        return [self._row_to_dict(r) for r in rows]

    def existing_paths(self) -> set[str]:
        rows = self.conn.execute("SELECT video_path FROM records").fetchall()
        return {r["video_path"] for r in rows}

    def get_vec(self, video_path: str, field: str) -> np.ndarray | None:
        """field ∈ "video" | "annotation";供 --related 取向量。"""
        col = {"video": "video_vec", "annotation": "ann_vec"}.get(field)
        if col is None:
            raise ValueError(f"unknown vec field: {field}")
        row = self.conn.execute(
            f"SELECT {col} FROM records WHERE video_path = ?", (video_path,)
        ).fetchone()
        return _from_blob(row[col]) if row else None

    def stats(self) -> dict:
        row = self.conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(video_vec IS NOT NULL) AS with_video_vec,
                      SUM(ann_vec IS NOT NULL) AS with_ann_vec,
                      SUM(has_annotation) AS has_annotation,
                      SUM(human_edited) AS human_edited
               FROM records"""
        ).fetchone()
        review_rows = self.conn.execute(
            "SELECT review_status, COUNT(*) AS n FROM records GROUP BY review_status"
        ).fetchall()
        meta_rows = self.conn.execute("SELECT key, value FROM meta").fetchall()
        return {
            "total": row["total"] or 0,
            "with_video_vec": row["with_video_vec"] or 0,
            "with_ann_vec": row["with_ann_vec"] or 0,
            "has_annotation": row["has_annotation"] or 0,
            "human_edited": row["human_edited"] or 0,
            "review_status": {r["review_status"]: r["n"] for r in review_rows},
            "meta": {r["key"]: r["value"] for r in meta_rows},
        }

    # ---- 检索 ----

    def search(
        self,
        query_vec,
        field: str,
        top_k: int,
        event_filter: list | None = None,
        review_filter=None,
        alpha: float = 0.6,
    ) -> list[tuple[str, float]]:
        """暴力余弦检索,返回 [(video_path, score)] 按分数降序。

        field ∈ "video" | "annotation" | "hybrid";hybrid 时
        score = alpha * cos_video + (1 - alpha) * cos_ann,缺一侧向量时用另一侧。
        """
        if field not in ("video", "annotation", "hybrid"):
            raise ValueError(f"unknown search field: {field}")
        q = np.asarray(query_vec, dtype=np.float32)
        event_set = {str(e) for e in event_filter} if event_filter else None
        if review_filter is None:
            review_set = None
        elif isinstance(review_filter, str):
            review_set = {review_filter}
        else:
            review_set = set(review_filter)

        rows = self.conn.execute(
            "SELECT video_path, video_vec, ann_vec, events, review_status FROM records"
        ).fetchall()
        scored: list[tuple[str, float]] = []
        for row in rows:
            if review_set is not None and row["review_status"] not in review_set:
                continue
            if event_set is not None:
                try:
                    events = {str(e) for e in json.loads(row["events"] or "[]")}
                except json.JSONDecodeError:
                    events = set()
                if not events & event_set:
                    continue
            v = _from_blob(row["video_vec"])
            a = _from_blob(row["ann_vec"])
            if field == "video":
                if v is None:
                    continue
                score = _cosine(q, v)
            elif field == "annotation":
                if a is None:
                    continue
                score = _cosine(q, a)
            else:
                if v is None and a is None:
                    continue
                vs = _cosine(q, v) if v is not None else None
                as_ = _cosine(q, a) if a is not None else None
                if vs is None:
                    score = as_
                elif as_ is None:
                    score = vs
                else:
                    score = alpha * vs + (1.0 - alpha) * as_
            scored.append((row["video_path"], float(score)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]
