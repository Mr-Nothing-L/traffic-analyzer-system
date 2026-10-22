#!/usr/bin/env python3
"""SFT Forge CLI — 基于文件名真值的多轮纠错 SFT label 批量生成。

用法(仓库根目录下):
    python3 scripts/sft_forge.py --dir "演示区" \
        --env-file traffic_analyzer/config/.env \
        --out output/sft_forge/labels.jsonl --workers 2

输入三选一:--video(可多次)/--dir/--list-file。断点续跑:已出现在 --out
与 quarantine/ 中的视频 stem 自动跳过。match=False 的结果写到 --out 同级的
quarantine/<stem>.json;每视频逐轮 trace 写 --trace-dir(默认 <out目录>/traces/)。

安全约定:provider 密钥只经 .env 读取,严禁打印。
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traffic_analyzer.core.config_manager import ConfigManager  # noqa: E402
from traffic_analyzer.core.vlm_exceptions import (  # noqa: E402
    AllProvidersExhaustedError,
    FatalAPIError,
)
from traffic_analyzer.evaluation import event_names  # noqa: E402
from traffic_analyzer.sftforge.engine import (  # noqa: E402
    TEMPLATE_CORRECT_MISSING,
    TEMPLATE_FORWARD,
    TEMPLATE_REVIEW_EXTRA,
    SftForgeEngine,
)
from traffic_analyzer.sftforge.exporter import (  # noqa: E402
    append_jsonl,
    build_train_row,
    collect_done_stems,
    write_trace_json,
)
from traffic_analyzer.sftforge.models import VideoForgeResult  # noqa: E402
from traffic_analyzer.sftforge.vlm_video import VideoVLMCaller, load_providers  # noqa: E402

logger = logging.getLogger("sft_forge")


def _collect_videos(args: argparse.Namespace) -> List[Path]:
    videos: List[Path] = []
    if args.video:
        videos.extend(Path(v) for v in args.video)
    if args.dir:
        videos.extend(sorted(Path(args.dir).glob("*.mp4")))
    if args.list_file:
        for line in Path(args.list_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                videos.append(Path(line))
    # 去重保序(按绝对路径)
    seen = set()
    unique: List[Path] = []
    for v in videos:
        key = str(v.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return unique


def _next_index(out_path: Path) -> int:
    """断点续跑时接上已有 JSONL 的最大 index。"""
    max_index = -1
    if out_path.exists():
        import json

        with out_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row.get("index"), int):
                    max_index = max(max_index, row["index"])
    return max_index + 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_argument_group("输入(至少其一)")
    src.add_argument("--video", action="append", help="单个视频路径,可多次传入")
    src.add_argument("--dir", help="视频目录(取其中全部 *.mp4)")
    src.add_argument("--list-file", help="视频路径清单文件,每行一个")
    parser.add_argument("--max-rounds", type=int, default=2, help="最大纠错轮次(默认 2)")
    parser.add_argument("--out", required=True, help="训练 JSONL 输出路径")
    parser.add_argument("--trace-dir", default=None, help="逐轮 trace 目录(默认 <out目录>/traces)")
    parser.add_argument("--workers", type=int, default=1, help="并发视频数(默认 1)")
    parser.add_argument(
        "--config-dir", default="traffic_analyzer/config", help="配置目录(prompt/事件定义)"
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="provider .env 路径(文件名须为 .env);缺省从 --config-dir 读取",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # openai/httpx 的 DEBUG 日志会 dump 完整请求体(含视频 base64),一律压到 INFO。
    for noisy in ("openai", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.INFO)

    videos = _collect_videos(args)
    if not videos:
        logger.error("没有输入视频")
        return 2

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    quarantine_dir = out_path.parent / "quarantine"
    trace_dir = Path(args.trace_dir) if args.trace_dir else out_path.parent / "traces"

    # 断点续跑:主输出 + quarantine 中出现的 stem 均视为已完成
    done_stems = collect_done_stems([out_path])
    if quarantine_dir.exists():
        done_stems |= {p.stem for p in quarantine_dir.glob("*.json")}
    todo = [v for v in videos if v.stem not in done_stems]
    logger.info(
        "视频总数=%d 已完成=%d 待处理=%d", len(videos), len(videos) - len(todo), len(todo)
    )
    if not todo:
        return 0

    manager = ConfigManager(config_dir=args.config_dir)
    manager.load_all()
    templates = {
        tid: manager.get_prompt_template(tid)
        for tid in (TEMPLATE_FORWARD, TEMPLATE_CORRECT_MISSING, TEMPLATE_REVIEW_EXTRA)
    }
    names = event_names(Path(args.config_dir) / "event_categories.yaml")
    providers = load_providers(args.env_file, args.config_dir)
    caller = VideoVLMCaller(providers)
    engine = SftForgeEngine(
        caller=caller,
        templates=templates,
        categories=manager.get_event_categories(),
        max_rounds=args.max_rounds,
        names=names,
    )

    next_index = _next_index(out_path)
    stats = {"match": 0, "quarantine": 0, "skipped": 0}

    def _handle_result(result: VideoForgeResult, index: Optional[int]) -> None:
        write_trace_json(result, trace_dir)
        if result.skipped:
            stats["skipped"] += 1
            return
        if result.match:
            append_jsonl(
                build_train_row(index, result.video, result.final_output, result.gt, names),
                out_path,
            )
            stats["match"] += 1
            return
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        (quarantine_dir / f"{Path(result.video).stem}.json").write_text(
            result.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        stats["quarantine"] += 1

    try:
        if args.workers <= 1:
            for video in todo:
                result = engine.run(video)
                _handle_result(result, next_index if result.match else None)
                if result.match:
                    next_index += 1
        else:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                future_map = {pool.submit(engine.run, v): v for v in todo}
                for future in as_completed(future_map):
                    result = future.result()  # FatalAPIError 由此上抛终止批处理
                    _handle_result(result, next_index if result.match else None)
                    if result.match:
                        next_index += 1
    except (FatalAPIError, AllProvidersExhaustedError) as exc:
        logger.error("API 不可用,批处理终止(已完成的视频已落盘,可断点续跑): %s", exc)
        return 1

    logger.info(
        "完成 | match=%d quarantine=%d skipped=%d | 输出=%s",
        stats["match"], stats["quarantine"], stats["skipped"], out_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
