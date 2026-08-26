#!/usr/bin/env python3
"""生成 agent 侧事件契约 agent/config/event_contract.json。

权威来源(ADR-0005:新增事件只需追加 YAML 配置,无需改代码):
  - traffic_analyzer/config/event_categories.yaml
      逐事件 event_id / name / name_zh / definition 与 adjudication_rules;
  - traffic_analyzer/config/annotation_spec.yaml
      逐事件 boundary_conditions(标注规范边界,含跨事件吸收/排除条件)与
      global_guidelines。

agent 运行时(agent/src/tools/builtin/eventContract.ts)启动时加载该 JSON:
  - submit_detection 的活跃事件枚举、编码位宽从它派生(fail-fast);
  - chat_system.md 的 {{EVENT_DEFINITIONS}} / {{ADJUDICATION_RULES}} 等占位符
    由它渲染,手抄副本已删除。

用法(项目根目录):
  python3 scripts/gen_agent_event_contract.py           # 生成(幂等,输出到 agent/config/)
  python3 scripts/gen_agent_event_contract.py --check   # 校验生成物与 YAML 同步,漂移则退出码 1

修改 event_categories.yaml / annotation_spec.yaml 后必须重新运行本脚本;
traffic_analyzer/tests/test_agent_event_contract.py 会用 --check 防漂移。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
EVENT_CATEGORIES_YAML = ROOT / "traffic_analyzer" / "config" / "event_categories.yaml"
ANNOTATION_SPEC_YAML = ROOT / "traffic_analyzer" / "config" / "annotation_spec.yaml"
OUTPUT_JSON = ROOT / "agent" / "config" / "event_contract.json"

SCHEMA_VERSION = 1
# ADR-0001:位 9 = 正常指示位(已分析且无事件检出时为 1),不对应任何事件类别。
NORMAL_BIT_INDEX = 9
# 标注文档 v4.5 的基础位宽 1..11(编号 9 为正常指示位);新增更高编号事件时
# 编码位宽随之扩展(= max(11, 最大事件编号))。
BASE_ENCODING_LENGTH = 11


def _dedent_block(text: str) -> str:
    """YAML 块标量(definition 等)去公共缩进、去首尾空白行。"""
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(line.rstrip() for line in lines)


def build_contract() -> dict:
    raw_events = yaml.safe_load(EVENT_CATEGORIES_YAML.read_text(encoding="utf-8"))
    raw_spec = yaml.safe_load(ANNOTATION_SPEC_YAML.read_text(encoding="utf-8"))

    categories = raw_events.get("event_categories") or []
    if not categories:
        raise ValueError("event_categories.yaml 中没有任何 event_categories 条目")

    seen_ids: set[int] = set()
    events: list[dict] = []
    active_ids: list[int] = []
    for cat in categories:
        event_id = int(cat["event_id"])
        if event_id in seen_ids:
            raise ValueError(f"event_categories.yaml 中 event_id 重复: {event_id}")
        seen_ids.add(event_id)
        if not cat.get("is_active", False):
            continue
        active_ids.append(event_id)
        events.append(
            {
                "event_id": event_id,
                "event_code": str(cat.get("event_code", "")),
                "name": str(cat.get("name", "")),
                "name_zh": str(cat["name_zh"]),
                "description": str(cat.get("description", "")),
                "definition": _dedent_block(str(cat.get("definition", ""))),
            }
        )

    if not active_ids:
        raise ValueError("event_categories.yaml 中没有 is_active: true 的事件")
    active_ids.sort()
    events.sort(key=lambda e: e["event_id"])

    # annotation_spec 的逐事件边界条件(仅活跃事件;spec 与 categories 的事件
    # ID 一致性由 config_manager 校验,这里只做存在性检查)。
    spec_events = {
        int(item["event_id"]): item
        for item in (raw_spec.get("annotation_spec") or {}).get("events") or []
    }
    for event in events:
        spec = spec_events.get(event["event_id"])
        if spec is None:
            raise ValueError(
                f"annotation_spec.yaml 缺少 event_id={event['event_id']} 的条目"
            )
        event["boundary_conditions"] = [
            str(cond).strip() for cond in spec.get("boundary_conditions") or []
        ]

    adjudication_rules = [
        {
            "rule_id": str(rule["rule_id"]),
            "name": str(rule.get("name", "")),
            "description": _dedent_block(str(rule.get("description", ""))),
            "priority": int(rule.get("priority", 0)),
        }
        for rule in raw_events.get("adjudication_rules") or []
    ]
    # priority 降序(与 config_manager.get_adjudication_rules 一致)。
    adjudication_rules.sort(key=lambda r: r["priority"], reverse=True)

    global_guidelines = [
        str(g).strip()
        for g in (raw_spec.get("annotation_spec") or {}).get("global_guidelines") or []
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "event_categories": "traffic_analyzer/config/event_categories.yaml",
            "annotation_spec": "traffic_analyzer/config/annotation_spec.yaml",
        },
        "encoding_length": max(BASE_ENCODING_LENGTH, max(active_ids)),
        "normal_bit_index": NORMAL_BIT_INDEX,
        "active_event_ids": active_ids,
        "events": events,
        "adjudication_rules": adjudication_rules,
        "global_guidelines": global_guidelines,
    }


def render(contract: dict) -> str:
    return json.dumps(contract, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="校验 agent/config/event_contract.json 与 YAML 同步(漂移退出码 1)",
    )
    args = parser.parse_args()

    expected = render(build_contract())

    if not args.check:
        OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_JSON.write_text(expected, encoding="utf-8")
        print(f"[gen_agent_event_contract] 已写入 {OUTPUT_JSON.relative_to(ROOT)}")
        return 0

    if not OUTPUT_JSON.is_file():
        print(
            f"[gen_agent_event_contract] 缺少 {OUTPUT_JSON.relative_to(ROOT)};"
            "请运行 python3 scripts/gen_agent_event_contract.py 生成"
        )
        return 1
    actual = OUTPUT_JSON.read_text(encoding="utf-8")
    if actual != expected:
        print(
            "[gen_agent_event_contract] 生成物与 YAML 不同步;"
            "请运行 python3 scripts/gen_agent_event_contract.py 重新生成"
        )
        return 1
    print(f"[gen_agent_event_contract] {OUTPUT_JSON.relative_to(ROOT)} 与 YAML 同步")
    return 0


if __name__ == "__main__":
    sys.exit(main())
