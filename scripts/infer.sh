#!/usr/bin/env bash
# 单视频推理(与 analyze.sh 同一 analyze 子命令的历史别名):./scripts/infer.sh <video_path> [output_md]
set -euo pipefail
video="${1:?用法: $0 <video_path> [output_md]}"
out="${2:-${video%.*}.md}"
python3 -m traffic_analyzer analyze --video "$video" --format markdown --output "$out"
