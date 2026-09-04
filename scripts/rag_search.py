#!/usr/bin/env python3
"""RAG 向量库查询 CLI(traffic_analyzer.rag.query 的薄封装)。

Usage:
    python scripts/rag_search.py text "应急车道占用白色SUV" [-k 10] [--alpha 0.6]
        [--only-confirmed] [--human-edited]
    python scripts/rag_search.py related <文件名> [-k 10]
    python scripts/rag_search.py adjacent <文件名> [--gap-s 600]
    python scripts/rag_search.py site <桩号> [--direction 进京] [--before T] [--after T]

[文件说明]
作用:四种检索——text(查询词 hybrid 检索)、related(video_vec 相似)、
adjacent(同 site 候选按时间邻近过滤,site 缺失回退 video_vec top-50 候选)、
site(桩号 + 方向 + start_ts 时间窗过滤);输出人类可读表格。
检索逻辑在 traffic_analyzer/rag/query.py,本文件只负责参数解析与表格打印。
上游:traffic_analyzer/rag/*(query、store、embed_client)。
下游:<workspace>/.agent/rag/vectors.db(只读)。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traffic_analyzer.rag.query import RagIndexNotFound, RagQueryError, run_search

DEFAULT_WORKSPACE = (
    "/media/wanji/Elements/大模型应用/高速交通事件Agent测试视频V4/sft_grpo_train_label"
)


def _fmt_ts(ts) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _print_rows(rows: list[tuple], headers: tuple[str, ...]) -> None:
    """rows 与 headers 等长;简单等宽表格。"""
    str_rows = [["" if c is None else str(c) for c in r] for r in rows]
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in str_rows)) if str_rows else len(headers[i])
        for i in range(len(headers))
    ]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * w for w in widths))
    for r in str_rows:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))


def _similarity_rows(results: list[dict]) -> list[tuple]:
    # events 空(API 返回 null)按原 CLI 行为显示为 "[]"。
    return [
        (i + 1, f"{r['score']:.4f}", r["video_path"], r["events"] or "[]", r["site"],
         _fmt_ts(r["start_ts"]))
        for i, r in enumerate(results)
    ]


def _plain_rows(results: list[dict]) -> list[tuple]:
    return [
        (i + 1, r["video_path"], r["events"] or "[]", r["site"], _fmt_ts(r["start_ts"]))
        for i, r in enumerate(results)
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("text", help="查询词 hybrid 检索")
    p.add_argument("query")
    p.add_argument("-k", "--top-k", type=int, default=10)
    p.add_argument("--alpha", type=float, default=0.6)
    p.add_argument("--only-confirmed", action="store_true")
    p.add_argument("--human-edited", action="store_true")

    p = sub.add_parser("related", help="video_vec 相似检索")
    p.add_argument("filename")
    p.add_argument("-k", "--top-k", type=int, default=10)

    p = sub.add_parser("adjacent", help="同 site 时间邻近检索")
    p.add_argument("filename")
    p.add_argument("--gap-s", type=float, default=600)

    p = sub.add_parser("site", help="桩号 + 方向 + 时间窗检索")
    p.add_argument("stake")
    p.add_argument("--direction", default=None)
    p.add_argument("--before", default=None, help="epoch 秒或 ISO 时间")
    p.add_argument("--after", default=None, help="epoch 秒或 ISO 时间")

    args = ap.parse_args()
    kwargs = {}
    if args.cmd == "text":
        kwargs = dict(
            query=args.query, k=args.top_k, alpha=args.alpha,
            only_confirmed=args.only_confirmed, human_edited=args.human_edited,
        )
    elif args.cmd in ("related", "adjacent"):
        kwargs = dict(video=args.filename)
        if args.cmd == "related":
            kwargs["k"] = args.top_k
        else:
            kwargs["gap_s"] = args.gap_s
    else:  # site
        kwargs = dict(query=args.stake, direction=args.direction,
                      before=args.before, after=args.after)
    try:
        resp = run_search(args.workspace, args.cmd, **kwargs)
    except (RagIndexNotFound, RagQueryError) as exc:
        print(exc)
        return 0
    if args.cmd in ("related", "adjacent"):
        target = resp["target"]
        if args.cmd == "adjacent":
            target = f"{target} ({resp['source']})"
        print(f"target: {target}")
    if args.cmd in ("text", "related"):
        _print_rows(
            _similarity_rows(resp["results"]),
            ("rank", "score", "video", "events", "site", "start"),
        )
    else:
        _print_rows(
            _plain_rows(resp["results"]),
            ("rank", "video", "events", "site", "start"),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
