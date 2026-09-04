#!/usr/bin/env python3
"""构建 RAG 向量索引:视频 / 标注文本 embedding + OSD 站点抽取入库。

Usage:
    python scripts/build_rag_index.py [--workspace DIR] [--concurrency 8]
        [--only-missing / --no-only-missing] [--refresh-annotations] [--limit N]

[文件说明]
作用:CLI 薄封装——参数解析后调 traffic_analyzer.rag.build.build_index(列 workspace
下 *.mp4,跳过已有记录(--only-missing),线程池内只做 HTTP/计算,主线程串行 upsert;
单视频失败记失败清单继续),结束写 output/rag_index_report.json。
上游:traffic_analyzer/rag/*(build、embed_client、store、annotations、site_osd)。
下游:<workspace>/.agent/rag/vectors.db、osd_cache.json;output/rag_index_report.json。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traffic_analyzer.rag.build import build_index

DEFAULT_WORKSPACE = (
    "/media/wanji/Elements/大模型应用/高速交通事件Agent测试视频V4/sft_grpo_train_label"
)
REPORT_PATH = Path(__file__).resolve().parents[1] / "output" / "rag_index_report.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument(
        "--only-missing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="跳过已入库视频(默认开启)",
    )
    ap.add_argument(
        "--refresh-annotations",
        action="store_true",
        help="已入库但有标注的视频也重新处理(刷新标注向量与字段)",
    )
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    report = build_index(
        args.workspace,
        concurrency=args.concurrency,
        only_missing=args.only_missing,
        refresh_annotations=args.refresh_annotations,
        limit=args.limit,
    )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"done: success={len(report['success'])} failed={len(report['failed'])} "
        f"report={REPORT_PATH}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
