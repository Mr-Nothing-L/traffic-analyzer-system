#!/usr/bin/env python3
"""RAG 向量库召回质量验证 harness,产出 output/rag_eval_report.md。

验证对象:`<workspace>/.agent/rag/vectors.db`(由 scripts/build_rag_index.py 构建)。
目标工作区默认指向 440 个 sft_grpo_train_label 视频,文件名形如
`01-02-04_Event_129_1751869790726_1.mp4`:
  - 前缀(`_Event_` 之前,`-` 分隔)= GT 事件 id 集合;
  - `_Event_(\d+)_` 段 = 批次号;批次 2048 = 难负样本(正常视频,不计入相关)。

验证内容:
  1. 事件召回:预设「查询词 → 期望事件 id」用例,在 video/annotation/hybrid
     三种 field 下各跑 top-20,统计 hit@10 / hit@20 / MRR;并对 hybrid 做
     alpha 小扫描(本地 sqlite 检索,代价可忽略),为默认参数提供依据。
  2. 机位自洽:随机抽 --sample 个视频,用自身 video 向量检索(排除自身),
     统计 top-5 中同批次号(及同 site,若 records 提供)的占比。
  3. 可信度分布:has_annotation / human_edited / review_status 计数。

用法(项目根目录):
  python3 scripts/rag_eval.py --help
  python3 scripts/rag_eval.py                       # 用默认工作区,全量验证
  python3 scripts/rag_eval.py --workspace <目录> --sample 30 --seed 42
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_WORKSPACE = "/media/wanji/Elements/大模型应用/高速交通事件Agent测试视频V4/sft_grpo_train_label"
DEFAULT_OUTPUT = ROOT / "output" / "rag_eval_report.md"

FIELDS = ("video", "annotation", "hybrid")
TOP_K = 20
CUTOFFS = (10, 20)
DEFAULT_ALPHA = 0.6
ALPHA_SWEEP = (0.4, 0.6, 0.8)  # 仅对 hybrid field 扫描
SELF_SIM_TOP = 5
HARD_NEGATIVE_BATCH = "2048"

# 查询词 → 期望事件 id(事件 id 语义见 docs/交通事件数据标注说明文档_v4.5.md)
QUERY_CASES: list[tuple[str, set[int]]] = [
    ("应急车道 违法停车", {1}),
    ("应急车道 养护车", {1, 2}),
    ("摩托车", {5}),
    ("拥堵 排队", {6}),
    ("道路施工 锥桶", {7}),
    ("车辆逆行 倒车", {8}),
    ("行人 路面", {4}),
]

FILENAME_RE = re.compile(r"^([0-9-]+)_Event_(\d+)_")


def fail(msg: str, code: int = 2) -> "SystemExit":
    print(f"错误:{msg}", file=sys.stderr)
    return SystemExit(code)


def parse_filename(basename: str) -> tuple[set[int], str | None]:
    """从文件名解析 (GT 事件 id 集合, 批次号);不匹配返回 (set(), None)。"""
    m = FILENAME_RE.match(basename)
    if not m:
        return set(), None
    ids = {int(x) for x in m.group(1).split("-") if x}
    return ids, m.group(2)


def _to_list(vec) -> list[float]:
    if hasattr(vec, "tolist"):
        return vec.tolist()
    return list(vec)


def load_rag(workspace: Path):
    """导入 rag 模块并打开库;不可用时给出友好报错而非 traceback。"""
    db_path = workspace / ".agent" / "rag" / "vectors.db"
    if not db_path.is_file():
        raise fail(
            f"RAG 向量库不存在:{db_path}\n"
            "请先运行 scripts/build_rag_index.py 建库,或用 --workspace 指向已建库的工作区。"
        )
    try:
        from traffic_analyzer.rag.embed_client import embed_texts
        from traffic_analyzer.rag.store import RagStore
    except ImportError as exc:
        raise fail(
            f"无法导入 traffic_analyzer.rag 模块({exc})。\n"
            "rag 模块由 build_rag_index 一侧提供,请确认其实已落地后再运行本脚本。"
        )
    try:
        store = RagStore(str(workspace))
    except Exception as exc:  # 库文件损坏/schema 不符等
        raise fail(f"打开 RAG 向量库失败:{db_path}({exc})")
    return store, embed_texts, db_path


def eval_recall(store, embed_texts) -> tuple[dict, dict]:
    """事件召回:返回 (recall[field][case_idx] = {hit@10,hit@20,rr}, alpha_sweep[alpha] = 同上 hybrid)。"""
    # 查询词很少(7 个),文本 embedding 秒级,一次 embed 全部用例
    queries = [q for q, _ in QUERY_CASES]
    try:
        q_vecs = [_to_list(v) for v in embed_texts(queries)]
    except Exception as exc:
        raise fail(f"查询词 embedding 失败({exc})。请检查 embedding 服务/配置后重试。", code=3)

    def run(field: str, alpha: float) -> list[dict]:
        out = []
        for q_vec, (query, expected) in zip(q_vecs, QUERY_CASES):
            results = store.search(q_vec, field=field, top_k=TOP_K, alpha=alpha)
            first_rank = None
            for rank, (path, _score) in enumerate(results, 1):
                gt_ids, batch = parse_filename(os.path.basename(path))
                if batch == HARD_NEGATIVE_BATCH:
                    continue  # 难负样本是正常视频,不算相关
                if expected & gt_ids:
                    first_rank = rank
                    break
            out.append(
                {
                    "query": query,
                    "expected": sorted(expected),
                    "hit@10": int(first_rank is not None and first_rank <= 10),
                    "hit@20": int(first_rank is not None),
                    "rr": (1.0 / first_rank) if first_rank else 0.0,
                    "first_rank": first_rank,
                }
            )
        return out

    recall = {field: run(field, DEFAULT_ALPHA) for field in FIELDS}
    alpha_sweep = {alpha: run("hybrid", alpha) for alpha in ALPHA_SWEEP}
    return recall, alpha_sweep


def eval_self_consistency(store, records: list[dict], sample: int, seed: int) -> dict:
    """机位自洽:自身 video 向量检索,top-5 同批次/同 site 占比。"""
    import random

    eligible = [r for r in records if parse_filename(os.path.basename(str(r.get("video_path", ""))))[1]]
    rng = random.Random(seed)
    picked = rng.sample(eligible, min(sample, len(eligible)))

    site_of = {os.path.basename(str(r.get("video_path", ""))): r.get("site") for r in records}
    has_site = any(site_of.values())

    same_batch_ratios: list[float] = []
    same_site_ratios: list[float] = []
    skipped = 0
    for rec in picked:
        vp = str(rec.get("video_path", ""))
        base = os.path.basename(vp)
        _ids, batch = parse_filename(base)
        try:
            vec = store.get_vec(vp, "video")
        except Exception:
            vec = None
        if vec is None:
            skipped += 1
            continue
        results = store.search(_to_list(vec), field="video", top_k=SELF_SIM_TOP + 1)
        neighbors = [os.path.basename(p) for p, _s in results if os.path.basename(p) != base][
            :SELF_SIM_TOP
        ]
        if not neighbors:
            skipped += 1
            continue
        n_batch = sum(1 for nb in neighbors if parse_filename(nb)[1] == batch)
        same_batch_ratios.append(n_batch / len(neighbors))
        if has_site:
            my_site = site_of.get(base)
            n_site = sum(1 for nb in neighbors if my_site and site_of.get(nb) == my_site)
            same_site_ratios.append(n_site / len(neighbors))

    return {
        "sampled": len(picked),
        "evaluated": len(same_batch_ratios),
        "skipped": skipped,
        "same_batch@5": (sum(same_batch_ratios) / len(same_batch_ratios)) if same_batch_ratios else None,
        "same_site@5": (sum(same_site_ratios) / len(same_site_ratios)) if has_site and same_site_ratios else None,
        "has_site_field": has_site,
    }


def credibility_distribution(records: list[dict]) -> dict:
    def truthy(v) -> bool:
        return v not in (None, "", 0, False, "0", "false", "False")

    return {
        "total": len(records),
        "has_annotation": Counter(bool(r.get("has_annotation")) and truthy(r.get("has_annotation")) for r in records),
        "human_edited": Counter(truthy(r.get("human_edited")) for r in records),
        "review_status": Counter(str(r.get("review_status") or "<空>") for r in records),
    }


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _mean(rows: list[dict], key: str) -> float:
    return sum(r[key] for r in rows) / len(rows) if rows else 0.0


def _field_score(rows: list[dict]) -> float:
    """field 综合分 = mean(hit@20) + mean(MRR),用于推荐默认 field。"""
    return _mean(rows, "hit@20") + _mean(rows, "rr")


def render_report(
    workspace: Path,
    db_path: Path,
    stats: dict,
    records: list[dict],
    recall: dict,
    alpha_sweep: dict,
    self_sim: dict,
    credibility: dict,
    args: argparse.Namespace,
) -> str:
    from datetime import datetime

    lines: list[str] = []
    w = lines.append
    w("# RAG 向量库召回质量验证报告")
    w("")
    w(f"- 生成时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    w(f"- 工作区:`{workspace}`")
    w(f"- 向量库:`{db_path}`")
    w(f"- 参数:sample={args.sample}, seed={args.seed}, top_k={TOP_K}, 默认 alpha={DEFAULT_ALPHA}")
    w("")

    w("## 1. 库概况")
    w("")
    w("| 指标 | 值 |")
    w("| --- | --- |")
    if isinstance(stats, dict) and stats:
        for k, v in stats.items():
            w(f"| {k} | {v} |")
    else:
        w("| (stats() 未返回内容) | — |")
    w(f"| records() 记录数 | {len(records)} |")
    hard_neg = sum(
        1
        for r in records
        if parse_filename(os.path.basename(str(r.get("video_path", ""))))[1] == HARD_NEGATIVE_BATCH
    )
    w(f"| 其中难负样本(Event_{HARD_NEGATIVE_BATCH}) | {hard_neg} |")
    w("")

    w("## 2. 事件召回")
    w("")
    w(
        "GT 事件 id 从文件名前缀解析;难负样本(批次 "
        f"{HARD_NEGATIVE_BATCH},正常视频)不计入相关结果。"
    )
    w("")
    header = "| 查询词 | 期望事件 |"
    sep = "| --- | --- |"
    for field in FIELDS:
        header += f" {field} hit@10 | {field} hit@20 | {field} MRR |"
        sep += " --- | --- | --- |"
    w(header)
    w(sep)
    for idx, (query, expected) in enumerate(QUERY_CASES):
        row = f"| {query} | {','.join(map(str, sorted(expected)))} |"
        for field in FIELDS:
            r = recall[field][idx]
            row += f" {_pct(r['hit@10'])} | {_pct(r['hit@20'])} | {r['rr']:.3f} |"
        w(row)
    avg = "| **平均** | |"
    for field in FIELDS:
        rows = recall[field]
        avg += f" **{_pct(_mean(rows, 'hit@10'))}** | **{_pct(_mean(rows, 'hit@20'))}** | **{_mean(rows, 'rr'):.3f}** |"
    w(avg)
    w("")

    w("### hybrid field alpha 扫描")
    w("")
    w("| alpha | hit@10 | hit@20 | MRR |")
    w("| --- | --- | --- | --- |")
    for alpha, rows in alpha_sweep.items():
        w(f"| {alpha} | {_pct(_mean(rows, 'hit@10'))} | {_pct(_mean(rows, 'hit@20'))} | {_mean(rows, 'rr'):.3f} |")
    w("")

    w("## 3. 机位自洽(video 向量自身检索,top-5)")
    w("")
    w(f"- 抽样:{self_sim['sampled']} 个(seed={args.seed}),有效评估 {self_sim['evaluated']} 个,跳过 {self_sim['skipped']} 个(无 video 向量或无近邻)")
    w(f"- top-5 同批次号占比:**{_pct(self_sim['same_batch@5'])}**")
    if self_sim["has_site_field"]:
        w(f"- top-5 同 site 占比:**{_pct(self_sim['same_site@5'])}**")
    else:
        w("- records 中 site 字段均为空,未统计同 site 占比")
    w("")

    w("## 4. 可信度分布")
    w("")
    w(f"- 总记录数:{credibility['total']}")
    ha = credibility["has_annotation"]
    w(f"- has_annotation:True {ha.get(True, 0)} / False {ha.get(False, 0)}")
    he = credibility["human_edited"]
    w(f"- human_edited:True {he.get(True, 0)} / False {he.get(False, 0)}")
    w("- review_status:")
    for status, cnt in sorted(credibility["review_status"].items(), key=lambda kv: -kv[1]):
        w(f"  - {status}:{cnt}")
    w("")

    w("## 5. 结论与建议")
    w("")
    best_field = max(FIELDS, key=lambda f: _field_score(recall[f]))
    best_alpha = max(alpha_sweep, key=lambda a: _field_score(alpha_sweep[a]))
    scores = ", ".join(f"{f}={_field_score(recall[f]):.3f}" for f in FIELDS)
    w(f"- 各 field 综合分(hit@20 均值 + MRR 均值):{scores}。")
    w(f"- **推荐默认 field:`{best_field}`**;**推荐 hybrid 默认 alpha:`{best_alpha}`**。")
    if self_sim["same_batch@5"] is not None and self_sim["same_batch@5"] < 0.5:
        w("- 注意:机位自洽同批次占比偏低(<50%),video 向量对同机位/同批次的聚类能力有限,依赖 video field 的相似检索需谨慎。")
    w("- 本报告基于文件名前缀的 GT 事件 id 自动评判,未逐条人工核对检索结果;个别用例的语义边界(如「养护车」是否属于违停)可能影响绝对数值,但 field 间横向对比仍然有效。")
    w("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RAG 向量库召回质量验证:事件召回 / 机位自洽 / 可信度分布,产出 markdown 报告。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE, help="RAG 库所在工作区(库位于 <workspace>/.agent/rag/vectors.db)")
    parser.add_argument("--sample", type=int, default=30, help="机位自洽抽样视频数")
    parser.add_argument("--seed", type=int, default=42, help="抽样随机种子")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="报告输出路径")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace)
    store, embed_texts, db_path = load_rag(workspace)

    stats = store.stats()
    records = store.records()
    if not records:
        raise fail(f"向量库为空(records() 返回 0 条):{db_path},请先完成建库。")

    recall, alpha_sweep = eval_recall(store, embed_texts)
    self_sim = eval_self_consistency(store, records, args.sample, args.seed)
    credibility = credibility_distribution(records)

    report = render_report(workspace, db_path, stats, records, recall, alpha_sweep, self_sim, credibility, args)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    print(f"报告已写入:{out_path}")
    for field in FIELDS:
        rows = recall[field]
        print(
            f"  {field:10s} hit@10={_pct(_mean(rows, 'hit@10'))} "
            f"hit@20={_pct(_mean(rows, 'hit@20'))} MRR={_mean(rows, 'rr'):.3f}"
        )
    print(f"  机位自洽 same_batch@5={_pct(self_sim['same_batch@5'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
