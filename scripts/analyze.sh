#!/usr/bin/env bash
# 单视频分析:./scripts/analyze.sh <video_path> [output_md]
# 输出缺省为 <video 去扩展名>.md;视频路径相对仓库根时需能被 traffic_analyzer 解析。
set -euo pipefail
video="${1:?用法: $0 <video_path> [output_md]}"
out="${2:-${video%.*}.md}"
python3 -m traffic_analyzer analyze --video "$video" --format markdown --output "$out"
