#!/usr/bin/env python3
"""把「演示区」的真实推理结果打包成前端 mock 数据(?mock=1 零 token 演示)。

读取:
  演示区/analysis/<stem>/<stem>.json            SFT 标注(含 event_attributes/attr_mentions)
  演示区/analysis/<stem>/report.md              分析报告
  演示区/analysis/<stem>/<stem>_evidence.json   证据(标定/区域/画廊)
  演示区/*.mp4                                  视频列表
  traffic_analyzer/config/event_categories.yaml 事件类别(id/name_zh/is_active)
  traffic_analyzer/config/event_options.yaml    各事件结构化选项组

输出:
  traffic_analyzer/web/static/js/mock_data.js   export const REAL_MOCK = {...}

mock.js 顶部动态 import 本文件;不存在时自动回退合成数据,因此本脚本可随时重跑。
"""

import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = REPO_ROOT / "演示区"
ANALYSIS_DIR = DEMO_DIR / "analysis"
OUT_JS = REPO_ROOT / "traffic_analyzer" / "web" / "static" / "js" / "mock_data.js"


def load_event_config():
    """event_categories.yaml + event_options.yaml → [{event_id,name_zh,is_active,options}]"""
    cats = yaml.safe_load(
        (REPO_ROOT / "traffic_analyzer" / "config" / "event_categories.yaml").read_text(encoding="utf-8")
    )["event_categories"]
    opts = yaml.safe_load(
        (REPO_ROOT / "traffic_analyzer" / "config" / "event_options.yaml").read_text(encoding="utf-8")
    )["event_options"]
    groups_by_id = {o["event_id"]: o.get("groups") or [] for o in opts}
    config = []
    for c in cats:
        options = [
            {
                "key": g["key"],
                "label": g["label"],
                "options": g.get("options") or [],
                "multi": bool(g.get("multi", False)),
                "required": bool(g.get("required", False)),
            }
            for g in groups_by_id.get(c["event_id"], [])
        ]
        config.append({
            "event_id": c["event_id"],
            "name_zh": c["name_zh"],
            "is_active": bool(c.get("is_active", False)),
            "options": options,
        })
    return config


def main() -> int:
    results = {}
    detected = {}
    for d in sorted(ANALYSIS_DIR.iterdir()):
        if not d.is_dir():
            continue
        stem = d.name
        sft_path = d / f"{stem}.json"
        report_path = d / "report.md"
        ev_path = d / f"{stem}_evidence.json"
        if not (sft_path.exists() and report_path.exists() and ev_path.exists()):
            print(f"[skip] {stem}: 缺少 sft/report/evidence 之一", file=sys.stderr)
            continue
        sft = json.loads(sft_path.read_text(encoding="utf-8"))
        results[stem] = {
            "report_md": report_path.read_text(encoding="utf-8"),
            "sft_label": sft,
            "evidence": json.loads(ev_path.read_text(encoding="utf-8")),
        }
        detected[stem] = list(sft.get("action") or [])

    videos = []
    for mp4 in sorted(DEMO_DIR.glob("*.mp4")):
        st = mp4.stat()
        videos.append({
            "name": mp4.name,
            "stem": mp4.stem,
            "rel": mp4.name,
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "has_results": mp4.stem in results,
        })

    real_mock = {
        # 演示区路径(相对仓库根,服务进程以仓库根为 CWD 启动时解析正确):
        # mock 初始化时把后端工作区切到这里,/api/workspace/stream 才能服务真实视频流
        "workspacePath": "演示区",
        "videos": videos,
        "eventConfig": load_event_config(),
        "detectedMap": detected,
        "results": results,
    }

    # JSON 字面量内嵌:U+2028/U+2029 在 JS 字符串中是非法行分隔符,需转义
    payload = json.dumps(real_mock, ensure_ascii=False)
    payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    OUT_JS.write_text(
        "/* 由 scripts/build_mock_data.py 生成,勿手改;重跑该脚本即可更新。 */\n"
        "export const REAL_MOCK = " + payload + ";\n",
        encoding="utf-8",
    )
    print(f"[ok] {OUT_JS} ({OUT_JS.stat().st_size} 字节)")
    print(f"     videos={len(videos)} results={len(results)} "
          f"eventConfig={len(real_mock['eventConfig'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
